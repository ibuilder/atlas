"""Bank reconciliation: import, match, resolve, complete.

Operators judge an accounting system by this screen, and there are three places
it usually goes wrong.

**Re-importing.** Statement windows overlap - somebody downloads 1-31 March
after already loading 1-15 March. Every imported line therefore carries a
stable identity: the bank's own reference where one exists, and otherwise a
fingerprint of date, amount, description, and an occurrence index. Re-importing
the same window inserts nothing. Importing a window in which the bank has since
added a fourth identical transaction inserts exactly one.

**Matching.** Suggestions are *ranked and explained*, never applied silently.
Automatic matching only takes a candidate that is both above the confidence
threshold and unambiguous - if two ledger lines score the same, a human
decides, because guessing between two payments of the same amount on the same
day is how a reconciliation quietly stops meaning anything.

**Completing.** A reconciliation cannot complete while the difference is
non-zero or an exception is unresolved. The entire value of the exercise is the
statement "these agree", and a system that lets somebody sign that off while
they do not agree has thrown that value away.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import BusinessRuleViolation, NotFound, ValidationFailed
from app.logging import get_logger
from app.models.accounting import (
    ZERO,
    BankAccount,
    BankTransaction,
    JournalEntry,
    JournalLine,
    Reconciliation,
    ReconciliationException,
    ReconciliationStatus,
)
from app.models.audit import AuditAction, AuditSeverity
from app.models.types import quantize_money, utcnow
from app.observability import RECONCILIATION_EXCEPTIONS
from app.services.audit.recorder import record_audit_event

__all__ = [
    "AUTO_MATCH_THRESHOLD",
    "ImportResult",
    "MatchCandidate",
    "MatchStatus",
    "StatementLine",
    "auto_match",
    "complete_reconciliation",
    "import_statement",
    "match_transaction",
    "open_reconciliation",
    "parse_statement_csv",
    "raise_exception",
    "refresh_totals",
    "unresolved_exceptions",
    "resolve_exception",
    "suggest_matches",
    "unmatch_transaction",
]

log = get_logger("services.accounting.reconciliation")

#: Only a candidate this confident *and* unambiguous is matched without a human.
#: Calibrated so that the exact amount on the exact date reaches it and nothing
#: weaker does: a payment a day late needs a reference or a recognisable name
#: before the system will claim it without asking.
AUTO_MATCH_THRESHOLD = 90

#: How far either side of the bank's posted date a ledger entry may sit and
#: still be the same event. Cheques and ACH settle days after they are written.
MATCH_WINDOW_DAYS = 5

#: Words that appear in every bank description and carry no signal.
_NOISE = frozenset(
    {
        "ach",
        "and",
        "bill",
        "card",
        "check",
        "cheque",
        "co",
        "corp",
        "debit",
        "deposit",
        "eft",
        "for",
        "inc",
        "llc",
        "ltd",
        "payment",
        "pmt",
        "the",
        "transfer",
        "trn",
        "xfer",
    }
)

_TOKEN = re.compile(r"[a-z0-9]+")


class MatchStatus:
    """As stored in ``BankTransaction.match_status``."""

    UNMATCHED = "unmatched"
    SUGGESTED = "suggested"
    MATCHED = "matched"
    IGNORED = "ignored"


@dataclass
class StatementLine:
    """One row as the bank supplied it."""

    posted_date: dt.date
    #: Signed: positive is money in, negative is money out.
    amount: Decimal
    description: str
    reference: str | None = None
    external_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImportResult:
    imported: list[BankTransaction] = field(default_factory=list)
    #: Lines already present, identified by their stable identity.
    duplicates: int = 0
    #: Lines outside the account or otherwise unusable.
    rejected: int = 0

    @property
    def count(self) -> int:
        return len(self.imported)


@dataclass(frozen=True)
class MatchCandidate:
    journal_line: JournalLine
    confidence: int
    reasons: tuple[str, ...]


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def statement_fingerprint(line: StatementLine, *, bank_account_id: str, occurrence: int) -> str:
    """A stable identity for a line the bank did not give one.

    Includes an occurrence index so that two genuinely identical transactions on
    the same day both survive - while a re-import of the same file produces the
    same two indices and therefore inserts neither again.
    """
    material = "|".join(
        [
            bank_account_id,
            line.posted_date.isoformat(),
            f"{quantize_money(line.amount):.4f}",
            " ".join(line.description.lower().split()),
            (line.reference or "").strip().lower(),
            str(occurrence),
        ]
    )
    return "fp:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def import_statement(
    session: Session,
    *,
    org_id: str,
    bank_account_id: str,
    lines: list[StatementLine],
    import_batch_id: str | None = None,
    actor_id: str | None = None,
) -> ImportResult:
    """Load statement lines, ignoring anything already present."""
    account = session.get(BankAccount, bank_account_id)
    if account is None or account.org_id != org_id:
        raise NotFound("No such bank account.")

    result = ImportResult()
    occurrences: Counter[str] = Counter()

    existing = set(
        session.execute(
            select(BankTransaction.external_id).where(
                BankTransaction.bank_account_id == bank_account_id,
                BankTransaction.external_id.is_not(None),
            )
        )
        .scalars()
        .all()
    )

    for line in lines:
        if line.amount is None or not line.description:
            result.rejected += 1
            continue

        identity = (line.external_id or "").strip()
        if not identity:
            key = f"{line.posted_date}|{line.amount}|{line.description}"
            occurrences[key] += 1
            identity = statement_fingerprint(
                line, bank_account_id=bank_account_id, occurrence=occurrences[key]
            )

        if identity in existing:
            result.duplicates += 1
            continue

        transaction = BankTransaction(
            org_id=org_id,
            bank_account_id=bank_account_id,
            posted_date=line.posted_date,
            amount=quantize_money(line.amount),
            description=line.description[:255],
            reference=(line.reference or None),
            external_id=identity[:120],
            match_status=MatchStatus.UNMATCHED,
            import_batch_id=import_batch_id,
            raw=line.raw or {},
        )
        session.add(transaction)
        existing.add(identity)
        result.imported.append(transaction)

    session.flush()
    log.info(
        "bank statement imported",
        extra={
            "event": "reconciliation.imported",
            "bank_account_id": bank_account_id,
            "imported": result.count,
            "duplicates": result.duplicates,
        },
    )
    if result.count:
        record_audit_event(
            action=AuditAction.RECONCILIATION_COMPLETED,
            resource_type="BankAccount",
            resource_id=bank_account_id,
            resource_label=account.name,
            payload={
                "imported": result.count,
                "duplicates": result.duplicates,
                "rejected": result.rejected,
            },
            reason="Bank statement imported.",
            org_id=org_id,
            actor_id=actor_id,
            session=session,
        )
    return result


def parse_statement_csv(
    text: str,
    *,
    date_column: str = "date",
    amount_column: str = "amount",
    description_column: str = "description",
    reference_column: str | None = "reference",
    external_id_column: str | None = "id",
    date_format: str | None = None,
) -> list[StatementLine]:
    """Read a bank CSV export into statement lines.

    Deliberately tolerant about *shape* - column names differ per bank - and
    deliberately strict about *values*: a row whose amount will not parse is
    rejected loudly rather than silently imported as zero, because a zero in a
    reconciliation is a difference somebody spends an afternoon hunting.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    lines: list[StatementLine] = []

    for number, row in enumerate(reader, start=2):  # row 1 is the header
        normalised = {(key or "").strip().lower(): (value or "") for key, value in row.items()}
        raw_date = normalised.get(date_column.lower(), "").strip()
        raw_amount = normalised.get(amount_column.lower(), "").strip()
        if not raw_date and not raw_amount:
            continue

        try:
            posted = (
                dt.datetime.strptime(raw_date, date_format).date()
                if date_format
                else dt.date.fromisoformat(raw_date)
            )
        except ValueError as exc:
            raise ValidationFailed(f"Row {number}: {raw_date!r} is not a date.") from exc

        cleaned = raw_amount.replace(",", "").replace("$", "").strip()
        if cleaned.startswith("(") and cleaned.endswith(")"):
            cleaned = "-" + cleaned[1:-1]  # accountants' parentheses
        try:
            amount = Decimal(cleaned)
        except (InvalidOperation, ValueError) as exc:
            raise ValidationFailed(f"Row {number}: {raw_amount!r} is not an amount.") from exc

        lines.append(
            StatementLine(
                posted_date=posted,
                amount=amount,
                description=normalised.get(description_column.lower(), "").strip(),
                reference=(
                    normalised.get(reference_column.lower(), "").strip() or None
                    if reference_column
                    else None
                ),
                external_id=(
                    normalised.get(external_id_column.lower(), "").strip() or None
                    if external_id_column
                    else None
                ),
                raw=dict(normalised),
            )
        )
    return lines


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _tokens(text: str) -> set[str]:
    return {
        token for token in _TOKEN.findall(text.lower()) if token not in _NOISE and len(token) > 2
    }


def score_match(
    transaction: BankTransaction, line: JournalLine, entry: JournalEntry
) -> tuple[int, tuple[str, ...]]:
    """How likely this ledger line is the same event, and why.

    The reasons are returned with the score because an unexplained ranking is a
    ranking nobody trusts, and an operator who does not trust the suggestions
    matches everything by hand anyway.
    """
    reasons: list[str] = []
    score = 0

    # Sign convention: money into the bank is a debit on the bank's GL account.
    ledger_amount = line.debit - line.credit
    if quantize_money(ledger_amount) == quantize_money(transaction.amount):
        score += 60
        reasons.append("amount matches exactly")
    else:
        return 0, ("amount does not match",)

    days = abs((entry.entry_date - transaction.posted_date).days)
    if days == 0:
        score += 30
        reasons.append("same date")
    elif days <= 2:
        score += 20
        reasons.append(f"{days} day(s) apart")
    elif days <= MATCH_WINDOW_DAYS:
        score += 10
        reasons.append(f"{days} days apart")
    else:
        return 0, (f"{days} days apart, outside the matching window",)

    reference = (transaction.reference or "").strip().lower()
    if reference:
        # The entry number is what a payment reference usually carries, and the
        # memo is where a person writes the cheque number.
        haystack = " ".join(
            filter(
                None,
                [
                    entry.entry_number or "",
                    entry.memo or "",
                    line.memo or "",
                    entry.description or "",
                ],
            )
        ).lower()
        if reference in haystack:
            score += 15
            reasons.append("reference appears in the entry")

    overlap = _tokens(transaction.description) & _tokens(
        f"{entry.description or ''} {line.memo or ''}"
    )
    if overlap:
        score += min(10, 4 * len(overlap))
        reasons.append("description mentions " + ", ".join(sorted(overlap)[:3]))

    return min(score, 100), tuple(reasons)


def suggest_matches(
    session: Session,
    *,
    transaction: BankTransaction,
    limit: int = 5,
) -> list[MatchCandidate]:
    """Ranked ledger lines that might be this bank line."""
    window = dt.timedelta(days=MATCH_WINDOW_DAYS)
    account = session.get(BankAccount, transaction.bank_account_id)
    if account is None:
        return []

    rows = session.execute(
        select(JournalLine, JournalEntry)
        .join(JournalEntry, JournalEntry.id == JournalLine.journal_entry_id)
        .where(
            JournalLine.org_id == transaction.org_id,
            JournalLine.account_id == account.gl_account_id,
            JournalEntry.entry_date >= transaction.posted_date - window,
            JournalEntry.entry_date <= transaction.posted_date + window,
        )
    ).all()

    already_matched = set(
        session.execute(
            select(BankTransaction.matched_journal_line_id).where(
                BankTransaction.org_id == transaction.org_id,
                BankTransaction.matched_journal_line_id.is_not(None),
                BankTransaction.id != transaction.id,
            )
        )
        .scalars()
        .all()
    )

    candidates: list[MatchCandidate] = []
    for line, entry in rows:
        if line.id in already_matched:
            continue
        score, reasons = score_match(transaction, line, entry)
        if score > 0:
            candidates.append(MatchCandidate(journal_line=line, confidence=score, reasons=reasons))

    candidates.sort(key=lambda c: (-c.confidence, c.journal_line.id))
    return candidates[:limit]


def match_transaction(
    session: Session,
    *,
    transaction: BankTransaction,
    journal_line: JournalLine,
    confidence: int | None = None,
    actor_id: str | None = None,
) -> BankTransaction:
    """Bind a bank line to a ledger line."""
    if journal_line.org_id != transaction.org_id:
        raise ValidationFailed("That ledger line belongs to another organization.")

    taken = session.execute(
        select(BankTransaction).where(
            BankTransaction.org_id == transaction.org_id,
            BankTransaction.matched_journal_line_id == journal_line.id,
            BankTransaction.id != transaction.id,
        )
    ).scalar_one_or_none()
    if taken is not None:
        raise BusinessRuleViolation(
            "That ledger line is already matched to another bank transaction. "
            "One ledger line settles once."
        )

    transaction.matched_journal_line_id = journal_line.id
    transaction.match_status = MatchStatus.MATCHED
    transaction.match_confidence = confidence
    session.flush()
    return transaction


def unmatch_transaction(session: Session, *, transaction: BankTransaction) -> BankTransaction:
    if transaction.reconciliation_id is not None:
        reconciliation = session.get(Reconciliation, transaction.reconciliation_id)
        if reconciliation is not None and reconciliation.status == ReconciliationStatus.COMPLETED:
            raise BusinessRuleViolation(
                "This transaction belongs to a completed reconciliation. "
                "Reopen the reconciliation first."
            )
    transaction.matched_journal_line_id = None
    transaction.match_status = MatchStatus.UNMATCHED
    transaction.match_confidence = None
    session.flush()
    return transaction


def auto_match(
    session: Session,
    *,
    org_id: str,
    bank_account_id: str,
    threshold: int = AUTO_MATCH_THRESHOLD,
    actor_id: str | None = None,
) -> list[BankTransaction]:
    """Match only what is both confident and unambiguous.

    A tie between two candidates is left for a human however high it scores:
    guessing between two payments of the same amount on the same day is how a
    reconciliation quietly stops meaning anything.
    """
    unmatched = (
        session.execute(
            select(BankTransaction).where(
                BankTransaction.org_id == org_id,
                BankTransaction.bank_account_id == bank_account_id,
                BankTransaction.match_status == MatchStatus.UNMATCHED,
            )
        )
        .scalars()
        .all()
    )

    matched: list[BankTransaction] = []
    for transaction in unmatched:
        candidates = suggest_matches(session, transaction=transaction, limit=2)
        if not candidates or candidates[0].confidence < threshold:
            continue
        if len(candidates) > 1 and candidates[1].confidence == candidates[0].confidence:
            transaction.match_status = MatchStatus.SUGGESTED
            transaction.match_confidence = candidates[0].confidence
            log.info(
                "ambiguous bank match left for a person",
                extra={
                    "event": "reconciliation.ambiguous",
                    "transaction_id": transaction.id,
                    "confidence": candidates[0].confidence,
                },
            )
            continue

        match_transaction(
            session,
            transaction=transaction,
            journal_line=candidates[0].journal_line,
            confidence=candidates[0].confidence,
            actor_id=actor_id,
        )
        matched.append(transaction)

    session.flush()
    return matched


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------


def open_reconciliation(
    session: Session,
    *,
    org_id: str,
    bank_account_id: str,
    statement_start: dt.date,
    statement_end: dt.date,
    opening_balance: Decimal,
    closing_balance: Decimal,
    actor_id: str | None = None,
) -> Reconciliation:
    """Start a reconciliation over a statement window."""
    if statement_end < statement_start:
        raise ValidationFailed("A statement period must end on or after it starts.")

    overlapping = session.execute(
        select(Reconciliation).where(
            Reconciliation.org_id == org_id,
            Reconciliation.bank_account_id == bank_account_id,
            Reconciliation.status != ReconciliationStatus.ABANDONED,
            Reconciliation.statement_start <= statement_end,
            Reconciliation.statement_end >= statement_start,
        )
    ).scalar_one_or_none()
    if overlapping is not None:
        raise BusinessRuleViolation(
            f"A reconciliation already covers {overlapping.statement_start} to "
            f"{overlapping.statement_end} on this account."
        )

    reconciliation = Reconciliation(
        org_id=org_id,
        bank_account_id=bank_account_id,
        statement_start=statement_start,
        statement_end=statement_end,
        statement_opening_balance=quantize_money(opening_balance),
        statement_closing_balance=quantize_money(closing_balance),
        status=ReconciliationStatus.DRAFT,
    )
    session.add(reconciliation)
    session.flush()

    # Claim the statement's transactions, so the totals are computed over a
    # fixed set rather than whatever happens to be in the window at the time.
    transactions = (
        session.execute(
            select(BankTransaction).where(
                BankTransaction.org_id == org_id,
                BankTransaction.bank_account_id == bank_account_id,
                BankTransaction.posted_date >= statement_start,
                BankTransaction.posted_date <= statement_end,
                BankTransaction.reconciliation_id.is_(None),
            )
        )
        .scalars()
        .all()
    )
    for transaction in transactions:
        transaction.reconciliation_id = reconciliation.id

    refresh_totals(session, reconciliation=reconciliation)
    return reconciliation


def refresh_totals(session: Session, *, reconciliation: Reconciliation) -> Reconciliation:
    """Recompute the cleared balance and the difference."""
    transactions = _transactions_for(session, reconciliation)
    cleared = sum((t.amount for t in transactions if t.match_status == MatchStatus.MATCHED), ZERO)
    reconciliation.cleared_balance = quantize_money(
        reconciliation.statement_opening_balance + cleared
    )
    reconciliation.ledger_balance = quantize_money(_ledger_balance(session, reconciliation))
    reconciliation.difference = quantize_money(
        reconciliation.statement_closing_balance - reconciliation.cleared_balance
    )
    session.flush()

    RECONCILIATION_EXCEPTIONS.labels(reconciliation.org_id).set(
        len(unresolved_exceptions(session, reconciliation))
    )
    return reconciliation


def unresolved_exceptions(
    session: Session, reconciliation: Reconciliation
) -> list[ReconciliationException]:
    """Queried rather than read off the relationship.

    A completion gate must not depend on whether a collection happens to be
    loaded: an ORM session configured not to expire on commit would hand back a
    stale list, and the reconciliation would sign off over an open exception.
    """
    return list(
        session.execute(
            select(ReconciliationException).where(
                ReconciliationException.org_id == reconciliation.org_id,
                ReconciliationException.reconciliation_id == reconciliation.id,
                ReconciliationException.resolved_at.is_(None),
            )
        )
        .scalars()
        .all()
    )


def _transactions_for(session: Session, reconciliation: Reconciliation) -> list[BankTransaction]:
    return list(
        session.execute(
            select(BankTransaction).where(
                BankTransaction.org_id == reconciliation.org_id,
                BankTransaction.reconciliation_id == reconciliation.id,
            )
        )
        .scalars()
        .all()
    )


def _ledger_balance(session: Session, reconciliation: Reconciliation) -> Decimal:
    """The GL balance of the bank account as at the statement end."""
    account = session.get(BankAccount, reconciliation.bank_account_id)
    if account is None:
        return ZERO
    rows = session.execute(
        select(JournalLine.debit, JournalLine.credit)
        .join(JournalEntry, JournalEntry.id == JournalLine.journal_entry_id)
        .where(
            JournalLine.org_id == reconciliation.org_id,
            JournalLine.account_id == account.gl_account_id,
            JournalEntry.entry_date <= reconciliation.statement_end,
        )
    ).all()
    return sum((debit - credit for debit, credit in rows), ZERO)


def raise_exception(
    session: Session,
    *,
    reconciliation: Reconciliation,
    kind: str,
    description: str,
    amount: Decimal | None = None,
    bank_transaction_id: str | None = None,
) -> ReconciliationException:
    """Record something that does not agree and needs a person."""
    if reconciliation.status == ReconciliationStatus.COMPLETED:
        raise BusinessRuleViolation("A completed reconciliation cannot take new exceptions.")

    exception = ReconciliationException(
        org_id=reconciliation.org_id,
        reconciliation_id=reconciliation.id,
        kind=kind,
        description=description[:255],
        amount=quantize_money(amount) if amount is not None else None,
        bank_transaction_id=bank_transaction_id,
    )
    session.add(exception)
    session.flush()
    refresh_totals(session, reconciliation=reconciliation)
    return exception


def resolve_exception(
    session: Session,
    *,
    exception: ReconciliationException,
    resolved_by_id: str,
    note: str,
) -> ReconciliationException:
    if not note or not note.strip():
        raise ValidationFailed("Resolving an exception requires a note explaining how.")
    exception.resolved_at = utcnow()
    exception.resolved_by_id = resolved_by_id
    exception.resolution_note = note
    session.flush()

    reconciliation = session.get(Reconciliation, exception.reconciliation_id)
    if reconciliation is not None:
        refresh_totals(session, reconciliation=reconciliation)
    return exception


def complete_reconciliation(
    session: Session,
    *,
    reconciliation: Reconciliation,
    completed_by_id: str,
    notes: str | None = None,
) -> Reconciliation:
    """Sign off. Refuses anything that does not actually agree."""
    if reconciliation.status == ReconciliationStatus.COMPLETED:
        return reconciliation

    refresh_totals(session, reconciliation=reconciliation)

    if reconciliation.difference != ZERO:
        raise BusinessRuleViolation(
            f"This reconciliation is out by {reconciliation.difference}. "
            "Match the remaining transactions or record an exception and resolve it."
        )

    unresolved = unresolved_exceptions(session, reconciliation)
    if unresolved:
        raise BusinessRuleViolation(
            f"{len(unresolved)} exception(s) are still unresolved: "
            f"{', '.join(e.description for e in unresolved[:3])}."
        )

    outstanding = [
        t
        for t in _transactions_for(session, reconciliation)
        if t.match_status not in (MatchStatus.MATCHED, MatchStatus.IGNORED)
    ]
    if outstanding:
        raise BusinessRuleViolation(
            f"{len(outstanding)} transaction(s) are neither matched nor deliberately ignored."
        )

    reconciliation.status = ReconciliationStatus.COMPLETED
    reconciliation.completed_at = utcnow()
    reconciliation.completed_by_id = completed_by_id
    reconciliation.notes = notes or reconciliation.notes
    session.flush()

    record_audit_event(
        action=AuditAction.RECONCILIATION_COMPLETED,
        resource_type="Reconciliation",
        resource_id=reconciliation.id,
        resource_label=f"{reconciliation.statement_start} to {reconciliation.statement_end}",
        severity=AuditSeverity.NOTICE,
        payload={
            "closing_balance": str(reconciliation.statement_closing_balance),
            "cleared_balance": str(reconciliation.cleared_balance),
            "ledger_balance": str(reconciliation.ledger_balance),
            "transactions": len(_transactions_for(session, reconciliation)),
        },
        reason="Bank reconciliation completed.",
        org_id=reconciliation.org_id,
        actor_id=completed_by_id,
        session=session,
    )
    return reconciliation


def reopen_reconciliation(
    session: Session,
    *,
    reconciliation: Reconciliation,
    actor_id: str,
    reason: str,
) -> Reconciliation:
    """Undo a completion. Audited as a notable event, because it is one."""
    if not reason or not reason.strip():
        raise ValidationFailed("Reopening a reconciliation requires a reason.")
    if reconciliation.status != ReconciliationStatus.COMPLETED:
        raise BusinessRuleViolation("Only a completed reconciliation can be reopened.")

    reconciliation.status = ReconciliationStatus.IN_REVIEW
    reconciliation.completed_at = None
    reconciliation.completed_by_id = None
    session.flush()

    record_audit_event(
        action=AuditAction.RECONCILIATION_COMPLETED,
        resource_type="Reconciliation",
        resource_id=reconciliation.id,
        severity=AuditSeverity.CRITICAL,
        payload={"reopened": True},
        reason=reason,
        org_id=reconciliation.org_id,
        actor_id=actor_id,
        session=session,
    )
    return reconciliation

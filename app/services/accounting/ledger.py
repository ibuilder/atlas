"""Double-entry posting, reversal, and period control.

The only sanctioned way to move the ledger. Direct construction of
:class:`JournalEntry` elsewhere bypasses period checks, control-account rules,
numbering, and the audit event - so it is not done.

Correction policy: a posted entry is never edited or deleted. A reversal is
posted with mirrored lines, both entries are linked, and the corrected version
is posted as a new entry. The books then show what happened *and* what was
believed at the time, which is what an auditor is actually asking for.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import BusinessRuleViolation, ErrorCode, NotFound, ValidationFailed
from app.logging import get_logger
from app.models.accounting import (
    ZERO,
    Account,
    FiscalPeriod,
    JournalEntry,
    JournalLine,
    JournalStatus,
    PeriodStatus,
)
from app.models.audit import AuditAction, AuditSeverity
from app.models.sequences import SequenceKey
from app.models.types import quantize_money, utcnow
from app.observability import LEDGER_POSTINGS
from app.services.audit.recorder import record_audit_event
from app.services.common.numbering import next_number

__all__ = [
    "LineInput",
    "ensure_period",
    "post_journal_entry",
    "reverse_journal_entry",
    "trial_balance",
]

log = get_logger("services.accounting.ledger")


@dataclass(frozen=True)
class LineInput:
    """One side of one account movement, as supplied by a caller."""

    account_id: str
    debit: Decimal = ZERO
    credit: Decimal = ZERO
    memo: str | None = None
    property_id: str | None = None
    unit_id: str | None = None
    lease_id: str | None = None
    owner_entity_id: str | None = None
    vendor_id: str | None = None
    bank_account_id: str | None = None


def post_journal_entry(
    session: Session,
    *,
    org_id: str,
    entry_date: dt.date,
    description: str,
    lines: list[LineInput],
    source_type: str | None = None,
    source_id: str | None = None,
    property_id: str | None = None,
    memo: str | None = None,
    is_adjusting: bool = False,
    post: bool = True,
    system_posting: bool = False,
    reverses_id: str | None = None,
    actor_id: str | None = None,
) -> JournalEntry:
    """Create - and by default post - a balanced journal entry.

    ``system_posting`` permits writes to control accounts (AR, AP, trust
    liability). Those accounts are maintained by the invoice, bill, and payment
    services; a human posting to them by hand is how a subsidiary ledger stops
    agreeing with its control account.
    """
    if len(lines) < 2:
        raise ValidationFailed("A journal entry requires at least two lines.")

    debit_total = quantize_money(sum((line.debit for line in lines), ZERO))
    credit_total = quantize_money(sum((line.credit for line in lines), ZERO))

    if debit_total != credit_total:
        raise BusinessRuleViolation(
            f"Entry does not balance: debits {debit_total} != credits {credit_total}.",
            code=ErrorCode.LEDGER_UNBALANCED,
        )
    if debit_total == ZERO:
        raise ValidationFailed("A journal entry must move a non-zero amount.")

    period = ensure_period(session, org_id=org_id, on_date=entry_date)
    if post and not period.accepts_postings:
        raise BusinessRuleViolation(
            f"Period {period.name} is {period.status}; postings are not accepted.",
            code=ErrorCode.PERIOD_CLOSED,
        )

    accounts = _load_accounts(session, org_id, {line.account_id for line in lines})
    if not system_posting:
        controls = [a.code for a in accounts.values() if a.is_control]
        if controls:
            raise BusinessRuleViolation(
                "Control accounts are maintained by the system and cannot be posted to "
                f"directly: {', '.join(sorted(controls))}."
            )
    inactive = [a.code for a in accounts.values() if not a.is_active]
    if inactive:
        raise ValidationFailed(f"These accounts are inactive: {', '.join(sorted(inactive))}.")

    entry = JournalEntry(
        org_id=org_id,
        entry_number=next_number(session, SequenceKey.JOURNAL_ENTRY, org_id=org_id),
        entry_date=entry_date,
        period_id=period.id,
        property_id=property_id,
        description=description,
        memo=memo,
        status=JournalStatus.POSTED if post else JournalStatus.DRAFT,
        source_type=source_type,
        source_id=source_id,
        is_adjusting=is_adjusting,
        # Set at construction, not afterwards: once an entry is posted the
        # immutability guard refuses further changes, and rightly so.
        reverses_id=reverses_id,
        total_debit=debit_total,
        total_credit=credit_total,
        posted_at=utcnow() if post else None,
        posted_by_id=actor_id if post else None,
    )
    session.add(entry)

    for index, line in enumerate(lines, start=1):
        session.add(
            JournalLine(
                org_id=org_id,
                entry=entry,
                line_number=index,
                account_id=line.account_id,
                debit=quantize_money(line.debit),
                credit=quantize_money(line.credit),
                memo=line.memo,
                property_id=line.property_id or property_id,
                unit_id=line.unit_id,
                lease_id=line.lease_id,
                owner_entity_id=line.owner_entity_id,
                vendor_id=line.vendor_id,
                bank_account_id=line.bank_account_id,
            )
        )

    session.flush()

    if post:
        LEDGER_POSTINGS.labels(source_type or "manual").inc()
        record_audit_event(
            action=AuditAction.JOURNAL_POSTED,
            resource_type="JournalEntry",
            resource_id=entry.id,
            resource_label=entry.entry_number,
            payload={
                "amount": str(debit_total),
                "entry_date": entry_date.isoformat(),
                "source_type": source_type,
                "lines": len(lines),
            },
            severity=AuditSeverity.NOTICE,
            org_id=org_id,
            session=session,
        )

    return entry


def reverse_journal_entry(
    session: Session,
    *,
    entry: JournalEntry,
    reason: str,
    reversal_date: dt.date | None = None,
    actor_id: str | None = None,
) -> JournalEntry:
    """Post a mirrored entry that cancels ``entry``."""
    # Most specific first: "already reversed" tells the caller what happened,
    # where "not posted" would leave them guessing which of the two it was.
    if entry.reversed_by_id is not None:
        raise BusinessRuleViolation(
            f"Entry {entry.entry_number} has already been reversed.",
            code=ErrorCode.IMMUTABLE_RECORD,
        )
    if entry.status != JournalStatus.POSTED:
        raise BusinessRuleViolation("Only a posted entry can be reversed.")
    if not reason or len(reason.strip()) < 5:
        raise ValidationFailed("A reversal requires a substantive reason.")

    # Reverse into the current period when the original period has closed:
    # reopening a closed period to book a correction is exactly what the close
    # process exists to prevent.
    target_date = reversal_date or entry.entry_date
    period = ensure_period(session, org_id=entry.org_id, on_date=target_date)
    if not period.accepts_postings:
        target_date = utcnow().date()

    mirrored = [
        LineInput(
            account_id=line.account_id,
            debit=line.credit,
            credit=line.debit,
            memo=f"Reversal of {entry.entry_number}",
            property_id=line.property_id,
            unit_id=line.unit_id,
            lease_id=line.lease_id,
            owner_entity_id=line.owner_entity_id,
            vendor_id=line.vendor_id,
            bank_account_id=line.bank_account_id,
        )
        for line in entry.lines
    ]

    reversal = post_journal_entry(
        session,
        org_id=entry.org_id,
        entry_date=target_date,
        description=f"Reversal of {entry.entry_number}",
        lines=mirrored,
        source_type=entry.source_type,
        source_id=entry.source_id,
        property_id=entry.property_id,
        memo=reason,
        is_adjusting=True,
        system_posting=True,
        reverses_id=entry.id,
        actor_id=actor_id,
    )

    entry.reversed_by_id = reversal.id
    entry.reversal_reason = reason
    entry.status = JournalStatus.REVERSED
    session.flush()

    record_audit_event(
        action=AuditAction.JOURNAL_REVERSED,
        resource_type="JournalEntry",
        resource_id=entry.id,
        resource_label=entry.entry_number,
        payload={"reversal_entry": reversal.entry_number, "amount": str(entry.total_debit)},
        reason=reason,
        severity=AuditSeverity.WARNING,
        org_id=entry.org_id,
        session=session,
    )
    return reversal


def ensure_period(session: Session, *, org_id: str, on_date: dt.date) -> FiscalPeriod:
    """Find or create the monthly period containing ``on_date``.

    Auto-creation keeps a first posting from failing because nobody opened the
    month. Periods created this way start open; closing remains a deliberate act.
    """
    period = session.execute(
        select(FiscalPeriod).where(
            FiscalPeriod.org_id == org_id,
            FiscalPeriod.start_date <= on_date,
            FiscalPeriod.end_date >= on_date,
        )
    ).scalar_one_or_none()
    if period is not None:
        return period

    last_day = calendar.monthrange(on_date.year, on_date.month)[1]
    period = FiscalPeriod(
        org_id=org_id,
        fiscal_year=on_date.year,
        period_number=on_date.month,
        name=on_date.strftime("%B %Y"),
        start_date=dt.date(on_date.year, on_date.month, 1),
        end_date=dt.date(on_date.year, on_date.month, last_day),
        status=PeriodStatus.OPEN,
    )
    session.add(period)
    session.flush()
    return period


def close_period(
    session: Session,
    *,
    period: FiscalPeriod,
    actor_id: str,
    checklist: dict | None = None,
) -> FiscalPeriod:
    """Close a period after verifying it is safe to do so."""
    if period.status in (PeriodStatus.CLOSED, PeriodStatus.LOCKED):
        raise BusinessRuleViolation(f"Period {period.name} is already closed.")

    unbalanced = (
        session.execute(
            select(JournalEntry).where(
                JournalEntry.org_id == period.org_id,
                JournalEntry.period_id == period.id,
                JournalEntry.status == JournalStatus.DRAFT,
            )
        )
        .scalars()
        .all()
    )
    if unbalanced:
        raise BusinessRuleViolation(
            f"{len(unbalanced)} draft entries remain in {period.name}; post or discard them first."
        )

    period.status = PeriodStatus.CLOSED
    period.closed_at = utcnow()
    period.closed_by_id = actor_id
    period.close_checklist = checklist or {}
    session.flush()

    record_audit_event(
        action=AuditAction.PERIOD_CLOSED,
        resource_type="FiscalPeriod",
        resource_id=period.id,
        resource_label=period.name,
        payload={"checklist": checklist or {}},
        severity=AuditSeverity.WARNING,
        org_id=period.org_id,
        session=session,
    )
    return period


def reopen_period(
    session: Session, *, period: FiscalPeriod, reason: str, actor_id: str
) -> FiscalPeriod:
    """Reopen a closed period. Deliberately hard, always audited as critical."""
    if period.status == PeriodStatus.LOCKED:
        raise BusinessRuleViolation(f"Period {period.name} is locked and cannot be reopened.")
    if period.status != PeriodStatus.CLOSED:
        raise BusinessRuleViolation(f"Period {period.name} is not closed.")
    if not reason or len(reason.strip()) < 10:
        raise ValidationFailed("Reopening a period requires a detailed reason.")

    period.status = PeriodStatus.OPEN
    period.reopened_at = utcnow()
    period.reopened_by_id = actor_id
    period.reopen_reason = reason
    session.flush()

    record_audit_event(
        action=AuditAction.PERIOD_REOPENED,
        resource_type="FiscalPeriod",
        resource_id=period.id,
        resource_label=period.name,
        reason=reason,
        severity=AuditSeverity.CRITICAL,
        org_id=period.org_id,
        session=session,
    )
    return period


def trial_balance(
    session: Session,
    *,
    org_id: str,
    as_of: dt.date | None = None,
    property_id: str | None = None,
) -> list[dict]:
    """Per-account debit and credit totals for posted entries.

    The integrity check that matters: total debits must equal total credits. If
    they ever do not, something bypassed :func:`post_journal_entry`.
    """
    stmt = (
        select(
            Account.id,
            Account.code,
            Account.name,
            Account.account_type,
            Account.normal_balance,
            JournalLine.debit,
            JournalLine.credit,
        )
        .join(JournalLine, JournalLine.account_id == Account.id)
        .join(JournalEntry, JournalEntry.id == JournalLine.journal_entry_id)
        .where(
            Account.org_id == org_id,
            JournalEntry.status.in_([JournalStatus.POSTED, JournalStatus.REVERSED]),
        )
    )
    if as_of is not None:
        stmt = stmt.where(JournalEntry.entry_date <= as_of)
    if property_id is not None:
        stmt = stmt.where(JournalLine.property_id == property_id)

    totals: dict[str, dict] = {}
    for account_id, code, name, account_type, normal_balance, debit, credit in session.execute(
        stmt
    ):
        bucket = totals.setdefault(
            account_id,
            {
                "account_id": account_id,
                "code": code,
                "name": name,
                "account_type": str(account_type),
                "normal_balance": str(normal_balance),
                "debit": ZERO,
                "credit": ZERO,
            },
        )
        bucket["debit"] += debit
        bucket["credit"] += credit

    rows = sorted(totals.values(), key=lambda row: row["code"])
    for row in rows:
        signed = row["debit"] - row["credit"]
        row["balance"] = signed if row["normal_balance"] == "debit" else -signed
    return rows


def _load_accounts(session: Session, org_id: str, account_ids: set[str]) -> dict[str, Account]:
    accounts = {
        account.id: account
        for account in session.execute(
            select(Account).where(Account.org_id == org_id, Account.id.in_(account_ids))
        ).scalars()
    }
    missing = account_ids - set(accounts)
    if missing:
        raise NotFound(f"Unknown account(s): {', '.join(sorted(missing))}.")
    return accounts


__all__ += ["close_period", "reopen_period"]

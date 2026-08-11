"""Trust accounting: the three-way reconciliation.

A trust account holds other people's money — security deposits, and in many
jurisdictions owner funds. Every state real-estate regulator asks the same
question, and it is not "does the bank statement match the ledger". It is:

    bank balance  ==  book balance  ==  the sum of what every beneficiary is owed

That third leg is the one that catches the failure that matters. Bank and book
can agree perfectly while the trust is short, because the shortfall is between
*beneficiaries* — one resident's deposit funding another's refund. A two-way
reconciliation reports "balanced" the whole time that is happening.

Two rules follow, and both are enforced rather than trained.

**Commingling is refused.** Operating expenses do not come out of a trust
account. The payables service already refuses a trust account as the source of
an operating disbursement; this module reports any that got in anyway, because
one wrong entry is a licence problem rather than a bookkeeping one.

**A negative beneficiary balance is an error, not a number.** Nobody can be
owed less than nothing. If one appears, the trust has paid out somebody else's
money, and the report says so in those words.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import NotFound, ValidationFailed
from app.logging import get_logger
from app.models.accounting import (
    BankAccount,
    JournalEntry,
    JournalLine,
)
from app.models.audit import AuditAction, AuditOutcome, AuditSeverity
from app.models.leasing import Lease
from app.models.types import quantize_money, utcnow
from app.services.accounting.deposits import deposit_balances
from app.services.audit.recorder import record_audit_event

__all__ = [
    "BeneficiaryBalance",
    "TrustPosition",
    "commingling_exceptions",
    "reconcile_trust",
]

log = get_logger("services.accounting.trust")

ZERO = Decimal("0")


@dataclass
class BeneficiaryBalance:
    """What one party is owed out of the trust."""

    lease_id: str
    lease_number: str
    resident_label: str
    amount: Decimal

    @property
    def is_impossible(self) -> bool:
        """Nobody can be owed less than nothing."""
        return self.amount < ZERO


@dataclass
class TrustPosition:
    """A three-way tie-out at a point in time."""

    bank_account_id: str
    as_of: dt.date
    #: What the bank says, from the reconciled statement.
    bank_balance: Decimal
    #: What the general ledger says.
    book_balance: Decimal
    beneficiaries: list[BeneficiaryBalance] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)

    @property
    def beneficiary_total(self) -> Decimal:
        return quantize_money(sum((b.amount for b in self.beneficiaries), ZERO))

    @property
    def bank_to_book(self) -> Decimal:
        return quantize_money(self.bank_balance - self.book_balance)

    @property
    def book_to_beneficiaries(self) -> Decimal:
        """The leg a two-way reconciliation never looks at."""
        return quantize_money(self.book_balance - self.beneficiary_total)

    @property
    def shortfall(self) -> Decimal:
        """Money owed that is not there. The number a regulator asks for."""
        gap = self.beneficiary_total - self.bank_balance
        return quantize_money(gap) if gap > ZERO else ZERO

    @property
    def is_balanced(self) -> bool:
        return (
            self.bank_to_book == ZERO and self.book_to_beneficiaries == ZERO and not self.exceptions
        )


def reconcile_trust(
    session: Session,
    *,
    org_id: str,
    bank_account_id: str,
    as_of: dt.date | None = None,
    bank_balance: Decimal | None = None,
    actor_id: str | None = None,
) -> TrustPosition:
    """Tie bank, book, and beneficiary ledgers together.

    ``bank_balance`` comes from the statement where one is supplied; otherwise
    the book balance is used and the bank leg is trivially zero, which is
    honest but not a reconciliation — the caller is told so through
    ``exceptions``.
    """
    account = session.get(BankAccount, bank_account_id)
    if account is None or account.org_id != org_id:
        raise NotFound("No such bank account.")
    if not account.is_trust:
        raise ValidationFailed(
            f"{account.name} is not a trust account. Use the ordinary "
            "reconciliation for operating accounts."
        )

    today = as_of or utcnow().date()
    book = _book_balance(session, org_id=org_id, account=account, as_of=today)

    position = TrustPosition(
        bank_account_id=bank_account_id,
        as_of=today,
        bank_balance=quantize_money(bank_balance) if bank_balance is not None else book,
        book_balance=book,
    )
    if bank_balance is None:
        position.exceptions.append(
            "No statement balance was supplied, so the bank leg was assumed from "
            "the ledger. This is a book position, not a reconciliation."
        )

    position.beneficiaries = _beneficiary_balances(
        session, org_id=org_id, bank_account_id=bank_account_id, as_of=today
    )

    for beneficiary in position.beneficiaries:
        if beneficiary.is_impossible:
            position.exceptions.append(
                f"{beneficiary.lease_number} shows a negative held balance of "
                f"{beneficiary.amount}. The trust has paid out more than it held "
                "for this lease, which means it has used somebody else's money."
            )

    if position.bank_to_book != ZERO:
        position.exceptions.append(f"Bank and book differ by {position.bank_to_book}.")
    if position.book_to_beneficiaries != ZERO:
        position.exceptions.append(
            f"The ledger holds {position.book_balance} but beneficiaries are owed "
            f"{position.beneficiary_total}, a difference of "
            f"{position.book_to_beneficiaries}. Bank and book can agree while this "
            "is wrong, which is why the third leg is checked."
        )

    commingled = commingling_exceptions(session, org_id=org_id, account=account, as_of=today)
    position.exceptions.extend(commingled)

    severity = AuditSeverity.NOTICE if position.is_balanced else AuditSeverity.CRITICAL
    record_audit_event(
        action=AuditAction.RECONCILIATION_COMPLETED,
        resource_type="BankAccount",
        resource_id=bank_account_id,
        resource_label=account.name,
        severity=severity,
        outcome=AuditOutcome.SUCCESS if position.is_balanced else AuditOutcome.FAILURE,
        payload={
            "as_of": today.isoformat(),
            "bank": str(position.bank_balance),
            "book": str(position.book_balance),
            "beneficiaries": str(position.beneficiary_total),
            "shortfall": str(position.shortfall),
            "exceptions": position.exceptions,
        },
        reason=(
            "Trust three-way reconciliation balanced."
            if position.is_balanced
            else f"Trust reconciliation found {len(position.exceptions)} exception(s)."
        ),
        org_id=org_id,
        actor_id=actor_id,
        session=session,
    )

    if position.shortfall > ZERO:
        log.error(
            "trust account is short of what it owes",
            extra={
                "event": "trust.shortfall",
                "bank_account_id": bank_account_id,
                "shortfall": str(position.shortfall),
            },
        )
    return position


def _book_balance(
    session: Session, *, org_id: str, account: BankAccount, as_of: dt.date
) -> Decimal:
    debits, credits = session.execute(
        select(
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        )
        .join(JournalEntry, JournalEntry.id == JournalLine.journal_entry_id)
        .where(
            JournalLine.org_id == org_id,
            JournalLine.account_id == account.gl_account_id,
            JournalEntry.entry_date <= as_of,
        )
    ).one()
    return quantize_money(Decimal(str(debits)) - Decimal(str(credits)))


def _beneficiary_balances(
    session: Session, *, org_id: str, bank_account_id: str, as_of: dt.date
) -> list[BeneficiaryBalance]:
    """What each lease is owed out of *this* account, as at ``as_of``.

    Summed from the deposit subledger rather than read off the lease, because
    a current-balance column cannot answer either question this leg has to
    answer.

    **Which account.** An operator with a trust account per jurisdiction needs
    each reconciled against the deposits that account holds. Scoping by
    organization alone reports a shortfall on one account and an equal surplus
    on the other, and - worse - ties out cleanly when one is genuinely short by
    exactly what the other is over.

    **Which date.** The ledger leg stops at ``as_of``. A beneficiary leg that
    reports today's balances is being compared against a different point in
    time, so a year-end tie-out run in March silently includes every deposit
    taken since.

    Leases with a zero balance are omitted: they are not beneficiaries. A
    *negative* balance is kept, because the trust having paid out more than it
    held for somebody is the single most important thing this can find.
    """
    balances_by_lease = deposit_balances(
        session, org_id=org_id, bank_account_id=bank_account_id, as_of=as_of
    )
    if not balances_by_lease:
        return []

    leases = {
        lease.id: lease
        for lease in session.execute(
            select(Lease).where(
                Lease.org_id == org_id,
                Lease.id.in_(list(balances_by_lease)),
            )
        ).scalars()
    }

    balances: list[BeneficiaryBalance] = []
    for lease_id, held in balances_by_lease.items():
        if held == ZERO:
            continue  # Settled and returned; no longer a beneficiary.
        lease = leases.get(lease_id)
        label = lease.lease_number if lease is not None else lease_id
        balances.append(
            BeneficiaryBalance(
                lease_id=lease_id,
                lease_number=label,
                resident_label=label,
                amount=quantize_money(held),
            )
        )
    balances.sort(key=lambda beneficiary: beneficiary.lease_number)
    return balances


def commingling_exceptions(
    session: Session, *, org_id: str, account: BankAccount, as_of: dt.date
) -> list[str]:
    """Operating activity that reached a trust account.

    One of these is a licence problem rather than a bookkeeping one, which is
    why they are reported individually rather than counted.
    """
    from app.models.accounting import Account, AccountType

    rows = session.execute(
        select(JournalEntry.entry_number, JournalEntry.entry_date, Account.name, JournalLine.id)
        .select_from(JournalLine)
        .join(JournalEntry, JournalEntry.id == JournalLine.journal_entry_id)
        .join(Account, Account.id == JournalLine.account_id)
        .where(
            JournalLine.org_id == org_id,
            JournalEntry.entry_date <= as_of,
            # Any entry touching this trust account...
            JournalEntry.id.in_(
                select(JournalLine.journal_entry_id).where(
                    JournalLine.org_id == org_id,
                    JournalLine.account_id == account.gl_account_id,
                )
            ),
            # ...whose other side is an operating expense or revenue account.
            Account.account_type.in_([AccountType.EXPENSE, AccountType.REVENUE]),
        )
    ).all()

    return [
        f"Entry {entry_number} on {entry_date} moves trust funds against "
        f"'{account_name}', an operating account. Trust money does not pay "
        "operating costs."
        for entry_number, entry_date, account_name, _ in rows
    ]


def trust_position_rows(position: TrustPosition) -> list[dict]:
    """Flatten a position for a report."""
    return [
        {
            "lease": beneficiary.lease_number,
            "held": beneficiary.amount,
            "status": "impossible" if beneficiary.is_impossible else "ok",
        }
        for beneficiary in position.beneficiaries
    ]

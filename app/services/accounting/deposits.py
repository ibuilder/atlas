"""Security deposits: the subledger that says whose money is in the trust.

A trust account holds money the operator does not own. Two questions have to be
answerable at any moment, and answering only the first is the failure mode this
module exists to prevent:

1. How much is in the account? The general ledger answers this.
2. Who is it owed to, and how much each? Nothing answers this unless something
   records it.

The second is the *beneficiary ledger*, and it is what makes a three-way
reconciliation three-way. Bank and book can agree perfectly while the operator
is short, because both measure the same pile of money from the same side. Only
the sum of what every resident is individually owed can contradict them.

Every movement is recorded here **and** posted to the ledger in the same call.
Doing one without the other is precisely how the two drift apart, so there is no
public function that does one without the other.

``amount`` on a movement is signed - positive took money in, negative let it
out - so a balance is a sum and there is no direction flag to get backwards.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import BusinessRuleViolation, NotFound, ValidationFailed
from app.logging import get_logger
from app.models.accounting import (
    BankAccount,
    DepositMovement,
    DepositMovementKind,
)
from app.models.audit import AuditAction, AuditSeverity
from app.models.leasing import Lease
from app.models.types import quantize_money, utcnow
from app.services.accounting.chart import AccountCode, account_by_code
from app.services.accounting.ledger import LineInput, post_journal_entry
from app.services.audit.recorder import record_audit_event

__all__ = [
    "collect_deposit",
    "deposit_balance",
    "deposit_balances",
    "holding_account_id",
    "record_deposit_movement",
    "release_deposit",
]

log = get_logger("services.accounting.deposits")

ZERO = Decimal("0")

#: Movements that reduce what is held. Kept as a set rather than a sign test so
#: a caller cannot ask for a "returned" movement that adds money.
_RELEASING = frozenset(
    {
        DepositMovementKind.RETURNED,
        DepositMovementKind.APPLIED,
        DepositMovementKind.FORFEITED,
    }
)


def _trust_account(session: Session, *, org_id: str, bank_account_id: str) -> BankAccount:
    account = session.get(BankAccount, bank_account_id)
    if account is None or account.org_id != org_id:
        raise NotFound("No such bank account.")
    if not account.is_trust:
        raise ValidationFailed(
            f"{account.name} is not a trust account. A security deposit that is "
            "not held in trust is commingled with operating money, which is a "
            "licensing matter rather than a bookkeeping preference."
        )
    return account


def _lease(session: Session, *, org_id: str, lease_id: str) -> Lease:
    lease = session.get(Lease, lease_id)
    if lease is None or lease.org_id != org_id:
        raise NotFound("No such lease.")
    return lease


def record_deposit_movement(
    session: Session,
    *,
    org_id: str,
    lease_id: str,
    bank_account_id: str,
    amount: Decimal,
    kind: DepositMovementKind,
    effective_date: dt.date | None = None,
    reason: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    actor_id: str | None = None,
) -> DepositMovement:
    """Record one signed movement, post it, and update the lease's balance.

    ``amount`` is supplied unsigned; ``kind`` decides the direction. Passing a
    magnitude and a meaning is harder to get backwards than passing a sign, and
    getting it backwards here turns a refund into a second collection.
    """
    magnitude = quantize_money(abs(amount))
    if not magnitude.is_finite() or magnitude <= ZERO:
        raise ValidationFailed("A deposit movement needs a non-zero amount.")

    account = _trust_account(session, org_id=org_id, bank_account_id=bank_account_id)
    lease = _lease(session, org_id=org_id, lease_id=lease_id)
    when = effective_date or utcnow().date()

    releasing = kind in _RELEASING
    signed = -magnitude if releasing else magnitude

    if releasing:
        held = deposit_balance(
            session, org_id=org_id, lease_id=lease_id, bank_account_id=bank_account_id, as_of=when
        )
        if magnitude > held:
            raise BusinessRuleViolation(
                f"Releasing {magnitude} would leave {lease.lease_number} owed "
                f"{held - magnitude}. A trust cannot pay out more than it holds "
                "for a beneficiary - that is somebody else's money."
            )

    trust_cash_id = account.gl_account_id
    liability = account_by_code(session, org_id, AccountCode.SECURITY_DEPOSITS_HELD)

    description = f"Deposit {kind} - {lease.lease_number}"
    lines = (
        [
            LineInput(
                account_id=trust_cash_id,
                debit=magnitude,
                memo=description,
                property_id=lease.property_id,
                lease_id=lease.id,
                bank_account_id=account.id,
            ),
            LineInput(
                account_id=liability.id,
                credit=magnitude,
                memo=description,
                property_id=lease.property_id,
                lease_id=lease.id,
            ),
        ]
        if not releasing
        else [
            LineInput(
                account_id=liability.id,
                debit=magnitude,
                memo=description,
                property_id=lease.property_id,
                lease_id=lease.id,
            ),
            LineInput(
                account_id=trust_cash_id,
                credit=magnitude,
                memo=description,
                property_id=lease.property_id,
                lease_id=lease.id,
                bank_account_id=account.id,
            ),
        ]
    )

    entry = post_journal_entry(
        session,
        org_id=org_id,
        entry_date=when,
        description=description,
        lines=lines,
        source_type="deposit_movement",
        property_id=lease.property_id,
        system_posting=True,
        actor_id=actor_id,
    )

    movement = DepositMovement(
        org_id=org_id,
        lease_id=lease.id,
        bank_account_id=account.id,
        amount=signed,
        effective_date=when,
        kind=kind,
        reason=reason,
        journal_entry_id=entry.id,
        source_type=source_type,
        source_id=source_id,
    )
    session.add(movement)
    session.flush()

    # The lease keeps a current balance for display and for anything that only
    # needs "now". The movements remain the authority: this is a cache, and the
    # reconciliation deliberately does not read it.
    lease.deposit_held = quantize_money(deposit_balance(session, org_id=org_id, lease_id=lease.id))
    session.flush()

    record_audit_event(
        action=AuditAction.PAYMENT_RECEIVED if not releasing else AuditAction.PAYMENT_APPLIED,
        resource_type="DepositMovement",
        resource_id=movement.id,
        resource_label=lease.lease_number,
        severity=AuditSeverity.NOTICE,
        payload={
            "kind": str(kind),
            "amount": str(signed),
            "bank_account_id": account.id,
            "effective_date": when.isoformat(),
            "held_after": str(lease.deposit_held),
        },
        reason=reason or f"Security deposit {kind} for {lease.lease_number}.",
        org_id=org_id,
        actor_id=actor_id,
        session=session,
    )
    return movement


def collect_deposit(
    session: Session,
    *,
    org_id: str,
    lease_id: str,
    bank_account_id: str,
    amount: Decimal,
    effective_date: dt.date | None = None,
    reason: str | None = None,
    actor_id: str | None = None,
) -> DepositMovement:
    """Take a deposit into trust."""
    return record_deposit_movement(
        session,
        org_id=org_id,
        lease_id=lease_id,
        bank_account_id=bank_account_id,
        amount=amount,
        kind=DepositMovementKind.COLLECTED,
        effective_date=effective_date,
        reason=reason,
        actor_id=actor_id,
    )


def release_deposit(
    session: Session,
    *,
    org_id: str,
    lease_id: str,
    bank_account_id: str,
    amount: Decimal,
    kind: DepositMovementKind = DepositMovementKind.RETURNED,
    effective_date: dt.date | None = None,
    reason: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    actor_id: str | None = None,
) -> DepositMovement:
    """Let a deposit, or part of one, back out of trust."""
    if kind not in _RELEASING:
        raise ValidationFailed(f"{kind} does not release money from the trust.")
    return record_deposit_movement(
        session,
        org_id=org_id,
        lease_id=lease_id,
        bank_account_id=bank_account_id,
        amount=amount,
        kind=kind,
        effective_date=effective_date,
        reason=reason,
        source_type=source_type,
        source_id=source_id,
        actor_id=actor_id,
    )


def holding_account_id(session: Session, *, org_id: str, lease_id: str) -> str | None:
    """Which trust account currently holds this lease's deposit.

    ``None`` when nothing is held - a lease whose deposit was never recorded,
    or one already settled. Raises where two accounts both hold a non-zero
    balance for the same lease, because splitting a disposition across accounts
    is a decision for a person rather than a default for this function.
    """
    rows = session.execute(
        select(DepositMovement.bank_account_id, func.coalesce(func.sum(DepositMovement.amount), 0))
        .where(DepositMovement.org_id == org_id, DepositMovement.lease_id == lease_id)
        .group_by(DepositMovement.bank_account_id)
    ).all()

    holding = [account_id for account_id, total in rows if Decimal(str(total)) != ZERO]
    if not holding:
        return None
    if len(holding) > 1:
        raise BusinessRuleViolation(
            "This lease has a deposit balance in more than one trust account. "
            "Move it into one account before settling, so the disposition names "
            "where the money came from."
        )
    return holding[0]


def deposit_balance(
    session: Session,
    *,
    org_id: str,
    lease_id: str,
    bank_account_id: str | None = None,
    as_of: dt.date | None = None,
) -> Decimal:
    """What is held for one lease, optionally in one account, at a date."""
    conditions = [
        DepositMovement.org_id == org_id,
        DepositMovement.lease_id == lease_id,
    ]
    if bank_account_id is not None:
        conditions.append(DepositMovement.bank_account_id == bank_account_id)
    if as_of is not None:
        conditions.append(DepositMovement.effective_date <= as_of)

    total = session.execute(
        select(func.coalesce(func.sum(DepositMovement.amount), 0)).where(*conditions)
    ).scalar_one()
    return quantize_money(Decimal(str(total)))


def deposit_balances(
    session: Session,
    *,
    org_id: str,
    bank_account_id: str | None = None,
    as_of: dt.date | None = None,
) -> dict[str, Decimal]:
    """What is held for every lease, keyed by lease id.

    Scoped to one account where given, and to a date where given. Both matter:
    an operator with a trust account per jurisdiction needs each reconciled
    against its own deposits, and a year-end tie-out needs the balance on the
    year end rather than the balance today.
    """
    conditions = [DepositMovement.org_id == org_id]
    if bank_account_id is not None:
        conditions.append(DepositMovement.bank_account_id == bank_account_id)
    if as_of is not None:
        conditions.append(DepositMovement.effective_date <= as_of)

    rows = session.execute(
        select(DepositMovement.lease_id, func.coalesce(func.sum(DepositMovement.amount), 0))
        .where(*conditions)
        .group_by(DepositMovement.lease_id)
    ).all()

    return {lease_id: quantize_money(Decimal(str(total))) for lease_id, total in rows}

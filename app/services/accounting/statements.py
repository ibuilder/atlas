"""Owner statements and distributions.

The interesting problem is **temporal ownership**. A property changes hands on
the 14th; the March statement must credit the outgoing owner for thirteen days
of income and the incoming owner for eighteen. Systems that store ownership as a
column on the property cannot express this at all, and systems that apportion by
"who owns it today" quietly pay the wrong person.

Atlas apportions **day by day**: for each day in the period, the owner's share is
their stake on that day, and their overall share is the mean of those daily
shares. That falls out of the temporal stake model and needs no special case for
a transfer, a fractional holding, or both at once.

Distributions are a draw against owner equity, and can never exceed cash on hand
less the owner's agreed reserve.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import BusinessRuleViolation, NotFound, ValidationFailed
from app.logging import get_logger
from app.models.accounting import (
    ZERO,
    Account,
    AccountType,
    BankAccount,
    DistributionStatus,
    JournalEntry,
    JournalLine,
    JournalStatus,
    OwnerDistribution,
    OwnerStatement,
    PaymentMethod,
)
from app.models.audit import AuditAction, AuditSeverity
from app.models.org import OwnerEntity, OwnershipStake
from app.models.sequences import SequenceKey
from app.models.types import quantize_money, utcnow
from app.services.accounting.chart import AccountCode, account_by_code
from app.services.accounting.ledger import LineInput, post_journal_entry
from app.services.audit.recorder import record_audit_event
from app.services.common.numbering import next_number

__all__ = [
    "MANAGEMENT_FEE_SETTING",
    "PeriodActivity",
    "generate_statement",
    "generate_statements_for_period",
    "issue_distribution",
    "ownership_share",
    "period_activity",
]

log = get_logger("services.accounting.statements")

#: Organization setting holding the management fee as a decimal fraction of
#: collected income (``"0.08"`` for eight percent).
MANAGEMENT_FEE_SETTING = "management_fee_rate"


@dataclass(frozen=True)
class PeriodActivity:
    """Income and expense recognised against one property in a period."""

    income: Decimal
    expense: Decimal

    @property
    def net(self) -> Decimal:
        return quantize_money(self.income - self.expense)


def ownership_share(
    session: Session,
    *,
    property_id: str,
    period_start: dt.date,
    period_end: dt.date,
    owner_entity_id: str,
) -> Decimal:
    """The owner's day-weighted share of a property across a period.

    Returned as a fraction, not a percentage. A stake of 100% held for the whole
    period gives ``1``; the same stake acquired half way through gives roughly
    ``0.5``.

    Day weighting is what makes a mid-period transfer correct without a special
    case: each day contributes its own ownership picture, and the period's share
    is their mean.
    """
    if period_end < period_start:
        raise ValidationFailed("The period end cannot precede its start.")

    stakes = (
        session.execute(select(OwnershipStake).where(OwnershipStake.property_id == property_id))
        .scalars()
        .all()
    )

    days = (period_end - period_start).days + 1
    held = Decimal(0)
    for offset in range(days):
        day = period_start + dt.timedelta(days=offset)
        for stake in stakes:
            if stake.owner_entity_id == owner_entity_id and stake.covers(day):
                held += Decimal(stake.percentage) / Decimal(100)

    return (held / Decimal(days)).quantize(Decimal("0.000001"))


def period_activity(
    session: Session,
    *,
    org_id: str,
    property_id: str,
    period_start: dt.date,
    period_end: dt.date,
) -> PeriodActivity:
    """Sum posted revenue and expense for one property in a period.

    Read from the ledger rather than from operational tables, so a statement can
    only ever say what the books say. An owner querying a figure and an
    accountant querying the ledger must not be able to get different answers.
    """
    rows = session.execute(
        select(Account.account_type, JournalLine.debit, JournalLine.credit)
        .join(JournalLine, JournalLine.account_id == Account.id)
        .join(JournalEntry, JournalEntry.id == JournalLine.journal_entry_id)
        .where(
            Account.org_id == org_id,
            JournalLine.property_id == property_id,
            JournalEntry.status.in_([JournalStatus.POSTED, JournalStatus.REVERSED]),
            JournalEntry.entry_date >= period_start,
            JournalEntry.entry_date <= period_end,
        )
    )

    income = expense = ZERO
    for account_type, debit, credit in rows:
        if account_type == AccountType.REVENUE:
            income += credit - debit
        elif account_type == AccountType.EXPENSE:
            expense += debit - credit

    return PeriodActivity(income=quantize_money(income), expense=quantize_money(expense))


def _management_fee_rate(session: Session, org_id: str) -> Decimal:
    from app.models.org import Organization

    organization = session.get(Organization, org_id)
    raw = organization.setting(MANAGEMENT_FEE_SETTING) if organization else None
    if raw is None:
        return Decimal(0)
    try:
        rate = Decimal(str(raw))
    except ArithmeticError:
        log.warning(
            "unparseable management fee rate; treating as zero",
            extra={"event": "statement.fee_rate_invalid", "org_id": org_id},
        )
        return Decimal(0)
    if rate < 0 or rate > 1:
        raise ValidationFailed(
            f"The management fee rate must be a fraction between 0 and 1, not {rate}."
        )
    return rate


def generate_statement(
    session: Session,
    *,
    org_id: str,
    owner_entity_id: str,
    property_id: str,
    period_start: dt.date,
    period_end: dt.date,
    actor_id: str | None = None,
) -> OwnerStatement:
    """Produce (or refresh) one owner's statement for one property and period.

    Idempotent: re-running for the same owner, property, and period end updates
    the existing statement rather than creating a second one, so a corrected
    ledger can be restated without duplicating.
    """
    owner = session.get(OwnerEntity, owner_entity_id)
    if owner is None or owner.org_id != org_id:
        raise NotFound("That owner was not found.")

    share = ownership_share(
        session,
        property_id=property_id,
        period_start=period_start,
        period_end=period_end,
        owner_entity_id=owner_entity_id,
    )
    activity = period_activity(
        session,
        org_id=org_id,
        property_id=property_id,
        period_start=period_start,
        period_end=period_end,
    )

    income = quantize_money(activity.income * share)
    expense = quantize_money(activity.expense * share)
    fee = quantize_money(income * _management_fee_rate(session, org_id))
    net = quantize_money(income - expense - fee)
    opening = _closing_balance_before(
        session,
        org_id=org_id,
        owner_entity_id=owner_entity_id,
        property_id=property_id,
        before=period_start,
    )

    statement = (
        session.execute(
            select(OwnerStatement).where(
                OwnerStatement.owner_entity_id == owner_entity_id,
                OwnerStatement.property_id == property_id,
                OwnerStatement.period_end == period_end,
            )
        )
        .scalars()
        .first()
    )

    if statement is None:
        statement = OwnerStatement(
            org_id=org_id,
            statement_number=next_number(session, SequenceKey.OWNER_STATEMENT, org_id=org_id),
            owner_entity_id=owner_entity_id,
            property_id=property_id,
            period_start=period_start,
            period_end=period_end,
            # Set explicitly: Python-side column defaults are not applied until
            # flush, and the closing balance below is computed from this value.
            distribution_amount=ZERO,
        )
        session.add(statement)
    elif statement.status == "issued":
        raise BusinessRuleViolation(
            f"Statement {statement.statement_number} has been issued and cannot be "
            "regenerated. Produce a corrected statement for a later period."
        )

    statement.opening_balance = opening
    statement.total_income = income
    statement.total_expense = expense
    statement.management_fee = fee
    statement.net_income = net
    statement.ownership_percentage = quantize_money(share * 100)
    statement.closing_balance = quantize_money(opening + net - statement.distribution_amount)
    statement.generated_at = utcnow()
    statement.status = "draft"
    statement.detail = {
        "property_income": str(activity.income),
        "property_expense": str(activity.expense),
        "owner_share": str(share),
        "days": (period_end - period_start).days + 1,
    }
    session.flush()

    record_audit_event(
        action=AuditAction.STATEMENT_GENERATED,
        resource_type="OwnerStatement",
        resource_id=statement.id,
        resource_label=statement.statement_number,
        payload={
            "owner_entity_id": owner_entity_id,
            "property_id": property_id,
            "share": str(share),
            "net_income": str(net),
        },
        org_id=org_id,
        actor_id=actor_id,
        session=session,
    )
    return statement


def _closing_balance_before(
    session: Session,
    *,
    org_id: str,
    owner_entity_id: str,
    property_id: str,
    before: dt.date,
) -> Decimal:
    previous = (
        session.execute(
            select(OwnerStatement)
            .where(
                OwnerStatement.org_id == org_id,
                OwnerStatement.owner_entity_id == owner_entity_id,
                OwnerStatement.property_id == property_id,
                OwnerStatement.period_end < before,
            )
            .order_by(OwnerStatement.period_end.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    return previous.closing_balance if previous else ZERO


def generate_statements_for_period(
    session: Session,
    *,
    org_id: str,
    period_start: dt.date,
    period_end: dt.date,
    actor_id: str | None = None,
) -> list[OwnerStatement]:
    """Generate a statement for every owner who held a stake during the period.

    Driven from the stakes rather than from current ownership, so an owner who
    sold mid-period still receives the statement covering the days they held it.
    """
    stakes = (
        session.execute(
            select(OwnershipStake).where(
                OwnershipStake.org_id == org_id,
                OwnershipStake.effective_from <= period_end,
            )
        )
        .scalars()
        .all()
    )

    pairs = {
        (stake.owner_entity_id, stake.property_id)
        for stake in stakes
        if stake.effective_to is None or stake.effective_to >= period_start
    }

    return [
        generate_statement(
            session,
            org_id=org_id,
            owner_entity_id=owner_id,
            property_id=property_id,
            period_start=period_start,
            period_end=period_end,
            actor_id=actor_id,
        )
        for owner_id, property_id in sorted(pairs)
    ]


def issue_distribution(
    session: Session,
    *,
    statement: OwnerStatement,
    bank_account_id: str,
    amount: Decimal,
    distribution_date: dt.date,
    method: PaymentMethod = PaymentMethod.ACH,
    approved_by_id: str | None = None,
    actor_id: str | None = None,
) -> OwnerDistribution:
    """Pay funds out to an owner as a draw against equity.

    Debit Owner Equity, credit the bank account's ledger account.
    """
    amount = quantize_money(amount)
    if amount <= ZERO:
        raise ValidationFailed("A distribution must be greater than zero.")

    owner = session.get(OwnerEntity, statement.owner_entity_id)
    if owner is None:
        raise NotFound("That owner was not found.")

    available = quantize_money(statement.closing_balance - owner.reserve_amount)
    if amount > available:
        raise BusinessRuleViolation(
            f"A distribution of {amount} exceeds the {available} available after "
            f"retaining the agreed reserve of {owner.reserve_amount}."
        )

    bank = session.get(BankAccount, bank_account_id)
    if bank is None or bank.org_id != statement.org_id:
        raise NotFound("That bank account was not found.")
    if bank.is_trust:
        raise BusinessRuleViolation(
            f"{bank.name} is a trust account and cannot fund an owner distribution."
        )

    equity = account_by_code(session, statement.org_id, AccountCode.OWNER_EQUITY)
    entry = post_journal_entry(
        session,
        org_id=statement.org_id,
        entry_date=distribution_date,
        description=f"Owner distribution - {owner.name}",
        lines=[
            LineInput(
                account_id=equity.id,
                debit=amount,
                memo=f"Distribution for {statement.statement_number}",
                property_id=statement.property_id,
                owner_entity_id=owner.id,
            ),
            LineInput(
                account_id=bank.gl_account_id,
                credit=amount,
                memo=f"Distribution for {statement.statement_number}",
                property_id=statement.property_id,
                owner_entity_id=owner.id,
                bank_account_id=bank.id,
            ),
        ],
        source_type="owner_distribution",
        source_id=statement.id,
        property_id=statement.property_id,
        system_posting=True,
        actor_id=actor_id,
    )

    distribution = OwnerDistribution(
        org_id=statement.org_id,
        distribution_number=next_number(session, SequenceKey.DISTRIBUTION, org_id=statement.org_id),
        owner_entity_id=owner.id,
        property_id=statement.property_id,
        statement_id=statement.id,
        bank_account_id=bank.id,
        amount=amount,
        distribution_date=distribution_date,
        method=method,
        status=DistributionStatus.ISSUED,
        approved_by_id=approved_by_id,
        approved_at=utcnow() if approved_by_id else None,
        journal_entry_id=entry.id,
    )
    session.add(distribution)

    statement.distribution_amount = quantize_money(statement.distribution_amount + amount)
    statement.closing_balance = quantize_money(statement.closing_balance - amount)
    session.flush()

    record_audit_event(
        action=AuditAction.OWNER_DISTRIBUTION,
        resource_type="OwnerDistribution",
        resource_id=distribution.id,
        resource_label=distribution.distribution_number,
        payload={
            "owner_entity_id": owner.id,
            "amount": str(amount),
            "statement": statement.statement_number,
        },
        severity=AuditSeverity.NOTICE,
        org_id=statement.org_id,
        actor_id=actor_id,
        session=session,
    )
    return distribution

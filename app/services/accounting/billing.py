"""Recurring billing and the delinquency sweep.

Both are scheduled jobs, so both must be **idempotent by watermark** rather than
by remembering that they ran. At-least-once delivery guarantees a re-run
eventually happens, and the failure mode here is charging a resident twice.

* Recurring charges advance ``LeaseCharge.last_billed_through``. A cycle already
  behind the watermark is never billed again, whatever the job does.
* The delinquency sweep advances ``Invoice.delinquency_stage``. A stage already
  reached is never re-assessed, so a late fee is charged once per invoice per
  stage and not once per run.

Proration is calendar-correct and to the day: a resident moving in on the 14th
of a 31-day month pays eighteen thirty-firsts, not half.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging import get_logger
from app.models.accounting import (
    ZERO,
    ChargeCode,
    Invoice,
    InvoiceStatus,
)
from app.models.audit import AuditAction, AuditSeverity
from app.models.leasing import ChargeFrequency, Lease, LeaseCharge, LeaseStatus
from app.models.resident import Notice, NoticeKind
from app.models.types import quantize_money, utcnow
from app.observability import DELINQUENCY_NOTICES
from app.services.accounting.chart import AccountCode, account_by_code
from app.services.accounting.receivables import ChargeInput, issue_invoice
from app.services.audit.recorder import record_audit_event

__all__ = [
    "DELINQUENCY_STAGES",
    "BillingRun",
    "DelinquencyRun",
    "generate_recurring_charges",
    "month_end",
    "prorated_amount",
    "sweep_delinquency",
]

log = get_logger("services.accounting.billing")


@dataclass
class BillingRun:
    invoices: list[Invoice] = field(default_factory=list)
    cycles_billed: int = 0
    leases_seen: int = 0


@dataclass
class DelinquencyRun:
    notices: list[Notice] = field(default_factory=list)
    late_fees: list[Invoice] = field(default_factory=list)
    escalated: int = 0


#: ``(stage, days past due, notice kind, assess a late fee)``.
#: Escalation is staged rather than a single overdue flag because the notices
#: carry different legal weight and different response deadlines.
DELINQUENCY_STAGES: tuple[tuple[int, int, NoticeKind, bool], ...] = (
    (1, 0, NoticeKind.LATE_RENT, True),
    (2, 10, NoticeKind.LATE_RENT, False),
    (3, 30, NoticeKind.PAY_OR_QUIT, False),
)


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------


def month_end(day: dt.date) -> dt.date:
    return dt.date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


def _add_months(source: dt.date, months: int) -> dt.date:
    total = source.month - 1 + months
    year = source.year + total // 12
    month = total % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(source.day, last))


def _cycle_end(start: dt.date, frequency: ChargeFrequency) -> dt.date:
    if frequency == ChargeFrequency.MONTHLY:
        return month_end(start)
    if frequency == ChargeFrequency.QUARTERLY:
        return _add_months(start, 3) - dt.timedelta(days=1)
    if frequency == ChargeFrequency.ANNUAL:
        return _add_months(start, 12) - dt.timedelta(days=1)
    return start  # one-time charges occupy a single day


def prorated_amount(
    amount: Decimal,
    cycle_start: dt.date,
    cycle_end: dt.date,
    billed_from: dt.date,
    billed_to: dt.date,
) -> Decimal:
    """Apportion a full cycle's amount to the days actually occupied.

    To the day, not to the half-month. A resident who moves in on the 14th of a
    31-day month owes eighteen thirty-firsts of the rent, and will check.
    """
    cycle_days = (cycle_end - cycle_start).days + 1
    billed_days = (billed_to - billed_from).days + 1
    if cycle_days <= 0 or billed_days >= cycle_days:
        return quantize_money(amount)
    return quantize_money(amount * Decimal(billed_days) / Decimal(cycle_days))


# ---------------------------------------------------------------------------
# Recurring charges
# ---------------------------------------------------------------------------


def generate_recurring_charges(
    session: Session,
    *,
    org_id: str,
    through_date: dt.date | None = None,
    actor_id: str | None = None,
) -> BillingRun:
    """Raise invoices for every lease charge cycle due on or before ``through_date``.

    One invoice per lease per cycle, carrying every charge due in that cycle, so
    a resident receives a single bill rather than one per charge line.
    """
    through = through_date or utcnow().date()
    run = BillingRun()

    leases = (
        session.execute(
            select(Lease).where(
                Lease.org_id == org_id,
                Lease.status.in_([LeaseStatus.ACTIVE, LeaseStatus.HOLDOVER]),
                Lease.start_date <= through,
            )
        )
        .scalars()
        .all()
    )

    for lease in leases:
        run.leases_seen += 1
        charges = (
            session.execute(
                select(LeaseCharge).where(
                    LeaseCharge.lease_id == lease.id, LeaseCharge.is_active.is_(True)
                )
            )
            .scalars()
            .all()
        )

        # cycle end -> the charge lines due in it
        cycles: dict[dt.date, list[tuple[LeaseCharge, ChargeInput]]] = {}

        for charge in charges:
            for cycle_start, cycle_end, amount in _due_cycles(charge, lease, through):
                account_id = _charge_account(session, org_id, charge)
                cycles.setdefault(cycle_end, []).append(
                    (
                        charge,
                        ChargeInput(
                            description=f"{charge.description} - {cycle_start:%B %Y}",
                            amount=amount,
                            account_id=account_id,
                            charge_code_id=charge.charge_code_id,
                            service_period_start=cycle_start,
                            service_period_end=cycle_end,
                        ),
                    )
                )

        for cycle_end in sorted(cycles):
            entries = cycles[cycle_end]
            issue_date = min(
                (
                    item[1].service_period_start
                    for item in entries
                    if item[1].service_period_start is not None
                ),
                default=cycle_end,
            )
            due_date = _due_date(lease, issue_date)

            invoice = issue_invoice(
                session,
                org_id=org_id,
                charges=[item[1] for item in entries],
                issue_date=issue_date,
                due_date=due_date,
                lease=lease,
                property_id=lease.property_id,
                unit_id=lease.unit_id,
                period_start=issue_date,
                period_end=cycle_end,
                actor_id=actor_id,
            )
            run.invoices.append(invoice)
            run.cycles_billed += 1

            # The watermark. Advanced only once the invoice exists, so a crash
            # before this point re-bills the cycle rather than skipping it -
            # the safe direction to fail.
            for charge, _ in entries:
                charge.last_billed_through = cycle_end

        session.flush()

    return run


def _due_cycles(
    charge: LeaseCharge, lease: Lease, through: dt.date
) -> list[tuple[dt.date, dt.date, Decimal]]:
    """Every unbilled cycle for a charge, with its prorated amount."""
    if charge.frequency == ChargeFrequency.ONE_TIME:
        if charge.last_billed_through is not None or charge.start_date > through:
            return []
        return [(charge.start_date, charge.start_date, quantize_money(charge.amount))]

    coverage_start = max(charge.start_date, lease.start_date)
    coverage_end = min(charge.end_date or lease.end_date, lease.end_date)

    cursor = (
        charge.last_billed_through + dt.timedelta(days=1)
        if charge.last_billed_through
        else coverage_start
    )

    cycles: list[tuple[dt.date, dt.date, Decimal]] = []
    guard = 0
    while cursor <= through and cursor <= coverage_end:
        guard += 1
        if guard > 600:  # ~50 years of monthly cycles; a loop this long is a bug
            log.error(
                "aborting runaway billing cycle expansion",
                extra={"event": "billing.cycle_guard", "charge_id": charge.id},
            )
            break

        # The natural cycle the cursor falls inside, so proration measures
        # against a full month rather than against the stub.
        natural_start = dt.date(cursor.year, cursor.month, 1)
        natural_end = _cycle_end(natural_start, charge.frequency)

        billed_from = max(cursor, coverage_start)
        billed_to = min(natural_end, coverage_end)
        if billed_to < billed_from:
            break

        amount = (
            prorated_amount(charge.amount, natural_start, natural_end, billed_from, billed_to)
            if charge.prorate
            else quantize_money(charge.amount)
        )
        if amount > ZERO:
            cycles.append((billed_from, billed_to, amount))

        cursor = billed_to + dt.timedelta(days=1)

    return cycles


def _due_date(lease: Lease, issue_date: dt.date) -> dt.date:
    """Rent is due on the lease's billing day of the issue month."""
    last = calendar.monthrange(issue_date.year, issue_date.month)[1]
    candidate = dt.date(issue_date.year, issue_date.month, min(lease.billing_day, last))
    return max(candidate, issue_date)


def _charge_account(session: Session, org_id: str, charge: LeaseCharge) -> str:
    code = session.get(ChargeCode, charge.charge_code_id)
    if code is not None:
        return code.gl_account_id
    return account_by_code(session, org_id, AccountCode.RENTAL_INCOME).id


# ---------------------------------------------------------------------------
# Delinquency
# ---------------------------------------------------------------------------


def sweep_delinquency(
    session: Session,
    *,
    org_id: str,
    as_of: dt.date | None = None,
    actor_id: str | None = None,
) -> DelinquencyRun:
    """Escalate overdue invoices through the notice stages.

    The lease's grace period is honoured before anything happens, and each stage
    fires at most once per invoice - the stage column is the watermark.
    """
    today = as_of or utcnow().date()
    run = DelinquencyRun()

    overdue = (
        session.execute(
            select(Invoice).where(
                Invoice.org_id == org_id,
                Invoice.status.in_([InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID]),
                Invoice.balance > ZERO,
                Invoice.due_date < today,
            )
        )
        .scalars()
        .all()
    )

    for invoice in overdue:
        lease = session.get(Lease, invoice.lease_id) if invoice.lease_id else None
        grace = lease.late_fee_grace_days if lease else 0
        days_past = (today - invoice.due_date).days - grace
        if days_past < 0:
            continue  # still inside the grace period

        target = 0
        for stage, threshold, _kind, _fee in DELINQUENCY_STAGES:
            if days_past >= threshold:
                target = stage

        if target <= invoice.delinquency_stage:
            continue  # already escalated to here

        for stage, _threshold, kind, assess_fee in DELINQUENCY_STAGES:
            if stage <= invoice.delinquency_stage or stage > target:
                continue

            if assess_fee and lease and lease.late_fee_amount and lease.late_fee_amount > ZERO:
                run.late_fees.append(
                    _assess_late_fee(
                        session, invoice=invoice, lease=lease, on=today, actor_id=actor_id
                    )
                )

            run.notices.append(
                _issue_notice(
                    session,
                    invoice=invoice,
                    lease=lease,
                    kind=kind,
                    stage=stage,
                    days_past=days_past,
                    on=today,
                )
            )
            DELINQUENCY_NOTICES.labels(str(stage)).inc()

        invoice.delinquency_stage = target
        run.escalated += 1

    session.flush()
    return run


def _assess_late_fee(
    session: Session, *, invoice: Invoice, lease: Lease, on: dt.date, actor_id: str | None
) -> Invoice:
    """Raise a separate invoice for the fee.

    Separate rather than a line on the original: the rent and the fee have
    different origins, and folding them together makes a partial payment
    impossible to apply correctly.
    """
    account = account_by_code(session, invoice.org_id, AccountCode.LATE_FEE_INCOME)
    return issue_invoice(
        session,
        org_id=invoice.org_id,
        charges=[
            ChargeInput(
                description=f"Late fee - invoice {invoice.invoice_number}",
                amount=quantize_money(lease.late_fee_amount or ZERO),
                account_id=account.id,
            )
        ],
        issue_date=on,
        due_date=on,
        lease=lease,
        property_id=invoice.property_id,
        unit_id=invoice.unit_id,
        memo=f"Assessed on {invoice.invoice_number}, {(on - invoice.due_date).days} days past due",
        actor_id=actor_id,
    )


def _issue_notice(
    session: Session,
    *,
    invoice: Invoice,
    lease: Lease | None,
    kind: NoticeKind,
    stage: int,
    days_past: int,
    on: dt.date,
) -> Notice:
    respond_days = lease.notice_period_days if lease and kind == NoticeKind.PAY_OR_QUIT else 7
    notice = Notice(
        org_id=invoice.org_id,
        kind=kind,
        lease_id=invoice.lease_id,
        resident_id=invoice.resident_id,
        unit_id=invoice.unit_id,
        status="draft",
        subject=f"Overdue balance on invoice {invoice.invoice_number}",
        body=(
            f"Invoice {invoice.invoice_number} for {invoice.total} was due on "
            f"{invoice.due_date:%d %B %Y} and has an outstanding balance of "
            f"{invoice.balance}. It is now {days_past} days past due."
        ),
        effective_date=on,
        respond_by=on + dt.timedelta(days=respond_days),
    )
    session.add(notice)
    session.flush()

    record_audit_event(
        action=AuditAction.NOTICE_ISSUED,
        resource_type="Notice",
        resource_id=notice.id,
        resource_label=f"{kind} stage {stage}",
        payload={
            "invoice": invoice.invoice_number,
            "stage": stage,
            "days_past_due": days_past,
            "balance": str(invoice.balance),
        },
        severity=AuditSeverity.NOTICE,
        org_id=invoice.org_id,
        session=session,
    )
    return notice

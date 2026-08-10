"""Accounts payable: recording bills, approving them, and paying them out.

Money leaving the business is the highest-risk operation in the system, so the
controls here are structural rather than procedural.

**Entering a bill and paying it are different authorities.** ``bill.manage``
records; ``bill.approve`` authorises; ``bill.pay`` disburses. The accountant
role holds the first and neither of the others. Beyond that, the *same person*
cannot approve a bill they recorded, whatever permissions they hold - the
classic fake-vendor fraud needs one person able to do both, so the code refuses
it by identity, not by role.

**Trust money cannot pay operating bills.** Funds held on behalf of residents
and owners live in trust accounts. Paying a contractor from one is not an
accounting error, it is commingling, and in most jurisdictions it is a licensing
matter. A disbursement against a trust account is refused outright.

**The vendor's own invoice number is unique per vendor.** That constraint is
what stops the same paper invoice being entered twice by two people and paid
twice.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import (
    ApprovalRequired,
    BusinessRuleViolation,
    Conflict,
    NotFound,
    ValidationFailed,
)
from app.logging import get_logger
from app.models.accounting import (
    ZERO,
    BankAccount,
    Bill,
    BillLine,
    BillPayment,
    BillStatus,
    PaymentMethod,
)
from app.models.audit import AuditAction, AuditOutcome, AuditSeverity
from app.models.sequences import SequenceKey
from app.models.types import quantize_money, utcnow
from app.models.vendor import Vendor
from app.services.accounting.chart import AccountCode, account_by_code
from app.services.accounting.ledger import LineInput, post_journal_entry
from app.services.audit.recorder import record_audit_event
from app.services.common.numbering import next_number
from app.services.common.unit_of_work import lock_row

__all__ = [
    "BillLineInput",
    "approve_bill",
    "outstanding_payable",
    "pay_bill",
    "record_bill",
    "requires_approval",
]

log = get_logger("services.accounting.payables")

#: Organization setting naming the amount above which a second person must
#: authorise. Absent, every bill needs approval - the safe default, because a
#: missing configuration should not silently remove a control.
APPROVAL_THRESHOLD_SETTING = "ap_approval_threshold"


@dataclass(frozen=True)
class BillLineInput:
    """One coded line of a vendor invoice."""

    description: str
    amount: Decimal
    account_id: str
    property_id: str | None = None
    unit_id: str | None = None
    quantity: Decimal = Decimal("1")
    is_owner_billable: bool = True


def requires_approval(session: Session, *, org_id: str, total: Decimal) -> bool:
    """Whether this amount needs a second person.

    An unset or unparseable threshold means *everything* needs approval. A
    control that disappears when configuration is missing is not a control.
    """
    from app.models.org import Organization

    organization = session.get(Organization, org_id)
    raw = organization.setting(APPROVAL_THRESHOLD_SETTING) if organization else None
    if raw is None:
        return True
    try:
        return quantize_money(total) > quantize_money(Decimal(str(raw)))
    except (ArithmeticError, ValueError):
        log.warning(
            "unparseable approval threshold; requiring approval",
            extra={"event": "ap.threshold_invalid", "org_id": org_id, "value": str(raw)},
        )
        return True


def record_bill(
    session: Session,
    *,
    org_id: str,
    vendor_id: str,
    bill_date: dt.date,
    due_date: dt.date,
    lines: list[BillLineInput],
    vendor_invoice_number: str | None = None,
    property_id: str | None = None,
    work_order_id: str | None = None,
    memo: str | None = None,
    actor_id: str | None = None,
) -> Bill:
    """Record a vendor invoice and post its ledger impact.

    Debit each coded expense account, credit Accounts Payable.
    """
    if not lines:
        raise ValidationFailed("A bill requires at least one line.")
    if due_date < bill_date:
        raise ValidationFailed("The due date cannot precede the bill date.")

    total = quantize_money(sum((line.amount for line in lines), ZERO))
    if total <= ZERO:
        raise ValidationFailed("A bill total must be greater than zero.")

    vendor = session.get(Vendor, vendor_id)
    if vendor is None or vendor.org_id != org_id:
        raise NotFound("That vendor was not found.")

    if vendor_invoice_number:
        # Checked explicitly as well as by the unique constraint, so the caller
        # gets a useful message rather than a database error - and so the
        # duplicate is named.
        duplicate = (
            session.execute(
                select(Bill).where(
                    Bill.vendor_id == vendor_id,
                    Bill.vendor_invoice_number == vendor_invoice_number,
                )
            )
            .scalars()
            .first()
        )
        if duplicate is not None:
            raise Conflict(
                f"Invoice {vendor_invoice_number} from this vendor was already "
                f"recorded as {duplicate.bill_number}."
            )

    approval_needed = requires_approval(session, org_id=org_id, total=total)

    bill = Bill(
        org_id=org_id,
        bill_number=next_number(session, SequenceKey.BILL, org_id=org_id),
        vendor_id=vendor_id,
        vendor_invoice_number=vendor_invoice_number,
        bill_date=bill_date,
        due_date=due_date,
        status=BillStatus.PENDING_APPROVAL if approval_needed else BillStatus.APPROVED,
        property_id=property_id,
        work_order_id=work_order_id,
        subtotal=total,
        total=total,
        balance=total,
        memo=memo,
        is_1099_reportable=vendor.is_1099_reportable,
        # Set explicitly rather than left to the flush listener, which reads the
        # ambient context. Separation of duties at approval time compares against
        # this column, so an unattributed bill would silently lose the control.
        created_by_id=actor_id,
        updated_by_id=actor_id,
    )
    session.add(bill)
    session.flush()

    for index, line in enumerate(lines, start=1):
        session.add(
            BillLine(
                org_id=org_id,
                bill_id=bill.id,
                line_number=index,
                account_id=line.account_id,
                property_id=line.property_id or property_id,
                unit_id=line.unit_id,
                description=line.description,
                quantity=line.quantity,
                unit_amount=(
                    quantize_money(line.amount / line.quantity) if line.quantity else line.amount
                ),
                amount=quantize_money(line.amount),
                is_owner_billable=line.is_owner_billable,
            )
        )

    payable = account_by_code(session, org_id, AccountCode.ACCOUNTS_PAYABLE)
    entry = post_journal_entry(
        session,
        org_id=org_id,
        entry_date=bill_date,
        description=f"Bill {bill.bill_number} - {vendor.name}",
        lines=[
            LineInput(
                account_id=line.account_id,
                debit=quantize_money(line.amount),
                memo=line.description,
                property_id=line.property_id or property_id,
                unit_id=line.unit_id,
                vendor_id=vendor_id,
            )
            for line in lines
        ]
        + [
            LineInput(
                account_id=payable.id,
                credit=total,
                memo=f"Bill {bill.bill_number}",
                property_id=property_id,
                vendor_id=vendor_id,
            )
        ],
        source_type="bill",
        source_id=bill.id,
        property_id=property_id,
        system_posting=True,
        actor_id=actor_id,
    )
    bill.journal_entry_id = entry.id
    session.flush()

    record_audit_event(
        action=AuditAction.BILL_RECORDED,
        resource_type="Bill",
        resource_id=bill.id,
        resource_label=bill.bill_number,
        payload={
            "vendor_id": vendor_id,
            "total": str(total),
            "vendor_invoice_number": vendor_invoice_number,
            "approval_required": approval_needed,
        },
        org_id=org_id,
        session=session,
    )
    return bill


def approve_bill(
    session: Session, *, bill: Bill, approver_id: str, note: str | None = None
) -> Bill:
    """Authorise a bill for payment.

    Separation of duties is enforced by identity, not by role: whoever recorded
    the bill cannot approve it, however senior they are. Fake-vendor fraud needs
    one person able to do both halves.
    """
    if bill.status == BillStatus.APPROVED:
        return bill
    if bill.status != BillStatus.PENDING_APPROVAL:
        raise BusinessRuleViolation(f"A {bill.status} bill cannot be approved.")

    if bill.created_by_id == approver_id:
        raise BusinessRuleViolation(
            "A bill cannot be approved by the person who recorded it. "
            "Separation of duties requires a second approver."
        )
    if bill.created_by_id is None:
        # No human recorded it, so there is no second party to be distinct from.
        # Worth noticing: an unattributed bill is either system-generated, or a
        # sign that attribution is being lost somewhere upstream.
        log.info(
            "approving a bill with no recorded author",
            extra={"event": "ap.approval_unattributed", "bill_id": bill.id},
        )

    bill.status = BillStatus.APPROVED
    bill.approved_at = utcnow()
    bill.approved_by_id = approver_id
    # Snapshot what was actually authorised. Approving "this bill" is not the
    # same as approving whatever this bill later becomes.
    bill.approved_total = bill.total
    session.flush()

    record_audit_event(
        action=AuditAction.BILL_APPROVED,
        resource_type="Bill",
        resource_id=bill.id,
        resource_label=bill.bill_number,
        payload={"total": str(bill.total), "approver_id": approver_id},
        reason=note,
        severity=AuditSeverity.NOTICE,
        org_id=bill.org_id,
        actor_id=approver_id,
        session=session,
    )
    return bill


def pay_bill(
    session: Session,
    *,
    bill: Bill,
    bank_account_id: str,
    amount: Decimal,
    paid_date: dt.date,
    method: PaymentMethod = PaymentMethod.CHECK,
    check_number: str | None = None,
    actor_id: str | None = None,
) -> BillPayment:
    """Disburse against an approved bill.

    Debit Accounts Payable, credit the bank account's general ledger account.
    """
    amount = quantize_money(amount)
    if amount <= ZERO:
        raise ValidationFailed("A payment amount must be greater than zero.")

    if bill.status == BillStatus.VOID:
        raise BusinessRuleViolation("A void bill cannot be paid.")
    if bill.status in (BillStatus.DRAFT, BillStatus.PENDING_APPROVAL):
        raise BusinessRuleViolation(f"Bill {bill.bill_number} has not been approved for payment.")

    # The approver authorised an amount, not a row. If the bill has moved since,
    # their decision does not carry over to the new figure.
    if bill.approved_total is not None and bill.approved_total != bill.total:
        record_audit_event(
            action=AuditAction.BILL_APPROVED,
            resource_type="Bill",
            resource_id=bill.id,
            resource_label=bill.bill_number,
            outcome=AuditOutcome.DENIED,
            severity=AuditSeverity.CRITICAL,
            payload={"approved_total": str(bill.approved_total), "current_total": str(bill.total)},
            reason="The bill changed after it was approved.",
            org_id=bill.org_id,
            actor_id=actor_id,
            session=session,
        )
        raise ApprovalRequired(
            f"Bill {bill.bill_number} was approved at {bill.approved_total} but now totals "
            f"{bill.total}. It must be approved again at the new amount."
        )

    # Locked, so two concurrent disbursements cannot each see the full balance
    # and together overpay the vendor.
    locked = lock_row(session, Bill, Bill.id == bill.id) or bill
    if amount > locked.balance:
        raise BusinessRuleViolation(
            f"Payment of {amount} exceeds the {locked.balance} outstanding on "
            f"bill {locked.bill_number}."
        )

    bank = session.get(BankAccount, bank_account_id)
    if bank is None or bank.org_id != bill.org_id:
        raise NotFound("That bank account was not found.")
    if not bank.is_active:
        raise BusinessRuleViolation(f"Bank account {bank.name} is not active.")
    if bank.is_trust:
        # Commingling, not a bookkeeping preference.
        raise BusinessRuleViolation(
            f"{bank.name} is a trust account. Trust funds are held on behalf of "
            "residents and owners and cannot be used to pay operating expenses."
        )

    payable = account_by_code(session, bill.org_id, AccountCode.ACCOUNTS_PAYABLE)
    entry = post_journal_entry(
        session,
        org_id=bill.org_id,
        entry_date=paid_date,
        description=f"Payment of bill {locked.bill_number}",
        lines=[
            LineInput(
                account_id=payable.id,
                debit=amount,
                memo=f"Bill {locked.bill_number}",
                property_id=locked.property_id,
                vendor_id=locked.vendor_id,
            ),
            LineInput(
                account_id=bank.gl_account_id,
                credit=amount,
                memo=f"Bill {locked.bill_number}",
                property_id=locked.property_id,
                vendor_id=locked.vendor_id,
                bank_account_id=bank.id,
            ),
        ],
        source_type="bill_payment",
        source_id=locked.id,
        property_id=locked.property_id,
        system_posting=True,
        actor_id=actor_id,
    )

    payment = BillPayment(
        org_id=locked.org_id,
        bill_id=locked.id,
        bank_account_id=bank.id,
        amount=amount,
        paid_date=paid_date,
        method=method,
        check_number=check_number,
        journal_entry_id=entry.id,
    )
    session.add(payment)

    locked.balance = quantize_money(locked.balance - amount)
    locked.status = BillStatus.PAID if locked.balance == ZERO else BillStatus.PARTIALLY_PAID
    session.flush()

    record_audit_event(
        action=AuditAction.BILL_PAID,
        resource_type="Bill",
        resource_id=locked.id,
        resource_label=locked.bill_number,
        payload={
            "amount": str(amount),
            "method": str(method),
            "bank_account": bank.code,
            "remaining_balance": str(locked.balance),
        },
        severity=AuditSeverity.NOTICE,
        org_id=locked.org_id,
        actor_id=actor_id,
        session=session,
    )
    return payment


def outstanding_payable(session: Session, *, org_id: str, vendor_id: str | None = None) -> Decimal:
    """Total unpaid balance across open bills."""
    conditions = [
        Bill.org_id == org_id,
        Bill.status.in_(
            [BillStatus.PENDING_APPROVAL, BillStatus.APPROVED, BillStatus.PARTIALLY_PAID]
        ),
    ]
    if vendor_id:
        conditions.append(Bill.vendor_id == vendor_id)

    balances = session.execute(select(Bill.balance).where(*conditions)).scalars().all()
    return quantize_money(sum(balances, ZERO))

"""Accounts receivable: invoicing, payment capture, and application.

Every operational fact has exactly one ledger consequence, produced here rather
than by a nightly job that "syncs" the two. A resident's balance and the AR
control account move in the same transaction, so they cannot disagree.

Application order is oldest-invoice-first by default. That is the convention
residents expect, and in most jurisdictions the one a court will assume - a
payment should retire the oldest debt, not the most convenient one.

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
    Invoice,
    InvoiceLine,
    InvoiceStatus,
    Payment,
    PaymentApplication,
    PaymentMethod,
    PaymentStatus,
)
from app.models.audit import AuditAction, AuditSeverity
from app.models.leasing import Lease
from app.models.sequences import SequenceKey
from app.models.types import quantize_money, utcnow
from app.observability import PAYMENTS
from app.services.accounting.chart import AccountCode, account_by_code
from app.services.accounting.ledger import LineInput, post_journal_entry
from app.services.audit.recorder import record_audit_event
from app.services.common.numbering import next_number
from app.services.common.unit_of_work import lock_row

__all__ = [
    "ChargeInput",
    "apply_payment",
    "issue_invoice",
    "outstanding_balance",
    "record_payment",
    "void_invoice",
]

log = get_logger("services.accounting.receivables")


@dataclass(frozen=True)
class ChargeInput:
    description: str
    amount: Decimal
    account_id: str
    charge_code_id: str | None = None
    quantity: Decimal = Decimal("1")
    service_period_start: dt.date | None = None
    service_period_end: dt.date | None = None


def issue_invoice(
    session: Session,
    *,
    org_id: str,
    charges: list[ChargeInput],
    issue_date: dt.date,
    due_date: dt.date,
    lease: Lease | None = None,
    resident_id: str | None = None,
    property_id: str | None = None,
    unit_id: str | None = None,
    memo: str | None = None,
    period_start: dt.date | None = None,
    period_end: dt.date | None = None,
    actor_id: str | None = None,
) -> Invoice:
    """Raise a receivable and post its ledger impact.

    Debit Accounts Receivable, credit each charge's revenue account.
    """
    if not charges:
        raise ValidationFailed("An invoice requires at least one charge.")
    if due_date < issue_date:
        raise ValidationFailed("The due date cannot precede the issue date.")

    total = quantize_money(sum((charge.amount for charge in charges), ZERO))
    if total <= ZERO:
        raise ValidationFailed("Invoice total must be greater than zero.")

    invoice = Invoice(
        org_id=org_id,
        invoice_number=next_number(session, SequenceKey.INVOICE, org_id=org_id),
        lease_id=lease.id if lease else None,
        resident_id=resident_id,
        property_id=property_id or (lease.property_id if lease else None),
        unit_id=unit_id or (lease.unit_id if lease else None),
        issue_date=issue_date,
        due_date=due_date,
        period_start=period_start,
        period_end=period_end,
        status=InvoiceStatus.OPEN,
        subtotal=total,
        total=total,
        balance=total,
        memo=memo,
    )
    session.add(invoice)
    session.flush()

    for index, charge in enumerate(charges, start=1):
        session.add(
            InvoiceLine(
                org_id=org_id,
                invoice_id=invoice.id,
                line_number=index,
                charge_code_id=charge.charge_code_id,
                account_id=charge.account_id,
                description=charge.description,
                quantity=charge.quantity,
                unit_amount=(
                    quantize_money(charge.amount / charge.quantity)
                    if charge.quantity
                    else charge.amount
                ),
                amount=quantize_money(charge.amount),
                service_period_start=charge.service_period_start,
                service_period_end=charge.service_period_end,
            )
        )

    receivable = account_by_code(session, org_id, AccountCode.ACCOUNTS_RECEIVABLE)
    lines = [
        LineInput(
            account_id=receivable.id,
            debit=total,
            memo=f"Invoice {invoice.invoice_number}",
            property_id=invoice.property_id,
            unit_id=invoice.unit_id,
            lease_id=invoice.lease_id,
        )
    ]
    lines += [
        LineInput(
            account_id=charge.account_id,
            credit=quantize_money(charge.amount),
            memo=charge.description,
            property_id=invoice.property_id,
            unit_id=invoice.unit_id,
            lease_id=invoice.lease_id,
        )
        for charge in charges
    ]

    entry = post_journal_entry(
        session,
        org_id=org_id,
        entry_date=issue_date,
        description=f"Invoice {invoice.invoice_number}",
        lines=lines,
        source_type="invoice",
        source_id=invoice.id,
        property_id=invoice.property_id,
        system_posting=True,
        actor_id=actor_id,
    )
    invoice.journal_entry_id = entry.id
    session.flush()

    record_audit_event(
        action=AuditAction.INVOICE_ISSUED,
        resource_type="Invoice",
        resource_id=invoice.id,
        resource_label=invoice.invoice_number,
        payload={
            "total": str(total),
            "due_date": due_date.isoformat(),
            "lease_id": invoice.lease_id,
        },
        org_id=org_id,
        session=session,
    )
    return invoice


def record_payment(
    session: Session,
    *,
    org_id: str,
    amount: Decimal,
    method: PaymentMethod,
    received_date: dt.date,
    lease_id: str | None = None,
    resident_id: str | None = None,
    property_id: str | None = None,
    bank_account_id: str | None = None,
    reference: str | None = None,
    external_id: str | None = None,
    memo: str | None = None,
    allocations: list[tuple[str, Decimal]] | None = None,
    status: PaymentStatus = PaymentStatus.SETTLED,
    actor_id: str | None = None,
) -> Payment:
    """Capture money received and apply it to open invoices.

    Debit cash, credit Accounts Receivable. Any amount beyond the open balance
    stays as unapplied credit on the payment rather than being refused - a
    resident who overpays should not get an error.
    """
    amount = quantize_money(amount)
    if amount <= ZERO:
        raise ValidationFailed("Payment amount must be greater than zero.")

    if external_id:
        # Processor webhooks are at-least-once; a repeat is a no-op, not a
        # second payment.
        existing = session.execute(
            select(Payment).where(Payment.org_id == org_id, Payment.external_id == external_id)
        ).scalar_one_or_none()
        if existing is not None:
            log.info(
                "duplicate payment suppressed",
                extra={"event": "payment.duplicate_ignored", "external_id": external_id},
            )
            return existing

    payment = Payment(
        org_id=org_id,
        payment_number=next_number(session, SequenceKey.PAYMENT, org_id=org_id),
        received_date=received_date,
        amount=amount,
        unapplied_amount=amount,
        method=method,
        status=status,
        lease_id=lease_id,
        resident_id=resident_id,
        property_id=property_id,
        bank_account_id=bank_account_id,
        reference=reference,
        external_id=external_id,
        memo=memo,
        settled_at=utcnow() if status == PaymentStatus.SETTLED else None,
    )
    session.add(payment)
    session.flush()

    cash = account_by_code(session, org_id, AccountCode.CASH_OPERATING)
    receivable = account_by_code(session, org_id, AccountCode.ACCOUNTS_RECEIVABLE)

    entry = post_journal_entry(
        session,
        org_id=org_id,
        entry_date=received_date,
        description=f"Payment {payment.payment_number}",
        lines=[
            LineInput(
                account_id=cash.id,
                debit=amount,
                memo=f"Payment {payment.payment_number}",
                property_id=property_id,
                lease_id=lease_id,
                bank_account_id=bank_account_id,
            ),
            LineInput(
                account_id=receivable.id,
                credit=amount,
                memo=f"Payment {payment.payment_number}",
                property_id=property_id,
                lease_id=lease_id,
            ),
        ],
        source_type="payment",
        source_id=payment.id,
        property_id=property_id,
        system_posting=True,
        actor_id=actor_id,
    )
    payment.journal_entry_id = entry.id
    session.flush()

    apply_payment(session, payment=payment, allocations=allocations, actor_id=actor_id)

    PAYMENTS.labels(str(method), str(status)).inc()
    record_audit_event(
        action=AuditAction.PAYMENT_RECEIVED,
        resource_type="Payment",
        resource_id=payment.id,
        resource_label=payment.payment_number,
        payload={
            "amount": str(amount),
            "method": str(method),
            "unapplied": str(payment.unapplied_amount),
        },
        severity=AuditSeverity.NOTICE,
        org_id=org_id,
        session=session,
    )
    return payment


def apply_payment(
    session: Session,
    *,
    payment: Payment,
    allocations: list[tuple[str, Decimal]] | None = None,
    actor_id: str | None = None,
) -> list[PaymentApplication]:
    """Allocate a payment's unapplied balance to invoices.

    Explicit allocations are honoured exactly; otherwise open invoices are
    retired oldest-due-first.
    """
    if payment.unapplied_amount <= ZERO:
        return []

    targets: list[tuple[Invoice, Decimal]] = []

    if allocations:
        for invoice_id, requested in allocations:
            invoice = lock_row(session, Invoice, Invoice.id == invoice_id)
            if invoice is None:
                raise NotFound(f"Invoice {invoice_id} was not found.")
            if invoice.org_id != payment.org_id:
                raise NotFound(f"Invoice {invoice_id} was not found.")
            if invoice.status in (InvoiceStatus.VOID, InvoiceStatus.DRAFT):
                raise BusinessRuleViolation(
                    f"Invoice {invoice.invoice_number} cannot receive a payment while {invoice.status}."
                )
            amount = quantize_money(requested)
            if amount > invoice.balance:
                raise BusinessRuleViolation(
                    f"Allocation of {amount} exceeds the {invoice.balance} balance on "
                    f"invoice {invoice.invoice_number}."
                )
            targets.append((invoice, amount))

        total_requested = sum((amount for _, amount in targets), ZERO)
        if total_requested > payment.unapplied_amount:
            raise BusinessRuleViolation(
                f"Allocations total {total_requested} but only {payment.unapplied_amount} is unapplied."
            )
    else:
        open_invoices = (
            session.execute(
                select(Invoice)
                .where(
                    Invoice.org_id == payment.org_id,
                    Invoice.status.in_([InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID]),
                    Invoice.balance > ZERO,
                    *([Invoice.lease_id == payment.lease_id] if payment.lease_id else []),
                    *(
                        [Invoice.resident_id == payment.resident_id]
                        if payment.resident_id and not payment.lease_id
                        else []
                    ),
                )
                .order_by(Invoice.due_date.asc(), Invoice.created_at.asc())
            )
            .scalars()
            .all()
        )

        remaining = payment.unapplied_amount
        for invoice in open_invoices:
            if remaining <= ZERO:
                break
            amount = min(remaining, invoice.balance)
            targets.append((invoice, amount))
            remaining -= amount

    created: list[PaymentApplication] = []
    for invoice, amount in targets:
        if amount <= ZERO:
            continue
        application = PaymentApplication(
            org_id=payment.org_id,
            payment_id=payment.id,
            invoice_id=invoice.id,
            amount=amount,
            applied_by_id=actor_id,
        )
        session.add(application)
        created.append(application)

        invoice.balance = quantize_money(invoice.balance - amount)
        invoice.status = (
            InvoiceStatus.PAID if invoice.balance == ZERO else InvoiceStatus.PARTIALLY_PAID
        )
        payment.unapplied_amount = quantize_money(payment.unapplied_amount - amount)

    session.flush()

    if created:
        record_audit_event(
            action=AuditAction.PAYMENT_APPLIED,
            resource_type="Payment",
            resource_id=payment.id,
            resource_label=payment.payment_number,
            payload={
                "applications": [
                    {"invoice_id": app.invoice_id, "amount": str(app.amount)} for app in created
                ],
                "remaining_unapplied": str(payment.unapplied_amount),
            },
            org_id=payment.org_id,
            session=session,
        )
    return created


def void_invoice(
    session: Session, *, invoice: Invoice, reason: str, actor_id: str | None = None
) -> Invoice:
    """Void an unpaid invoice by reversing its ledger entry."""
    if invoice.status == InvoiceStatus.VOID:
        raise BusinessRuleViolation("This invoice is already void.")
    if invoice.balance != invoice.total:
        raise BusinessRuleViolation(
            "An invoice with payments applied cannot be voided; reverse the payments first."
        )
    if not reason or len(reason.strip()) < 5:
        raise ValidationFailed("Voiding an invoice requires a reason.")

    from app.models.accounting import JournalEntry
    from app.services.accounting.ledger import reverse_journal_entry

    if invoice.journal_entry_id:
        entry = session.get(JournalEntry, invoice.journal_entry_id)
        if entry is not None and entry.reversed_by_id is None:
            reverse_journal_entry(session, entry=entry, reason=reason, actor_id=actor_id)

    invoice.status = InvoiceStatus.VOID
    invoice.balance = ZERO
    invoice.voided_at = utcnow()
    invoice.void_reason = reason
    session.flush()

    record_audit_event(
        action=AuditAction.INVOICE_VOIDED,
        resource_type="Invoice",
        resource_id=invoice.id,
        resource_label=invoice.invoice_number,
        reason=reason,
        severity=AuditSeverity.WARNING,
        org_id=invoice.org_id,
        session=session,
    )
    return invoice


def outstanding_balance(
    session: Session, *, org_id: str, lease_id: str | None = None, resident_id: str | None = None
) -> Decimal:
    """Total open receivable for a lease or resident."""
    conditions = [
        Invoice.org_id == org_id,
        Invoice.status.in_([InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID]),
    ]
    if lease_id:
        conditions.append(Invoice.lease_id == lease_id)
    if resident_id:
        conditions.append(Invoice.resident_id == resident_id)

    balances = session.execute(select(Invoice.balance).where(*conditions)).scalars().all()
    return quantize_money(sum(balances, ZERO))

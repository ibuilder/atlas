"""Invoicing and payment application.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.errors import BusinessRuleViolation
from app.models.accounting import InvoiceStatus, PaymentMethod
from app.services.accounting.chart import AccountCode
from app.services.accounting.ledger import trial_balance
from app.services.accounting.receivables import (
    ChargeInput,
    issue_invoice,
    outstanding_balance,
    record_payment,
    void_invoice,
)

pytestmark = pytest.mark.integration


def _invoice(db, org, accounts, lease, amount="1000.00", days_ago=0):
    """Raise an invoice issued and due ``days_ago`` days back.

    Issue and due dates move together: a due date before its issue date is
    rejected by the service, as it should be.
    """
    issued = dt.date.today() - dt.timedelta(days=days_ago)
    return issue_invoice(
        db.session,
        org_id=org.id,
        charges=[
            ChargeInput(
                description="Rent",
                amount=Decimal(amount),
                account_id=accounts[AccountCode.RENTAL_INCOME].id,
            )
        ],
        issue_date=issued,
        due_date=issued,
        lease=lease,
        property_id=lease.property_id,
    )


def test_issuing_an_invoice_posts_to_the_ledger(db, org, scope, accounts, lease_record):
    invoice = _invoice(db, org, accounts, lease_record, "1200.00")
    db.session.commit()

    assert invoice.status == InvoiceStatus.OPEN
    assert invoice.total == invoice.balance == Decimal("1200.0000")
    assert invoice.journal_entry_id is not None

    rows = {row["code"]: row for row in trial_balance(db.session, org_id=org.id)}
    assert rows[AccountCode.ACCOUNTS_RECEIVABLE]["debit"] == Decimal("1200.0000")
    assert rows[AccountCode.RENTAL_INCOME]["credit"] == Decimal("1200.0000")


def test_payment_clears_the_balance_and_the_books_stay_balanced(
    db, org, scope, accounts, lease_record
):
    invoice = _invoice(db, org, accounts, lease_record, "800.00")
    db.session.commit()

    record_payment(
        db.session,
        org_id=org.id,
        amount=Decimal("800.00"),
        method=PaymentMethod.ACH,
        received_date=dt.date.today(),
        lease_id=lease_record.id,
    )
    db.session.commit()

    assert invoice.status == InvoiceStatus.PAID
    assert invoice.balance == Decimal("0.0000")

    rows = trial_balance(db.session, org_id=org.id)
    assert sum(row["debit"] for row in rows) == sum(row["credit"] for row in rows)


def test_partial_payment_leaves_a_remainder(db, org, scope, accounts, lease_record):
    invoice = _invoice(db, org, accounts, lease_record, "1000.00")
    db.session.commit()

    record_payment(
        db.session,
        org_id=org.id,
        amount=Decimal("400.00"),
        method=PaymentMethod.CARD,
        received_date=dt.date.today(),
        lease_id=lease_record.id,
    )
    db.session.commit()

    assert invoice.status == InvoiceStatus.PARTIALLY_PAID
    assert invoice.balance == Decimal("600.0000")


def test_payments_apply_oldest_due_first(db, org, scope, accounts, lease_record):
    old = _invoice(db, org, accounts, lease_record, "300.00", days_ago=30)
    new = _invoice(db, org, accounts, lease_record, "300.00", days_ago=0)
    db.session.commit()

    record_payment(
        db.session,
        org_id=org.id,
        amount=Decimal("300.00"),
        method=PaymentMethod.CHECK,
        received_date=dt.date.today(),
        lease_id=lease_record.id,
    )
    db.session.commit()

    assert old.balance == Decimal("0.0000")
    assert new.balance == Decimal("300.0000")


def test_overpayment_is_retained_as_unapplied_credit(db, org, scope, accounts, lease_record):
    """A resident who pays too much gets a credit, not an error."""
    _invoice(db, org, accounts, lease_record, "500.00")
    db.session.commit()

    payment = record_payment(
        db.session,
        org_id=org.id,
        amount=Decimal("750.00"),
        method=PaymentMethod.ACH,
        received_date=dt.date.today(),
        lease_id=lease_record.id,
    )
    db.session.commit()

    assert payment.unapplied_amount == Decimal("250.0000")
    assert payment.applied_amount == Decimal("500.0000")


def test_duplicate_external_payment_is_suppressed(db, org, scope, accounts, lease_record):
    """Processor webhooks are at-least-once; a repeat must not double-charge."""
    _invoice(db, org, accounts, lease_record, "200.00")
    db.session.commit()

    first = record_payment(
        db.session,
        org_id=org.id,
        amount=Decimal("200.00"),
        method=PaymentMethod.ACH,
        received_date=dt.date.today(),
        lease_id=lease_record.id,
        external_id="evt_abc123",
    )
    db.session.commit()

    second = record_payment(
        db.session,
        org_id=org.id,
        amount=Decimal("200.00"),
        method=PaymentMethod.ACH,
        received_date=dt.date.today(),
        lease_id=lease_record.id,
        external_id="evt_abc123",
    )
    db.session.commit()

    assert first.id == second.id
    assert outstanding_balance(db.session, org_id=org.id, lease_id=lease_record.id) == Decimal(
        "0.0000"
    )


def test_explicit_allocation_beyond_balance_is_refused(db, org, scope, accounts, lease_record):
    invoice = _invoice(db, org, accounts, lease_record, "100.00")
    db.session.commit()

    with pytest.raises(BusinessRuleViolation, match="exceeds"):
        record_payment(
            db.session,
            org_id=org.id,
            amount=Decimal("500.00"),
            method=PaymentMethod.CASH,
            received_date=dt.date.today(),
            lease_id=lease_record.id,
            allocations=[(invoice.id, Decimal("500.00"))],
        )


def test_paid_invoice_cannot_be_voided(db, org, scope, accounts, lease_record):
    invoice = _invoice(db, org, accounts, lease_record, "150.00")
    db.session.commit()
    record_payment(
        db.session,
        org_id=org.id,
        amount=Decimal("150.00"),
        method=PaymentMethod.CASH,
        received_date=dt.date.today(),
        lease_id=lease_record.id,
    )
    db.session.commit()

    with pytest.raises(BusinessRuleViolation, match="payments applied"):
        void_invoice(db.session, invoice=invoice, reason="Raised in error")


def test_voiding_an_invoice_reverses_its_ledger_impact(db, org, scope, accounts, lease_record):
    invoice = _invoice(db, org, accounts, lease_record, "425.00")
    db.session.commit()

    void_invoice(db.session, invoice=invoice, reason="Duplicate of INV-000001")
    db.session.commit()

    assert invoice.status == InvoiceStatus.VOID
    assert invoice.balance == Decimal("0.0000")

    rows = trial_balance(db.session, org_id=org.id)
    receivable = next(row for row in rows if row["code"] == AccountCode.ACCOUNTS_RECEIVABLE)
    assert receivable["balance"] == Decimal("0.0000")


def test_outstanding_balance_sums_open_invoices_only(db, org, scope, accounts, lease_record):
    _invoice(db, org, accounts, lease_record, "100.00")
    _invoice(db, org, accounts, lease_record, "250.50")
    db.session.commit()

    assert outstanding_balance(db.session, org_id=org.id, lease_id=lease_record.id) == Decimal(
        "350.5000"
    )

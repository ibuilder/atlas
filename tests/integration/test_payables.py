"""Accounts payable: recording, approval, and disbursement.

Money leaving the business, so the controls are the point: separation of duties,
trust-account protection, duplicate-invoice prevention, and a ledger that stays
balanced through all of it.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.errors import BusinessRuleViolation, Conflict, ValidationFailed
from app.models.accounting import BankAccount, BankAccountType, BillStatus, PaymentMethod
from app.services.accounting.chart import AccountCode
from app.services.accounting.ledger import trial_balance
from app.services.accounting.payables import (
    BillLineInput,
    approve_bill,
    outstanding_payable,
    pay_bill,
    record_bill,
)

pytestmark = pytest.mark.integration

RECORDER = "019fea00-0000-7000-8000-0000000000a1"
APPROVER = "019fea00-0000-7000-8000-0000000000a2"


@pytest.fixture()
def operating_account(db, org, scope, accounts):
    record = BankAccount(
        org_id=org.id,
        code="OPER",
        name="Operating",
        account_type=BankAccountType.OPERATING,
        gl_account_id=accounts[AccountCode.CASH_OPERATING].id,
        is_trust=False,
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture()
def trust_account(db, org, scope, accounts):
    record = BankAccount(
        org_id=org.id,
        code="TRUST",
        name="Security Deposit Trust",
        account_type=BankAccountType.TRUST,
        gl_account_id=accounts[AccountCode.CASH_TRUST].id,
        is_trust=True,
    )
    db.session.add(record)
    db.session.commit()
    return record


def _bill(db, org, accounts, vendor_record, amount="500.00", invoice=None, actor=RECORDER):
    today = dt.date.today()
    return record_bill(
        db.session,
        org_id=org.id,
        vendor_id=vendor_record.id,
        bill_date=today,
        due_date=today + dt.timedelta(days=30),
        lines=[
            BillLineInput(
                description="Boiler repair",
                amount=Decimal(amount),
                account_id=accounts[AccountCode.REPAIRS_MAINTENANCE].id,
            )
        ],
        vendor_invoice_number=invoice,
        actor_id=actor,
    )


def _set_threshold(db, org, value):
    org.settings = {**(org.settings or {}), "ap_approval_threshold": value}
    db.session.commit()


# ------------------------------------------------------------------ recording


def test_recording_a_bill_posts_to_the_ledger(db, org, scope, accounts, vendor_record):
    bill = _bill(db, org, accounts, vendor_record, "750.00")
    db.session.commit()

    assert bill.bill_number.startswith("BILL-")
    assert bill.total == bill.balance == Decimal("750.0000")
    assert bill.journal_entry_id is not None

    rows = {row["code"]: row for row in trial_balance(db.session, org_id=org.id)}
    assert rows[AccountCode.REPAIRS_MAINTENANCE]["debit"] == Decimal("750.0000")
    assert rows[AccountCode.ACCOUNTS_PAYABLE]["credit"] == Decimal("750.0000")


def test_duplicate_vendor_invoice_is_refused(db, org, scope, accounts, vendor_record):
    """The control that stops one paper invoice being paid twice."""
    _bill(db, org, accounts, vendor_record, "100.00", invoice="INV-9001")
    db.session.commit()

    with pytest.raises(Conflict, match="already"):
        _bill(db, org, accounts, vendor_record, "100.00", invoice="INV-9001")


def test_same_invoice_number_from_a_different_vendor_is_fine(
    db, org, scope, accounts, vendor_record
):
    """Vendors number their own invoices; collisions across them mean nothing."""
    from app.models.vendor import ComplianceStatus, Vendor, VendorStatus

    other = Vendor(
        org_id=org.id,
        code="VND2",
        name="Second Trades",
        status=VendorStatus.ACTIVE,
        compliance_status=ComplianceStatus.VALID,
    )
    db.session.add(other)
    db.session.commit()

    _bill(db, org, accounts, vendor_record, "100.00", invoice="0001")
    today = dt.date.today()
    record_bill(
        db.session,
        org_id=org.id,
        vendor_id=other.id,
        bill_date=today,
        due_date=today,
        lines=[
            BillLineInput(
                description="Cleaning",
                amount=Decimal("60.00"),
                account_id=accounts[AccountCode.REPAIRS_MAINTENANCE].id,
            )
        ],
        vendor_invoice_number="0001",
    )
    db.session.commit()

    assert outstanding_payable(db.session, org_id=org.id) == Decimal("160.0000")


def test_zero_and_negative_bills_are_refused(db, org, scope, accounts, vendor_record):
    with pytest.raises(ValidationFailed):
        _bill(db, org, accounts, vendor_record, "0.00")


def test_due_date_before_bill_date_is_refused(db, org, scope, accounts, vendor_record):
    today = dt.date.today()
    with pytest.raises(ValidationFailed, match="due date"):
        record_bill(
            db.session,
            org_id=org.id,
            vendor_id=vendor_record.id,
            bill_date=today,
            due_date=today - dt.timedelta(days=1),
            lines=[
                BillLineInput(
                    description="x",
                    amount=Decimal("10"),
                    account_id=accounts[AccountCode.REPAIRS_MAINTENANCE].id,
                )
            ],
        )


# ------------------------------------------------------------------ approval


def test_missing_threshold_requires_approval(db, org, scope, accounts, vendor_record):
    """A control that vanishes when configuration is absent is not a control."""
    bill = _bill(db, org, accounts, vendor_record, "1.00")
    db.session.commit()
    assert bill.status == BillStatus.PENDING_APPROVAL


def test_bills_below_the_threshold_skip_approval(db, org, scope, accounts, vendor_record):
    _set_threshold(db, org, "1000.00")
    bill = _bill(db, org, accounts, vendor_record, "250.00")
    db.session.commit()
    assert bill.status == BillStatus.APPROVED


def test_bills_above_the_threshold_need_approval(db, org, scope, accounts, vendor_record):
    _set_threshold(db, org, "1000.00")
    bill = _bill(db, org, accounts, vendor_record, "1000.01")
    db.session.commit()
    assert bill.status == BillStatus.PENDING_APPROVAL


def test_recorder_cannot_approve_their_own_bill(db, org, scope, accounts, vendor_record):
    """Fake-vendor fraud needs one person able to do both halves."""
    bill = _bill(db, org, accounts, vendor_record, "5000.00", actor=RECORDER)
    db.session.commit()

    with pytest.raises(BusinessRuleViolation, match="(?i)separation of duties"):
        approve_bill(db.session, bill=bill, approver_id=RECORDER)


def test_a_second_person_can_approve(db, org, scope, accounts, vendor_record):
    bill = _bill(db, org, accounts, vendor_record, "5000.00", actor=RECORDER)
    db.session.commit()

    approve_bill(db.session, bill=bill, approver_id=APPROVER, note="Checked against the quote")
    db.session.commit()

    assert bill.status == BillStatus.APPROVED
    assert bill.approved_by_id == APPROVER
    assert bill.approved_at is not None


def test_approval_is_audited(db, org, scope, accounts, vendor_record):
    from app.models.audit import AuditEvent

    bill = _bill(db, org, accounts, vendor_record, "5000.00")
    db.session.commit()
    approve_bill(db.session, bill=bill, approver_id=APPROVER)
    db.session.commit()

    approvals = db.session.query(AuditEvent).filter(AuditEvent.action == "ap.bill_approved").count()
    assert approvals == 1


# --------------------------------------------------------------- disbursement


def test_unapproved_bills_cannot_be_paid(
    db, org, scope, accounts, vendor_record, operating_account
):
    bill = _bill(db, org, accounts, vendor_record, "800.00")
    db.session.commit()

    with pytest.raises(BusinessRuleViolation, match="not been approved"):
        pay_bill(
            db.session,
            bill=bill,
            bank_account_id=operating_account.id,
            amount=Decimal("800.00"),
            paid_date=dt.date.today(),
        )


def test_payment_clears_the_bill_and_the_books_stay_balanced(
    db, org, scope, accounts, vendor_record, operating_account
):
    bill = _bill(db, org, accounts, vendor_record, "800.00")
    approve_bill(db.session, bill=bill, approver_id=APPROVER)
    db.session.commit()

    pay_bill(
        db.session,
        bill=bill,
        bank_account_id=operating_account.id,
        amount=Decimal("800.00"),
        paid_date=dt.date.today(),
        method=PaymentMethod.ACH,
    )
    db.session.commit()

    assert bill.status == BillStatus.PAID
    assert bill.balance == Decimal("0.0000")

    rows = trial_balance(db.session, org_id=org.id)
    assert sum(r["debit"] for r in rows) == sum(r["credit"] for r in rows)
    payable = next(r for r in rows if r["code"] == AccountCode.ACCOUNTS_PAYABLE)
    assert payable["balance"] == Decimal("0.0000")


def test_partial_payment_leaves_a_balance(
    db, org, scope, accounts, vendor_record, operating_account
):
    bill = _bill(db, org, accounts, vendor_record, "1000.00")
    approve_bill(db.session, bill=bill, approver_id=APPROVER)
    db.session.commit()

    pay_bill(
        db.session,
        bill=bill,
        bank_account_id=operating_account.id,
        amount=Decimal("400.00"),
        paid_date=dt.date.today(),
    )
    db.session.commit()

    assert bill.status == BillStatus.PARTIALLY_PAID
    assert bill.balance == Decimal("600.0000")


def test_overpayment_is_refused(db, org, scope, accounts, vendor_record, operating_account):
    bill = _bill(db, org, accounts, vendor_record, "100.00")
    approve_bill(db.session, bill=bill, approver_id=APPROVER)
    db.session.commit()

    with pytest.raises(BusinessRuleViolation, match="exceeds"):
        pay_bill(
            db.session,
            bill=bill,
            bank_account_id=operating_account.id,
            amount=Decimal("150.00"),
            paid_date=dt.date.today(),
        )


def test_trust_accounts_cannot_pay_operating_bills(
    db, org, scope, accounts, vendor_record, trust_account
):
    """Commingling, not a bookkeeping preference.

    Trust funds belong to residents and owners. Paying a contractor from one is
    a licensing matter in most jurisdictions.
    """
    bill = _bill(db, org, accounts, vendor_record, "300.00")
    approve_bill(db.session, bill=bill, approver_id=APPROVER)
    db.session.commit()

    with pytest.raises(BusinessRuleViolation, match="trust account"):
        pay_bill(
            db.session,
            bill=bill,
            bank_account_id=trust_account.id,
            amount=Decimal("300.00"),
            paid_date=dt.date.today(),
        )


def test_inactive_bank_accounts_are_refused(
    db, org, scope, accounts, vendor_record, operating_account
):
    operating_account.is_active = False
    bill = _bill(db, org, accounts, vendor_record, "50.00")
    approve_bill(db.session, bill=bill, approver_id=APPROVER)
    db.session.commit()

    with pytest.raises(BusinessRuleViolation, match="not active"):
        pay_bill(
            db.session,
            bill=bill,
            bank_account_id=operating_account.id,
            amount=Decimal("50.00"),
            paid_date=dt.date.today(),
        )


def test_a_bank_account_from_another_tenant_is_not_found(
    db, org, other_org, scope, accounts, vendor_record
):
    from app.context import RequestContext, bind_context, clear_context, new_correlation_id
    from app.errors import NotFound
    from app.services.accounting.chart import seed_chart_of_accounts

    token = bind_context(
        RequestContext(correlation_id=new_correlation_id(), org_id=other_org.id, source="test")
    )
    try:
        foreign_chart = seed_chart_of_accounts(db.session, other_org.id)
        foreign = BankAccount(
            org_id=other_org.id,
            code="OTHER",
            name="Rival Operating",
            account_type=BankAccountType.OPERATING,
            gl_account_id=foreign_chart[AccountCode.CASH_OPERATING].id,
        )
        db.session.add(foreign)
        db.session.commit()
        foreign_id = foreign.id
    finally:
        clear_context(token)

    bill = _bill(db, org, accounts, vendor_record, "75.00")
    approve_bill(db.session, bill=bill, approver_id=APPROVER)
    db.session.commit()

    with pytest.raises(NotFound):
        pay_bill(
            db.session,
            bill=bill,
            bank_account_id=foreign_id,
            amount=Decimal("75.00"),
            paid_date=dt.date.today(),
        )


def test_outstanding_payable_counts_only_open_bills(
    db, org, scope, accounts, vendor_record, operating_account
):
    first = _bill(db, org, accounts, vendor_record, "200.00", invoice="A")
    second = _bill(db, org, accounts, vendor_record, "300.00", invoice="B")
    approve_bill(db.session, bill=first, approver_id=APPROVER)
    approve_bill(db.session, bill=second, approver_id=APPROVER)
    db.session.commit()

    assert outstanding_payable(db.session, org_id=org.id) == Decimal("500.0000")

    pay_bill(
        db.session,
        bill=first,
        bank_account_id=operating_account.id,
        amount=Decimal("200.00"),
        paid_date=dt.date.today(),
    )
    db.session.commit()

    assert outstanding_payable(db.session, org_id=org.id) == Decimal("300.0000")


def test_a_bill_edited_after_approval_cannot_be_paid(
    db, org, scope, accounts, vendor_record, operating_account
):
    """The approver authorised $4,200. Nobody authorised $42,000."""
    from app.errors import ApprovalRequired

    _set_threshold(db, org, "1000.00")
    bill = _bill(db, org, accounts, vendor_record, "4200.00")
    approve_bill(db.session, bill=bill, approver_id=APPROVER)
    db.session.commit()
    assert bill.approved_total == Decimal("4200.0000")

    bill.total = Decimal("42000.00")
    bill.balance = Decimal("42000.00")
    db.session.commit()

    with pytest.raises(ApprovalRequired):
        pay_bill(
            db.session,
            bill=bill,
            bank_account_id=operating_account.id,
            amount=Decimal("42000.00"),
            paid_date=dt.date.today(),
        )


def test_an_unchanged_bill_still_pays(db, org, scope, accounts, vendor_record, operating_account):
    _set_threshold(db, org, "1000.00")
    bill = _bill(db, org, accounts, vendor_record, "4200.00")
    approve_bill(db.session, bill=bill, approver_id=APPROVER)
    db.session.commit()

    payment = pay_bill(
        db.session,
        bill=bill,
        bank_account_id=operating_account.id,
        amount=Decimal("4200.00"),
        paid_date=dt.date.today(),
    )
    db.session.commit()
    assert payment.amount == Decimal("4200.0000")

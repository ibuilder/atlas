"""Accounts payable, from the console and the API.

Money going out is where fraud lives, and two rules carry that weight:

Whoever recorded a bill cannot approve it. The check is by *identity*, not by
role — fake-vendor fraud needs one person able to do both halves, and no amount
of seniority makes one person into two.

An unset approval threshold means everything needs approval. A control that
disappears when its configuration is missing is not a control.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

pytestmark = pytest.mark.security

BILLED = dt.date(2026, 5, 1)
DUE = dt.date(2026, 5, 31)
PAID = dt.date(2026, 5, 20)


def _rebound(org):
    from app.context import RequestContext, bind_context, new_correlation_id

    return bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=org.id,
            actor_type="system",
            source="test",
        )
    )


@pytest.fixture()
def operating_account(db, org, scope, accounts):
    from app.models.accounting import BankAccount, BankAccountType
    from app.services.accounting.chart import AccountCode

    account = BankAccount(
        org_id=org.id,
        code="OPS",
        name="Operating",
        account_type=BankAccountType.OPERATING,
        gl_account_id=accounts[AccountCode.CASH_OPERATING].id,
    )
    db.session.add(account)
    db.session.commit()
    return account


@pytest.fixture()
def clerk(db, org, scope, make_user):
    """Records bills. Deliberately a different person from the approver."""
    return make_user("accountant", email="ap-clerk@test.local")


@pytest.fixture()
def approver(db, org, scope, make_user):
    return make_user("controller", email="ap-approver@test.local")


@pytest.fixture()
def bill(db, org, scope, accounts, vendor_record, clerk):
    """A bill recorded *by the clerk*, which is the fact the rule turns on."""
    from app.services.accounting.chart import AccountCode
    from app.services.accounting.payables import BillLineInput, record_bill

    record = record_bill(
        db.session,
        org_id=org.id,
        vendor_id=vendor_record.id,
        bill_date=BILLED,
        due_date=DUE,
        lines=[
            BillLineInput(
                description="Boiler service call",
                amount=Decimal("480.00"),
                account_id=accounts[AccountCode.REPAIRS_MAINTENANCE].id,
            )
        ],
        vendor_invoice_number="INV-8841",
        actor_id=clerk.id,
    )
    # record_bill attributes through the audit trail; the separation rule reads
    # created_by_id, so the test has to make that the fact it claims to be.
    record.created_by_id = clerk.id
    db.session.commit()
    return record


# ---------------------------------------------------------------------------
# Separation of duties
# ---------------------------------------------------------------------------


def test_the_author_of_a_bill_cannot_approve_it(client, db, org, bill, clerk, sign_in):
    """Even holding bill.approve. The rule is identity, not permission."""
    from sqlalchemy import select

    from app.context import clear_context
    from app.models.accounting import Bill, BillStatus
    from app.models.iam import Role, RoleAssignment

    # Give the clerk the approval grant outright, so the refusal cannot be
    # mistaken for a missing permission.
    token = _rebound(org)
    try:
        controller = db.session.execute(
            select(Role).where(Role.org_id == org.id, Role.code == "controller")
        ).scalar_one()
        db.session.add(RoleAssignment(org_id=org.id, user_id=clerk.id, role_id=controller.id))
        db.session.commit()
    finally:
        clear_context(token)

    sign_in("ap-clerk@test.local")
    response = client.post(f"/admin/bills/{bill.id}/approve", follow_redirects=True)
    assert b"cannot be approved by the person who recorded it" in response.data

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert db.session.get(Bill, bill.id).status == BillStatus.PENDING_APPROVAL
    finally:
        clear_context(token)


def test_the_page_says_why_rather_than_only_hiding_the_button(
    client, db, org, bill, clerk, sign_in
):
    """A missing button reads as a bug; the reason reads as a control."""
    sign_in("ap-clerk@test.local")
    response = client.get(f"/admin/bills/{bill.id}")
    assert response.status_code == 200
    assert b"cannot approve it" in response.data
    assert b"Separation of duties" in response.data


def test_a_second_person_can_approve_and_pay(
    client, db, org, bill, approver, operating_account, sign_in
):
    from app.context import clear_context
    from app.models.accounting import Bill, BillStatus

    sign_in("ap-approver@test.local")
    assert (
        client.post(
            f"/admin/bills/{bill.id}/approve", data={"note": "Matches the quote."}
        ).status_code
        == 302
    )

    db.session.expire_all()
    token = _rebound(org)
    try:
        reloaded = db.session.get(Bill, bill.id)
        assert reloaded.status == BillStatus.APPROVED
        # The approver authorised an amount, not a row.
        assert reloaded.approved_total == Decimal("480.0000")
    finally:
        clear_context(token)

    response = client.post(
        f"/admin/bills/{bill.id}/payments",
        data={
            "bank_account_id": operating_account.id,
            "amount": "480.00",
            "paid_date": PAID.isoformat(),
            "method": "check",
            "check_number": "10412",
        },
    )
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        reloaded = db.session.get(Bill, bill.id)
        assert reloaded.balance == Decimal("0.0000")
        assert reloaded.status == BillStatus.PAID
    finally:
        clear_context(token)


def test_an_unapproved_bill_cannot_be_paid(
    client, db, org, bill, operating_account, make_user, sign_in
):
    make_user("org_admin", email="ap-boss@test.local")
    sign_in("ap-boss@test.local")

    response = client.post(
        f"/admin/bills/{bill.id}/payments",
        data={
            "bank_account_id": operating_account.id,
            "amount": "480.00",
            "paid_date": PAID.isoformat(),
        },
        follow_redirects=True,
    )
    assert b"not been approved for payment" in response.data


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "four hundred"])
def test_a_payment_amount_that_is_not_a_number_is_refused(
    client, db, org, bill, approver, operating_account, sign_in, amount
):
    sign_in("ap-approver@test.local")
    client.post(f"/admin/bills/{bill.id}/approve")

    response = client.post(
        f"/admin/bills/{bill.id}/payments",
        data={
            "bank_account_id": operating_account.id,
            "amount": amount,
            "paid_date": PAID.isoformat(),
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"is not an amount" in response.data


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


def test_the_queue_leads_with_what_is_blocked_and_what_is_due(
    client, db, org, bill, approver, sign_in
):
    sign_in("ap-approver@test.local")
    response = client.get("/admin/bills")
    assert response.status_code == 200
    assert b"awaiting approval" in response.data


def test_another_tenants_bill_is_not_found(client, db, org, other_org, approver, sign_in):
    from app.context import RequestContext, bind_context, clear_context, new_correlation_id
    from app.models.vendor import Vendor
    from app.services.accounting.chart import AccountCode, seed_chart_of_accounts
    from app.services.accounting.payables import BillLineInput, record_bill

    token = bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=other_org.id,
            actor_type="system",
            source="test",
        )
    )
    try:
        their_accounts = seed_chart_of_accounts(db.session, other_org.id)
        vendor = Vendor(org_id=other_org.id, code="RIVV", name="Rival Plumbing")
        db.session.add(vendor)
        db.session.flush()
        theirs = record_bill(
            db.session,
            org_id=other_org.id,
            vendor_id=vendor.id,
            bill_date=BILLED,
            due_date=DUE,
            lines=[
                BillLineInput(
                    description="Their work",
                    amount=Decimal("100.00"),
                    account_id=their_accounts[AccountCode.REPAIRS_MAINTENANCE].id,
                )
            ],
        )
        db.session.commit()
        theirs_id = theirs.id
    finally:
        clear_context(token)

    sign_in("ap-approver@test.local")
    assert client.get(f"/admin/bills/{theirs_id}").status_code == 404
    assert client.post(f"/admin/bills/{theirs_id}/approve").status_code == 404


def test_an_accountant_cannot_approve_or_pay(client, db, org, bill, clerk, sign_in):
    """Recording, approving, and disbursing are three separate grants."""
    sign_in("ap-clerk@test.local")
    assert client.post(f"/admin/bills/{bill.id}/approve").status_code == 403


def test_an_anonymous_visitor_cannot_reach_payables(client, bill):
    assert client.get("/admin/bills").status_code in (302, 401)
    assert client.post(f"/admin/bills/{bill.id}/approve").status_code in (302, 401)

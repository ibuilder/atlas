"""Accounts payable over the API.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

pytestmark = pytest.mark.integration

BILLED = dt.date(2026, 5, 1)
DUE = dt.date(2026, 5, 31)
PAID = dt.date(2026, 5, 20)


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
def expense_account(db, org, scope, accounts):
    from app.services.accounting.chart import AccountCode

    return accounts[AccountCode.REPAIRS_MAINTENANCE]


@pytest.fixture()
def clerk(db, org, scope, make_user, sign_in):
    make_user("accountant", email="api-clerk@test.local")
    sign_in("api-clerk@test.local")
    return "api-clerk@test.local"


def _bill_payload(vendor_id: str, account_id: str, amount: str = "480.00") -> dict:
    return {
        "vendor_id": vendor_id,
        "bill_date": BILLED.isoformat(),
        "due_date": DUE.isoformat(),
        "vendor_invoice_number": "INV-8841",
        "lines": [
            {
                "description": "Boiler service call",
                "amount": amount,
                "account_id": account_id,
            }
        ],
    }


def test_a_bill_is_recorded_with_its_lines(client, db, org, vendor_record, expense_account, clerk):
    response = client.post(
        "/api/v1/bills", json=_bill_payload(vendor_record.id, expense_account.id)
    )
    assert response.status_code == 201, response.get_json()
    bill_id = response.get_json()["id"]

    body = client.get(f"/api/v1/bills/{bill_id}").get_json()
    assert len(body["lines"]) == 1
    assert Decimal(body["total"]) == Decimal("480.00")
    assert body["payments"] == []
    # An unset threshold means everything needs a second person.
    assert body["requires_approval"] is True


def test_the_author_cannot_approve_their_own_bill(
    client, db, org, vendor_record, expense_account, clerk, make_user, sign_in
):
    """The clerk is given the approval grant, so the refusal is the rule."""
    from sqlalchemy import select

    bill_id = client.post(
        "/api/v1/bills", json=_bill_payload(vendor_record.id, expense_account.id)
    ).get_json()["id"]

    from app.context import RequestContext, bind_context, clear_context, new_correlation_id
    from app.models.iam import Role, RoleAssignment, User

    # Signing in leaves no tenant context bound, so arranging state after a
    # request needs its own scope.
    token = bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=org.id,
            actor_type="system",
            source="test",
        )
    )
    try:
        controller = db.session.execute(
            select(Role).where(Role.org_id == org.id, Role.code == "controller")
        ).scalar_one()
        user = db.session.execute(
            select(User).where(User.email == "api-clerk@test.local")
        ).scalar_one()
        db.session.add(RoleAssignment(org_id=org.id, user_id=user.id, role_id=controller.id))
        db.session.commit()
    finally:
        clear_context(token)

    sign_in("api-clerk@test.local")
    response = client.post(f"/api/v1/bills/{bill_id}/approve", json={})
    assert response.status_code in (409, 422)
    assert b"person who recorded it" in response.data


def test_the_funnel_runs_with_two_people(
    client,
    db,
    org,
    vendor_record,
    expense_account,
    operating_account,
    clerk,
    make_user,
    sign_in,
):
    bill_id = client.post(
        "/api/v1/bills", json=_bill_payload(vendor_record.id, expense_account.id)
    ).get_json()["id"]

    make_user("controller", email="api-approver@test.local")
    sign_in("api-approver@test.local")

    response = client.post(f"/api/v1/bills/{bill_id}/approve", json={"note": "Matches the quote."})
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["status"] == "approved"
    assert Decimal(body["approved_total"]) == Decimal("480.00")

    response = client.post(
        f"/api/v1/bills/{bill_id}/payments",
        json={
            "bank_account_id": operating_account.id,
            "amount": "480.00",
            "paid_date": PAID.isoformat(),
            "method": "check",
            "check_number": "10412",
        },
    )
    assert response.status_code == 201, response.get_json()
    assert Decimal(client.get(f"/api/v1/bills/{bill_id}").get_json()["balance"]) == Decimal("0")


def test_a_bill_with_no_lines_is_rejected_at_the_schema(
    client, vendor_record, expense_account, clerk
):
    payload = _bill_payload(vendor_record.id, expense_account.id)
    payload["lines"] = []
    assert client.post("/api/v1/bills", json=payload).status_code == 422


def test_a_due_date_before_the_bill_date_is_rejected(client, vendor_record, expense_account, clerk):
    payload = _bill_payload(vendor_record.id, expense_account.id)
    payload["due_date"] = (BILLED - dt.timedelta(days=1)).isoformat()
    assert client.post("/api/v1/bills", json=payload).status_code == 422


def test_a_zero_line_is_rejected(client, vendor_record, expense_account, clerk):
    payload = _bill_payload(vendor_record.id, expense_account.id, amount="0")
    assert client.post("/api/v1/bills", json=payload).status_code == 422


def test_an_unapproved_bill_cannot_be_paid(
    client,
    db,
    org,
    vendor_record,
    expense_account,
    operating_account,
    clerk,
    make_user,
    sign_in,
):
    bill_id = client.post(
        "/api/v1/bills", json=_bill_payload(vendor_record.id, expense_account.id)
    ).get_json()["id"]

    make_user("org_admin", email="api-boss@test.local")
    sign_in("api-boss@test.local")

    response = client.post(
        f"/api/v1/bills/{bill_id}/payments",
        json={
            "bank_account_id": operating_account.id,
            "amount": "480.00",
            "paid_date": PAID.isoformat(),
        },
    )
    assert response.status_code in (409, 422)
    assert b"not been approved" in response.data


def test_the_due_filter_is_approved_unpaid_and_late(
    client, db, org, vendor_record, expense_account, clerk, make_user, sign_in
):
    """Not a general date filter: it is what a payment run today is for."""
    bill_id = client.post(
        "/api/v1/bills", json=_bill_payload(vendor_record.id, expense_account.id)
    ).get_json()["id"]

    # Pending approval, and long past due: still not in a payment run.
    assert client.get("/api/v1/bills?due=true").get_json()["data"] == []

    make_user("controller", email="api-approver2@test.local")
    sign_in("api-approver2@test.local")
    client.post(f"/api/v1/bills/{bill_id}/approve", json={})

    assert [row["id"] for row in client.get("/api/v1/bills?due=true").get_json()["data"]] == [
        bill_id
    ]


def test_outstanding_answers_per_vendor(client, db, org, vendor_record, expense_account, clerk):
    client.post("/api/v1/bills", json=_bill_payload(vendor_record.id, expense_account.id))

    body = client.get(f"/api/v1/payables/outstanding?vendor_id={vendor_record.id}").get_json()
    assert Decimal(body["outstanding"]) == Decimal("480.00")


def test_another_tenants_bill_is_not_found(client, db, org, other_org, clerk, make_user, sign_in):
    """404 for a record they may act on but do not own.

    The approve check is deliberately made as somebody who *holds* the grant:
    permission is enforced before lookup, so a 403 there would prove nothing
    about tenant scoping.
    """
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
        vendor = Vendor(org_id=other_org.id, code="RIVV2", name="Rival Plumbing")
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

    assert client.get(f"/api/v1/bills/{theirs_id}").status_code == 404

    make_user("controller", email="api-scoping@test.local")
    sign_in("api-scoping@test.local")
    assert client.post(f"/api/v1/bills/{theirs_id}/approve", json={}).status_code == 404


def test_a_technician_cannot_record_a_bill(
    client, vendor_record, expense_account, make_user, sign_in
):
    make_user("technician", email="tech-ap@test.local")
    sign_in("tech-ap@test.local")

    response = client.post(
        "/api/v1/bills", json=_bill_payload(vendor_record.id, expense_account.id)
    )
    assert response.status_code == 403

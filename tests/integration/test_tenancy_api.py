"""Renewals, move-outs, and deposit disposition over the API.

The console covers a manager working a lease by hand. This covers the other
caller — a resident portal, a partner integration, a nightly job — where the
same rules have to hold with nobody watching the screen.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

pytestmark = pytest.mark.integration

TERM_START = dt.date(2026, 1, 1)
TERM_END = dt.date(2026, 12, 31)
COLLECTED = dt.date(2026, 1, 3)
NOTICE = dt.date(2026, 6, 1)
LEAVING = dt.date(2026, 6, 30)


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
def controller(db, org, scope, make_user, sign_in):
    """Holds LEASE_RENEW, LEASE_TERMINATE and DEPOSIT_RELEASE together."""
    make_user("org_admin", email="tenancy-api@test.local")
    sign_in("tenancy-api@test.local")
    return "tenancy-api@test.local"


@pytest.fixture()
def trust_account(db, org, scope, accounts):
    from app.models.accounting import BankAccount, BankAccountType
    from app.services.accounting.chart import AccountCode

    account = BankAccount(
        org_id=org.id,
        code="TRUST",
        name="Resident deposits",
        account_type=BankAccountType.TRUST,
        gl_account_id=accounts[AccountCode.CASH_TRUST].id,
        is_trust=True,
    )
    db.session.add(account)
    db.session.commit()
    return account


@pytest.fixture()
def lease_record(db, org, scope, property_record, unit_record):
    from app.models.leasing import Lease, LeaseStatus
    from app.models.sequences import SequenceKey
    from app.services.common.numbering import next_number

    lease = Lease(
        org_id=org.id,
        lease_number=next_number(db.session, SequenceKey.LEASE, org_id=org.id),
        property_id=property_record.id,
        unit_id=unit_record.id,
        status=LeaseStatus.ACTIVE,
        start_date=TERM_START,
        end_date=TERM_END,
        rent_amount=Decimal("2000.00"),
        security_deposit=Decimal("2000.00"),
    )
    db.session.add(lease)
    db.session.commit()
    return lease


@pytest.fixture()
def with_deposit(db, org, scope, lease_record, trust_account):
    from app.services.accounting.deposits import collect_deposit

    collect_deposit(
        db.session,
        org_id=org.id,
        lease_id=lease_record.id,
        bank_account_id=trust_account.id,
        amount=Decimal("2000.00"),
        effective_date=COLLECTED,
    )
    db.session.commit()
    return lease_record


# ---------------------------------------------------------------------------


def test_the_end_of_a_tenancy_runs_end_to_end(client, db, org, with_deposit, controller):
    response = client.post(
        f"/api/v1/leases/{with_deposit.id}/notice",
        json={
            "notice_date": NOTICE.isoformat(),
            "scheduled_date": LEAVING.isoformat(),
            "reason": "Relocating",
        },
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    move_out_id = body["id"]
    # What was collected, not what the contract says.
    assert Decimal(body["deposit_held"]) == Decimal("2000.00")

    response = client.post(
        f"/api/v1/move-outs/{move_out_id}/record",
        json={"actual_date": LEAVING.isoformat(), "disposition_days": 21},
    )
    assert response.status_code == 200, response.get_json()
    assert (
        response.get_json()["disposition_due_by"] == (LEAVING + dt.timedelta(days=21)).isoformat()
    )

    response = client.post(
        f"/api/v1/move-outs/{move_out_id}/disposition",
        json={
            "deductions": [
                {"description": "Carpet replacement, bedroom", "amount": "420.00"},
                {"description": "Missing keys", "amount": "45.00"},
            ]
        },
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert Decimal(body["deposit_deductions"]) == Decimal("465.00")
    assert Decimal(body["deposit_refunded"]) == Decimal("1535.00")
    assert body["disposition_sent_at"] is not None


def test_a_renewal_carries_the_offered_terms(client, db, org, lease_record, controller):
    response = client.post(
        f"/api/v1/leases/{lease_record.id}/renewals",
        json={
            "offered_rent": "2100.00",
            "proposed_start": "2027-01-01",
            "proposed_end": "2027-12-31",
        },
    )
    assert response.status_code == 201, response.get_json()
    renewal_id = response.get_json()["id"]

    listed = client.get(f"/api/v1/leases/{lease_record.id}/renewals").get_json()
    assert [row["id"] for row in listed["data"]] == [renewal_id]

    response = client.post(f"/api/v1/renewals/{renewal_id}/accept")
    assert response.status_code == 201, response.get_json()
    assert Decimal(response.get_json()["rent_amount"]) == Decimal("2100.00")


def test_a_renewal_that_ends_before_it_starts_is_rejected(client, lease_record, controller):
    response = client.post(
        f"/api/v1/leases/{lease_record.id}/renewals",
        json={
            "offered_rent": "2100.00",
            "proposed_start": "2027-12-31",
            "proposed_end": "2027-01-01",
        },
    )
    assert response.status_code == 422


def test_a_declined_offer_cannot_then_be_accepted(client, lease_record, controller):
    renewal_id = client.post(
        f"/api/v1/leases/{lease_record.id}/renewals",
        json={
            "offered_rent": "2100.00",
            "proposed_start": "2027-01-01",
            "proposed_end": "2027-12-31",
        },
    ).get_json()["id"]

    assert (
        client.post(
            f"/api/v1/renewals/{renewal_id}/decline", json={"reason": "Moving out"}
        ).status_code
        == 200
    )
    response = client.post(f"/api/v1/renewals/{renewal_id}/accept")
    assert response.status_code in (409, 422)


def test_withholding_more_than_was_collected_is_refused(client, db, org, with_deposit, controller):
    move_out_id = client.post(
        f"/api/v1/leases/{with_deposit.id}/notice",
        json={"notice_date": NOTICE.isoformat(), "scheduled_date": LEAVING.isoformat()},
    ).get_json()["id"]
    client.post(
        f"/api/v1/move-outs/{move_out_id}/record", json={"actual_date": LEAVING.isoformat()}
    )

    response = client.post(
        f"/api/v1/move-outs/{move_out_id}/disposition",
        json={"deductions": [{"description": "Gut renovation", "amount": "9000.00"}]},
    )
    assert response.status_code in (409, 422)
    assert b"exceed" in response.data


def test_a_deduction_without_an_amount_is_rejected_at_the_schema(
    client, db, org, with_deposit, controller
):
    move_out_id = client.post(
        f"/api/v1/leases/{with_deposit.id}/notice",
        json={"notice_date": NOTICE.isoformat(), "scheduled_date": LEAVING.isoformat()},
    ).get_json()["id"]
    client.post(
        f"/api/v1/move-outs/{move_out_id}/record", json={"actual_date": LEAVING.isoformat()}
    )

    response = client.post(
        f"/api/v1/move-outs/{move_out_id}/disposition",
        json={"deductions": [{"description": "Something", "amount": "0"}]},
    )
    assert response.status_code == 422


def test_deductions_and_an_inspection_together_are_rejected(
    client, db, org, with_deposit, controller
):
    """Two sources for the same figure is one of them being ignored."""
    move_out_id = client.post(
        f"/api/v1/leases/{with_deposit.id}/notice",
        json={"notice_date": NOTICE.isoformat(), "scheduled_date": LEAVING.isoformat()},
    ).get_json()["id"]

    response = client.post(
        f"/api/v1/move-outs/{move_out_id}/disposition",
        json={
            "deductions": [{"description": "Keys", "amount": "45.00"}],
            "from_inspection_id": "01a00000-0000-7000-8000-000000000000",
        },
    )
    assert response.status_code == 422


def test_settling_before_the_move_out_is_recorded_is_refused(
    client, db, org, with_deposit, controller
):
    move_out_id = client.post(
        f"/api/v1/leases/{with_deposit.id}/notice",
        json={"notice_date": NOTICE.isoformat(), "scheduled_date": LEAVING.isoformat()},
    ).get_json()["id"]

    response = client.post(
        f"/api/v1/move-outs/{move_out_id}/disposition",
        json={"deductions": [{"description": "Keys", "amount": "45.00"}]},
    )
    assert response.status_code in (409, 422)
    assert b"before the move-out is recorded" in response.data


def test_the_overdue_list_means_what_the_service_means(client, db, org, with_deposit, controller):
    """The report somebody should read every morning, not a general filter."""
    from app.context import clear_context
    from app.models.leasing import MoveOut

    move_out_id = client.post(
        f"/api/v1/leases/{with_deposit.id}/notice",
        json={"notice_date": NOTICE.isoformat(), "scheduled_date": LEAVING.isoformat()},
    ).get_json()["id"]
    # A window long enough that the deadline has not passed yet: the point of
    # the assertion below is that the list tracks the stored deadline, and it
    # would prove nothing against a record that was already late.
    client.post(
        f"/api/v1/move-outs/{move_out_id}/record",
        json={"actual_date": LEAVING.isoformat(), "disposition_days": 365},
    )

    assert client.get("/api/v1/move-outs?overdue=true").get_json()["data"] == []

    db.session.expire_all()
    token = _rebound(org)
    try:
        db.session.get(MoveOut, move_out_id).disposition_due_by = dt.date(2020, 1, 1)
        db.session.commit()
    finally:
        clear_context(token)

    overdue = client.get("/api/v1/move-outs?overdue=true").get_json()["data"]
    assert [row["id"] for row in overdue] == [move_out_id]


def test_a_second_notice_on_one_lease_is_refused(client, db, org, with_deposit, controller):
    payload = {"notice_date": NOTICE.isoformat(), "scheduled_date": LEAVING.isoformat()}
    assert client.post(f"/api/v1/leases/{with_deposit.id}/notice", json=payload).status_code == 201
    response = client.post(f"/api/v1/leases/{with_deposit.id}/notice", json=payload)
    assert response.status_code in (409, 422)


def test_another_tenants_records_are_not_found(client, db, org, other_org, controller):
    from app.context import RequestContext, bind_context, clear_context, new_correlation_id
    from app.models.leasing import Lease, LeaseStatus
    from app.models.org import Property, PropertyType, Unit, UnitStatus
    from app.models.sequences import SequenceKey
    from app.services.common.numbering import next_number
    from app.services.leasing.tenancy import give_notice

    token = bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=other_org.id,
            actor_type="system",
            source="test",
        )
    )
    try:
        prop = Property(
            org_id=other_org.id,
            code="RIV5",
            name="Rival Five",
            property_type=PropertyType.RESIDENTIAL_MULTI,
            address_line1="5 Rival Way",
            city="Elsewhere",
            region="RS",
            postal_code="99999",
        )
        db.session.add(prop)
        db.session.flush()
        unit = Unit(
            org_id=other_org.id,
            property_id=prop.id,
            unit_number="5Z",
            status=UnitStatus.OCCUPIED,
            market_rent=Decimal("1000.00"),
        )
        db.session.add(unit)
        db.session.flush()
        lease = Lease(
            org_id=other_org.id,
            lease_number=next_number(db.session, SequenceKey.LEASE, org_id=other_org.id),
            property_id=prop.id,
            unit_id=unit.id,
            status=LeaseStatus.ACTIVE,
            start_date=TERM_START,
            end_date=TERM_END,
            rent_amount=Decimal("1000.00"),
        )
        db.session.add(lease)
        db.session.flush()
        move_out = give_notice(db.session, lease=lease, notice_date=NOTICE, scheduled_date=LEAVING)
        db.session.commit()
        lease_id, move_out_id = lease.id, move_out.id
    finally:
        clear_context(token)

    assert client.get(f"/api/v1/move-outs/{move_out_id}").status_code == 404
    assert (
        client.post(
            f"/api/v1/leases/{lease_id}/notice",
            json={"notice_date": NOTICE.isoformat(), "scheduled_date": LEAVING.isoformat()},
        ).status_code
        == 404
    )
    assert client.get(f"/api/v1/leases/{lease_id}/renewals").status_code == 404


def test_a_technician_cannot_end_a_tenancy(client, lease_record, make_user, sign_in):
    make_user("technician", email="tech-tenancy@test.local")
    sign_in("tech-tenancy@test.local")

    response = client.post(
        f"/api/v1/leases/{lease_record.id}/notice",
        json={"notice_date": NOTICE.isoformat(), "scheduled_date": LEAVING.isoformat()},
    )
    assert response.status_code == 403

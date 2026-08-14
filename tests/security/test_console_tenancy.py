"""Renewals, move-outs, and deposit disposition, from the console.

This is the most litigated thing a management company does, and the surfaces
have to carry the rules rather than soften them:

The statutory clock starts when the move-out is recorded, and the deadline is
*stored*. A recomputed deadline drifts every time somebody changes the setting;
a stored one is the date the law will be measured against.

Withholding more than was collected is refused. That is a claim against the
resident, not a disposition, and it wants raising as one.

And the deposit settles against what was actually taken, not against the figure
in the contract - which is a different number the moment a deposit is waived,
part-paid, or replaced by a rider.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

pytestmark = pytest.mark.security

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
def manager(db, org, scope, make_user, sign_in):
    """Holds LEASE_RENEW and LEASE_TERMINATE, but not DEPOSIT_RELEASE."""
    make_user("property_manager", email="tenancy-pm@test.local")
    sign_in("tenancy-pm@test.local")
    return "tenancy-pm@test.local"


@pytest.fixture()
def controller(db, org, scope, make_user, sign_in):
    """Holds DEPOSIT_RELEASE. Releasing money is a separate grant on purpose."""
    make_user("controller", email="tenancy-ctl@test.local")
    sign_in("tenancy-ctl@test.local")
    return "tenancy-ctl@test.local"


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
    """A deposit actually collected, which is what a disposition settles against."""
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
# Renewals
# ---------------------------------------------------------------------------


def test_a_renewal_can_be_offered_and_accepted(client, db, org, lease_record, manager):
    from app.context import clear_context
    from app.models.leasing import Lease, LeaseRenewal, LeaseStatus

    response = client.post(
        f"/admin/leases/{lease_record.id}/renewals",
        data={
            "offered_rent": "2100.00",
            "proposed_start": "2027-01-01",
            "proposed_end": "2027-12-31",
            "term_months": "12",
            "expires_in_days": "30",
        },
    )
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        from sqlalchemy import select

        renewal = db.session.execute(
            select(LeaseRenewal).where(LeaseRenewal.lease_id == lease_record.id)
        ).scalar_one()
        renewal_id = renewal.id
    finally:
        clear_context(token)

    assert (
        client.post(f"/admin/renewals/{renewal_id}", data={"action": "accept"}).status_code == 302
    )

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert db.session.get(Lease, lease_record.id).status == LeaseStatus.RENEWED
        new_lease = db.session.get(Lease, db.session.get(LeaseRenewal, renewal_id).new_lease_id)
        # The offered terms, not today's asking rent.
        assert new_lease.rent_amount == Decimal("2100.00")
    finally:
        clear_context(token)


def test_a_lapsed_offer_cannot_be_accepted(client, db, org, scope, lease_record, manager):
    """Honouring an expired price is a decision, not a default."""
    from app.context import clear_context
    from app.models.leasing import LeaseRenewal
    from app.models.types import utcnow
    from app.services.leasing.tenancy import offer_renewal

    token = _rebound(org)
    try:
        renewal = offer_renewal(
            db.session,
            lease=lease_record,
            offered_rent=Decimal("2100.00"),
            proposed_start=dt.date(2027, 1, 1),
            proposed_end=dt.date(2027, 12, 31),
        )
        renewal.offer_expires_at = utcnow() - dt.timedelta(days=1)
        db.session.commit()
        renewal_id = renewal.id
    finally:
        clear_context(token)

    response = client.post(
        f"/admin/renewals/{renewal_id}", data={"action": "accept"}, follow_redirects=True
    )
    assert b"expired" in response.data

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert db.session.get(LeaseRenewal, renewal_id).status != "accepted"
    finally:
        clear_context(token)


def test_a_second_open_offer_is_refused(client, db, org, lease_record, manager):
    """Two live offers on one lease is two prices the resident could accept."""
    data = {
        "offered_rent": "2100.00",
        "proposed_start": "2027-01-01",
        "proposed_end": "2027-12-31",
    }
    assert client.post(f"/admin/leases/{lease_record.id}/renewals", data=data).status_code == 302
    response = client.post(
        f"/admin/leases/{lease_record.id}/renewals", data=data, follow_redirects=True
    )
    assert response.status_code == 200
    assert b"already an open renewal offer" in response.data


# ---------------------------------------------------------------------------
# Notice and move-out
# ---------------------------------------------------------------------------


def test_notice_captures_what_was_collected_not_what_the_lease_says(
    client, db, org, with_deposit, manager
):
    """A waived or part-paid deposit makes these two different numbers."""
    from sqlalchemy import select

    from app.context import clear_context
    from app.models.leasing import MoveOut

    response = client.post(
        f"/admin/leases/{with_deposit.id}/notice",
        data={
            "notice_date": NOTICE.isoformat(),
            "scheduled_date": LEAVING.isoformat(),
            "reason": "Relocating for work",
        },
    )
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        record = db.session.execute(
            select(MoveOut).where(MoveOut.lease_id == with_deposit.id)
        ).scalar_one()
        assert record.deposit_held == Decimal("2000.0000")
    finally:
        clear_context(token)


def test_a_move_out_scheduled_before_notice_is_refused(client, db, org, lease_record, manager):
    response = client.post(
        f"/admin/leases/{lease_record.id}/notice",
        data={
            "notice_date": LEAVING.isoformat(),
            "scheduled_date": NOTICE.isoformat(),
        },
        follow_redirects=True,
    )
    assert b"before notice was given" in response.data


def test_recording_the_move_out_stores_the_deadline(client, db, org, with_deposit, manager):
    """Stored, not recomputed: this is the date the law measures against."""
    from sqlalchemy import select

    from app.context import clear_context
    from app.models.leasing import MoveOut

    client.post(
        f"/admin/leases/{with_deposit.id}/notice",
        data={"notice_date": NOTICE.isoformat(), "scheduled_date": LEAVING.isoformat()},
    )
    db.session.expire_all()
    token = _rebound(org)
    try:
        move_out_id = (
            db.session.execute(select(MoveOut).where(MoveOut.lease_id == with_deposit.id))
            .scalar_one()
            .id
        )
    finally:
        clear_context(token)

    response = client.post(
        f"/admin/move-outs/{move_out_id}/record",
        data={"actual_date": LEAVING.isoformat(), "disposition_days": "21"},
    )
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        record = db.session.get(MoveOut, move_out_id)
        assert record.actual_date == LEAVING
        assert record.disposition_due_by == LEAVING + dt.timedelta(days=21)
    finally:
        clear_context(token)


# ---------------------------------------------------------------------------
# Disposition
# ---------------------------------------------------------------------------


@pytest.fixture()
def moved_out(db, org, scope, with_deposit):
    from app.services.leasing.tenancy import give_notice, record_move_out

    move_out = give_notice(
        db.session, lease=with_deposit, notice_date=NOTICE, scheduled_date=LEAVING
    )
    record_move_out(db.session, move_out=move_out, actual_date=LEAVING, start_turn_on_vacancy=False)
    db.session.commit()
    return move_out


def test_a_disposition_withholds_and_returns(client, db, org, moved_out, controller):
    from app.context import clear_context
    from app.models.leasing import MoveOut

    response = client.post(
        f"/admin/move-outs/{moved_out.id}/disposition",
        data={"deductions": "Carpet replacement, bedroom | 420.00\nMissing keys | 45.00"},
    )
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        record = db.session.get(MoveOut, moved_out.id)
        assert record.deposit_deductions == Decimal("465.0000")
        assert record.deposit_refunded == Decimal("1535.0000")
        assert record.disposition_sent_at is not None
        assert len(record.deduction_detail) == 2
    finally:
        clear_context(token)


def test_withholding_more_than_was_collected_is_refused(client, db, org, moved_out, controller):
    """That is a claim against the resident, not a disposition."""
    from app.context import clear_context
    from app.models.leasing import MoveOut

    response = client.post(
        f"/admin/move-outs/{moved_out.id}/disposition",
        data={"deductions": "Gut renovation | 9000.00"},
        follow_redirects=True,
    )
    assert b"exceed" in response.data

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert db.session.get(MoveOut, moved_out.id).disposition_sent_at is None
    finally:
        clear_context(token)


def test_no_deductions_is_a_full_refund(client, db, org, moved_out, controller):
    """A real and common outcome, not an empty form to reject."""
    from app.context import clear_context
    from app.models.leasing import MoveOut

    assert (
        client.post(
            f"/admin/move-outs/{moved_out.id}/disposition", data={"deductions": ""}
        ).status_code
        == 302
    )

    db.session.expire_all()
    token = _rebound(org)
    try:
        record = db.session.get(MoveOut, moved_out.id)
        assert record.deposit_deductions == Decimal("0.0000")
        assert record.deposit_refunded == Decimal("2000.0000")
    finally:
        clear_context(token)


@pytest.mark.parametrize(
    "line", ["Carpet replacement 420.00", "Carpet | NaN", "Carpet | not a number"]
)
def test_a_malformed_deduction_is_refused_rather_than_crashing(
    client, db, org, moved_out, controller, line
):
    """NaN parses as a Decimal and then raises on the service's own comparison."""
    response = client.post(
        f"/admin/move-outs/{moved_out.id}/disposition",
        data={"deductions": line},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"is not a" in response.data


def test_settling_twice_is_refused(client, db, org, moved_out, controller):
    client.post(f"/admin/move-outs/{moved_out.id}/disposition", data={"deductions": "Keys | 45.00"})
    response = client.post(
        f"/admin/move-outs/{moved_out.id}/disposition",
        data={"deductions": "Keys again | 45.00"},
        follow_redirects=True,
    )
    assert b"already been settled" in response.data


def test_settling_before_the_move_out_is_recorded_is_refused(
    client, db, org, scope, with_deposit, controller
):
    from app.context import clear_context
    from app.services.leasing.tenancy import give_notice

    token = _rebound(org)
    try:
        move_out = give_notice(
            db.session, lease=with_deposit, notice_date=NOTICE, scheduled_date=LEAVING
        )
        db.session.commit()
        move_out_id = move_out.id
    finally:
        clear_context(token)

    response = client.post(
        f"/admin/move-outs/{move_out_id}/disposition",
        data={"deductions": "Keys | 45.00"},
        follow_redirects=True,
    )
    assert b"before the move-out is recorded" in response.data


# ---------------------------------------------------------------------------
# Who may, and whose
# ---------------------------------------------------------------------------


def test_the_board_leads_with_what_is_overdue(client, db, org, scope, moved_out, manager):
    """Past the deadline the deductions are usually forfeit entirely."""
    moved_out.disposition_due_by = dt.date(2020, 1, 1)
    db.session.commit()

    response = client.get("/admin/move-outs")
    assert response.status_code == 200
    assert b"overdue" in response.data.lower()


def test_a_manager_cannot_settle_a_deposit(client, db, org, moved_out, manager):
    """Ending a tenancy and releasing money are separate grants."""
    response = client.post(
        f"/admin/move-outs/{moved_out.id}/disposition", data={"deductions": "Keys | 45.00"}
    )
    assert response.status_code == 403


def test_another_tenants_lease_and_move_out_are_not_found(client, db, org, other_org, manager):
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
            code="RIV4",
            name="Rival Four",
            property_type=PropertyType.RESIDENTIAL_MULTI,
            address_line1="4 Rival Way",
            city="Elsewhere",
            region="RS",
            postal_code="99999",
        )
        db.session.add(prop)
        db.session.flush()
        unit = Unit(
            org_id=other_org.id,
            property_id=prop.id,
            unit_number="4Z",
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

    assert client.get(f"/admin/leases/{lease_id}").status_code == 404
    assert client.get(f"/admin/move-outs/{move_out_id}").status_code == 404
    assert (
        client.post(
            f"/admin/move-outs/{move_out_id}/record", data={"actual_date": LEAVING.isoformat()}
        ).status_code
        == 404
    )


def test_an_anonymous_visitor_cannot_reach_any_of_it(client, lease_record):
    assert client.get("/admin/move-outs").status_code in (302, 401)
    assert client.get(f"/admin/leases/{lease_record.id}").status_code in (302, 401)

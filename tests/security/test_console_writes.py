"""The console's write surfaces.

These are staff routes, so the question is not "is this theirs?" but "may they
do it, and does the console let the service refuse?" A page that quietly drops
a service's refusal is worse than one that never offered the button.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

pytestmark = pytest.mark.security

VACATED = dt.date(2026, 4, 1)
JANUARY = dt.date(2026, 1, 1)


def _rebound(org):
    """A tenant scope for reading after a request has run."""
    from app.context import RequestContext, bind_context, new_correlation_id

    return bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=org.id,
            actor_type="system",
            source="test",
        )
    )


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------


@pytest.fixture()
def turn(db, org, scope, unit_record):
    from app.services.leasing.turns import start_turn

    record = start_turn(db.session, org_id=org.id, unit_id=unit_record.id, started_on=VACATED)
    db.session.commit()
    return record


@pytest.fixture()
def manager(db, org, scope, make_user, sign_in):
    make_user("property_manager", email="pm@test.local")
    sign_in("pm@test.local")
    return "pm@test.local"


def test_a_step_can_be_completed_from_the_console(client, db, org, turn, manager):
    from app.context import clear_context
    from app.models.leasing import StepStatus

    step_id = turn.steps[0].id
    response = client.post(f"/admin/turns/{turn.id}/steps/{step_id}", data={"action": "complete"})
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        from app.models.leasing import TurnStep

        assert db.session.get(TurnStep, step_id).status == StepStatus.DONE
    finally:
        clear_context(token)


def test_skipping_without_a_reason_is_refused_by_the_service(client, db, org, turn, manager):
    """The console passes the empty field through rather than inventing a rule."""
    from app.context import clear_context
    from app.models.leasing import StepStatus, TurnStep

    step_id = turn.steps[0].id
    response = client.post(
        f"/admin/turns/{turn.id}/steps/{step_id}",
        data={"action": "skip", "reason": "   "},
        follow_redirects=True,
    )
    assert b"needs a reason" in response.data

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert db.session.get(TurnStep, step_id).status == StepStatus.PENDING
    finally:
        clear_context(token)


def test_marking_ready_with_work_outstanding_is_refused(client, db, org, turn, manager):
    """The rule that matters, surfaced rather than swallowed."""
    from app.context import clear_context
    from app.models.leasing import Turn, TurnStatus

    response = client.post(
        f"/admin/turns/{turn.id}", data={"action": "ready"}, follow_redirects=True
    )
    assert b"required step" in response.data

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert db.session.get(Turn, turn.id).status != TurnStatus.READY
    finally:
        clear_context(token)


def test_a_turn_completes_once_every_required_step_is_settled(client, db, org, turn, manager):
    from app.context import clear_context
    from app.models.leasing import Turn, TurnStatus
    from app.models.org import Unit, UnitStatus

    for step in turn.steps:
        if step.is_required:
            client.post(f"/admin/turns/{turn.id}/steps/{step.id}", data={"action": "complete"})

    response = client.post(f"/admin/turns/{turn.id}", data={"action": "ready"})
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        reloaded = db.session.get(Turn, turn.id)
        assert reloaded.status == TurnStatus.READY
        assert reloaded.days_vacant is not None
        assert db.session.get(Unit, reloaded.unit_id).status == UnitStatus.VACANT_READY
    finally:
        clear_context(token)


def test_a_role_without_unit_manage_cannot_write(client, db, org, turn, make_user, sign_in):
    make_user("auditor", email="readonly@test.local")
    sign_in("readonly@test.local")

    response = client.post(
        f"/admin/turns/{turn.id}/steps/{turn.steps[0].id}", data={"action": "complete"}
    )
    assert response.status_code == 403


def test_another_tenants_turn_is_not_found(client, db, org, other_org, manager):
    from app.context import RequestContext, bind_context, clear_context, new_correlation_id
    from app.models.org import Property, PropertyType, Unit, UnitStatus
    from app.services.leasing.turns import start_turn

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
            code="RIV",
            name="Rival",
            property_type=PropertyType.RESIDENTIAL_MULTI,
            address_line1="1 Rival Way",
            city="Elsewhere",
            region="RS",
            postal_code="99999",
        )
        db.session.add(prop)
        db.session.flush()
        unit = Unit(
            org_id=other_org.id,
            property_id=prop.id,
            unit_number="9Z",
            status=UnitStatus.VACANT_NOT_READY,
            market_rent=Decimal("1000.00"),
        )
        db.session.add(unit)
        db.session.flush()
        theirs = start_turn(db.session, org_id=other_org.id, unit_id=unit.id, started_on=VACATED)
        db.session.commit()
        theirs_id = theirs.id
    finally:
        clear_context(token)

    assert client.post(f"/admin/turns/{theirs_id}", data={"action": "ready"}).status_code == 404


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


@pytest.fixture()
def owners(db, org, scope):
    from app.models.org import OwnerEntity, OwnerType

    created = []
    for code, name in (("ALPHA", "Alpha Holdings"), ("BETA", "Beta Trust")):
        entity = OwnerEntity(org_id=org.id, code=code, name=name, owner_type=OwnerType.COMPANY)
        db.session.add(entity)
        created.append(entity)
    db.session.commit()
    return created


@pytest.fixture()
def controller(db, org, scope, make_user, sign_in):
    """Holds OWNER_MANAGE."""
    make_user("org_admin", email="owner-admin@test.local")
    sign_in("owner-admin@test.local")
    return "owner-admin@test.local"


def test_a_stake_can_be_recorded_from_the_console(
    client, db, org, property_record, owners, controller
):
    from app.context import clear_context

    response = client.post(
        f"/admin/ownership/{property_record.id}",
        data={
            "action": "record",
            "owner_entity_id": owners[0].id,
            "percentage": "100",
            "effective_from": JANUARY.isoformat(),
        },
    )
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        from app.services.portfolio.ownership import total_allocated

        assert total_allocated(
            db.session, org_id=org.id, property_id=property_record.id, on_date=JANUARY
        ) == Decimal("100.0000")
    finally:
        clear_context(token)


def test_a_transfer_preserves_the_total(
    client, db, org, scope, property_record, owners, controller
):
    from app.context import clear_context

    client.post(
        f"/admin/ownership/{property_record.id}",
        data={
            "action": "record",
            "owner_entity_id": owners[0].id,
            "percentage": "100",
            "effective_from": JANUARY.isoformat(),
        },
    )
    march = dt.date(2026, 3, 14)
    response = client.post(
        f"/admin/ownership/{property_record.id}",
        data={
            "action": "transfer",
            "from_owner_entity_id": owners[0].id,
            "to_owner_entity_id": owners[1].id,
            "percentage": "40",
            "effective_from": march.isoformat(),
        },
    )
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        from app.services.portfolio.ownership import assert_fully_allocated

        assert_fully_allocated(
            db.session, org_id=org.id, property_id=property_record.id, on_date=march
        )
    finally:
        clear_context(token)


def test_over_allocating_is_refused_and_said_so(
    client, db, org, property_record, owners, controller
):
    client.post(
        f"/admin/ownership/{property_record.id}",
        data={
            "action": "record",
            "owner_entity_id": owners[0].id,
            "percentage": "100",
            "effective_from": JANUARY.isoformat(),
        },
    )
    response = client.post(
        f"/admin/ownership/{property_record.id}",
        data={
            "action": "record",
            "owner_entity_id": owners[1].id,
            "percentage": "5",
            "effective_from": JANUARY.isoformat(),
        },
        follow_redirects=True,
    )
    assert b"already allocated" in response.data


@pytest.mark.parametrize("percentage", ["NaN", "not a number", "Infinity"])
def test_a_non_numeric_percentage_is_refused_rather_than_crashing(
    client, db, org, property_record, owners, controller, percentage
):
    """NaN survives Decimal() and then raises on every comparison downstream."""
    response = client.post(
        f"/admin/ownership/{property_record.id}",
        data={
            "action": "record",
            "owner_entity_id": owners[0].id,
            "percentage": percentage,
            "effective_from": JANUARY.isoformat(),
        },
    )
    assert response.status_code == 302


def test_a_malformed_date_is_refused(client, db, org, property_record, owners, controller):
    response = client.post(
        f"/admin/ownership/{property_record.id}",
        data={
            "action": "record",
            "owner_entity_id": owners[0].id,
            "percentage": "50",
            "effective_from": "the fourteenth",
        },
        follow_redirects=True,
    )
    assert b"not a date" in response.data


def test_a_role_without_owner_manage_cannot_write(
    client, db, org, property_record, owners, make_user, sign_in
):
    make_user("technician", email="tech-owner@test.local")
    sign_in("tech-owner@test.local")

    response = client.post(
        f"/admin/ownership/{property_record.id}",
        data={"action": "record", "owner_entity_id": owners[0].id, "percentage": "50"},
    )
    assert response.status_code == 403


def test_an_anonymous_visitor_cannot_write(client, property_record):
    response = client.post(f"/admin/ownership/{property_record.id}", data={"action": "record"})
    assert response.status_code in (302, 401)

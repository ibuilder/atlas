"""Spaces and asset lifecycle, from the console.

Both were reachable only from the demo seed. Two rules carry them:

A space hierarchy rolls up. A floor's area is the rooms on it, and reporting
the floor's own figure as the total is how square footage quietly halves. The
same walk is what makes a cycle dangerous rather than untidy — one loop hangs
every page that reports a roll-up.

An asset is retired, never deleted. Its service history is the evidence behind
this replacement decision and behind the next one for the same model.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

pytestmark = pytest.mark.security

TODAY = dt.date(2026, 8, 14)


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
    """Holds PROPERTY_UPDATE and ASSET_MANAGE."""
    make_user("property_manager", email="asset-pm@test.local")
    sign_in("asset-pm@test.local")
    return "asset-pm@test.local"


@pytest.fixture()
def spaces(db, org, scope, property_record):
    """A building with a floor and two rooms, so the roll-up has something to add."""
    from app.models.asset_graph import SpaceKind
    from app.services.assets.spaces import create_space

    building = create_space(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        code="BLDG",
        name="Harrow Court",
        kind=SpaceKind.COMMON_AREA,
    )
    floor = create_space(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        code="L02",
        name="Second floor",
        kind=SpaceKind.CIRCULATION,
        parent=building,
        area_sqft=Decimal("200"),
    )
    for code, area in (("R201", "400"), ("R202", "350")):
        create_space(
            db.session,
            org_id=org.id,
            property_id=property_record.id,
            code=code,
            name=f"Room {code}",
            kind=SpaceKind.ROOM,
            parent=floor,
            area_sqft=Decimal(area),
        )
    db.session.commit()
    return {"building": building, "floor": floor}


@pytest.fixture()
def asset(db, org, scope, property_record):
    from app.models.asset_graph import Asset, AssetCategory, AssetCriticality, AssetStatus

    record = Asset(
        org_id=org.id,
        code="BOIL-01",
        name="Main boiler",
        category=AssetCategory.HVAC,
        status=AssetStatus.ACTIVE,
        criticality=AssetCriticality.CRITICAL,
        property_id=property_record.id,
        manufacturer="Vaillant",
        installed_on=TODAY - dt.timedelta(days=365 * 8),
        expected_life_years=15,
        purchase_price=Decimal("4200.00"),
        replacement_cost=Decimal("6000.00"),
    )
    db.session.add(record)
    db.session.commit()
    return record


# ---------------------------------------------------------------------------
# Spaces
# ---------------------------------------------------------------------------


def test_the_tree_rolls_area_up(client, db, org, property_record, spaces, manager):
    """A floor's area is the rooms on it, not the figure on the floor."""
    response = client.get(f"/admin/properties/{property_record.id}/spaces")
    assert response.status_code == 200
    # 200 on the floor plus 400 and 350 in the rooms.
    assert b"950" in response.data


def test_a_space_can_be_added_under_another(client, db, org, property_record, spaces, manager):
    from sqlalchemy import select

    from app.context import clear_context
    from app.models.asset_graph import Space

    response = client.post(
        f"/admin/properties/{property_record.id}/spaces",
        data={
            "code": "R203",
            "name": "Room R203",
            "kind": "room",
            "parent_space_id": spaces["floor"].id,
            "area_sqft": "150",
        },
    )
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        added = db.session.execute(
            select(Space).where(Space.org_id == org.id, Space.code == "R203")
        ).scalar_one()
        assert added.parent_space_id == spaces["floor"].id
    finally:
        clear_context(token)


def test_a_space_cannot_be_moved_into_its_own_subtree(
    client, db, org, property_record, spaces, manager
):
    """A cycle here is not untidiness: every roll-up walks this tree."""
    from app.context import clear_context
    from app.models.asset_graph import Space

    response = client.post(
        f"/admin/spaces/{spaces['building'].id}/move",
        data={"new_parent_id": spaces["floor"].id},
        follow_redirects=True,
    )
    assert response.status_code == 200

    db.session.expire_all()
    token = _rebound(org)
    try:
        # Unchanged: the building is still at the top.
        assert db.session.get(Space, spaces["building"].id).parent_space_id is None
    finally:
        clear_context(token)


def test_a_space_without_a_code_is_refused(client, db, org, property_record, spaces, manager):
    response = client.post(
        f"/admin/properties/{property_record.id}/spaces",
        data={"code": "", "name": "Nameless", "kind": "room"},
        follow_redirects=True,
    )
    assert b"code and a name" in response.data


def test_another_tenants_property_has_no_space_tree(client, db, org, other_org, manager):
    from app.context import RequestContext, bind_context, clear_context, new_correlation_id
    from app.models.org import Property, PropertyType

    token = bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=other_org.id,
            actor_type="system",
            source="test",
        )
    )
    try:
        theirs = Property(
            org_id=other_org.id,
            code="RIV7",
            name="Rival Seven",
            property_type=PropertyType.RESIDENTIAL_MULTI,
            address_line1="7 Rival Way",
            city="Elsewhere",
            region="RS",
            postal_code="99999",
        )
        db.session.add(theirs)
        db.session.commit()
        theirs_id = theirs.id
    finally:
        clear_context(token)

    assert client.get(f"/admin/properties/{theirs_id}/spaces").status_code == 404


# ---------------------------------------------------------------------------
# Asset lifecycle
# ---------------------------------------------------------------------------


def test_the_page_shows_the_numbers_behind_the_recommendation(client, asset, manager):
    """The recommendation is worth little; the numbers are worth a lot."""
    response = client.get(f"/admin/assets/{asset.id}")
    assert response.status_code == 200
    assert b"Spent on it so far" in response.data
    assert b"Failures in the last year" in response.data
    assert b"Past its expected life" in response.data


def test_recording_service_derives_the_aggregates(client, db, org, asset, manager):
    """Maintained here rather than separately, so they cannot disagree."""
    from app.context import clear_context
    from app.models.asset_graph import Asset

    response = client.post(
        f"/admin/assets/{asset.id}/service",
        data={
            "event_type": "repair",
            "performed_on": (TODAY - dt.timedelta(days=30)).isoformat(),
            "cost": "480.00",
            "condition_after": "3",
            "notes": "Replaced the pump",
        },
    )
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        reloaded = db.session.get(Asset, asset.id)
        assert reloaded.service_count == 1
        assert reloaded.lifetime_service_cost == Decimal("480.0000")
        assert reloaded.condition_score == 3
        assert reloaded.last_serviced_on == TODAY - dt.timedelta(days=30)
    finally:
        clear_context(token)


def test_service_cannot_be_recorded_in_the_future(client, db, org, asset, manager):
    response = client.post(
        f"/admin/assets/{asset.id}/service",
        data={
            "event_type": "repair",
            "performed_on": (TODAY + dt.timedelta(days=365)).isoformat(),
            "cost": "100.00",
        },
        follow_redirects=True,
    )
    assert b"future date" in response.data


@pytest.mark.parametrize("cost", ["NaN", "Infinity", "a lot"])
def test_a_cost_that_is_not_a_number_is_refused(client, db, org, asset, manager, cost):
    response = client.post(
        f"/admin/assets/{asset.id}/service",
        data={
            "event_type": "repair",
            "performed_on": (TODAY - dt.timedelta(days=1)).isoformat(),
            "cost": cost,
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"is not a date, an amount" in response.data or b"is not an amount" in response.data


def test_repeated_failures_move_the_recommendation(client, db, org, scope, asset, manager):
    """Three failures in a year is the shape this exists to notice."""
    from app.models.asset_graph import ServiceEventType
    from app.services.assets.lifecycle import record_service, repair_or_replace

    token = _rebound(org)
    try:
        from app.context import clear_context

        for days in (30, 120, 250):
            record_service(
                db.session,
                asset=asset,
                event_type=ServiceEventType.REPAIR,
                performed_on=TODAY - dt.timedelta(days=days),
                cost=Decimal("1200.00"),
            )
        db.session.commit()
        advice = repair_or_replace(db.session, asset=asset, as_of=TODAY)
        assert advice.failures_last_year == 3
        assert advice.recommendation != "repair"
        assert advice.reasons
    finally:
        clear_context(token)

    response = client.get(f"/admin/assets/{asset.id}")
    assert b"Advisory" in response.data


def test_marking_uncovered_work_as_claimable_is_refused(client, db, org, scope, asset, manager):
    """A claim on work that was never covered is a claim that gets rejected."""
    from app.context import clear_context
    from app.models.asset_graph import AssetServiceEvent, ServiceEventType
    from app.services.assets.lifecycle import record_service

    token = _rebound(org)
    try:
        event = record_service(
            db.session,
            asset=asset,
            event_type=ServiceEventType.REPAIR,
            performed_on=TODAY - dt.timedelta(days=10),
            cost=Decimal("400.00"),
        )
        db.session.commit()
        event_id = event.id
    finally:
        clear_context(token)

    response = client.post(f"/admin/asset-events/{event_id}/warranty", follow_redirects=True)
    assert b"not covered" in response.data

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert db.session.get(AssetServiceEvent, event_id).was_under_warranty is False
    finally:
        clear_context(token)


def test_retiring_keeps_the_record(client, db, org, asset, manager):
    """The history is the evidence behind this decision and the next one."""
    from app.context import clear_context
    from app.models.asset_graph import Asset, AssetStatus

    response = client.post(
        f"/admin/assets/{asset.id}/retire",
        data={
            "retired_on": TODAY.isoformat(),
            "reason": "Compressor failed for the third time in ten months",
        },
    )
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        reloaded = db.session.get(Asset, asset.id)
        assert reloaded is not None
        assert reloaded.status == AssetStatus.RETIRED
    finally:
        clear_context(token)


def test_retiring_without_a_reason_is_refused(client, db, org, asset, manager):
    response = client.post(
        f"/admin/assets/{asset.id}/retire",
        data={"reason": "   "},
        follow_redirects=True,
    )
    assert b"requires a reason" in response.data


def test_retired_assets_are_hidden_but_not_gone(client, db, org, scope, asset, manager):

    client.post(
        f"/admin/assets/{asset.id}/retire",
        data={"reason": "End of life"},
    )

    assert b"Main boiler" not in client.get("/admin/assets").data
    assert b"Main boiler" in client.get("/admin/assets?status=retired").data
    assert client.get(f"/admin/assets/{asset.id}").status_code == 200


# ---------------------------------------------------------------------------
# Who may, and whose
# ---------------------------------------------------------------------------


def test_an_auditor_can_read_but_not_write(client, db, org, asset, make_user, sign_in):
    make_user("auditor", email="asset-readonly@test.local")
    sign_in("asset-readonly@test.local")

    assert client.get("/admin/assets").status_code == 200
    assert client.post(f"/admin/assets/{asset.id}/retire", data={"reason": "no"}).status_code == 403


def test_another_tenants_asset_is_not_found(client, db, org, other_org, manager):
    from app.context import RequestContext, bind_context, clear_context, new_correlation_id
    from app.models.asset_graph import Asset, AssetCategory, AssetStatus
    from app.models.org import Property, PropertyType

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
            code="RIV8",
            name="Rival Eight",
            property_type=PropertyType.RESIDENTIAL_MULTI,
            address_line1="8 Rival Way",
            city="Elsewhere",
            region="RS",
            postal_code="99999",
        )
        db.session.add(prop)
        db.session.flush()
        theirs = Asset(
            org_id=other_org.id,
            code="RIVBOIL",
            name="Their boiler",
            category=AssetCategory.HVAC,
            status=AssetStatus.ACTIVE,
            property_id=prop.id,
        )
        db.session.add(theirs)
        db.session.commit()
        theirs_id = theirs.id
    finally:
        clear_context(token)

    assert client.get(f"/admin/assets/{theirs_id}").status_code == 404
    assert (
        client.post(f"/admin/assets/{theirs_id}/retire", data={"reason": "no"}).status_code == 404
    )


def test_an_anonymous_visitor_cannot_reach_assets(client, asset, property_record):
    assert client.get("/admin/assets").status_code in (302, 401)
    assert client.get(f"/admin/properties/{property_record.id}/spaces").status_code in (302, 401)

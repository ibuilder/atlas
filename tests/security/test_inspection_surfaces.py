"""Inspections, from the console and the API.

The rule worth guarding is the one about evidence. A failed item that demands a
photo blocks sign-off, and nobody can take that photo retrospectively — so the
refusal has to land at the inspection, not three weeks later at a deposit
disposition that then has nothing behind it.

The other is that the checklist is *copied* onto the inspection at the template
version used. Editing a template afterwards must never change what a completed
inspection appears to have asked.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from decimal import Decimal

import pytest

pytestmark = pytest.mark.security

SECTIONS = [
    {
        "section": "Kitchen",
        "items": [{"name": "Sink"}, {"name": "Extractor", "requires_photo": True}],
    },
    {"section": "Safety", "items": [{"name": "Smoke alarm", "requires_photo": True}]},
]


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
def inspector(db, org, scope, make_user, sign_in):
    """Holds INSPECTION_MANAGE and INSPECTION_PERFORM."""
    make_user("property_manager", email="inspector@test.local")
    sign_in("inspector@test.local")
    return "inspector@test.local"


@pytest.fixture()
def template(db, org, scope):
    from app.models.maintenance import InspectionKind, InspectionTemplate

    record = InspectionTemplate(
        org_id=org.id,
        code="MOVE_OUT",
        name="Move-out inspection",
        kind=InspectionKind.MOVE_OUT,
        version=1,
        sections=SECTIONS,
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture()
def inspection(db, org, scope, property_record, unit_record, template):
    from app.models.maintenance import InspectionKind
    from app.models.types import utcnow
    from app.services.maintenance.inspections import schedule_inspection

    record = schedule_inspection(
        db.session,
        org_id=org.id,
        kind=InspectionKind.MOVE_OUT,
        property_id=property_record.id,
        unit_id=unit_record.id,
        template=template,
        scheduled_for=utcnow(),
    )
    db.session.commit()
    return record


def _record(client, inspection_id, item_id, result, **extra):
    return client.post(
        f"/admin/inspections/{inspection_id}/findings",
        data={"item_id": item_id, "result": result, **extra},
    )


# ---------------------------------------------------------------------------
# Booking and recording
# ---------------------------------------------------------------------------


def test_an_inspection_can_be_booked_with_a_checklist(
    client, db, org, property_record, template, inspector
):
    from sqlalchemy import select

    from app.context import clear_context
    from app.models.maintenance import Inspection

    response = client.post(
        "/admin/inspections",
        data={
            "kind": "move_out",
            "property_id": property_record.id,
            "template_code": "MOVE_OUT",
        },
    )
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        record = db.session.execute(
            select(Inspection).where(Inspection.org_id == org.id)
        ).scalar_one()
        # Copied, not referenced: three items off the template.
        assert len(record.items) == 3
    finally:
        clear_context(token)


def test_recording_a_finding_starts_the_inspection(client, db, org, inspection, inspector):
    """A separate start button is a step nobody would remember to press."""
    from app.context import clear_context
    from app.models.maintenance import Inspection

    item_id = sorted(inspection.items, key=lambda i: i.sort_order)[0].id
    assert _record(client, inspection.id, item_id, "pass").status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert db.session.get(Inspection, inspection.id).status == "in_progress"
    finally:
        clear_context(token)


def test_re_recording_an_item_overwrites_rather_than_appends(
    client, db, org, inspection, inspector
):
    """A replayed capture must not leave two findings on one checklist line."""
    from app.context import clear_context
    from app.models.maintenance import InspectionItem, ItemResult

    item_id = sorted(inspection.items, key=lambda i: i.sort_order)[0].id
    _record(client, inspection.id, item_id, "pass")
    _record(client, inspection.id, item_id, "fail", notes="Leaking after all", remedy_cost="90.00")

    db.session.expire_all()
    token = _rebound(org)
    try:
        item = db.session.get(InspectionItem, item_id)
        assert item.result == ItemResult.FAIL
        assert item.remedy_cost == Decimal("90.0000")
        assert len(db.session.get(type(item), item_id).inspection.items) == 3
    finally:
        clear_context(token)


# ---------------------------------------------------------------------------
# Sign-off
# ---------------------------------------------------------------------------


def test_sign_off_is_refused_while_an_item_has_no_finding(client, db, org, inspection, inspector):
    from app.context import clear_context
    from app.models.maintenance import Inspection

    item_id = sorted(inspection.items, key=lambda i: i.sort_order)[0].id
    _record(client, inspection.id, item_id, "pass")

    response = client.post(f"/admin/inspections/{inspection.id}/complete", follow_redirects=True)
    assert b"no finding" in response.data

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert db.session.get(Inspection, inspection.id).status != "completed"
    finally:
        clear_context(token)


def test_sign_off_is_refused_while_a_failed_item_lacks_its_photo(
    client, db, org, inspection, inspector
):
    """That photo cannot be taken retrospectively, so the refusal lands here."""
    from app.context import clear_context
    from app.models.maintenance import Inspection

    for item in inspection.items:
        # "Extractor" and "Smoke alarm" both demand a photo.
        _record(client, inspection.id, item.id, "fail" if item.requires_photo else "pass")

    response = client.post(f"/admin/inspections/{inspection.id}/complete", follow_redirects=True)
    assert b"require a photo before sign-off" in response.data

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert db.session.get(Inspection, inspection.id).status != "completed"
    finally:
        clear_context(token)


def test_the_page_says_what_is_outstanding_before_the_button_is_pressed(
    client, db, org, inspection, inspector
):
    """Asked of the service, so the page and the refusal cannot disagree."""
    for item in inspection.items:
        _record(client, inspection.id, item.id, "fail" if item.requires_photo else "pass")

    response = client.get(f"/admin/inspections/{inspection.id}")
    assert response.status_code == 200
    assert b"Evidence outstanding" in response.data


def test_a_clean_pass_needs_no_photo(client, db, org, inspection, inspector):
    """Photographing forty working light switches is how honesty stops."""
    from app.context import clear_context
    from app.models.maintenance import Inspection, InspectionResult

    for item in inspection.items:
        _record(client, inspection.id, item.id, "pass")

    assert client.post(f"/admin/inspections/{inspection.id}/complete").status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        record = db.session.get(Inspection, inspection.id)
        assert record.status == "completed"
        assert record.result == InspectionResult.PASS
    finally:
        clear_context(token)


def test_a_failed_item_without_a_photo_requirement_raises_work(
    client, db, org, inspection, inspector
):
    from sqlalchemy import select

    from app.context import clear_context
    from app.models.maintenance import InspectionItem, WorkOrder

    ordered = sorted(inspection.items, key=lambda i: i.sort_order)
    sink = next(item for item in ordered if not item.requires_photo)
    for item in ordered:
        _record(
            client,
            inspection.id,
            item.id,
            "fail" if item.id == sink.id else "pass",
            notes="Tap will not shut off",
            remedy_cost="180.00",
        )

    assert client.post(f"/admin/inspections/{inspection.id}/complete").status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        raised = list(
            db.session.execute(select(WorkOrder).where(WorkOrder.org_id == org.id)).scalars()
        )
        assert len(raised) == 1
        assert db.session.get(InspectionItem, sink.id).work_order_id == raised[0].id
    finally:
        clear_context(token)


def test_a_completed_inspection_cannot_be_edited(client, db, org, inspection, inspector):
    for item in inspection.items:
        _record(client, inspection.id, item.id, "pass")
    client.post(f"/admin/inspections/{inspection.id}/complete")

    item_id = sorted(inspection.items, key=lambda i: i.sort_order)[0].id
    response = client.post(
        f"/admin/inspections/{inspection.id}/findings",
        data={"item_id": item_id, "result": "fail"},
        follow_redirects=True,
    )
    assert b"cannot be edited" in response.data


# ---------------------------------------------------------------------------
# Who may, and whose
# ---------------------------------------------------------------------------


def test_an_auditor_cannot_record_or_sign_off(client, db, org, inspection, make_user, sign_in):
    make_user("auditor", email="insp-readonly@test.local")
    sign_in("insp-readonly@test.local")

    item_id = sorted(inspection.items, key=lambda i: i.sort_order)[0].id
    assert _record(client, inspection.id, item_id, "pass").status_code == 403
    assert client.post(f"/admin/inspections/{inspection.id}/complete").status_code == 403


def test_another_tenants_inspection_is_not_found(client, db, org, other_org, inspector):
    from app.context import RequestContext, bind_context, clear_context, new_correlation_id
    from app.models.maintenance import InspectionKind
    from app.models.org import Property, PropertyType
    from app.services.maintenance.inspections import schedule_inspection

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
            code="RIV6",
            name="Rival Six",
            property_type=PropertyType.RESIDENTIAL_MULTI,
            address_line1="6 Rival Way",
            city="Elsewhere",
            region="RS",
            postal_code="99999",
        )
        db.session.add(prop)
        db.session.flush()
        theirs = schedule_inspection(
            db.session,
            org_id=other_org.id,
            kind=InspectionKind.ROUTINE,
            property_id=prop.id,
        )
        db.session.commit()
        theirs_id = theirs.id
    finally:
        clear_context(token)

    assert client.get(f"/admin/inspections/{theirs_id}").status_code == 404
    assert client.post(f"/admin/inspections/{theirs_id}/complete").status_code == 404


def test_an_anonymous_visitor_cannot_reach_inspections(client, inspection):
    assert client.get("/admin/inspections").status_code in (302, 401)
    assert client.post(f"/admin/inspections/{inspection.id}/complete").status_code in (302, 401)


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


def test_the_api_runs_the_same_lifecycle(client, db, org, inspection, inspector):
    body = client.get(f"/api/v1/inspections/{inspection.id}").get_json()
    assert len(body["items"]) == 3

    assert client.post(f"/api/v1/inspections/{inspection.id}/start").status_code == 200

    for item in body["items"]:
        response = client.post(
            f"/api/v1/inspections/{inspection.id}/findings",
            json={"item_id": item["id"], "result": "pass"},
        )
        assert response.status_code == 200, response.get_json()

    response = client.post(
        f"/api/v1/inspections/{inspection.id}/complete",
        json={"notes": "Clean throughout.", "inspector_signed": True},
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["result"] == "pass"
    assert response.get_json()["work_orders_raised"] == []


def test_the_api_refuses_sign_off_without_evidence(client, db, org, inspection, inspector):
    body = client.get(f"/api/v1/inspections/{inspection.id}").get_json()
    for item in body["items"]:
        client.post(
            f"/api/v1/inspections/{inspection.id}/findings",
            json={
                "item_id": item["id"],
                "result": "fail" if item["requires_photo"] else "pass",
                "remedy_cost": "120.00",
                "is_resident_responsible": True,
            },
        )

    response = client.post(f"/api/v1/inspections/{inspection.id}/complete", json={})
    assert response.status_code in (409, 422)
    assert b"require a photo" in response.data


def test_a_finding_for_another_inspections_item_is_not_found(
    client, db, org, inspection, template, property_record, inspector
):
    """The item id is checked against *this* inspection, not just the tenant."""
    other = client.post(
        "/api/v1/inspections",
        json={
            "kind": "routine",
            "property_id": property_record.id,
            "template_code": "MOVE_OUT",
        },
    )
    assert other.status_code == 201, other.get_json()
    stranger = client.get(f"/api/v1/inspections/{other.get_json()['id']}").get_json()["items"][0]

    response = client.post(
        f"/api/v1/inspections/{inspection.id}/findings",
        json={"item_id": stranger["id"], "result": "pass"},
    )
    assert response.status_code == 404


def test_a_negative_remedy_cost_is_rejected_at_the_schema(client, inspection, inspector):
    item_id = sorted(inspection.items, key=lambda i: i.sort_order)[0].id
    response = client.post(
        f"/api/v1/inspections/{inspection.id}/findings",
        json={"item_id": item_id, "result": "fail", "remedy_cost": "-1.00"},
    )
    assert response.status_code == 422


def test_the_list_filters_by_kind_and_unit(client, db, org, inspection, unit_record, inspector):
    body = client.get("/api/v1/inspections?kind=move_out").get_json()
    assert [row["id"] for row in body["data"]] == [inspection.id]

    assert client.get("/api/v1/inspections?kind=safety").get_json()["data"] == []
    assert [
        row["id"]
        for row in client.get(f"/api/v1/inspections?unit_id={unit_record.id}").get_json()["data"]
    ] == [inspection.id]


def test_a_scheduled_inspection_keeps_the_version_it_was_booked_at(
    client, db, org, scope, property_record, template, inspector
):
    """Editing the template afterwards must not rewrite what was asked."""
    from app.context import clear_context
    from app.models.maintenance import InspectionKind, InspectionTemplate

    booked = client.post(
        "/api/v1/inspections",
        json={
            "kind": "move_out",
            "property_id": property_record.id,
            "template_code": "MOVE_OUT",
        },
    ).get_json()["id"]

    token = _rebound(org)
    try:
        db.session.add(
            InspectionTemplate(
                org_id=org.id,
                code="MOVE_OUT",
                name="Move-out inspection",
                kind=InspectionKind.MOVE_OUT,
                version=2,
                sections=[{"section": "Everything", "items": [{"name": "One question only"}]}],
            )
        )
        db.session.commit()
    finally:
        clear_context(token)

    items = client.get(f"/api/v1/inspections/{booked}").get_json()["items"]
    assert len(items) == 3
    assert "One question only" not in [item["name"] for item in items]

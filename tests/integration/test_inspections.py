"""Inspections.

The three acceptance cases: an inspection renders against the template version
it actually used, a failed item raises work, and an offline capture replays
without duplicating anything.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.errors import BusinessRuleViolation, ValidationFailed
from app.models.maintenance import (
    Inspection,
    InspectionKind,
    InspectionResult,
    InspectionTemplate,
    ItemResult,
    Priority,
    WorkOrder,
)
from app.models.types import utcnow
from app.services.maintenance.inspections import (
    InspectionStatus,
    ItemFinding,
    checklist_as_performed,
    complete_inspection,
    current_template,
    raise_work_orders_from_findings,
    record_finding,
    replay_offline_capture,
    schedule_inspection,
    start_inspection,
)

pytestmark = pytest.mark.integration

SECTIONS = [
    {
        "section": "Kitchen",
        "items": [{"name": "Sink"}, {"name": "Extractor", "requires_photo": True}],
    },
    {"section": "Safety", "items": [{"name": "Smoke alarm", "requires_photo": True}]},
]


@pytest.fixture()
def template(db, org, scope):
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
    record = schedule_inspection(
        db.session,
        org_id=org.id,
        kind=InspectionKind.MOVE_OUT,
        property_id=property_record.id,
        unit_id=unit_record.id,
        template=template,
        scheduled_for=utcnow() + dt.timedelta(days=1),
    )
    db.session.commit()
    return record


@pytest.fixture()
def document_for(db, org, scope, tmp_path, monkeypatch):
    """Attach a photo to a checklist item, the way the field app would."""
    import io

    from app.models.documents import DocumentCategory
    from app.services.documents.service import upload_document

    # A one-pixel PNG: real magic bytes, so the content sniffer is satisfied.
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
        "00000049454e44ae426082"
    )

    def _attach(item_id: str):
        return upload_document(
            db.session,
            org_id=org.id,
            stream=io.BytesIO(png),
            filename=f"evidence-{item_id[:8]}.png",
            declared_content_type="image/png",
            category=DocumentCategory.INSPECTION,
            links=[("inspection_item", item_id, "evidence")],
        )

    return _attach


def _item(inspection, name):
    return next(item for item in inspection.items if item.name == name)


def _pass_everything(db, inspection, exclude=()):
    for item in inspection.items:
        if item.name in exclude:
            continue
        record_finding(
            db.session,
            inspection=inspection,
            finding=ItemFinding(item_id=item.id, result=ItemResult.PASS),
        )
    db.session.commit()


# ---------------------------------------------------------------- templates


def test_the_checklist_is_copied_onto_the_inspection(db, org, scope, inspection):
    names = sorted(item.name for item in inspection.items)
    assert names == ["Extractor", "Sink", "Smoke alarm"]
    assert _item(inspection, "Extractor").requires_photo is True
    assert _item(inspection, "Sink").requires_photo is False


def test_editing_the_template_afterwards_does_not_change_the_inspection(
    db, org, scope, template, inspection
):
    """The acceptance case: it renders against the version actually used."""
    template.sections = [{"section": "Kitchen", "items": [{"name": "Completely different"}]}]
    db.session.commit()
    db.session.refresh(inspection)

    performed = checklist_as_performed(inspection)
    captured = {item["name"] for section in performed for item in section["items"]}
    assert captured == {"Sink", "Extractor", "Smoke alarm"}


def test_the_inspection_records_which_template_version_it_ran(db, org, scope, inspection, template):
    assert inspection.template_id == template.id
    assert template.version == 1


def test_the_current_template_is_the_highest_active_version(db, org, scope, template):
    newer = InspectionTemplate(
        org_id=org.id,
        code="MOVE_OUT",
        name="Move-out inspection",
        kind=InspectionKind.MOVE_OUT,
        version=2,
        sections=SECTIONS,
    )
    db.session.add(newer)
    db.session.commit()

    assert current_template(db.session, org_id=org.id, code="MOVE_OUT").id == newer.id


# ---------------------------------------------------------------- lifecycle


def test_an_incomplete_inspection_cannot_be_signed_off(db, org, scope, inspection):
    start_inspection(db.session, inspection=inspection)
    _pass_everything(db, inspection, exclude={"Smoke alarm"})

    with pytest.raises(ValidationFailed) as exc:
        complete_inspection(db.session, inspection=inspection)
    assert "Smoke alarm" in str(exc.value)


def test_a_clean_inspection_passes(db, org, scope, inspection):
    start_inspection(db.session, inspection=inspection)
    _pass_everything(db, inspection)
    complete_inspection(db.session, inspection=inspection, inspector_signed=True)
    db.session.commit()

    assert inspection.status == InspectionStatus.COMPLETED
    assert inspection.result == InspectionResult.PASS
    assert inspection.inspector_signature_at is not None


def test_a_failed_item_makes_the_whole_inspection_fail(db, org, scope, inspection, document_for):
    start_inspection(db.session, inspection=inspection)
    _pass_everything(db, inspection, exclude={"Smoke alarm"})
    alarm = _item(inspection, "Smoke alarm")
    record_finding(
        db.session,
        inspection=inspection,
        finding=ItemFinding(item_id=alarm.id, result=ItemResult.FAIL, severity="critical"),
    )
    document_for(alarm.id)
    complete_inspection(db.session, inspection=inspection)
    db.session.commit()

    assert inspection.result == InspectionResult.FAIL


def test_a_completed_inspection_cannot_be_edited(db, org, scope, inspection):
    start_inspection(db.session, inspection=inspection)
    _pass_everything(db, inspection)
    complete_inspection(db.session, inspection=inspection)
    db.session.commit()

    with pytest.raises(BusinessRuleViolation):
        record_finding(
            db.session,
            inspection=inspection,
            finding=ItemFinding(item_id=inspection.items[0].id, result=ItemResult.FAIL),
        )


# ------------------------------------------------------------------ photos


def test_a_failed_item_that_demands_a_photo_blocks_sign_off(db, org, scope, inspection):
    """The deduction has to be defensible six months later."""
    start_inspection(db.session, inspection=inspection)
    _pass_everything(db, inspection, exclude={"Extractor"})
    record_finding(
        db.session,
        inspection=inspection,
        finding=ItemFinding(
            item_id=_item(inspection, "Extractor").id,
            result=ItemResult.FAIL,
            remedy_cost=Decimal("180.00"),
        ),
    )
    db.session.commit()

    with pytest.raises(ValidationFailed) as exc:
        complete_inspection(db.session, inspection=inspection)
    assert "Extractor" in str(exc.value)


def test_evidence_unblocks_sign_off(db, org, scope, inspection, document_for):
    start_inspection(db.session, inspection=inspection)
    _pass_everything(db, inspection, exclude={"Extractor"})
    extractor = _item(inspection, "Extractor")
    record_finding(
        db.session,
        inspection=inspection,
        finding=ItemFinding(item_id=extractor.id, result=ItemResult.FAIL),
    )
    document_for(extractor.id)

    complete_inspection(db.session, inspection=inspection)
    db.session.commit()
    assert inspection.status == InspectionStatus.COMPLETED


def test_a_passing_item_does_not_demand_a_photo(db, org, scope, inspection):
    """Photographing forty working light switches is how checklists get faked."""
    start_inspection(db.session, inspection=inspection)
    _pass_everything(db, inspection)
    complete_inspection(db.session, inspection=inspection)
    db.session.commit()
    assert inspection.result == InspectionResult.PASS


# ------------------------------------------------------------ raising work


def test_a_failed_item_raises_a_work_order(db, org, scope, inspection):
    start_inspection(db.session, inspection=inspection)
    sink = _item(inspection, "Sink")
    record_finding(
        db.session,
        inspection=inspection,
        finding=ItemFinding(
            item_id=sink.id,
            result=ItemResult.FAIL,
            severity="high",
            notes="Tap is seized.",
            remedy_cost=Decimal("120.00"),
            is_resident_responsible=True,
        ),
    )
    raised = raise_work_orders_from_findings(db.session, inspection=inspection)
    db.session.commit()

    assert len(raised) == 1
    order = raised[0]
    assert order.title == "Kitchen: Sink"
    assert order.description == "Tap is seized."
    assert order.priority == Priority.URGENT
    assert order.estimated_cost == Decimal("120.0000")
    assert order.is_resident_billable is True
    assert order.unit_id == inspection.unit_id
    assert sink.work_order_id == order.id


def test_raising_work_twice_raises_one_job(db, org, scope, inspection):
    """One broken window, one job - however many times the call is made."""
    start_inspection(db.session, inspection=inspection)
    record_finding(
        db.session,
        inspection=inspection,
        finding=ItemFinding(item_id=_item(inspection, "Sink").id, result=ItemResult.FAIL),
    )
    first = raise_work_orders_from_findings(db.session, inspection=inspection)
    db.session.commit()
    second = raise_work_orders_from_findings(db.session, inspection=inspection)
    db.session.commit()

    assert len(first) == 1
    assert second == []
    assert db.session.query(WorkOrder).count() == 1


def test_a_passing_item_raises_nothing(db, org, scope, inspection):
    start_inspection(db.session, inspection=inspection)
    _pass_everything(db, inspection)
    assert raise_work_orders_from_findings(db.session, inspection=inspection) == []


def test_a_critical_finding_becomes_an_emergency(db, org, scope, inspection):
    start_inspection(db.session, inspection=inspection)
    record_finding(
        db.session,
        inspection=inspection,
        finding=ItemFinding(
            item_id=_item(inspection, "Smoke alarm").id,
            result=ItemResult.FAIL,
            severity="critical",
        ),
    )
    raised = raise_work_orders_from_findings(db.session, inspection=inspection)
    db.session.commit()
    assert raised[0].priority == Priority.EMERGENCY


# --------------------------------------------------------------- offline


def _capture(inspection):
    return [
        ItemFinding(item_id=_item(inspection, "Sink").id, result=ItemResult.FAIL, notes="Seized."),
        ItemFinding(item_id=_item(inspection, "Extractor").id, result=ItemResult.PASS),
        ItemFinding(item_id=_item(inspection, "Smoke alarm").id, result=ItemResult.PASS),
    ]


def test_an_offline_capture_applies(db, org, scope, inspection):
    captured_at = utcnow() - dt.timedelta(hours=3)
    outcome = replay_offline_capture(
        db.session,
        inspection=inspection,
        findings=_capture(inspection),
        device_captured_at=captured_at,
    )
    db.session.commit()

    assert outcome.items_recorded == 3
    assert outcome.completed is True
    assert len(outcome.work_orders) == 1
    assert inspection.captured_offline is True
    assert inspection.device_captured_at == captured_at
    assert inspection.result == InspectionResult.FAIL


def test_replaying_the_same_capture_duplicates_nothing(db, org, scope, inspection):
    """The acceptance case. A flaky connection must not double the work."""
    captured_at = utcnow() - dt.timedelta(hours=3)
    findings = _capture(inspection)

    replay_offline_capture(
        db.session, inspection=inspection, findings=findings, device_captured_at=captured_at
    )
    db.session.commit()
    second = replay_offline_capture(
        db.session, inspection=inspection, findings=findings, device_captured_at=captured_at
    )
    db.session.commit()
    third = replay_offline_capture(
        db.session, inspection=inspection, findings=findings, device_captured_at=captured_at
    )
    db.session.commit()

    assert second.duplicates == 3
    assert third.duplicates == 3
    assert db.session.query(WorkOrder).count() == 1
    assert len(inspection.items) == 3
    assert db.session.query(Inspection).count() == 1


def test_the_device_timestamp_is_kept_not_the_server_one(db, org, scope, inspection):
    """The finding happened in the flat at 09:00, not at the coffee shop at 14:00."""
    captured_at = utcnow() - dt.timedelta(hours=5)
    replay_offline_capture(
        db.session,
        inspection=inspection,
        findings=_capture(inspection),
        device_captured_at=captured_at,
    )
    db.session.commit()

    assert inspection.started_at == captured_at
    assert inspection.completed_at == captured_at


# ------------------------------------------------------------- isolation


def test_inspections_do_not_cross_organizations(db, org, other_org, scope, inspection):
    from app.errors import NotFound

    with pytest.raises(NotFound):
        current_template(db.session, org_id=other_org.id, code="MOVE_OUT")

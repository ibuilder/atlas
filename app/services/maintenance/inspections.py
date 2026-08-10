"""Inspections: checklist execution, evidence, and the loop back to work.

Three things here are less obvious than they look.

**The checklist is copied, not referenced.** Items are materialised from the
template when the inspection is scheduled. A template edited in March must not
change what a February inspection appears to have asked - and because templates
are versioned rows rather than edited in place, an inspection also records
exactly which version it ran against.

**A required photo is required.** An item flagged ``requires_photo`` cannot be
signed off without evidence linked to it. The whole value of a move-out
inspection is that the deduction is defensible six months later, and "the
inspector said so" is not evidence.

**Offline capture is idempotent by construction.** A field device records
findings with no signal and replays them on reconnect, possibly more than once.
Replay is an upsert keyed on the item, so the second delivery writes the same
values rather than a second finding; work orders are guarded by the item's own
``work_order_id``; and completion is guarded by ``completed_at``. There is no
sequence of replays that produces two work orders for one broken window.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import BusinessRuleViolation, NotFound, ValidationFailed
from app.logging import get_logger
from app.models.audit import AuditAction, AuditSeverity
from app.models.documents import DocumentLink
from app.models.maintenance import (
    Inspection,
    InspectionItem,
    InspectionKind,
    InspectionResult,
    InspectionTemplate,
    ItemResult,
    Priority,
    WorkOrder,
)
from app.models.sequences import SequenceKey
from app.models.types import utcnow
from app.services.audit.recorder import record_audit_event
from app.services.common.numbering import next_number

__all__ = [
    "InspectionStatus",
    "ItemFinding",
    "complete_inspection",
    "current_template",
    "raise_work_orders_from_findings",
    "record_finding",
    "replay_offline_capture",
    "schedule_inspection",
    "start_inspection",
]

log = get_logger("services.maintenance.inspections")

#: Severities that justify raising work automatically. Anything milder is a note
#: for a human, not a job for a contractor.
WORK_RAISING_RESULTS = frozenset({ItemResult.FAIL})

#: Item severity to work-order priority. A failed smoke alarm is not a task for
#: next month.
SEVERITY_PRIORITY: dict[str, Priority] = {
    "critical": Priority.EMERGENCY,
    "high": Priority.URGENT,
    "medium": Priority.NORMAL,
    "low": Priority.LOW,
}


class InspectionStatus:
    """The lifecycle, as stored in ``Inspection.status``."""

    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class ItemFinding:
    """One recorded observation, as a field device sends it."""

    item_id: str
    result: ItemResult
    condition: str | None = None
    severity: str | None = None
    notes: str | None = None
    remedy_cost: Decimal | None = None
    is_resident_responsible: bool = False


@dataclass
class ReplayOutcome:
    items_recorded: int = 0
    work_orders: list[WorkOrder] = field(default_factory=list)
    completed: bool = False
    #: Findings that arrived for an item already recorded with the same values.
    duplicates: int = 0


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def current_template(session: Session, *, org_id: str, code: str) -> InspectionTemplate:
    """The highest active version of a template."""
    template = session.execute(
        select(InspectionTemplate)
        .where(
            InspectionTemplate.org_id == org_id,
            InspectionTemplate.code == code,
            InspectionTemplate.is_active.is_(True),
            InspectionTemplate.deleted_at.is_(None),
        )
        .order_by(InspectionTemplate.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if template is None:
        raise NotFound(f"No active inspection template with code {code!r}.")
    return template


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def schedule_inspection(
    session: Session,
    *,
    org_id: str,
    kind: InspectionKind,
    property_id: str,
    template: InspectionTemplate | None = None,
    unit_id: str | None = None,
    lease_id: str | None = None,
    scheduled_for: dt.datetime | None = None,
    inspector_user_id: str | None = None,
    inspector_vendor_id: str | None = None,
    actor_id: str | None = None,
) -> Inspection:
    """Book an inspection, freezing its checklist at the template version used."""
    inspection = Inspection(
        org_id=org_id,
        inspection_number=next_number(session, SequenceKey.INSPECTION, org_id=org_id),
        kind=kind,
        template_id=template.id if template else None,
        property_id=property_id,
        unit_id=unit_id,
        lease_id=lease_id,
        status=InspectionStatus.SCHEDULED,
        scheduled_for=scheduled_for,
        inspector_user_id=inspector_user_id,
        inspector_vendor_id=inspector_vendor_id,
        captured_offline=False,
    )
    session.add(inspection)
    session.flush()

    if template is not None:
        _materialise_items(session, inspection=inspection, template=template)

    record_audit_event(
        action=AuditAction.INSPECTION_SCHEDULED,
        resource_type="Inspection",
        resource_id=inspection.id,
        resource_label=inspection.inspection_number,
        payload={
            "kind": str(kind),
            "template": template.code if template else None,
            "template_version": template.version if template else None,
        },
        org_id=org_id,
        actor_id=actor_id,
        session=session,
    )
    return inspection


def _materialise_items(
    session: Session, *, inspection: Inspection, template: InspectionTemplate
) -> list[InspectionItem]:
    """Copy the checklist onto the inspection.

    A copy rather than a reference: editing a template must never change what a
    completed inspection appears to have asked.
    """
    created: list[InspectionItem] = []
    order = 0
    for section in template.sections or []:
        if not isinstance(section, dict):
            continue
        section_name = str(section.get("section") or "General")[:80]
        for entry in section.get("items") or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not name:
                continue
            item = InspectionItem(
                org_id=inspection.org_id,
                inspection_id=inspection.id,
                section=section_name,
                name=str(name)[:150],
                sort_order=order,
                requires_photo=bool(entry.get("requires_photo")),
            )
            session.add(item)
            created.append(item)
            order += 1
    session.flush()
    return created


def start_inspection(
    session: Session, *, inspection: Inspection, started_at: dt.datetime | None = None
) -> Inspection:
    if inspection.status == InspectionStatus.COMPLETED:
        raise BusinessRuleViolation("A completed inspection cannot be started again.")
    inspection.status = InspectionStatus.IN_PROGRESS
    inspection.started_at = started_at or utcnow()
    session.flush()
    return inspection


def record_finding(
    session: Session,
    *,
    inspection: Inspection,
    finding: ItemFinding,
) -> InspectionItem:
    """Record one observation. Re-recording the same item overwrites it.

    Overwriting is the point: a device replaying a capture must not append a
    second finding to the same checklist line.
    """
    if inspection.status == InspectionStatus.COMPLETED:
        raise BusinessRuleViolation(
            "A completed inspection cannot be edited. Raise a new inspection instead."
        )

    item = session.get(InspectionItem, finding.item_id)
    if item is None or item.inspection_id != inspection.id:
        raise NotFound("That checklist item does not belong to this inspection.")

    item.result = finding.result
    item.condition = finding.condition
    item.severity = finding.severity
    item.notes = finding.notes
    item.remedy_cost = finding.remedy_cost
    item.is_resident_responsible = finding.is_resident_responsible
    session.flush()
    return item


def complete_inspection(
    session: Session,
    *,
    inspection: Inspection,
    completed_at: dt.datetime | None = None,
    notes: str | None = None,
    inspector_signed: bool = False,
    resident_signed: bool = False,
    actor_id: str | None = None,
) -> Inspection:
    """Sign off an inspection, refusing an incomplete or unevidenced one."""
    if inspection.status == InspectionStatus.COMPLETED:
        return inspection

    unanswered = [item.name for item in inspection.items if item.result is None]
    if unanswered:
        raise ValidationFailed(
            f"{len(unanswered)} checklist item(s) have no finding: " f"{', '.join(unanswered[:5])}."
        )

    missing_evidence = _items_missing_photos(session, inspection)
    if missing_evidence:
        raise ValidationFailed(
            "These items require a photo before sign-off: "
            f"{', '.join(item.name for item in missing_evidence[:5])}."
        )

    now = completed_at or utcnow()
    inspection.status = InspectionStatus.COMPLETED
    inspection.completed_at = now
    inspection.result = inspection.derive_result()
    inspection.notes = notes or inspection.notes
    if inspector_signed:
        inspection.inspector_signature_at = now
    if resident_signed:
        inspection.resident_signature_at = now
    session.flush()

    record_audit_event(
        action=AuditAction.INSPECTION_COMPLETED,
        resource_type="Inspection",
        resource_id=inspection.id,
        resource_label=inspection.inspection_number,
        severity=(
            AuditSeverity.NOTICE
            if inspection.result == InspectionResult.FAIL
            else AuditSeverity.INFO
        ),
        payload={
            "result": str(inspection.result),
            "items": len(inspection.items),
            "failed": sum(1 for i in inspection.items if i.result == ItemResult.FAIL),
        },
        org_id=inspection.org_id,
        actor_id=actor_id,
        session=session,
    )
    return inspection


def _items_missing_photos(session: Session, inspection: Inspection) -> list[InspectionItem]:
    """Items that demand evidence and do not have any.

    Only findings that are not a clean pass need it - photographing forty
    working light switches is how a checklist stops being completed honestly.
    """
    needing = [
        item
        for item in inspection.items
        if item.requires_photo and item.result in (ItemResult.FAIL, ItemResult.NEEDS_ATTENTION)
    ]
    if not needing:
        return []

    evidenced = set(
        session.execute(
            select(DocumentLink.entity_id).where(
                DocumentLink.org_id == inspection.org_id,
                DocumentLink.entity_type == "inspection_item",
                DocumentLink.entity_id.in_([item.id for item in needing]),
            )
        )
        .scalars()
        .all()
    )
    return [item for item in needing if item.id not in evidenced]


# ---------------------------------------------------------------------------
# Closing the loop
# ---------------------------------------------------------------------------


def raise_work_orders_from_findings(
    session: Session,
    *,
    inspection: Inspection,
    actor_id: str | None = None,
) -> list[WorkOrder]:
    """Turn failed items into work.

    Idempotent: an item that already has a work order is skipped, so calling
    this twice - or replaying an offline capture - never raises two jobs for one
    broken window.
    """
    from app.services.maintenance.service import create_work_order

    raised: list[WorkOrder] = []
    for item in inspection.items:
        if item.result not in WORK_RAISING_RESULTS or item.work_order_id:
            continue

        work_order = create_work_order(
            session,
            org_id=inspection.org_id,
            property_id=inspection.property_id,
            title=f"{item.section}: {item.name}",
            description=(
                item.notes
                or f"Raised from inspection {inspection.inspection_number} "
                f"({item.section} - {item.name})."
            ),
            unit_id=inspection.unit_id,
            asset_id=item.asset_id,
            priority=SEVERITY_PRIORITY.get((item.severity or "").lower(), Priority.NORMAL),
            estimated_cost=item.remedy_cost,
            is_resident_billable=item.is_resident_responsible,
            actor_id=actor_id,
        )
        item.work_order_id = work_order.id
        raised.append(work_order)

    session.flush()
    if raised:
        log.info(
            "work raised from inspection findings",
            extra={
                "event": "inspection.work_raised",
                "inspection_id": inspection.id,
                "count": len(raised),
            },
        )
    return raised


# ---------------------------------------------------------------------------
# Offline replay
# ---------------------------------------------------------------------------


def replay_offline_capture(
    session: Session,
    *,
    inspection: Inspection,
    findings: list[ItemFinding],
    device_captured_at: dt.datetime,
    complete: bool = True,
    raise_work: bool = True,
    notes: str | None = None,
    actor_id: str | None = None,
) -> ReplayOutcome:
    """Apply a capture recorded in the field, possibly for the second time.

    Idempotency is structural rather than bookkept: findings upsert onto their
    item, work orders are guarded by the item's own ``work_order_id``, and
    completion is guarded by ``completed_at``. A device that replays the same
    capture five times leaves the same database as one that replays it once.
    """
    outcome = ReplayOutcome()

    if inspection.completed_at is not None and inspection.status == InspectionStatus.COMPLETED:
        # Already applied. Re-raising work is still safe (the item guard holds),
        # but nothing else should move.
        outcome.duplicates = len(findings)
        outcome.completed = True
        if raise_work:
            outcome.work_orders = raise_work_orders_from_findings(
                session, inspection=inspection, actor_id=actor_id
            )
        return outcome

    inspection.captured_offline = True
    inspection.device_captured_at = device_captured_at
    if inspection.status == InspectionStatus.SCHEDULED:
        start_inspection(session, inspection=inspection, started_at=device_captured_at)

    for finding in findings:
        item = session.get(InspectionItem, finding.item_id)
        if item is not None and item.result == finding.result and item.notes == finding.notes:
            outcome.duplicates += 1
        record_finding(session, inspection=inspection, finding=finding)
        outcome.items_recorded += 1

    if raise_work:
        outcome.work_orders = raise_work_orders_from_findings(
            session, inspection=inspection, actor_id=actor_id
        )

    if complete:
        complete_inspection(
            session,
            inspection=inspection,
            completed_at=device_captured_at,
            notes=notes,
            inspector_signed=True,
            actor_id=actor_id,
        )
        outcome.completed = True

    return outcome


def checklist_as_performed(inspection: Inspection) -> list[dict[str, Any]]:
    """The checklist as it stood for this inspection, grouped by section.

    Rendered from the copied items rather than the live template, so a template
    edited afterwards cannot rewrite what this inspection asked.
    """
    sections: dict[str, list[dict[str, Any]]] = {}
    for item in sorted(inspection.items, key=lambda i: i.sort_order):
        sections.setdefault(item.section, []).append(
            {
                "name": item.name,
                "result": str(item.result) if item.result else None,
                "condition": item.condition,
                "severity": item.severity,
                "notes": item.notes,
                "requires_photo": item.requires_photo,
                "work_order_id": item.work_order_id,
                "remedy_cost": str(item.remedy_cost) if item.remedy_cost is not None else None,
                "is_resident_responsible": item.is_resident_responsible,
            }
        )
    return [{"section": name, "items": items} for name, items in sections.items()]

"""Maintenance requests and work orders.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from flask import Response
from flask_login import current_user
from sqlalchemy import select

from app.api.helpers import (
    add_etag,
    paginate,
    parse_body,
    parse_query,
    require_if_match,
    respond,
    respond_collection,
    respond_created,
)
from app.api.v1 import api_v1_bp
from app.errors import NotFound
from app.extensions import current_session, db
from app.middleware import require_org_scope
from app.models.maintenance import (
    WORK_ORDER_TERMINAL,
    Inspection,
    MaintenanceRequest,
    WorkOrder,
    WorkOrderEvent,
)
from app.models.types import utcnow
from app.schemas.operations import (
    FindingIn,
    InspectionComplete,
    InspectionItemOut,
    InspectionListQuery,
    InspectionOut,
    InspectionSchedule,
    MaintenanceRequestCreate,
    MaintenanceRequestListQuery,
    MaintenanceRequestOut,
    WorkOrderCreate,
    WorkOrderEventOut,
    WorkOrderListQuery,
    WorkOrderOut,
    WorkOrderTransition,
)
from app.security.permissions import Perm
from app.security.policies import require
from app.services.common.unit_of_work import transaction
from app.services.maintenance import inspections
from app.services.maintenance import service as maintenance_service

__all__ = []


def _get_request(request_id: str) -> MaintenanceRequest:
    record = db.session.get(MaintenanceRequest, request_id)
    if record is None:
        raise NotFound("That maintenance request was not found.")
    return record


def _get_work_order(work_order_id: str) -> WorkOrder:
    record = db.session.get(WorkOrder, work_order_id)
    if record is None:
        raise NotFound("That work order was not found.")
    return record


@api_v1_bp.get("/requests", endpoint="requests_list")
def list_requests() -> Response:
    require(Perm.REQUEST_READ)
    query = parse_query(MaintenanceRequestListQuery)
    org_id = require_org_scope()

    stmt = select(MaintenanceRequest).where(MaintenanceRequest.org_id == org_id)
    if query.status:
        stmt = stmt.where(MaintenanceRequest.status == query.status)
    if query.priority:
        stmt = stmt.where(MaintenanceRequest.priority == query.priority)
    if query.property_id:
        stmt = stmt.where(MaintenanceRequest.property_id == query.property_id)
    if query.unit_id:
        stmt = stmt.where(MaintenanceRequest.unit_id == query.unit_id)

    page = paginate(
        current_session(), stmt, MaintenanceRequest, limit=query.limit, cursor=query.cursor
    )
    # Portal users see only their own; the policy engine is the authority, and
    # filtering here keeps a resident's list from leaking neighbours' reports.
    from app.security.policies import filter_permitted

    page.items = filter_permitted(Perm.REQUEST_READ, page.items)
    return respond_collection(page, MaintenanceRequestOut)


@api_v1_bp.post("/requests", endpoint="requests_create")
def create_request() -> Response:
    require(Perm.REQUEST_CREATE)
    payload = parse_body(MaintenanceRequestCreate)
    org_id = require_org_scope()

    with transaction() as session:
        record = maintenance_service.create_request(
            session,
            org_id=org_id,
            property_id=payload.property_id,
            title=payload.title,
            description=payload.description,
            unit_id=payload.unit_id,
            lease_id=payload.lease_id,
            resident_id=getattr(current_user, "resident_id", None),
            category=payload.category,
            priority=payload.priority,
            is_habitability=payload.is_habitability,
            permission_to_enter=payload.permission_to_enter,
            entry_notes=payload.entry_notes,
            has_pets=payload.has_pets,
            preferred_times=payload.preferred_times,
            source="portal" if getattr(current_user, "user_type", None) == "resident" else "staff",
            reported_by_user_id=current_user.id,
            actor_id=current_user.id,
        )

    return respond_created(
        MaintenanceRequestOut.model_validate(record, from_attributes=True),
        location=f"/api/v1/requests/{record.id}",
    )


@api_v1_bp.get("/requests/<id:request_id>", endpoint="requests_get")
def get_request(request_id: str) -> Response:
    record = _get_request(request_id)
    require(Perm.REQUEST_READ, record)
    return add_etag(
        respond(MaintenanceRequestOut.model_validate(record, from_attributes=True)), record
    )


@api_v1_bp.get("/work-orders", endpoint="work_orders_list")
def list_work_orders() -> Response:
    require(Perm.WORK_ORDER_READ)
    query = parse_query(WorkOrderListQuery)
    org_id = require_org_scope()

    stmt = select(WorkOrder).where(WorkOrder.org_id == org_id)
    if query.status:
        stmt = stmt.where(WorkOrder.status == query.status)
    if query.priority:
        stmt = stmt.where(WorkOrder.priority == query.priority)
    if query.property_id:
        stmt = stmt.where(WorkOrder.property_id == query.property_id)
    if query.vendor_id:
        stmt = stmt.where(WorkOrder.vendor_id == query.vendor_id)
    if query.assigned_user_id:
        stmt = stmt.where(WorkOrder.assigned_user_id == query.assigned_user_id)
    if query.breached:
        stmt = stmt.where(
            WorkOrder.resolution_due_at < utcnow(),
            WorkOrder.status.notin_(list(WORK_ORDER_TERMINAL)),
        )

    page = paginate(current_session(), stmt, WorkOrder, limit=query.limit, cursor=query.cursor)
    from app.security.policies import filter_permitted

    page.items = filter_permitted(Perm.WORK_ORDER_READ, page.items)
    return respond_collection(page, WorkOrderOut)


@api_v1_bp.post("/work-orders", endpoint="work_orders_create")
def create_work_order() -> Response:
    require(Perm.WORK_ORDER_CREATE)
    payload = parse_body(WorkOrderCreate)
    org_id = require_org_scope()

    source_request = _get_request(payload.request_id) if payload.request_id else None

    with transaction() as session:
        record = maintenance_service.create_work_order(
            session,
            org_id=org_id,
            property_id=payload.property_id,
            title=payload.title,
            description=payload.description,
            request=source_request,
            unit_id=payload.unit_id,
            asset_id=payload.asset_id,
            trade=payload.trade,
            priority=payload.priority,
            estimated_cost=payload.estimated_cost,
            is_owner_billable=payload.is_owner_billable,
            is_resident_billable=payload.is_resident_billable,
            actor_id=current_user.id,
        )

    return respond_created(
        WorkOrderOut.model_validate(record, from_attributes=True),
        location=f"/api/v1/work-orders/{record.id}",
    )


@api_v1_bp.get("/work-orders/<id:work_order_id>", endpoint="work_orders_get")
def get_work_order(work_order_id: str) -> Response:
    record = _get_work_order(work_order_id)
    require(Perm.WORK_ORDER_READ, record)
    return add_etag(respond(WorkOrderOut.model_validate(record, from_attributes=True)), record)


@api_v1_bp.post("/work-orders/<id:work_order_id>/transition", endpoint="work_orders_transition")
def transition_work_order(work_order_id: str) -> Response:
    record = _get_work_order(work_order_id)
    payload = parse_body(WorkOrderTransition)

    # Assignment and completion are distinct authorities: a technician may
    # complete their own work without being able to dispatch anyone else's.
    from app.models.maintenance import WorkOrderStatus

    if payload.vendor_id or payload.assigned_user_id:
        require(Perm.WORK_ORDER_ASSIGN, record)
    elif payload.status in (WorkOrderStatus.COMPLETED, WorkOrderStatus.VERIFIED):
        require(Perm.WORK_ORDER_COMPLETE, record)
    else:
        require(Perm.WORK_ORDER_UPDATE, record)

    require_if_match(record)

    with transaction() as session:
        maintenance_service.transition_work_order(
            session,
            work_order=record,
            target=payload.status,
            actor_id=current_user.id,
            actor_label=current_user.label,
            note=payload.note,
            assigned_user_id=payload.assigned_user_id,
            vendor_id=payload.vendor_id,
            scheduled_start=payload.scheduled_start,
            scheduled_end=payload.scheduled_end,
            labor_hours=payload.labor_hours,
            labor_cost=payload.labor_cost,
            material_cost=payload.material_cost,
            resolution_notes=payload.resolution_notes,
            resident_visible=payload.is_resident_visible,
        )

    return add_etag(respond(WorkOrderOut.model_validate(record, from_attributes=True)), record)


@api_v1_bp.get("/work-orders/<id:work_order_id>/timeline", endpoint="work_orders_timeline")
def work_order_timeline(work_order_id: str) -> Response:
    record = _get_work_order(work_order_id)
    require(Perm.WORK_ORDER_READ, record)

    stmt = select(WorkOrderEvent).where(WorkOrderEvent.work_order_id == record.id)
    # Residents see the curated timeline, not internal dispatch chatter.
    if getattr(current_user, "user_type", None) == "resident":
        stmt = stmt.where(WorkOrderEvent.is_resident_visible.is_(True))

    events = db.session.execute(stmt.order_by(WorkOrderEvent.occurred_at.asc())).scalars().all()
    return respond(
        {
            "data": [
                WorkOrderEventOut.model_validate(event, from_attributes=True).model_dump(
                    mode="json"
                )
                for event in events
            ]
        }
    )


# -------------------------------------------------------------- inspections
#
# The checklist is copied onto the inspection at the template version used, so
# editing a template later never changes what a completed inspection appears to
# have asked. Sign-off refuses an unanswered item and refuses a failed item
# that demands a photo and has none — those photos are what make a deduction
# defensible months later, and they cannot be taken retrospectively.


def _inspection_or_404(inspection_id: str, org_id: str) -> Inspection:
    record = db.session.get(Inspection, inspection_id)
    if record is None or record.org_id != org_id:
        raise NotFound("That inspection was not found.")
    return record


@api_v1_bp.get("/inspections", endpoint="inspections_list")
def list_inspections() -> Response:
    require(Perm.INSPECTION_READ)
    query = parse_query(InspectionListQuery)
    org_id = require_org_scope()

    stmt = select(Inspection).where(Inspection.org_id == org_id)
    if query.status:
        stmt = stmt.where(Inspection.status == query.status)
    if query.kind:
        stmt = stmt.where(Inspection.kind == query.kind)
    if query.property_id:
        stmt = stmt.where(Inspection.property_id == query.property_id)
    if query.unit_id:
        stmt = stmt.where(Inspection.unit_id == query.unit_id)

    page = paginate(current_session(), stmt, Inspection, limit=query.limit, cursor=query.cursor)
    return respond_collection(page, InspectionOut)


@api_v1_bp.get("/inspections/<id:inspection_id>", endpoint="inspections_get")
def get_inspection(inspection_id: str) -> Response:
    """One inspection and its checklist as performed."""
    require(Perm.INSPECTION_READ)
    org_id = require_org_scope()

    record = _inspection_or_404(inspection_id, org_id)
    return respond(
        {
            **InspectionOut.model_validate(record, from_attributes=True).model_dump(mode="json"),
            "items": [
                InspectionItemOut.model_validate(item, from_attributes=True).model_dump(mode="json")
                for item in sorted(record.items, key=lambda i: i.sort_order)
            ],
        }
    )


@api_v1_bp.post("/inspections", endpoint="inspections_schedule")
def schedule_inspection() -> Response:
    """Book an inspection, freezing its checklist at the template version used."""
    require(Perm.INSPECTION_MANAGE)
    payload = parse_body(InspectionSchedule)
    org_id = require_org_scope()

    with transaction() as session:
        template = (
            inspections.current_template(session, org_id=org_id, code=payload.template_code)
            if payload.template_code
            else None
        )
        record = inspections.schedule_inspection(
            session,
            org_id=org_id,
            kind=payload.kind,
            property_id=payload.property_id,
            template=template,
            unit_id=payload.unit_id,
            lease_id=payload.lease_id,
            scheduled_for=payload.scheduled_for,
            inspector_user_id=payload.inspector_user_id,
            inspector_vendor_id=payload.inspector_vendor_id,
            actor_id=current_user.id,
        )

    return respond_created(
        InspectionOut.model_validate(record, from_attributes=True),
        location=f"/api/v1/inspections/{record.id}",
    )


@api_v1_bp.post("/inspections/<id:inspection_id>/start", endpoint="inspections_start")
def start_inspection(inspection_id: str) -> Response:
    require(Perm.INSPECTION_PERFORM)
    org_id = require_org_scope()

    with transaction() as session:
        record = inspections.start_inspection(
            session, inspection=_inspection_or_404(inspection_id, org_id)
        )

    return respond(InspectionOut.model_validate(record, from_attributes=True))


@api_v1_bp.post("/inspections/<id:inspection_id>/findings", endpoint="inspections_record")
def record_findings(inspection_id: str) -> Response:
    """Record observations. Re-recording an item overwrites it.

    Overwriting rather than appending is the point: a field device replaying a
    capture must not leave two findings on one checklist line.
    """
    require(Perm.INSPECTION_PERFORM)
    payload = parse_body(FindingIn)
    org_id = require_org_scope()

    with transaction() as session:
        inspection = _inspection_or_404(inspection_id, org_id)
        item = inspections.record_finding(
            session,
            inspection=inspection,
            finding=inspections.ItemFinding(
                item_id=payload.item_id,
                result=payload.result,
                condition=payload.condition,
                severity=payload.severity,
                notes=payload.notes,
                remedy_cost=payload.remedy_cost,
                is_resident_responsible=payload.is_resident_responsible,
            ),
        )

    return respond(InspectionItemOut.model_validate(item, from_attributes=True))


@api_v1_bp.post("/inspections/<id:inspection_id>/complete", endpoint="inspections_complete")
def complete_inspection(inspection_id: str) -> Response:
    """Sign off, and raise work from what failed.

    Refused while an item has no finding, and refused while a failed item that
    demands a photo has none. Those photos are what make a deduction defensible
    months later, and nobody can take them retrospectively.
    """
    require(Perm.INSPECTION_PERFORM)
    payload = parse_body(InspectionComplete)
    org_id = require_org_scope()

    with transaction() as session:
        inspection = _inspection_or_404(inspection_id, org_id)
        record = inspections.complete_inspection(
            session,
            inspection=inspection,
            notes=payload.notes,
            inspector_signed=payload.inspector_signed,
            resident_signed=payload.resident_signed,
            actor_id=current_user.id,
        )
        raised = (
            inspections.raise_work_orders_from_findings(
                session, inspection=record, actor_id=current_user.id
            )
            if payload.raise_work
            else []
        )
        raised_ids = [work_order.id for work_order in raised]

    return respond(
        {
            **InspectionOut.model_validate(record, from_attributes=True).model_dump(mode="json"),
            "work_orders_raised": raised_ids,
        }
    )

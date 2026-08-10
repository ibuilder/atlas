"""Maintenance: intake, triage, and the work-order lifecycle.

Status changes go through :func:`transition_work_order`, which validates against
the declared state machine and writes a timeline event for every move. A status
column that any caller can assign freely is how a work order ends up "completed"
without ever having been "in progress", and how nobody can explain a nine-day
repair afterwards.

SLA deadlines are computed once, at creation, from the policy in force then -
not recomputed on read. Otherwise editing a policy silently rewrites whether
last month's work orders breached.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import BusinessRuleViolation, ValidationFailed
from app.logging import get_logger
from app.models.audit import AuditAction, AuditSeverity
from app.models.maintenance import (
    DEFAULT_SLA_MINUTES,
    WORK_ORDER_TERMINAL,
    MaintenanceRequest,
    Priority,
    RequestStatus,
    SLAPolicy,
    WorkOrder,
    WorkOrderEvent,
    WorkOrderStatus,
)
from app.models.sequences import SequenceKey
from app.models.types import utcnow
from app.models.vendor import Vendor
from app.observability import SLA_BREACHES
from app.services.audit.recorder import record_audit_event
from app.services.common.numbering import next_number

__all__ = [
    "create_request",
    "create_work_order",
    "resolve_sla",
    "transition_work_order",
    "triage_request",
]

log = get_logger("services.maintenance")

#: Habitability keywords that force emergency handling regardless of what the
#: reporter selected. People under-report urgency; the statute does not care.
HABITABILITY_SIGNALS = (
    "no heat",
    "no hot water",
    "no water",
    "gas leak",
    "smell gas",
    "carbon monoxide",
    "sewage",
    "flooding",
    "no power",
    "no electricity",
    "cannot lock",
    "broken lock",
    "smoke detector",
    "burst pipe",
)


def create_request(
    session: Session,
    *,
    org_id: str,
    property_id: str,
    title: str,
    description: str,
    unit_id: str | None = None,
    lease_id: str | None = None,
    resident_id: str | None = None,
    category: str = "general",
    priority: Priority = Priority.NORMAL,
    is_habitability: bool = False,
    permission_to_enter: bool = False,
    entry_notes: str | None = None,
    has_pets: bool = False,
    preferred_times: list[str] | None = None,
    source: str = "portal",
    reported_by_user_id: str | None = None,
    actor_id: str | None = None,
) -> MaintenanceRequest:
    """Record an inbound maintenance report."""
    detected = is_habitability or _looks_like_habitability(f"{title} {description}")
    if detected and not is_habitability:
        log.info(
            "habitability escalation applied from request text",
            extra={"event": "maintenance.habitability_detected", "property_id": property_id},
        )

    request = MaintenanceRequest(
        org_id=org_id,
        request_number=next_number(session, SequenceKey.MAINTENANCE_REQUEST, org_id=org_id),
        property_id=property_id,
        unit_id=unit_id,
        lease_id=lease_id,
        resident_id=resident_id,
        title=title,
        description=description,
        category=category,
        priority=Priority.EMERGENCY if detected else priority,
        is_habitability=detected,
        status=RequestStatus.NEW,
        source=source,
        reported_by_user_id=reported_by_user_id,
        permission_to_enter=permission_to_enter,
        entry_notes=entry_notes,
        has_pets=has_pets,
        preferred_times=preferred_times or [],
    )
    session.add(request)
    session.flush()

    record_audit_event(
        action=AuditAction.REQUEST_CREATED,
        resource_type="MaintenanceRequest",
        resource_id=request.id,
        resource_label=request.request_number,
        payload={
            "priority": str(request.priority),
            "habitability": detected,
            "source": source,
        },
        severity=AuditSeverity.WARNING if detected else AuditSeverity.INFO,
        org_id=org_id,
        session=session,
    )
    return request


def _looks_like_habitability(text: str) -> bool:
    lowered = text.lower()
    return any(signal in lowered for signal in HABITABILITY_SIGNALS)


def triage_request(
    session: Session,
    *,
    request: MaintenanceRequest,
    priority: Priority | None = None,
    trade: str | None = None,
    category: str | None = None,
    actor_id: str | None = None,
) -> MaintenanceRequest:
    """Categorise and prioritise an incoming request."""
    if request.status not in (RequestStatus.NEW, RequestStatus.TRIAGED):
        raise BusinessRuleViolation(f"A {request.status} request cannot be triaged.")

    if priority is not None:
        # Habitability floors the priority. Triage can raise urgency, never
        # lower it below the statutory line.
        if request.is_habitability and priority != Priority.EMERGENCY:
            raise BusinessRuleViolation(
                "A habitability request cannot be de-prioritised below emergency."
            )
        request.priority = priority
    if trade is not None:
        request.trade = trade
    if category is not None:
        request.category = category

    request.status = RequestStatus.TRIAGED
    request.triaged_at = utcnow()
    request.triaged_by_id = actor_id
    session.flush()
    return request


def resolve_sla(
    session: Session, *, org_id: str, priority: Priority, property_id: str | None = None
) -> tuple[int, int]:
    """Response and resolution targets in minutes.

    A property-specific policy wins over an organization-wide one; absent both,
    the built-in defaults apply.
    """
    policies = (
        session.execute(
            select(SLAPolicy).where(
                SLAPolicy.org_id == org_id,
                SLAPolicy.priority == priority,
                SLAPolicy.is_active.is_(True),
            )
        )
        .scalars()
        .all()
    )

    specific = next((p for p in policies if p.property_id == property_id), None)
    organization_wide = next((p for p in policies if p.property_id is None), None)
    policy = specific or organization_wide

    if policy is not None:
        return policy.response_minutes, policy.resolution_minutes
    return DEFAULT_SLA_MINUTES[priority]


def create_work_order(
    session: Session,
    *,
    org_id: str,
    property_id: str,
    title: str,
    description: str,
    request: MaintenanceRequest | None = None,
    unit_id: str | None = None,
    asset_id: str | None = None,
    trade: str | None = None,
    priority: Priority = Priority.NORMAL,
    estimated_cost: Decimal | None = None,
    is_owner_billable: bool = True,
    is_resident_billable: bool = False,
    open_immediately: bool = True,
    actor_id: str | None = None,
) -> WorkOrder:
    """Raise a work order, stamping its SLA deadlines."""
    effective_priority = request.effective_priority() if request else priority
    response_minutes, resolution_minutes = resolve_sla(
        session, org_id=org_id, priority=effective_priority, property_id=property_id
    )
    now = utcnow()

    work_order = WorkOrder(
        org_id=org_id,
        work_order_number=next_number(session, SequenceKey.WORK_ORDER, org_id=org_id),
        request_id=request.id if request else None,
        property_id=property_id,
        unit_id=unit_id or (request.unit_id if request else None),
        asset_id=asset_id,
        title=title,
        description=description,
        trade=trade or (request.trade if request else None),
        priority=effective_priority,
        status=WorkOrderStatus.OPEN if open_immediately else WorkOrderStatus.DRAFT,
        estimated_cost=estimated_cost,
        is_owner_billable=is_owner_billable,
        is_resident_billable=is_resident_billable,
        permission_to_enter=request.permission_to_enter if request else False,
        response_due_at=now + dt.timedelta(minutes=response_minutes),
        resolution_due_at=now + dt.timedelta(minutes=resolution_minutes),
    )
    session.add(work_order)
    session.flush()

    _append_event(
        session,
        work_order,
        event_type="created",
        to_status=work_order.status,
        actor_id=actor_id,
        note=f"Work order raised with a {resolution_minutes // 60}h resolution target.",
        resident_visible=True,
    )

    if request is not None and request.status in (RequestStatus.NEW, RequestStatus.TRIAGED):
        request.status = RequestStatus.IN_PROGRESS

    record_audit_event(
        action=AuditAction.WORK_ORDER_CREATED,
        resource_type="WorkOrder",
        resource_id=work_order.id,
        resource_label=work_order.work_order_number,
        payload={
            "priority": str(effective_priority),
            "trade": work_order.trade,
            "resolution_due_at": work_order.resolution_due_at.isoformat(),
        },
        org_id=org_id,
        session=session,
    )
    return work_order


def transition_work_order(
    session: Session,
    *,
    work_order: WorkOrder,
    target: WorkOrderStatus,
    actor_id: str | None = None,
    actor_label: str = "System",
    note: str | None = None,
    assigned_user_id: str | None = None,
    vendor_id: str | None = None,
    scheduled_start: dt.datetime | None = None,
    scheduled_end: dt.datetime | None = None,
    labor_hours: Decimal | None = None,
    labor_cost: Decimal | None = None,
    material_cost: Decimal | None = None,
    resolution_notes: str | None = None,
    resident_visible: bool = False,
) -> WorkOrder:
    """Move a work order through its lifecycle, recording the timeline."""
    if target == work_order.status:
        return work_order

    if not work_order.can_transition_to(target):
        raise BusinessRuleViolation(
            f"A work order cannot move from {work_order.status} to {target}."
        )

    previous = work_order.status
    now = utcnow()

    if vendor_id is not None:
        _assert_vendor_dispatchable(session, work_order.org_id, vendor_id)
        work_order.vendor_id = vendor_id
    if assigned_user_id is not None:
        work_order.assigned_user_id = assigned_user_id
    if vendor_id is not None or assigned_user_id is not None:
        work_order.assigned_at = work_order.assigned_at or now

    if scheduled_start is not None:
        work_order.scheduled_start = scheduled_start
    if scheduled_end is not None:
        if scheduled_start and scheduled_end < scheduled_start:
            raise ValidationFailed("The scheduled end cannot precede the scheduled start.")
        work_order.scheduled_end = scheduled_end
    if labor_hours is not None:
        work_order.labor_hours = labor_hours
    if labor_cost is not None:
        work_order.labor_cost = labor_cost
    if material_cost is not None:
        work_order.material_cost = material_cost
    if resolution_notes is not None:
        work_order.resolution_notes = resolution_notes

    # First substantive touch stops the response clock.
    if work_order.first_response_at is None and target in (
        WorkOrderStatus.ASSIGNED,
        WorkOrderStatus.SCHEDULED,
        WorkOrderStatus.IN_PROGRESS,
    ):
        work_order.first_response_at = now

    if target == WorkOrderStatus.IN_PROGRESS and work_order.started_at is None:
        work_order.started_at = now

    if target == WorkOrderStatus.COMPLETED:
        work_order.completed_at = now
        work_order.recalculate_cost()
        if work_order.resolution_due_at and now > work_order.resolution_due_at:
            work_order.sla_breached_at = work_order.sla_breached_at or now
            SLA_BREACHES.labels(str(work_order.priority)).inc()
            record_audit_event(
                action=AuditAction.WORK_ORDER_SLA_BREACHED,
                resource_type="WorkOrder",
                resource_id=work_order.id,
                resource_label=work_order.work_order_number,
                payload={
                    "due_at": work_order.resolution_due_at.isoformat(),
                    "completed_at": now.isoformat(),
                    "overdue_minutes": int(
                        (now - work_order.resolution_due_at).total_seconds() // 60
                    ),
                },
                severity=AuditSeverity.WARNING,
                org_id=work_order.org_id,
                session=session,
            )

    if target == WorkOrderStatus.VERIFIED:
        work_order.verified_at = now
        work_order.verified_by_id = actor_id

    work_order.status = target
    session.flush()

    _append_event(
        session,
        work_order,
        event_type="status_changed",
        from_status=previous,
        to_status=target,
        actor_id=actor_id,
        actor_label=actor_label,
        note=note,
        resident_visible=resident_visible
        or target in (WorkOrderStatus.SCHEDULED, WorkOrderStatus.COMPLETED),
    )

    if target in WORK_ORDER_TERMINAL:
        _close_originating_request(session, work_order)

    action = {
        WorkOrderStatus.ASSIGNED: AuditAction.WORK_ORDER_ASSIGNED,
        WorkOrderStatus.COMPLETED: AuditAction.WORK_ORDER_COMPLETED,
    }.get(target)
    if action:
        record_audit_event(
            action=action,
            resource_type="WorkOrder",
            resource_id=work_order.id,
            resource_label=work_order.work_order_number,
            payload={
                "from": str(previous),
                "to": str(target),
                "vendor_id": work_order.vendor_id,
                "total_cost": str(work_order.total_cost),
            },
            org_id=work_order.org_id,
            session=session,
        )

    return work_order


def _assert_vendor_dispatchable(session: Session, org_id: str, vendor_id: str) -> None:
    """Refuse to dispatch to a vendor whose compliance has lapsed.

    Sending an uninsured contractor into an occupied unit is the exposure the
    compliance tracking exists to prevent; enforcing it at assignment time is
    the only moment it helps.
    """
    vendor = session.get(Vendor, vendor_id)
    if vendor is None or vendor.org_id != org_id:
        raise ValidationFailed("The selected vendor was not found.")
    if not vendor.is_dispatchable:
        raise BusinessRuleViolation(
            f"Vendor {vendor.name} is not dispatchable: status {vendor.status}, "
            f"compliance {vendor.compliance_status}."
        )


def _close_originating_request(session: Session, work_order: WorkOrder) -> None:
    """Close the request once every work order it spawned is finished."""
    if not work_order.request_id:
        return
    request = session.get(MaintenanceRequest, work_order.request_id)
    if request is None or request.status in (RequestStatus.CLOSED, RequestStatus.CANCELLED):
        return

    siblings = (
        session.execute(select(WorkOrder).where(WorkOrder.request_id == request.id)).scalars().all()
    )
    if all(sibling.status in WORK_ORDER_TERMINAL for sibling in siblings):
        request.status = RequestStatus.RESOLVED
        request.resolved_at = utcnow()
        session.flush()


def _append_event(
    session: Session,
    work_order: WorkOrder,
    *,
    event_type: str,
    from_status: WorkOrderStatus | None = None,
    to_status: WorkOrderStatus | None = None,
    actor_id: str | None = None,
    actor_label: str = "System",
    note: str | None = None,
    resident_visible: bool = False,
) -> WorkOrderEvent:
    event = WorkOrderEvent(
        org_id=work_order.org_id,
        work_order_id=work_order.id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        actor_id=actor_id,
        actor_label=actor_label,
        note=note,
        is_resident_visible=resident_visible,
    )
    session.add(event)
    session.flush()
    return event


def overdue_work_orders(session: Session, *, org_id: str) -> list[WorkOrder]:
    """Open work orders past their resolution deadline."""
    now = utcnow()
    return list(
        session.execute(
            select(WorkOrder)
            .where(
                WorkOrder.org_id == org_id,
                WorkOrder.resolution_due_at.is_not(None),
                WorkOrder.resolution_due_at < now,
                WorkOrder.status.notin_(list(WORK_ORDER_TERMINAL)),
            )
            .order_by(WorkOrder.resolution_due_at.asc())
        ).scalars()
    )


__all__ += ["overdue_work_orders"]

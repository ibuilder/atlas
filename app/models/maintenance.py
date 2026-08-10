"""Maintenance: intake, triage, work orders, inspections, preventive schedules.

A resident's report and the work that resolves it are separate records. One
report can spawn three work orders across three trades; three reports of the
same leak collapse into one. Fusing them - the shortcut almost every simple
system takes - makes both of those ordinary situations unrepresentable.

Habitability is a first-class field rather than just another priority level.
No-heat, no-water, and no-power carry statutory response deadlines in most
jurisdictions, and the SLA clock for them must not be adjustable by whoever is
triaging at the time.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import SoftDeleteMixin, TenantModel
from app.models.types import GUID, JSONType, Money, UTCDateTime, enum_column, utcnow

__all__ = [
    "Inspection",
    "InspectionItem",
    "InspectionKind",
    "InspectionResult",
    "InspectionTemplate",
    "ItemResult",
    "MaintenanceRequest",
    "PreventiveMaintenanceSchedule",
    "Priority",
    "RequestStatus",
    "SLAPolicy",
    "WorkOrder",
    "WorkOrderEvent",
    "WorkOrderStatus",
]


class Priority(StrEnum):
    EMERGENCY = "emergency"
    URGENT = "urgent"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


#: Default response and resolution targets, in minutes. Overridable per
#: organization via :class:`SLAPolicy`, but never below the habitability floor.
DEFAULT_SLA_MINUTES: dict[Priority, tuple[int, int]] = {
    Priority.EMERGENCY: (60, 24 * 60),
    Priority.URGENT: (4 * 60, 48 * 60),
    Priority.HIGH: (8 * 60, 72 * 60),
    Priority.NORMAL: (24 * 60, 7 * 24 * 60),
    Priority.LOW: (72 * 60, 30 * 24 * 60),
}


class RequestStatus(StrEnum):
    NEW = "new"
    TRIAGED = "triaged"
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    RESOLVED = "resolved"
    CLOSED = "closed"
    DUPLICATE = "duplicate"
    CANCELLED = "cancelled"


class WorkOrderStatus(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    ASSIGNED = "assigned"
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    AWAITING_PARTS = "awaiting_parts"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    VERIFIED = "verified"
    CANCELLED = "cancelled"


#: The state machine, declared once. Services validate transitions against this
#: rather than each writing their own ad-hoc `if status ==` chain.
WORK_ORDER_TRANSITIONS: dict[WorkOrderStatus, frozenset[WorkOrderStatus]] = {
    WorkOrderStatus.DRAFT: frozenset({WorkOrderStatus.OPEN, WorkOrderStatus.CANCELLED}),
    WorkOrderStatus.OPEN: frozenset(
        {WorkOrderStatus.ASSIGNED, WorkOrderStatus.SCHEDULED, WorkOrderStatus.CANCELLED}
    ),
    WorkOrderStatus.ASSIGNED: frozenset(
        {
            WorkOrderStatus.SCHEDULED,
            WorkOrderStatus.IN_PROGRESS,
            WorkOrderStatus.ON_HOLD,
            WorkOrderStatus.OPEN,
            WorkOrderStatus.CANCELLED,
        }
    ),
    WorkOrderStatus.SCHEDULED: frozenset(
        {
            WorkOrderStatus.IN_PROGRESS,
            WorkOrderStatus.ON_HOLD,
            WorkOrderStatus.ASSIGNED,
            WorkOrderStatus.CANCELLED,
        }
    ),
    WorkOrderStatus.IN_PROGRESS: frozenset(
        {
            WorkOrderStatus.AWAITING_PARTS,
            WorkOrderStatus.AWAITING_APPROVAL,
            WorkOrderStatus.ON_HOLD,
            WorkOrderStatus.COMPLETED,
            WorkOrderStatus.CANCELLED,
        }
    ),
    WorkOrderStatus.ON_HOLD: frozenset(
        {WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.ASSIGNED, WorkOrderStatus.CANCELLED}
    ),
    WorkOrderStatus.AWAITING_PARTS: frozenset(
        {WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.ON_HOLD, WorkOrderStatus.CANCELLED}
    ),
    WorkOrderStatus.AWAITING_APPROVAL: frozenset(
        {WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.CANCELLED, WorkOrderStatus.ON_HOLD}
    ),
    WorkOrderStatus.COMPLETED: frozenset({WorkOrderStatus.VERIFIED, WorkOrderStatus.IN_PROGRESS}),
    WorkOrderStatus.VERIFIED: frozenset(),
    WorkOrderStatus.CANCELLED: frozenset(),
}

#: Terminal states, for queue filters and SLA stop conditions.
WORK_ORDER_TERMINAL = frozenset(
    {WorkOrderStatus.COMPLETED, WorkOrderStatus.VERIFIED, WorkOrderStatus.CANCELLED}
)


class InspectionKind(StrEnum):
    MOVE_IN = "move_in"
    MOVE_OUT = "move_out"
    ROUTINE = "routine"
    ANNUAL = "annual"
    SAFETY = "safety"
    COMPLIANCE = "compliance"
    TURN = "turn"
    DRIVE_BY = "drive_by"


class InspectionResult(StrEnum):
    PASS = "pass"
    PASS_WITH_ITEMS = "pass_with_items"
    FAIL = "fail"
    INCOMPLETE = "incomplete"


class ItemResult(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NEEDS_ATTENTION = "needs_attention"
    NOT_APPLICABLE = "not_applicable"


class MaintenanceRequest(TenantModel, SoftDeleteMixin):
    """A reported problem, from any intake channel."""

    __tablename__ = "maintenance_requests"
    __table_args__ = (
        UniqueConstraint("org_id", "request_number", name="uq_maintenance_requests_org_number"),
        Index("ix_maintenance_requests_org_status", "org_id", "status"),
        Index("ix_maintenance_requests_unit", "org_id", "unit_id", "status"),
        Index("ix_maintenance_requests_org_created", "org_id", "created_at"),
    )

    request_number: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    property_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    unit_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("units.id", ondelete="RESTRICT"), index=True
    )
    lease_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("leases.id", ondelete="SET NULL"), index=True
    )
    resident_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("residents.id", ondelete="SET NULL"), index=True
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False, default="general")
    trade: Mapped[str | None] = mapped_column(String(40), index=True)
    location_in_unit: Mapped[str | None] = mapped_column(String(120))

    priority: Mapped[Priority] = mapped_column(
        enum_column(Priority), nullable=False, default=Priority.NORMAL, index=True
    )
    #: Statutory habitability issue - heat, water, power, sewage, security,
    #: pest infestation. Forces emergency handling regardless of triage opinion.
    is_habitability: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[RequestStatus] = mapped_column(
        enum_column(RequestStatus), nullable=False, default=RequestStatus.NEW, index=True
    )
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="portal")

    reported_by_user_id: Mapped[str | None] = mapped_column(GUID, index=True)
    reporter_name: Mapped[str | None] = mapped_column(String(150))
    reporter_phone: Mapped[str | None] = mapped_column(String(40))

    #: Entry permission is a legal matter, not a convenience field.
    permission_to_enter: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    entry_notes: Mapped[str | None] = mapped_column(String(255))
    has_pets: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    preferred_times: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)

    triaged_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    triaged_by_id: Mapped[str | None] = mapped_column(GUID)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    closed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    duplicate_of_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("maintenance_requests.id", ondelete="SET NULL"), index=True
    )
    resident_notified_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    work_orders: Mapped[list[WorkOrder]] = relationship(
        back_populates="request", passive_deletes=True
    )

    def effective_priority(self) -> Priority:
        """Habitability issues are emergencies, whatever the form said."""
        return Priority.EMERGENCY if self.is_habitability else self.priority


class SLAPolicy(TenantModel):
    """Response and resolution targets for a priority band."""

    __tablename__ = "sla_policies"
    __table_args__ = (
        UniqueConstraint("org_id", "priority", "property_id", name="uq_sla_policies_scope"),
        CheckConstraint("response_minutes > 0 AND resolution_minutes > 0", name="sla_positive"),
        CheckConstraint("resolution_minutes >= response_minutes", name="sla_ordering"),
        Index("ix_sla_policies_org_created", "org_id", "created_at"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    priority: Mapped[Priority] = mapped_column(enum_column(Priority), nullable=False)
    #: Null applies organization-wide; a property-specific row overrides it.
    property_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="CASCADE"), index=True
    )
    response_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Emergency clocks run around the clock; routine work does not.
    business_hours_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    escalate_to_user_id: Mapped[str | None] = mapped_column(GUID)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class WorkOrder(TenantModel, SoftDeleteMixin):
    """A unit of work: dispatched, scheduled, executed, billed."""

    __tablename__ = "work_orders"
    __table_args__ = (
        UniqueConstraint("org_id", "work_order_number", name="uq_work_orders_org_number"),
        CheckConstraint(
            "estimated_cost IS NULL OR estimated_cost >= 0", name="estimated_cost_non_negative"
        ),
        CheckConstraint("total_cost >= 0", name="total_cost_non_negative"),
        Index("ix_work_orders_org_status", "org_id", "status"),
        Index("ix_work_orders_assignee", "org_id", "assigned_user_id", "status"),
        Index("ix_work_orders_vendor", "org_id", "vendor_id", "status"),
        Index("ix_work_orders_sla", "org_id", "resolution_due_at", "status"),
        Index("ix_work_orders_org_created", "org_id", "created_at"),
    )

    work_order_number: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    request_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("maintenance_requests.id", ondelete="SET NULL"), index=True
    )
    property_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    unit_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("units.id", ondelete="RESTRICT"), index=True
    )
    asset_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("assets.id", ondelete="SET NULL"), index=True
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    trade: Mapped[str | None] = mapped_column(String(40), index=True)
    priority: Mapped[Priority] = mapped_column(
        enum_column(Priority), nullable=False, default=Priority.NORMAL, index=True
    )
    status: Mapped[WorkOrderStatus] = mapped_column(
        enum_column(WorkOrderStatus), nullable=False, default=WorkOrderStatus.DRAFT, index=True
    )

    assigned_user_id: Mapped[str | None] = mapped_column(GUID, index=True)
    vendor_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="RESTRICT"), index=True
    )
    assigned_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    scheduled_start: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)
    scheduled_end: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    verified_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    verified_by_id: Mapped[str | None] = mapped_column(GUID)

    # SLA. Deadlines are stored, not computed on read: the policy in force when
    # the work order was raised is the one it is measured against.
    response_due_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    resolution_due_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)
    first_response_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    sla_breached_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    estimated_cost: Mapped[Decimal | None] = mapped_column(Money)
    labor_hours: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    labor_cost: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    material_cost: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    total_cost: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))

    #: Who ultimately pays. Charging a resident for ordinary wear, or an owner
    #: for resident damage, is a dispute waiting to happen.
    is_owner_billable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_resident_billable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approved_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    approved_by_id: Mapped[str | None] = mapped_column(GUID)

    permission_to_enter: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    resolution_notes: Mapped[str | None] = mapped_column(Text)
    cancellation_reason: Mapped[str | None] = mapped_column(String(255))
    #: Set when a preventive schedule generated this work order.
    pm_schedule_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("preventive_maintenance_schedules.id", ondelete="SET NULL"), index=True
    )

    request: Mapped[MaintenanceRequest | None] = relationship(back_populates="work_orders")
    events: Mapped[list[WorkOrderEvent]] = relationship(
        back_populates="work_order",
        cascade="all, delete-orphan",
        order_by="WorkOrderEvent.occurred_at",
        passive_deletes=True,
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in WORK_ORDER_TERMINAL

    @property
    def is_sla_breached(self) -> bool:
        if self.sla_breached_at is not None:
            return True
        if self.resolution_due_at is None or self.is_terminal:
            return False
        return utcnow() > self.resolution_due_at

    def can_transition_to(self, target: WorkOrderStatus) -> bool:
        return target in WORK_ORDER_TRANSITIONS.get(self.status, frozenset())

    def recalculate_cost(self) -> Decimal:
        self.total_cost = (self.labor_cost or Decimal("0")) + (self.material_cost or Decimal("0"))
        return self.total_cost


class WorkOrderEvent(TenantModel):
    """An entry in the work order's timeline.

    Every status change, assignment, note, and cost adjustment lands here with
    an actor and a timestamp. When a resident asks why a repair took nine days,
    this is the answer.
    """

    __tablename__ = "work_order_events"
    __table_args__ = (
        Index("ix_work_order_events_wo_time", "work_order_id", "occurred_at"),
        Index("ix_work_order_events_org_created", "org_id", "created_at"),
    )

    work_order_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("work_orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    from_status: Mapped[WorkOrderStatus | None] = mapped_column(enum_column(WorkOrderStatus))
    to_status: Mapped[WorkOrderStatus | None] = mapped_column(enum_column(WorkOrderStatus))

    actor_id: Mapped[str | None] = mapped_column(GUID)
    actor_label: Mapped[str] = mapped_column(String(150), nullable=False, default="System")
    note: Mapped[str | None] = mapped_column(Text)
    #: Whether the resident can see this entry in their portal timeline.
    is_resident_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    work_order: Mapped[WorkOrder] = relationship(back_populates="events")


class InspectionTemplate(TenantModel, SoftDeleteMixin):
    """A reusable checklist definition."""

    __tablename__ = "inspection_templates"
    __table_args__ = (
        UniqueConstraint("org_id", "code", "version", name="uq_inspection_templates_code_version"),
        Index("ix_inspection_templates_org_created", "org_id", "created_at"),
    )

    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    kind: Mapped[InspectionKind] = mapped_column(enum_column(InspectionKind), nullable=False)
    #: Versioned rather than edited in place, so a completed inspection always
    #: renders against the checklist that was actually used.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: ``[{"section": "Kitchen", "items": [{"name": "Sink", "requires_photo": true}]}]``
    sections: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)


class Inspection(TenantModel, SoftDeleteMixin):
    """A performed or scheduled inspection."""

    __tablename__ = "inspections"
    __table_args__ = (
        UniqueConstraint("org_id", "inspection_number", name="uq_inspections_org_number"),
        Index("ix_inspections_org_status", "org_id", "status"),
        Index("ix_inspections_scheduled", "org_id", "scheduled_for"),
        Index("ix_inspections_org_created", "org_id", "created_at"),
    )

    inspection_number: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    kind: Mapped[InspectionKind] = mapped_column(
        enum_column(InspectionKind), nullable=False, index=True
    )
    template_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("inspection_templates.id", ondelete="SET NULL"), index=True
    )

    property_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    unit_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("units.id", ondelete="RESTRICT"), index=True
    )
    lease_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("leases.id", ondelete="SET NULL"), index=True
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled", index=True)
    scheduled_for: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    inspector_user_id: Mapped[str | None] = mapped_column(GUID, index=True)
    inspector_vendor_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="SET NULL"), index=True
    )

    result: Mapped[InspectionResult | None] = mapped_column(enum_column(InspectionResult))
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    notes: Mapped[str | None] = mapped_column(Text)

    #: Evidence captured in the field. Offline-tolerant clients replay these on
    #: reconnect, so the timestamps are the device's, not the server's.
    captured_offline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    device_captured_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    resident_signature_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    inspector_signature_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    items: Mapped[list[InspectionItem]] = relationship(
        back_populates="inspection", cascade="all, delete-orphan", passive_deletes=True
    )

    def derive_result(self) -> InspectionResult:
        if not self.items:
            return InspectionResult.INCOMPLETE
        if any(item.result == ItemResult.FAIL for item in self.items):
            return InspectionResult.FAIL
        if any(item.result == ItemResult.NEEDS_ATTENTION for item in self.items):
            return InspectionResult.PASS_WITH_ITEMS
        if any(item.result is None for item in self.items):
            return InspectionResult.INCOMPLETE
        return InspectionResult.PASS


class InspectionItem(TenantModel):
    """One checklist line and its finding."""

    __tablename__ = "inspection_items"
    __table_args__ = (
        Index("ix_inspection_items_inspection", "inspection_id", "sort_order"),
        Index("ix_inspection_items_org_created", "org_id", "created_at"),
    )

    inspection_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("inspections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    section: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    result: Mapped[ItemResult | None] = mapped_column(enum_column(ItemResult))
    condition: Mapped[str | None] = mapped_column(String(40))
    severity: Mapped[str | None] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(Text)
    requires_photo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    asset_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("assets.id", ondelete="SET NULL"), index=True
    )
    #: Repair raised from this finding, closing the loop from inspection to work.
    work_order_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("work_orders.id", ondelete="SET NULL"), index=True
    )
    #: Estimated cost to remedy, used for move-out deposit deductions.
    remedy_cost: Mapped[Decimal | None] = mapped_column(Money)
    is_resident_responsible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    inspection: Mapped[Inspection] = relationship(back_populates="items")


class PreventiveMaintenanceSchedule(TenantModel, SoftDeleteMixin):
    """A recurring maintenance plan for a property or an asset."""

    __tablename__ = "preventive_maintenance_schedules"
    __table_args__ = (
        CheckConstraint("interval_value > 0", name="interval_positive"),
        Index("ix_pm_schedules_next_due", "org_id", "next_due_on", "is_active"),
        Index("ix_preventive_maintenance_schedules_org_created", "org_id", "created_at"),
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    property_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="CASCADE"), index=True
    )
    unit_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("units.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )

    trade: Mapped[str | None] = mapped_column(String(40))
    priority: Mapped[Priority] = mapped_column(
        enum_column(Priority), nullable=False, default=Priority.LOW
    )
    interval_unit: Mapped[str] = mapped_column(String(10), nullable=False, default="month")
    interval_value: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    #: Seasonal schedules only fire in these months (1-12). Empty means any.
    active_months: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)

    next_due_on: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    last_completed_on: Mapped[dt.date | None] = mapped_column(Date)
    #: Generate the work order this many days before it is due, so it can be
    #: scheduled rather than arriving already late.
    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False, default=14)
    assigned_vendor_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="SET NULL"), index=True
    )
    estimated_cost: Mapped[Decimal | None] = mapped_column(Money)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Guards against a re-run generating a duplicate work order for one cycle.
    last_generated_for: Mapped[dt.date | None] = mapped_column(Date)

    def advance(self, from_date: dt.date) -> dt.date:
        """Next occurrence after ``from_date``."""
        if self.interval_unit == "day":
            return from_date + dt.timedelta(days=self.interval_value)
        if self.interval_unit == "week":
            return from_date + dt.timedelta(weeks=self.interval_value)
        if self.interval_unit == "year":
            return _add_months(from_date, self.interval_value * 12)
        return _add_months(from_date, self.interval_value)


def _add_months(source: dt.date, months: int) -> dt.date:
    """Calendar-correct month arithmetic, clamping to the end of short months."""
    total = source.month - 1 + months
    year = source.year + total // 12
    month = total % 12 + 1
    if month == 12:
        last_day = 31
    else:
        last_day = (dt.date(year, month + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(year, month, min(source.day, last_day))

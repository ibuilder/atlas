"""Preventive maintenance: turning a schedule into work.

This is a scheduled job, so it is idempotent by watermark. Each schedule
records the cycle it last generated for, and a cycle already behind that mark is
never generated again. Running the job three times in a morning produces one
boiler service, not three.

Two behaviours are worth stating because they are choices rather than
consequences:

* **A missed cycle generates once, not once per cycle missed.** If the job has
  not run since March, an annual gutter clean does not arrive as five overdue
  work orders. One is raised and the schedule moves to its next occurrence -
  the point of preventive maintenance is that the work happens, and a queue of
  identical backdated orders is noise that gets bulk-closed.
* **A seasonal schedule that comes due out of season is deferred, not
  cancelled.** A gutter clean restricted to autumn and due in July rolls
  forward to the next autumn occurrence. It is not raised in July, and it is
  not lost.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging import get_logger
from app.models.maintenance import (
    PreventiveMaintenanceSchedule,
    WorkOrder,
    WorkOrderStatus,
)
from app.models.org import Unit
from app.models.types import utcnow

__all__ = [
    "PreventiveRun",
    "due_schedules",
    "generate_preventive_work",
    "next_seasonal_occurrence",
    "record_schedule_completion",
]

log = get_logger("services.maintenance.preventive")

#: A schedule whose active months can never be reached would otherwise spin
#: forever. Twenty-four hops is more than any legitimate seasonal window needs.
MAX_SEASONAL_HOPS = 24


@dataclass
class PreventiveRun:
    """What one pass of the generator did."""

    work_orders: list[WorkOrder] = field(default_factory=list)
    #: Schedules rolled forward because they came due out of season.
    deferred: int = 0
    #: Schedules already generated for their current cycle.
    already_generated: int = 0
    #: Schedules that could not be resolved to a property.
    unresolved: int = 0

    @property
    def generated(self) -> int:
        return len(self.work_orders)


def next_seasonal_occurrence(
    schedule: PreventiveMaintenanceSchedule, from_date: dt.date
) -> dt.date:
    """Roll a due date forward to the next date inside the active months.

    Returns ``from_date`` unchanged when the schedule is not seasonal, or when
    the date already falls in season.
    """
    months = [m for m in (schedule.active_months or []) if isinstance(m, int)]
    if not months or from_date.month in months:
        return from_date

    candidate = from_date
    for _ in range(MAX_SEASONAL_HOPS):
        candidate = schedule.advance(candidate)
        if candidate.month in months:
            return candidate

    # An interval that can never land in the active months - a quarterly
    # schedule restricted to a single month it always steps over, say. Rolling
    # by whole years preserves the month and reaches the window.
    log.warning(
        "seasonal schedule could not be advanced into its active months",
        extra={
            "event": "pm.seasonal_unreachable",
            "schedule_id": schedule.id,
            "active_months": months,
        },
    )
    target = min(months)
    year = from_date.year + (1 if from_date.month >= target else 0)
    last_day = 31 if target == 12 else (dt.date(year, target + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(year, target, min(from_date.day, last_day))


def due_schedules(
    session: Session, *, org_id: str, as_of: dt.date | None = None
) -> list[PreventiveMaintenanceSchedule]:
    """Schedules whose lead time has opened.

    A schedule due on the 30th with fourteen days' lead is picked up on the
    16th, so the work can be scheduled rather than arriving already late.
    """
    today = as_of or utcnow().date()
    schedules = (
        session.execute(
            select(PreventiveMaintenanceSchedule).where(
                PreventiveMaintenanceSchedule.org_id == org_id,
                PreventiveMaintenanceSchedule.is_active.is_(True),
                PreventiveMaintenanceSchedule.deleted_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    return [
        schedule
        for schedule in schedules
        if schedule.next_due_on - dt.timedelta(days=schedule.lead_time_days) <= today
    ]


def generate_preventive_work(
    session: Session,
    *,
    org_id: str,
    as_of: dt.date | None = None,
    actor_id: str | None = None,
) -> PreventiveRun:
    """Raise work orders for everything whose lead time has opened."""
    from app.services.maintenance.service import create_work_order

    today = as_of or utcnow().date()
    run = PreventiveRun()

    for schedule in due_schedules(session, org_id=org_id, as_of=today):
        cycle = schedule.next_due_on

        if schedule.last_generated_for is not None and schedule.last_generated_for >= cycle:
            run.already_generated += 1
            continue

        in_season = next_seasonal_occurrence(schedule, cycle)
        if in_season != cycle:
            # Out of season: defer to the next occurrence that is in season and
            # leave the watermark alone, so nothing is lost or duplicated.
            schedule.next_due_on = in_season
            run.deferred += 1
            log.info(
                "preventive schedule deferred to its season",
                extra={
                    "event": "pm.deferred",
                    "schedule_id": schedule.id,
                    "from": cycle.isoformat(),
                    "to": in_season.isoformat(),
                },
            )
            continue

        property_id = _resolve_property(session, schedule)
        if property_id is None:
            run.unresolved += 1
            log.warning(
                "preventive schedule has no property to raise work against",
                extra={"event": "pm.unresolved", "schedule_id": schedule.id},
            )
            continue

        work_order = create_work_order(
            session,
            org_id=org_id,
            property_id=property_id,
            title=schedule.name,
            description=schedule.description or f"Preventive maintenance due {cycle.isoformat()}.",
            unit_id=schedule.unit_id,
            asset_id=schedule.asset_id,
            trade=schedule.trade,
            priority=schedule.priority,
            estimated_cost=schedule.estimated_cost,
            is_owner_billable=True,
            actor_id=actor_id,
        )
        run.work_orders.append(work_order)

        # Advance past every cycle already behind us. A job that has not run
        # since March raises one gutter clean, not five.
        schedule.last_generated_for = cycle
        upcoming = schedule.advance(cycle)
        for _ in range(MAX_SEASONAL_HOPS):
            if upcoming > today:
                break
            upcoming = schedule.advance(upcoming)
        schedule.next_due_on = next_seasonal_occurrence(schedule, upcoming)

    session.flush()
    log.info(
        "preventive maintenance generated",
        extra={
            "event": "pm.generated",
            "count": run.generated,
            "deferred": run.deferred,
            "org_id": org_id,
        },
    )
    return run


def _resolve_property(session: Session, schedule: PreventiveMaintenanceSchedule) -> str | None:
    """A work order needs a property; a schedule may only name a unit or asset."""
    if schedule.property_id:
        return schedule.property_id
    if schedule.unit_id:
        unit = session.get(Unit, schedule.unit_id)
        if unit is not None:
            return unit.property_id
    if schedule.asset_id:
        from app.models.asset_graph import Asset

        asset = session.get(Asset, schedule.asset_id)
        if asset is not None:
            return asset.property_id
    return None


def record_schedule_completion(
    session: Session, *, schedule: PreventiveMaintenanceSchedule, completed_on: dt.date
) -> PreventiveMaintenanceSchedule:
    """Note that the work actually happened.

    Kept separate from generation: a work order being *raised* and the boiler
    actually being serviced are different facts, and a schedule that reports
    the first as the second is how a building goes three years without a
    service while the system says everything is current.
    """
    schedule.last_completed_on = completed_on
    session.flush()
    return schedule


def open_preventive_work(session: Session, *, org_id: str) -> list[WorkOrder]:
    """Preventive work orders still outstanding."""
    return list(
        session.execute(
            select(WorkOrder)
            .where(
                WorkOrder.org_id == org_id,
                WorkOrder.status.not_in([WorkOrderStatus.CLOSED, WorkOrderStatus.CANCELLED]),
                WorkOrder.deleted_at.is_(None),
            )
            .order_by(WorkOrder.resolution_due_at)
        )
        .scalars()
        .all()
    )

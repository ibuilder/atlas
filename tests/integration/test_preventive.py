"""Preventive maintenance generation.

The two acceptance cases: a re-run makes no duplicate, and a winter schedule
does not fire in July.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.models.maintenance import PreventiveMaintenanceSchedule, Priority, WorkOrder
from app.services.maintenance.preventive import (
    due_schedules,
    generate_preventive_work,
    next_seasonal_occurrence,
    record_schedule_completion,
)

pytestmark = pytest.mark.integration

TODAY = dt.date(2026, 7, 15)


def _schedule(db, org, property_record, **overrides):
    params = {
        "name": "Boiler service",
        "description": "Annual boiler service and safety check.",
        "property_id": property_record.id,
        "trade": "hvac",
        "priority": Priority.LOW,
        "interval_unit": "month",
        "interval_value": 6,
        "active_months": [],
        "next_due_on": TODAY,
        "lead_time_days": 14,
        "is_active": True,
    }
    params.update(overrides)
    schedule = PreventiveMaintenanceSchedule(org_id=org.id, **params)
    db.session.add(schedule)
    db.session.commit()
    return schedule


# --------------------------------------------------------------- lead time


def test_a_schedule_inside_its_lead_time_is_picked_up(db, org, scope, property_record):
    """Due on the 30th with a fortnight's lead: raised on the 16th, not the 30th."""
    _schedule(db, org, property_record, next_due_on=dt.date(2026, 7, 30), lead_time_days=14)
    assert len(due_schedules(db.session, org_id=org.id, as_of=dt.date(2026, 7, 16))) == 1


def test_a_schedule_outside_its_lead_time_is_not(db, org, scope, property_record):
    _schedule(db, org, property_record, next_due_on=dt.date(2026, 7, 30), lead_time_days=14)
    assert due_schedules(db.session, org_id=org.id, as_of=dt.date(2026, 7, 15)) == []


def test_an_inactive_schedule_never_generates(db, org, scope, property_record):
    _schedule(db, org, property_record, is_active=False)
    run = generate_preventive_work(db.session, org_id=org.id, as_of=TODAY)
    assert run.generated == 0


# -------------------------------------------------------------- generation


def test_a_due_schedule_raises_a_work_order(db, org, scope, property_record):
    schedule = _schedule(db, org, property_record, estimated_cost=Decimal("450.00"))
    run = generate_preventive_work(db.session, org_id=org.id, as_of=TODAY)
    db.session.commit()

    assert run.generated == 1
    order = run.work_orders[0]
    assert order.title == "Boiler service"
    assert order.trade == "hvac"
    assert order.estimated_cost == Decimal("450.0000")
    assert order.property_id == property_record.id
    # The watermark advanced, and so did the next occurrence.
    assert schedule.last_generated_for == TODAY
    assert schedule.next_due_on == dt.date(2027, 1, 15)


def test_running_twice_generates_one_work_order(db, org, scope, property_record):
    """The assertion the watermark exists for."""
    _schedule(db, org, property_record)
    first = generate_preventive_work(db.session, org_id=org.id, as_of=TODAY)
    db.session.commit()
    second = generate_preventive_work(db.session, org_id=org.id, as_of=TODAY)
    db.session.commit()

    assert first.generated == 1
    assert second.generated == 0
    assert db.session.query(WorkOrder).count() == 1


def test_a_long_gap_generates_once_not_once_per_missed_cycle(db, org, scope, property_record):
    """Five overdue gutter cleans is noise that gets bulk-closed."""
    schedule = _schedule(
        db,
        org,
        property_record,
        interval_unit="month",
        interval_value=1,
        next_due_on=dt.date(2026, 2, 15),
    )
    run = generate_preventive_work(db.session, org_id=org.id, as_of=TODAY)
    db.session.commit()

    assert run.generated == 1
    assert schedule.next_due_on > TODAY


def test_the_next_cycle_generates_after_the_first(db, org, scope, property_record):
    _schedule(db, org, property_record, interval_unit="month", interval_value=1)
    generate_preventive_work(db.session, org_id=org.id, as_of=TODAY)
    db.session.commit()
    later = generate_preventive_work(db.session, org_id=org.id, as_of=dt.date(2026, 8, 20))
    db.session.commit()

    assert later.generated == 1
    assert db.session.query(WorkOrder).count() == 2


# ---------------------------------------------------------------- seasonal


def test_a_winter_schedule_does_not_fire_in_july(db, org, scope, property_record):
    """The acceptance case, stated plainly."""
    schedule = _schedule(
        db,
        org,
        property_record,
        name="Gutter clearance",
        active_months=[10, 11, 12],
        interval_unit="month",
        interval_value=1,
    )
    run = generate_preventive_work(db.session, org_id=org.id, as_of=TODAY)
    db.session.commit()

    assert run.generated == 0
    assert run.deferred == 1
    # Deferred, not cancelled: it lands in October.
    assert schedule.next_due_on.month == 10
    assert schedule.next_due_on.year == 2026


def test_a_seasonal_schedule_fires_once_it_is_in_season(db, org, scope, property_record):
    _schedule(
        db,
        org,
        property_record,
        active_months=[10, 11, 12],
        interval_unit="month",
        interval_value=1,
        next_due_on=dt.date(2026, 10, 1),
    )
    run = generate_preventive_work(db.session, org_id=org.id, as_of=dt.date(2026, 10, 1))
    db.session.commit()
    assert run.generated == 1


def test_an_unreachable_season_still_terminates(db, org, scope, property_record):
    """A schedule whose interval always steps over its window must not spin."""
    schedule = _schedule(
        db,
        org,
        property_record,
        active_months=[3],
        interval_unit="month",
        interval_value=2,
        next_due_on=dt.date(2026, 7, 15),
    )
    landed = next_seasonal_occurrence(schedule, schedule.next_due_on)
    assert landed.month == 3
    assert landed > schedule.next_due_on


def test_a_schedule_with_no_active_months_is_not_seasonal(db, org, scope, property_record):
    schedule = _schedule(db, org, property_record, active_months=[])
    assert next_seasonal_occurrence(schedule, TODAY) == TODAY


# ---------------------------------------------------------------- resolution


def test_a_unit_schedule_resolves_its_property(db, org, scope, property_record, unit_record):
    _schedule(db, org, property_record, property_id=None, unit_id=unit_record.id)
    run = generate_preventive_work(db.session, org_id=org.id, as_of=TODAY)
    db.session.commit()

    assert run.generated == 1
    assert run.work_orders[0].property_id == property_record.id
    assert run.work_orders[0].unit_id == unit_record.id


def test_a_schedule_with_nothing_to_attach_to_is_reported_not_crashed(
    db, org, scope, property_record
):
    _schedule(db, org, property_record, property_id=None)
    run = generate_preventive_work(db.session, org_id=org.id, as_of=TODAY)
    db.session.commit()

    assert run.generated == 0
    assert run.unresolved == 1


# --------------------------------------------------------------- completion


def test_generation_is_not_completion(db, org, scope, property_record):
    """Raising the order is not the boiler being serviced."""
    schedule = _schedule(db, org, property_record)
    generate_preventive_work(db.session, org_id=org.id, as_of=TODAY)
    db.session.commit()
    assert schedule.last_completed_on is None

    record_schedule_completion(db.session, schedule=schedule, completed_on=TODAY)
    db.session.commit()
    assert schedule.last_completed_on == TODAY


def test_schedules_do_not_cross_organizations(db, org, other_org, scope, property_record):
    _schedule(db, org, property_record)
    run = generate_preventive_work(db.session, org_id=other_org.id, as_of=TODAY)
    assert run.generated == 0

"""Maintenance lifecycle, triage, and SLA behaviour.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.errors import BusinessRuleViolation
from app.models.maintenance import Priority, RequestStatus, WorkOrderStatus
from app.models.types import utcnow
from app.models.vendor import ComplianceStatus
from app.services.maintenance.service import (
    create_request,
    create_work_order,
    resolve_sla,
    transition_work_order,
    triage_request,
)

pytestmark = pytest.mark.integration


def _request(db, org, property_record, **kwargs):
    return create_request(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        title=kwargs.pop("title", "Dripping tap"),
        description=kwargs.pop("description", "The kitchen tap drips constantly."),
        **kwargs,
    )


def test_request_is_numbered_and_recorded(db, org, scope, property_record):
    request = _request(db, org, property_record)
    db.session.commit()

    assert request.request_number.startswith("REQ-")
    assert request.status == RequestStatus.NEW
    assert request.priority == Priority.NORMAL


def test_habitability_language_forces_emergency_priority(db, org, scope, property_record):
    """People under-report urgency. The statute does not care."""
    request = _request(
        db,
        org,
        property_record,
        title="No heat since last night",
        description="The heating stopped and the flat is cold.",
        priority=Priority.LOW,
    )
    db.session.commit()

    assert request.is_habitability
    assert request.priority == Priority.EMERGENCY
    assert request.effective_priority() == Priority.EMERGENCY


def test_habitability_cannot_be_de_prioritised_in_triage(db, org, scope, property_record):
    request = _request(
        db, org, property_record, title="Gas leak smell in the hallway", description="Strong smell."
    )
    db.session.commit()

    with pytest.raises(BusinessRuleViolation, match="de-prioritised"):
        triage_request(db.session, request=request, priority=Priority.LOW)


def test_triage_sets_trade_and_status(db, org, scope, property_record):
    request = _request(db, org, property_record)
    db.session.commit()

    triage_request(db.session, request=request, priority=Priority.HIGH, trade="plumbing")
    db.session.commit()

    assert request.status == RequestStatus.TRIAGED
    assert request.trade == "plumbing"
    assert request.triaged_at is not None


def test_sla_deadlines_are_stamped_at_creation(db, org, scope, property_record):
    work_order = create_work_order(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        title="Replace washer",
        description="Tap washer perished.",
        priority=Priority.EMERGENCY,
    )
    db.session.commit()

    response_minutes, resolution_minutes = resolve_sla(
        db.session, org_id=org.id, priority=Priority.EMERGENCY
    )
    assert response_minutes == 60
    assert resolution_minutes == 24 * 60
    assert work_order.response_due_at is not None
    assert work_order.resolution_due_at > work_order.response_due_at


def test_valid_transitions_are_recorded_on_the_timeline(
    db, org, scope, property_record, vendor_record
):
    work_order = create_work_order(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        title="Fix light",
        description="Hallway light failed.",
    )
    db.session.commit()

    transition_work_order(
        db.session,
        work_order=work_order,
        target=WorkOrderStatus.ASSIGNED,
        vendor_id=vendor_record.id,
    )
    transition_work_order(db.session, work_order=work_order, target=WorkOrderStatus.IN_PROGRESS)
    transition_work_order(
        db.session,
        work_order=work_order,
        target=WorkOrderStatus.COMPLETED,
        labor_cost=Decimal("120.00"),
        material_cost=Decimal("18.50"),
    )
    db.session.commit()

    assert work_order.status == WorkOrderStatus.COMPLETED
    assert work_order.total_cost == Decimal("138.5000")
    assert work_order.first_response_at is not None
    assert work_order.completed_at is not None
    assert [event.to_status for event in work_order.events] == [
        WorkOrderStatus.OPEN,
        WorkOrderStatus.ASSIGNED,
        WorkOrderStatus.IN_PROGRESS,
        WorkOrderStatus.COMPLETED,
    ]


def test_invalid_transition_is_refused(db, org, scope, property_record):
    work_order = create_work_order(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        title="Skip ahead",
        description="Attempt an illegal jump.",
    )
    db.session.commit()

    # OPEN -> COMPLETED skips assignment and execution entirely.
    with pytest.raises(BusinessRuleViolation, match="cannot move"):
        transition_work_order(db.session, work_order=work_order, target=WorkOrderStatus.COMPLETED)


def test_uninsured_vendor_cannot_be_dispatched(db, org, scope, property_record, vendor_record):
    """An expired certificate of insurance is exactly what this blocks."""
    vendor_record.compliance_status = ComplianceStatus.EXPIRED
    vendor_record.compliance_expires_at = dt.date.today() - dt.timedelta(days=1)
    db.session.commit()

    work_order = create_work_order(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        title="Boiler service",
        description="Annual service.",
    )
    db.session.commit()

    with pytest.raises(BusinessRuleViolation, match="not dispatchable"):
        transition_work_order(
            db.session,
            work_order=work_order,
            target=WorkOrderStatus.ASSIGNED,
            vendor_id=vendor_record.id,
        )


def test_late_completion_records_an_sla_breach(db, org, scope, property_record):
    work_order = create_work_order(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        title="Overdue job",
        description="Left too long.",
    )
    # Simulate the deadline having passed rather than waiting for it.
    work_order.resolution_due_at = utcnow() - dt.timedelta(hours=3)
    db.session.commit()

    transition_work_order(db.session, work_order=work_order, target=WorkOrderStatus.ASSIGNED)
    transition_work_order(db.session, work_order=work_order, target=WorkOrderStatus.IN_PROGRESS)
    transition_work_order(db.session, work_order=work_order, target=WorkOrderStatus.COMPLETED)
    db.session.commit()

    assert work_order.sla_breached_at is not None
    assert work_order.is_sla_breached


def test_completing_all_work_resolves_the_request(db, org, scope, property_record):
    request = _request(db, org, property_record)
    db.session.commit()

    work_order = create_work_order(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        title="From request",
        description="Linked work.",
        request=request,
    )
    transition_work_order(db.session, work_order=work_order, target=WorkOrderStatus.ASSIGNED)
    transition_work_order(db.session, work_order=work_order, target=WorkOrderStatus.IN_PROGRESS)
    transition_work_order(db.session, work_order=work_order, target=WorkOrderStatus.COMPLETED)
    db.session.commit()

    assert request.status == RequestStatus.RESOLVED
    assert request.resolved_at is not None


def test_overdue_query_finds_only_open_breaches(db, org, scope, property_record):
    from app.services.maintenance.service import overdue_work_orders

    late = create_work_order(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        title="Late",
        description="Overdue.",
    )
    late.resolution_due_at = utcnow() - dt.timedelta(days=1)

    create_work_order(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        title="On time",
        description="Fine.",
    )
    db.session.commit()

    overdue = overdue_work_orders(db.session, org_id=org.id)
    assert [record.id for record in overdue] == [late.id]

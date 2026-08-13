"""Turns: the vacancy clock, and the step that must not be skipped silently.

Two things carry this module. A turn cannot be called ready while a required
step is outstanding — a unit advertised as ready before it is produces a
cancelled move-in, not a saved day. And days-vacant is measured from the day the
keys came back rather than the day somebody opened a turn, which is why the
move-out path starts one.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.errors import BusinessRuleViolation, NotFound, ValidationFailed
from app.models.leasing import StepStatus, TurnStatus
from app.models.org import UnitStatus
from app.services.leasing.turns import (
    DEFAULT_TEMPLATE,
    StepTemplate,
    cancel_turn,
    complete_step,
    link_work_order,
    mark_ready,
    open_turns,
    outstanding_steps,
    skip_step,
    start_turn,
    turn_board,
    turn_for_unit,
)

pytestmark = pytest.mark.integration

VACATED = dt.date(2026, 4, 1)
ACTOR = "019fea00-0000-7000-8000-00000000b001"


@pytest.fixture()
def turn(db, org, scope, unit_record):
    record = start_turn(
        db.session,
        org_id=org.id,
        unit_id=unit_record.id,
        started_on=VACATED,
        actor_id=ACTOR,
    )
    db.session.commit()
    return record


def _finish_required(db, turn):
    for step in turn.steps:
        if step.is_required:
            complete_step(db.session, step=step, actor_id=ACTOR)


# ---------------------------------------------------------------------------
# Starting
# ---------------------------------------------------------------------------


def test_starting_a_turn_lays_out_the_steps(db, org, scope, turn, unit_record):
    assert turn.status == TurnStatus.SCHEDULED
    assert len(turn.steps) == len(DEFAULT_TEMPLATE)
    assert [step.sequence for step in turn.steps] == list(range(1, len(DEFAULT_TEMPLATE) + 1))
    assert unit_record.status == UnitStatus.TURN


def test_a_target_date_is_set_from_the_start(db, org, scope, turn):
    assert turn.target_ready_on == VACATED + dt.timedelta(days=14)


def test_a_second_open_turn_on_one_unit_is_refused(db, org, scope, turn, unit_record):
    """Two turns on one unit means two people each believe they own it."""
    with pytest.raises(BusinessRuleViolation) as exc:
        start_turn(db.session, org_id=org.id, unit_id=unit_record.id, started_on=VACATED)
    assert "already has an open turn" in str(exc.value)


def test_a_turn_can_be_started_again_once_the_first_is_done(db, org, scope, turn, unit_record):
    _finish_required(db, turn)
    mark_ready(db.session, turn=turn, ready_on=VACATED + dt.timedelta(days=5))
    db.session.commit()

    second = start_turn(
        db.session,
        org_id=org.id,
        unit_id=unit_record.id,
        started_on=VACATED + dt.timedelta(days=200),
    )
    assert second.id != turn.id


def test_an_empty_template_is_refused(db, org, scope, unit_record):
    with pytest.raises(ValidationFailed):
        start_turn(
            db.session,
            org_id=org.id,
            unit_id=unit_record.id,
            started_on=VACATED,
            template=(),
        )


def test_another_tenants_unit_is_not_found(db, org, scope):
    with pytest.raises(NotFound):
        start_turn(
            db.session,
            org_id=org.id,
            unit_id="019fea00-0000-7000-8000-0000000000ff",
            started_on=VACATED,
        )


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def test_completing_a_step_moves_the_turn_into_progress(db, org, scope, turn):
    complete_step(db.session, step=turn.steps[0], actor_id=ACTOR)
    db.session.commit()

    assert turn.status == TurnStatus.IN_PROGRESS
    assert turn.steps[0].status == StepStatus.DONE
    assert turn.steps[0].completed_at is not None


def test_completing_a_step_twice_is_a_no_op(db, org, scope, turn):
    complete_step(db.session, step=turn.steps[0], actor_id=ACTOR)
    first = turn.steps[0].completed_at
    complete_step(db.session, step=turn.steps[0], actor_id=ACTOR)
    assert turn.steps[0].completed_at == first


def test_skipping_needs_a_reason(db, org, scope, turn):
    """An unexplained omission is the one somebody has to account for later."""
    with pytest.raises(ValidationFailed) as exc:
        skip_step(db.session, step=turn.steps[0], reason="   ")
    assert "needs a reason" in str(exc.value)


def test_a_skipped_step_records_why(db, org, scope, turn):
    skip_step(
        db.session,
        step=turn.steps[0],
        reason="Condition report already filed by the outgoing inspection.",
        actor_id=ACTOR,
    )
    db.session.commit()

    assert turn.steps[0].status == StepStatus.SKIPPED
    assert "outgoing inspection" in turn.steps[0].skip_reason


def test_linking_a_work_order_starts_the_step(db, org, scope, turn, property_record):
    from app.models.maintenance import WorkOrder, WorkOrderStatus
    from app.models.sequences import SequenceKey
    from app.services.common.numbering import next_number

    order = WorkOrder(
        org_id=org.id,
        work_order_number=next_number(db.session, SequenceKey.WORK_ORDER, org_id=org.id),
        property_id=property_record.id,
        title="Paint the flat",
        description="Two coats throughout.",
        status=WorkOrderStatus.ASSIGNED,
    )
    db.session.add(order)
    db.session.flush()

    step = next(step for step in turn.steps if step.name == "Paint")
    link_work_order(db.session, step=step, work_order_id=order.id)
    db.session.commit()

    assert step.work_order_id == order.id
    assert step.status == StepStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# Ready: the rule the module exists for
# ---------------------------------------------------------------------------


def test_a_turn_with_outstanding_required_steps_cannot_be_ready(db, org, scope, turn):
    with pytest.raises(BusinessRuleViolation) as exc:
        mark_ready(db.session, turn=turn)
    assert "required step" in str(exc.value)


def test_an_optional_step_does_not_block_readiness(db, org, scope, turn):
    optional = [step for step in turn.steps if not step.is_required]
    assert optional, "the default template should have at least one optional step"

    _finish_required(db, turn)
    mark_ready(db.session, turn=turn, ready_on=VACATED + dt.timedelta(days=9))
    db.session.commit()

    assert turn.status == TurnStatus.READY
    assert all(step.status == StepStatus.PENDING for step in optional)


def test_a_skipped_required_step_no_longer_blocks(db, org, scope, turn):
    """Skipping is allowed - with a reason on the record - but never silent."""
    for step in turn.steps:
        if step.is_required and step.name != "Re-key locks":
            complete_step(db.session, step=step, actor_id=ACTOR)

    rekey = next(step for step in turn.steps if step.name == "Re-key locks")
    assert outstanding_steps(turn) == [rekey]

    skip_step(db.session, step=rekey, reason="Smart locks; codes rotated instead.", actor_id=ACTOR)
    mark_ready(db.session, turn=turn, ready_on=VACATED + dt.timedelta(days=6))
    db.session.commit()

    assert turn.status == TurnStatus.READY


def test_marking_ready_stops_the_clock_and_frees_the_unit(db, org, scope, turn, unit_record):
    _finish_required(db, turn)
    mark_ready(db.session, turn=turn, ready_on=VACATED + dt.timedelta(days=11))
    db.session.commit()

    assert turn.ready_on == VACATED + dt.timedelta(days=11)
    assert turn.days_vacant == 11
    assert unit_record.status == UnitStatus.VACANT_READY


def test_ready_before_the_turn_started_is_refused(db, org, scope, turn):
    _finish_required(db, turn)
    with pytest.raises(ValidationFailed):
        mark_ready(db.session, turn=turn, ready_on=VACATED - dt.timedelta(days=1))


def test_marking_ready_twice_is_a_no_op(db, org, scope, turn):
    _finish_required(db, turn)
    mark_ready(db.session, turn=turn, ready_on=VACATED + dt.timedelta(days=4))
    mark_ready(db.session, turn=turn, ready_on=VACATED + dt.timedelta(days=40))
    assert turn.days_vacant == 4


def test_a_late_turn_is_audited_as_a_warning(db, org, scope, turn):
    from app.models.audit import AuditEvent, AuditSeverity

    _finish_required(db, turn)
    mark_ready(db.session, turn=turn, ready_on=VACATED + dt.timedelta(days=45))
    db.session.commit()

    warnings = [
        event
        for event in db.session.query(AuditEvent).all()
        if event.resource_type == "Turn" and event.severity == AuditSeverity.WARNING
    ]
    assert len(warnings) == 1
    assert warnings[0].payload["days_vacant"] == 45


def test_the_audit_records_what_was_skipped(db, org, scope, turn):
    from app.models.audit import AuditEvent

    for step in turn.steps:
        if step.is_required and step.name != "Deep clean":
            complete_step(db.session, step=step, actor_id=ACTOR)
    clean = next(step for step in turn.steps if step.name == "Deep clean")
    skip_step(db.session, step=clean, reason="Professionally cleaned by the outgoing resident.")
    mark_ready(db.session, turn=turn, ready_on=VACATED + dt.timedelta(days=3))
    db.session.commit()

    ready = [
        event
        for event in db.session.query(AuditEvent).all()
        if event.resource_type == "Turn" and event.payload.get("ready_on")
    ]
    assert ready[0].payload["skipped"] == [
        {"step": "Deep clean", "reason": "Professionally cleaned by the outgoing resident."}
    ]


# ---------------------------------------------------------------------------
# Cancelling, and the board
# ---------------------------------------------------------------------------


def test_cancelling_needs_a_reason(db, org, scope, turn):
    with pytest.raises(ValidationFailed):
        cancel_turn(db.session, turn=turn, reason="")


def test_a_cancelled_turn_cannot_be_made_ready(db, org, scope, turn):
    cancel_turn(db.session, turn=turn, reason="Resident withdrew notice.")
    with pytest.raises(BusinessRuleViolation):
        mark_ready(db.session, turn=turn)


def test_a_completed_turn_cannot_be_cancelled(db, org, scope, turn):
    _finish_required(db, turn)
    mark_ready(db.session, turn=turn, ready_on=VACATED + dt.timedelta(days=2))
    with pytest.raises(BusinessRuleViolation):
        cancel_turn(db.session, turn=turn, reason="Changed our minds.")


def test_the_board_separates_in_flight_from_completed(
    db, org, scope, property_record, unit_record, turn
):
    from app.models.org import Unit

    second_unit = Unit(
        org_id=org.id,
        property_id=property_record.id,
        unit_number="2B",
        market_rent=Decimal("2100.00"),
        status=UnitStatus.OCCUPIED,
    )
    db.session.add(second_unit)
    db.session.flush()

    finished = start_turn(
        db.session,
        org_id=org.id,
        unit_id=second_unit.id,
        started_on=VACATED,
    )
    for step in finished.steps:
        if step.is_required:
            complete_step(db.session, step=step)
    mark_ready(db.session, turn=finished, ready_on=VACATED + dt.timedelta(days=8))
    db.session.commit()

    board = turn_board(db.session, org_id=org.id, completed_since=VACATED)
    in_flight = [t.id for column in board.columns for t in column.turns]

    assert turn.id in in_flight
    assert finished.id not in in_flight
    assert [t.id for t in board.completed] == [finished.id]
    assert board.average_days_vacant == Decimal("8.0")


def test_the_average_ignores_turns_still_running(db, org, scope, turn):
    """An average that counts unfinished turns flatters itself as they worsen."""
    board = turn_board(db.session, org_id=org.id, completed_since=VACATED)
    assert board.average_days_vacant is None


def test_open_turns_are_oldest_first(db, org, scope, property_record, turn):
    """Which is the order to worry in."""
    from app.models.org import Unit

    newer_unit = Unit(
        org_id=org.id,
        property_id=property_record.id,
        unit_number="3C",
        market_rent=Decimal("2200.00"),
        status=UnitStatus.OCCUPIED,
    )
    db.session.add(newer_unit)
    db.session.flush()
    newer = start_turn(
        db.session,
        org_id=org.id,
        unit_id=newer_unit.id,
        started_on=VACATED + dt.timedelta(days=30),
    )
    db.session.commit()

    assert [t.id for t in open_turns(db.session, org_id=org.id)] == [turn.id, newer.id]


def test_turn_for_unit_finds_only_the_open_one(db, org, scope, unit_record, turn):
    assert turn_for_unit(db.session, org_id=org.id, unit_id=unit_record.id).id == turn.id

    _finish_required(db, turn)
    mark_ready(db.session, turn=turn, ready_on=VACATED + dt.timedelta(days=1))
    db.session.commit()

    assert turn_for_unit(db.session, org_id=org.id, unit_id=unit_record.id) is None


# ---------------------------------------------------------------------------
# The move-out path, which is where a turn really starts
# ---------------------------------------------------------------------------


def test_recording_a_move_out_starts_the_turn(db, org, scope, lease_record, unit_record):
    """The clock starts when the keys come back, not when somebody remembers."""
    from app.models.leasing import LeaseStatus
    from app.services.leasing.tenancy import give_notice, record_move_out

    lease_record.status = LeaseStatus.ACTIVE
    db.session.commit()

    move_out = give_notice(
        db.session,
        lease=lease_record,
        notice_date=VACATED - dt.timedelta(days=40),
        scheduled_date=VACATED,
    )
    record_move_out(db.session, move_out=move_out, actual_date=VACATED, actor_id=ACTOR)
    db.session.commit()

    started = turn_for_unit(db.session, org_id=org.id, unit_id=unit_record.id)
    assert started is not None
    assert started.started_on == VACATED
    assert started.move_out_id == move_out.id
    assert unit_record.status == UnitStatus.TURN


def test_a_custom_template_is_honoured(db, org, scope, unit_record):
    turn = start_turn(
        db.session,
        org_id=org.id,
        unit_id=unit_record.id,
        started_on=VACATED,
        template=(
            StepTemplate("Wipe down", trade="cleaning"),
            StepTemplate("Photograph", trade="inspection", is_required=False),
        ),
        target_days=3,
    )
    db.session.commit()

    assert [step.name for step in turn.steps] == ["Wipe down", "Photograph"]
    assert turn.target_ready_on == VACATED + dt.timedelta(days=3)


def test_cancelling_puts_the_unit_back_where_it_was(db, org, scope, property_record):
    """Leaving it in TURN is worse than any wrong answer.

    Nothing else sets that status, so a unit stranded in it would never again
    read as occupied or lettable and no report would show it.
    """
    from app.models.org import Unit

    occupied = Unit(
        org_id=org.id,
        property_id=property_record.id,
        unit_number="4D",
        market_rent=Decimal("2300.00"),
        status=UnitStatus.OCCUPIED,
    )
    db.session.add(occupied)
    db.session.flush()

    turn = start_turn(db.session, org_id=org.id, unit_id=occupied.id, started_on=VACATED)
    db.session.commit()
    assert occupied.status == UnitStatus.TURN

    cancel_turn(db.session, turn=turn, reason="Resident withdrew notice.", actor_id=ACTOR)
    db.session.commit()

    assert occupied.status == UnitStatus.OCCUPIED


def test_cancelling_falls_back_when_the_prior_status_is_unknown(db, org, scope, turn, unit_record):
    """Turns opened before the column existed have nothing to restore."""
    turn.unit_status_before = None
    db.session.flush()

    cancel_turn(db.session, turn=turn, reason="Coming off market.", actor_id=ACTOR)
    db.session.commit()

    assert unit_record.status == UnitStatus.VACANT_NOT_READY


def test_the_cancellation_records_what_the_unit_went_back_to(db, org, scope, turn):
    from app.models.audit import AuditEvent

    cancel_turn(db.session, turn=turn, reason="Resident withdrew notice.", actor_id=ACTOR)
    db.session.commit()

    cancelled = [
        event for event in db.session.query(AuditEvent).all() if event.payload.get("cancelled")
    ]
    assert cancelled[0].payload["unit_status_restored"] == str(UnitStatus.VACANT_READY)

"""Turning a unit around between tenancies.

A turn is the gap between one resident leaving and the next arriving, and it is
where the largest controllable cost in residential management sits. Work orders
already record *what was done*. They cannot answer *how long the unit was not
earning*, because the clock starts when the keys come back rather than when
somebody raises a job, and it stops when the unit is genuinely lettable rather
than when the last invoice is paid. That is what this records.

The rule the module exists to enforce: **a turn is not ready while a required
step is outstanding.** A unit marketed as ready that is not produces a
cancelled move-in, a refunded holding deposit, and a resident who tells people.
Declaring readiness therefore checks the steps rather than trusting the person
clicking the button, and the steps are records rather than a checklist in a
notes field - because a stalled turn has to say which step it stalled on, and
"was the smoke alarm tested" is a question somebody eventually asks under oath.

Steps carry an optional work order. The step is the commitment; the work order
is how it was carried out, and not every step needs one - testing a smoke alarm
takes a minute and a signature, not a dispatch.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import BusinessRuleViolation, NotFound, ValidationFailed
from app.logging import get_logger
from app.models.audit import AuditAction, AuditSeverity
from app.models.leasing import MoveOut, StepStatus, Turn, TurnStatus, TurnStep
from app.models.org import Unit, UnitStatus
from app.models.types import utcnow
from app.services.audit.recorder import record_audit_event

__all__ = [
    "DEFAULT_TEMPLATE",
    "DEFAULT_TURN_DAYS",
    "BoardColumn",
    "StepTemplate",
    "TurnBoard",
    "cancel_turn",
    "complete_step",
    "link_work_order",
    "mark_ready",
    "open_turns",
    "outstanding_steps",
    "skip_step",
    "start_turn",
    "turn_board",
    "turn_for_unit",
]

log = get_logger("services.leasing.turns")

#: What a standard turn is given before it is late. Configurable per call; this
#: is the number most operators quote and none of them hit in winter.
DEFAULT_TURN_DAYS = 14


@dataclass(frozen=True)
class StepTemplate:
    """One step in a turn template."""

    name: str
    trade: str | None = None
    is_required: bool = True


#: The ordinary sequence. Ordered by dependency rather than by importance -
#: painting before flooring is how a floor gets painted.
DEFAULT_TEMPLATE: tuple[StepTemplate, ...] = (
    StepTemplate("Inspect and photograph condition", trade="inspection"),
    StepTemplate("Remove abandoned belongings", trade="general", is_required=False),
    StepTemplate("Repairs from the move-out inspection", trade="general"),
    StepTemplate("Paint", trade="painting"),
    StepTemplate("Flooring clean or replace", trade="flooring"),
    StepTemplate("Deep clean", trade="cleaning"),
    StepTemplate("Test smoke and CO alarms", trade="safety"),
    StepTemplate("Re-key locks", trade="locksmith"),
    StepTemplate("Final walk and photographs", trade="inspection"),
)


def _unit(session: Session, *, org_id: str, unit_id: str) -> Unit:
    record = session.get(Unit, unit_id)
    if record is None or record.org_id != org_id:
        raise NotFound("That unit was not found.")
    return record


def start_turn(
    session: Session,
    *,
    org_id: str,
    unit_id: str,
    started_on: dt.date,
    move_out: MoveOut | None = None,
    template: tuple[StepTemplate, ...] = DEFAULT_TEMPLATE,
    target_days: int = DEFAULT_TURN_DAYS,
    notes: str | None = None,
    actor_id: str | None = None,
) -> Turn:
    """Open a turn on a unit and lay out its steps.

    Refuses a second open turn on the same unit: two turns on one unit means
    two people each believe they own it, and the days-vacant figure becomes
    whichever one somebody happens to read.
    """
    unit = _unit(session, org_id=org_id, unit_id=unit_id)

    existing = turn_for_unit(session, org_id=org_id, unit_id=unit_id)
    if existing is not None:
        raise BusinessRuleViolation(
            f"That unit already has an open turn started on {existing.started_on}. "
            "Finish or cancel it before starting another."
        )
    if not template:
        raise ValidationFailed("A turn needs at least one step.")

    turn = Turn(
        org_id=org_id,
        unit_id=unit.id,
        property_id=unit.property_id,
        move_out_id=move_out.id if move_out is not None else None,
        status=TurnStatus.SCHEDULED,
        started_on=started_on,
        target_ready_on=started_on + dt.timedelta(days=target_days),
        notes=notes,
    )
    session.add(turn)
    session.flush()

    for index, step in enumerate(template, start=1):
        session.add(
            TurnStep(
                org_id=org_id,
                turn=turn,
                sequence=index,
                name=step.name,
                trade=step.trade,
                is_required=step.is_required,
            )
        )

    # The unit is not lettable and should not read as vacant-ready anywhere.
    unit.status = UnitStatus.TURN
    session.flush()

    record_audit_event(
        action=AuditAction.UNIT_UPDATED,
        resource_type="Turn",
        resource_id=turn.id,
        resource_label=unit.unit_number,
        severity=AuditSeverity.NOTICE,
        payload={
            "unit_id": unit.id,
            "started_on": started_on.isoformat(),
            "target_ready_on": turn.target_ready_on.isoformat() if turn.target_ready_on else None,
            "steps": len(template),
        },
        reason="Turn started.",
        org_id=org_id,
        actor_id=actor_id,
        session=session,
    )
    return turn


def turn_for_unit(session: Session, *, org_id: str, unit_id: str) -> Turn | None:
    """The open turn on a unit, if there is one."""
    return session.execute(
        select(Turn).where(
            Turn.org_id == org_id,
            Turn.unit_id == unit_id,
            Turn.deleted_at.is_(None),
            Turn.status.in_([TurnStatus.SCHEDULED, TurnStatus.IN_PROGRESS]),
        )
    ).scalar_one_or_none()


def _touch(turn: Turn) -> None:
    """A turn with work under way is in progress, whatever it was before."""
    if turn.status == TurnStatus.SCHEDULED:
        turn.status = TurnStatus.IN_PROGRESS


def complete_step(
    session: Session,
    *,
    step: TurnStep,
    actor_id: str | None = None,
) -> TurnStep:
    """Mark a step done. Idempotent."""
    if step.status == StepStatus.DONE:
        return step
    step.status = StepStatus.DONE
    step.completed_at = utcnow()
    step.completed_by_id = actor_id
    _touch(step.turn)
    session.flush()
    return step


def skip_step(
    session: Session,
    *,
    step: TurnStep,
    reason: str,
    actor_id: str | None = None,
) -> TurnStep:
    """Deliberately not do a step, with a reason on the record.

    A required step can be skipped - sometimes the carpet really is fine - but
    never silently: the reason is mandatory, because "why was this unit let
    without a re-key" is asked afterwards, not before.
    """
    text = (reason or "").strip()
    if not text:
        raise ValidationFailed(
            "Skipping a step needs a reason. An unexplained omission is the one "
            "somebody has to account for later."
        )
    step.status = StepStatus.SKIPPED
    step.skip_reason = text[:255]
    step.completed_at = utcnow()
    step.completed_by_id = actor_id
    _touch(step.turn)
    session.flush()

    if step.is_required:
        log.warning(
            "required turn step skipped",
            extra={
                "event": "turn.required_step_skipped",
                "turn_id": step.turn_id,
                "step": step.name,
                "reason": text[:120],
            },
        )
    return step


def link_work_order(session: Session, *, step: TurnStep, work_order_id: str) -> TurnStep:
    """Attach the job that carries out a step."""
    step.work_order_id = work_order_id
    if step.status == StepStatus.PENDING:
        step.status = StepStatus.IN_PROGRESS
    _touch(step.turn)
    session.flush()
    return step


def outstanding_steps(turn: Turn) -> list[TurnStep]:
    """Required steps that are neither done nor deliberately skipped."""
    return [
        step
        for step in turn.steps
        if step.is_required and step.status not in (StepStatus.DONE, StepStatus.SKIPPED)
    ]


def mark_ready(
    session: Session,
    *,
    turn: Turn,
    ready_on: dt.date | None = None,
    actor_id: str | None = None,
) -> Turn:
    """Declare the unit lettable, and stop the clock.

    Refuses while a required step is outstanding. This is the whole point: a
    unit marketed as ready that is not produces a cancelled move-in, a refunded
    holding deposit, and a resident who tells people.
    """
    if turn.status == TurnStatus.READY:
        return turn
    if turn.status == TurnStatus.CANCELLED:
        raise BusinessRuleViolation("A cancelled turn cannot be made ready.")

    outstanding = outstanding_steps(turn)
    if outstanding:
        names = ", ".join(step.name for step in outstanding[:3])
        raise BusinessRuleViolation(
            f"{len(outstanding)} required step(s) are outstanding: {names}. "
            "A unit advertised as ready before it is produces a cancelled "
            "move-in, not a saved day."
        )

    when = ready_on or utcnow().date()
    if when < turn.started_on:
        raise ValidationFailed("A unit cannot be ready before the turn started.")

    turn.status = TurnStatus.READY
    turn.ready_on = when

    unit = session.get(Unit, turn.unit_id)
    if unit is not None:
        unit.status = UnitStatus.VACANT_READY
    session.flush()

    late = turn.target_ready_on is not None and when > turn.target_ready_on
    record_audit_event(
        action=AuditAction.UNIT_UPDATED,
        resource_type="Turn",
        resource_id=turn.id,
        resource_label=unit.unit_number if unit is not None else turn.unit_id,
        severity=AuditSeverity.WARNING if late else AuditSeverity.NOTICE,
        payload={
            "ready_on": when.isoformat(),
            "days_vacant": turn.days_vacant,
            "target_ready_on": (turn.target_ready_on.isoformat() if turn.target_ready_on else None),
            "skipped": [
                {"step": step.name, "reason": step.skip_reason}
                for step in turn.steps
                if step.status == StepStatus.SKIPPED
            ],
        },
        reason=(
            f"Unit ready after {turn.days_vacant} days."
            if not late
            else f"Unit ready after {turn.days_vacant} days, past the {turn.target_ready_on} target."
        ),
        org_id=turn.org_id,
        actor_id=actor_id,
        session=session,
    )
    return turn


def cancel_turn(session: Session, *, turn: Turn, reason: str, actor_id: str | None = None) -> Turn:
    """Abandon a turn - the resident stayed, or the unit is coming off market."""
    text = (reason or "").strip()
    if not text:
        raise ValidationFailed("Cancelling a turn needs a reason.")
    if turn.status == TurnStatus.READY:
        raise BusinessRuleViolation("A completed turn cannot be cancelled.")

    turn.status = TurnStatus.CANCELLED
    turn.notes = f"{turn.notes}\n{text}" if turn.notes else text
    session.flush()

    record_audit_event(
        action=AuditAction.UNIT_UPDATED,
        resource_type="Turn",
        resource_id=turn.id,
        severity=AuditSeverity.NOTICE,
        payload={"cancelled": True},
        reason=text[:255],
        org_id=turn.org_id,
        actor_id=actor_id,
        session=session,
    )
    return turn


# ---------------------------------------------------------------------------
# Reading: the board, and the number the board exists to produce
# ---------------------------------------------------------------------------


@dataclass
class BoardColumn:
    """One column of the turn board."""

    status: TurnStatus
    turns: list[Turn]

    @property
    def count(self) -> int:
        return len(self.turns)


@dataclass
class TurnBoard:
    """Every turn in flight, plus what they are costing in days."""

    columns: list[BoardColumn]
    completed: list[Turn]

    @property
    def overdue(self) -> list[Turn]:
        return [
            turn
            for column in self.columns
            for turn in column.turns
            if turn.is_overdue and turn.target_ready_on is not None
        ]

    @property
    def average_days_vacant(self) -> Decimal | None:
        """Across completed turns. The number the board exists to move."""
        measured = [turn.days_vacant for turn in self.completed if turn.days_vacant is not None]
        if not measured:
            return None
        return (Decimal(sum(measured)) / Decimal(len(measured))).quantize(Decimal("0.1"))


def open_turns(session: Session, *, org_id: str, property_id: str | None = None) -> list[Turn]:
    """Turns still in flight, oldest first - which is the order to worry in."""
    conditions = [
        Turn.org_id == org_id,
        Turn.deleted_at.is_(None),
        Turn.status.in_([TurnStatus.SCHEDULED, TurnStatus.IN_PROGRESS]),
    ]
    if property_id:
        conditions.append(Turn.property_id == property_id)

    return list(
        session.execute(select(Turn).where(*conditions).order_by(Turn.started_on)).scalars().all()
    )


def turn_board(
    session: Session,
    *,
    org_id: str,
    property_id: str | None = None,
    completed_since: dt.date | None = None,
) -> TurnBoard:
    """The board: what is in flight, and what recently finished."""
    in_flight = open_turns(session, org_id=org_id, property_id=property_id)

    since = completed_since or (utcnow().date() - dt.timedelta(days=90))
    conditions = [
        Turn.org_id == org_id,
        Turn.deleted_at.is_(None),
        Turn.status == TurnStatus.READY,
        Turn.ready_on >= since,
    ]
    if property_id:
        conditions.append(Turn.property_id == property_id)

    completed = list(
        session.execute(select(Turn).where(*conditions).order_by(Turn.ready_on.desc()))
        .scalars()
        .all()
    )

    return TurnBoard(
        columns=[
            BoardColumn(
                status=status,
                turns=[turn for turn in in_flight if turn.status == status],
            )
            for status in (TurnStatus.SCHEDULED, TurnStatus.IN_PROGRESS)
        ],
        completed=completed,
    )

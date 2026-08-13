"""Who owns a property, and from when.

Ownership is stored as time-bounded stakes rather than a current percentage,
because every question worth asking about it is a question about a date. An
owner statement for March has to resolve who held the asset in March, not who
holds it today, and a property that changed hands on the 14th has two owners
with a claim on that month.

Transfers therefore never mutate a stake's owner. They **close** the outgoing
stake the day before the transfer and **open** the incoming one on the day of
it, so the historical record stays true and
:func:`~app.services.accounting.statements.ownership_share` - which weights each
day separately - apportions the period correctly with no special case for a
mid-period change.

The invariant this module exists to protect: **on any date a property is owned
at all, its stakes total exactly 100%.** A transfer that drops four percent
does not fail. It silently under-distributes every statement from then on, and
nobody notices until an owner adds up a year of them.

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
from app.models.org import OwnerEntity, OwnershipStake, Property
from app.models.types import utcnow
from app.services.audit.recorder import record_audit_event

__all__ = [
    "FULL",
    "OwnershipTransfer",
    "StakePeriod",
    "assert_fully_allocated",
    "ownership_on",
    "ownership_timeline",
    "record_initial_stake",
    "total_allocated",
    "transfer_ownership",
]

log = get_logger("services.portfolio.ownership")

ZERO = Decimal("0")
#: Percentages are stored as NUMERIC(7, 4); comparisons quantize to match.
FULL = Decimal("100.0000")
_PLACES = Decimal("0.0001")


@dataclass
class OwnershipTransfer:
    """What a transfer produced, for the caller and the audit payload."""

    property_id: str
    effective_from: dt.date
    percentage: Decimal
    from_owner_entity_id: str
    to_owner_entity_id: str
    #: The stake that was closed, and the ones opened in its place.
    closed_stake_id: str
    opened_stake_id: str
    #: Present when only part of a stake moved: the seller keeps the rest, in a
    #: new stake starting the same day, because the closed one cannot be reused.
    retained_stake_id: str | None = None


def _property(session: Session, *, org_id: str, property_id: str) -> Property:
    record = session.get(Property, property_id)
    if record is None or record.org_id != org_id:
        raise NotFound("That property was not found.")
    return record


def _owner(session: Session, *, org_id: str, owner_entity_id: str) -> OwnerEntity:
    record = session.get(OwnerEntity, owner_entity_id)
    if record is None or record.org_id != org_id:
        raise NotFound("That owner was not found.")
    return record


def ownership_on(
    session: Session, *, org_id: str, property_id: str, on_date: dt.date
) -> list[OwnershipStake]:
    """Every stake covering ``on_date``, largest share first."""
    stakes = (
        session.execute(
            select(OwnershipStake).where(
                OwnershipStake.org_id == org_id,
                OwnershipStake.property_id == property_id,
                OwnershipStake.effective_from <= on_date,
            )
        )
        .scalars()
        .all()
    )
    covering = [stake for stake in stakes if stake.covers(on_date)]
    covering.sort(key=lambda stake: (-Decimal(stake.percentage), stake.owner_entity_id))
    return covering


def total_allocated(
    session: Session, *, org_id: str, property_id: str, on_date: dt.date
) -> Decimal:
    """The sum of every stake covering ``on_date``. Zero if unowned."""
    stakes = ownership_on(session, org_id=org_id, property_id=property_id, on_date=on_date)
    return sum((Decimal(stake.percentage) for stake in stakes), ZERO).quantize(_PLACES)


def assert_fully_allocated(
    session: Session, *, org_id: str, property_id: str, on_date: dt.date
) -> None:
    """Refuse an allocation that is neither nothing nor everything.

    Zero is allowed: a property the company manages without holding an equity
    record for is ordinary. Anything strictly between zero and a hundred is not
    - it means a share exists that nobody is recorded as holding, and every
    owner statement from that date understates the distribution.
    """
    total = total_allocated(session, org_id=org_id, property_id=property_id, on_date=on_date)
    if total in (ZERO, FULL):
        return
    raise BusinessRuleViolation(
        f"Ownership of this property totals {total}% on {on_date}, not 100%. "
        "A share nobody holds is one that never reaches an owner statement."
    )


def record_initial_stake(
    session: Session,
    *,
    org_id: str,
    property_id: str,
    owner_entity_id: str,
    percentage: Decimal,
    effective_from: dt.date,
    is_primary_contact: bool = False,
    actor_id: str | None = None,
) -> OwnershipStake:
    """Open a stake on a property. Used to establish ownership, not to move it.

    Deliberately does not enforce the 100% invariant on the way in: ownership is
    usually entered one stake at a time, and refusing the first of three would
    make it impossible to enter any. Call :func:`assert_fully_allocated` once
    the set is complete - and the transfer path, where the total *must* be
    preserved, checks it itself.
    """
    share = Decimal(percentage).quantize(_PLACES)
    if share <= ZERO or share > FULL:
        raise ValidationFailed("A stake must be greater than 0% and at most 100%.")

    _property(session, org_id=org_id, property_id=property_id)
    _owner(session, org_id=org_id, owner_entity_id=owner_entity_id)

    existing = total_allocated(
        session, org_id=org_id, property_id=property_id, on_date=effective_from
    )
    if existing + share > FULL:
        raise BusinessRuleViolation(
            f"That would put ownership at {existing + share}% on {effective_from}. "
            f"{existing}% is already allocated."
        )

    stake = OwnershipStake(
        org_id=org_id,
        property_id=property_id,
        owner_entity_id=owner_entity_id,
        percentage=share,
        effective_from=effective_from,
        is_primary_contact=is_primary_contact,
    )
    session.add(stake)
    session.flush()

    record_audit_event(
        action=AuditAction.OWNERSHIP_CHANGED,
        resource_type="OwnershipStake",
        resource_id=stake.id,
        resource_label=f"{share}% from {effective_from}",
        severity=AuditSeverity.NOTICE,
        payload={
            "property_id": property_id,
            "owner_entity_id": owner_entity_id,
            "percentage": str(share),
            "effective_from": effective_from.isoformat(),
        },
        reason="Ownership stake recorded.",
        org_id=org_id,
        actor_id=actor_id,
        session=session,
    )
    return stake


def transfer_ownership(
    session: Session,
    *,
    org_id: str,
    property_id: str,
    from_owner_entity_id: str,
    to_owner_entity_id: str,
    effective_from: dt.date,
    percentage: Decimal | None = None,
    reason: str | None = None,
    actor_id: str | None = None,
) -> OwnershipTransfer:
    """Move a share from one owner to another on a date.

    ``percentage`` defaults to the seller's whole holding. A partial transfer
    leaves the seller with the remainder, in a *new* stake starting the same
    day: the closed one is history and must not be edited to say something
    different from what it said at the time.

    The share transferred is preserved exactly, so a property that totalled 100%
    the day before still totals 100% the day after. That is checked rather than
    assumed.
    """
    if from_owner_entity_id == to_owner_entity_id:
        raise ValidationFailed("An owner cannot transfer a stake to themselves.")

    _property(session, org_id=org_id, property_id=property_id)
    _owner(session, org_id=org_id, owner_entity_id=to_owner_entity_id)

    covering = ownership_on(session, org_id=org_id, property_id=property_id, on_date=effective_from)
    outgoing = next(
        (stake for stake in covering if stake.owner_entity_id == from_owner_entity_id), None
    )
    if outgoing is None:
        raise BusinessRuleViolation(
            "That owner holds no stake in this property on "
            f"{effective_from}, so there is nothing to transfer."
        )

    held = Decimal(outgoing.percentage).quantize(_PLACES)
    share = held if percentage is None else Decimal(percentage).quantize(_PLACES)
    if share <= ZERO:
        raise ValidationFailed("A transfer must move more than 0%.")
    if share > held:
        raise BusinessRuleViolation(
            f"That owner holds {held}% on {effective_from}; {share}% cannot be transferred."
        )

    # The outgoing stake ends the day before, so the two never both cover the
    # transfer date - which would double-count it in the day-weighted share.
    closes_on = effective_from - dt.timedelta(days=1)
    if closes_on < outgoing.effective_from:
        raise BusinessRuleViolation(
            f"That stake begins on {outgoing.effective_from}, so it cannot be "
            f"closed on {closes_on}. Transfer on or after the day it starts."
        )
    outgoing.effective_to = closes_on

    incoming = OwnershipStake(
        org_id=org_id,
        property_id=property_id,
        owner_entity_id=to_owner_entity_id,
        percentage=share,
        effective_from=effective_from,
        is_primary_contact=outgoing.is_primary_contact and share == held,
    )
    session.add(incoming)

    retained: OwnershipStake | None = None
    remainder = (held - share).quantize(_PLACES)
    if remainder > ZERO:
        retained = OwnershipStake(
            org_id=org_id,
            property_id=property_id,
            owner_entity_id=from_owner_entity_id,
            percentage=remainder,
            effective_from=effective_from,
            is_primary_contact=outgoing.is_primary_contact,
        )
        session.add(retained)

    session.flush()

    # The whole point of the exercise: what totalled 100% yesterday still does.
    assert_fully_allocated(session, org_id=org_id, property_id=property_id, on_date=effective_from)

    transfer = OwnershipTransfer(
        property_id=property_id,
        effective_from=effective_from,
        percentage=share,
        from_owner_entity_id=from_owner_entity_id,
        to_owner_entity_id=to_owner_entity_id,
        closed_stake_id=outgoing.id,
        opened_stake_id=incoming.id,
        retained_stake_id=retained.id if retained is not None else None,
    )

    record_audit_event(
        action=AuditAction.OWNERSHIP_CHANGED,
        resource_type="Property",
        resource_id=property_id,
        resource_label=f"{share}% on {effective_from}",
        # Ownership decides who receives money. A change to it is not routine.
        severity=AuditSeverity.WARNING,
        payload={
            "from_owner_entity_id": from_owner_entity_id,
            "to_owner_entity_id": to_owner_entity_id,
            "percentage": str(share),
            "retained": str(remainder),
            "effective_from": effective_from.isoformat(),
            "closed_stake_id": outgoing.id,
            "opened_stake_id": incoming.id,
        },
        reason=reason or f"{share}% transferred with effect from {effective_from}.",
        org_id=org_id,
        actor_id=actor_id,
        session=session,
    )

    log.info(
        "ownership transferred",
        extra={
            "event": "ownership.transferred",
            "property_id": property_id,
            "percentage": str(share),
            "effective_from": effective_from.isoformat(),
        },
    )
    return transfer


@dataclass(frozen=True)
class StakePeriod:
    """One stake as it stood, for a history view."""

    stake_id: str
    owner_entity_id: str
    percentage: Decimal
    effective_from: dt.date
    effective_to: dt.date | None
    is_current: bool


def ownership_timeline(session: Session, *, org_id: str, property_id: str) -> list[StakePeriod]:
    """Every stake ever recorded, oldest first. The history an owner disputes."""
    stakes = (
        session.execute(
            select(OwnershipStake)
            .where(
                OwnershipStake.org_id == org_id,
                OwnershipStake.property_id == property_id,
            )
            .order_by(OwnershipStake.effective_from, OwnershipStake.created_at)
        )
        .scalars()
        .all()
    )
    today = utcnow().date()
    return [
        StakePeriod(
            stake_id=stake.id,
            owner_entity_id=stake.owner_entity_id,
            percentage=Decimal(stake.percentage),
            effective_from=stake.effective_from,
            effective_to=stake.effective_to,
            is_current=stake.covers(today),
        )
        for stake in stakes
    ]

"""Renewals and move-outs, including deposit disposition.

Deposit disposition is the single most litigated thing a management company
does, and almost every loss comes from the same three failures. Each is
prevented here rather than trained against.

**The clock is statutory and starts at the move-out, not at the paperwork.**
Most jurisdictions give between fourteen and thirty days to return the deposit
or account for it, and missing the deadline forfeits the deductions - sometimes
with a penalty multiple on top. The due date is computed when the move-out is
recorded and stored, so it cannot quietly drift.

**A deduction needs evidence, not an opinion.** Every line carries a
description and an amount, and where it came from an inspection finding it
carries that too. "Cleaning - $400" with nothing behind it is what a magistrate
disallows.

**You cannot deduct more than you hold.** The arithmetic is enforced: the
refund is what remains, and a deduction schedule exceeding the deposit is
refused rather than producing a negative refund nobody notices.

Renewals carry their own rule: the offer's terms are fixed when it is sent. A
resident accepts what they were offered, not what the rent has become since.

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
from app.models.accounting import DepositMovementKind
from app.models.audit import AuditAction, AuditOutcome, AuditSeverity
from app.models.leasing import Lease, LeaseRenewal, LeaseStatus, MoveOut
from app.models.sequences import SequenceKey
from app.models.types import quantize_money, utcnow
from app.services.accounting.deposits import (
    deposit_balance,
    holding_account_id,
    release_deposit,
)
from app.services.audit.recorder import record_audit_event
from app.services.common.numbering import next_number

__all__ = [
    "DEFAULT_DISPOSITION_DAYS",
    "Deduction",
    "accept_renewal",
    "decline_renewal",
    "give_notice",
    "offer_renewal",
    "overdue_dispositions",
    "record_move_out",
    "settle_deposit",
]

log = get_logger("services.leasing.tenancy")

ZERO = Decimal("0")

#: Statutory default. Jurisdictions vary between 14 and 45 days, so this is a
#: floor to be overridden per organization, not a fact about the law anywhere.
DEFAULT_DISPOSITION_DAYS = 21

#: An offer nobody answers should lapse rather than sit there indefinitely.
DEFAULT_OFFER_DAYS = 30


@dataclass(frozen=True)
class Deduction:
    """One withholding from a deposit, with the evidence for it."""

    description: str
    amount: Decimal
    #: The inspection item this came from, where it came from one.
    inspection_item_id: str | None = None
    is_resident_responsible: bool = True

    def as_line(self) -> dict:
        return {
            "description": self.description,
            "amount": str(quantize_money(self.amount)),
            "inspection_item_id": self.inspection_item_id,
            "resident_responsible": self.is_resident_responsible,
        }


# ---------------------------------------------------------------------------
# Renewals
# ---------------------------------------------------------------------------


def offer_renewal(
    session: Session,
    *,
    lease: Lease,
    offered_rent: Decimal,
    proposed_start: dt.date,
    proposed_end: dt.date,
    term_months: int = 12,
    expires_in_days: int = DEFAULT_OFFER_DAYS,
    actor_id: str | None = None,
) -> LeaseRenewal:
    """Offer terms for the next term.

    The terms are fixed here. A resident who accepts is accepting *this* offer,
    not whatever the asking rent has become in the meantime.
    """
    if lease.status not in (LeaseStatus.ACTIVE, LeaseStatus.HOLDOVER):
        raise BusinessRuleViolation(f"A {lease.status} lease cannot be renewed.")
    if offered_rent < ZERO:
        raise ValidationFailed("An offered rent cannot be negative.")
    if proposed_end <= proposed_start:
        raise ValidationFailed("A renewal must end after it starts.")

    outstanding = session.execute(
        select(LeaseRenewal).where(
            LeaseRenewal.org_id == lease.org_id,
            LeaseRenewal.lease_id == lease.id,
            LeaseRenewal.status.in_(["draft", "offered"]),
        )
    ).scalar_one_or_none()
    if outstanding is not None:
        raise BusinessRuleViolation(
            "There is already an open renewal offer on this lease. Withdraw it first, "
            "so the resident is never holding two."
        )

    renewal = LeaseRenewal(
        org_id=lease.org_id,
        lease_id=lease.id,
        status="offered",
        offered_rent=quantize_money(offered_rent),
        offered_term_months=term_months,
        proposed_start=proposed_start,
        proposed_end=proposed_end,
        offer_sent_at=utcnow(),
        offer_expires_at=utcnow() + dt.timedelta(days=expires_in_days),
    )
    session.add(renewal)
    session.flush()

    record_audit_event(
        action=AuditAction.LEASE_RENEWED,
        resource_type="LeaseRenewal",
        resource_id=renewal.id,
        resource_label=lease.lease_number,
        payload={
            "offered_rent": str(renewal.offered_rent),
            "current_rent": str(lease.rent_amount),
            "increase": str(renewal.rent_increase) if renewal.rent_increase else None,
        },
        reason="Renewal offered.",
        org_id=lease.org_id,
        actor_id=actor_id,
        session=session,
    )
    return renewal


def accept_renewal(
    session: Session, *, renewal: LeaseRenewal, actor_id: str | None = None
) -> Lease:
    """Accept an offer, creating the new lease on the offered terms."""
    _assert_open(renewal)

    lease = session.get(Lease, renewal.lease_id)
    if lease is None:  # pragma: no cover - defensive
        raise NotFound("That renewal has no lease.")

    new_lease = Lease(
        org_id=lease.org_id,
        lease_number=next_number(session, SequenceKey.LEASE, org_id=lease.org_id),
        property_id=lease.property_id,
        unit_id=lease.unit_id,
        status=LeaseStatus.DRAFT,
        start_date=renewal.proposed_start,
        end_date=renewal.proposed_end,
        # The offered terms, not today's asking rent.
        rent_amount=renewal.offered_rent,
        security_deposit=lease.security_deposit,
    )
    session.add(new_lease)
    session.flush()

    renewal.status = "accepted"
    renewal.response = "accepted"
    renewal.responded_at = utcnow()
    renewal.new_lease_id = new_lease.id
    lease.status = LeaseStatus.RENEWED
    session.flush()

    record_audit_event(
        action=AuditAction.LEASE_RENEWED,
        resource_type="Lease",
        resource_id=new_lease.id,
        resource_label=new_lease.lease_number,
        severity=AuditSeverity.NOTICE,
        payload={"from_lease": lease.lease_number, "rent": str(new_lease.rent_amount)},
        reason="Renewal accepted.",
        org_id=lease.org_id,
        actor_id=actor_id,
        session=session,
    )
    return new_lease


def decline_renewal(
    session: Session, *, renewal: LeaseRenewal, reason: str | None = None
) -> LeaseRenewal:
    _assert_open(renewal)
    renewal.status = "declined"
    renewal.response = "declined"
    renewal.responded_at = utcnow()
    renewal.declined_reason = (reason or "")[:255] or None
    session.flush()
    return renewal


def _assert_open(renewal: LeaseRenewal) -> None:
    if renewal.status not in ("draft", "offered"):
        raise BusinessRuleViolation(f"That offer was already {renewal.status}.")
    if renewal.offer_expires_at is not None and renewal.offer_expires_at <= utcnow():
        raise BusinessRuleViolation(
            "That offer has expired. Make a new one rather than honouring a lapsed price."
        )


def expire_stale_offers(session: Session, *, org_id: str) -> int:
    """Lapse offers nobody answered. Idempotent."""
    stale = (
        session.execute(
            select(LeaseRenewal).where(
                LeaseRenewal.org_id == org_id,
                LeaseRenewal.status == "offered",
                LeaseRenewal.offer_expires_at.is_not(None),
                LeaseRenewal.offer_expires_at <= utcnow(),
            )
        )
        .scalars()
        .all()
    )
    for renewal in stale:
        renewal.status = "expired"
    if stale:
        session.flush()
    return len(stale)


# ---------------------------------------------------------------------------
# Move-out
# ---------------------------------------------------------------------------


def give_notice(
    session: Session,
    *,
    lease: Lease,
    notice_date: dt.date,
    scheduled_date: dt.date,
    reason: str | None = None,
    is_early_termination: bool = False,
) -> MoveOut:
    """Record that a resident is leaving."""
    if lease.status not in (LeaseStatus.ACTIVE, LeaseStatus.HOLDOVER):
        raise BusinessRuleViolation(f"A {lease.status} lease cannot receive notice.")
    if scheduled_date < notice_date:
        raise ValidationFailed("A move-out cannot be scheduled before notice was given.")

    existing = session.execute(
        select(MoveOut).where(MoveOut.org_id == lease.org_id, MoveOut.lease_id == lease.id)
    ).scalar_one_or_none()
    if existing is not None:
        raise BusinessRuleViolation("Notice has already been recorded on this lease.")

    move_out = MoveOut(
        org_id=lease.org_id,
        lease_id=lease.id,
        status="notice_given",
        notice_given_at=notice_date,
        scheduled_date=scheduled_date,
        reason=reason,
        is_early_termination=is_early_termination,
        # What was actually taken, not what the lease specified. Settling
        # against the contracted figure refunds money that was never collected
        # - and where a deposit was waived, refunds a full month's rent.
        deposit_held=deposit_balance(session, org_id=lease.org_id, lease_id=lease.id),
    )
    session.add(move_out)
    session.flush()

    record_audit_event(
        action=AuditAction.LEASE_TERMINATED,
        resource_type="MoveOut",
        resource_id=move_out.id,
        resource_label=lease.lease_number,
        payload={
            "scheduled": scheduled_date.isoformat(),
            "early": is_early_termination,
            "deposit_held": str(move_out.deposit_held),
        },
        reason=reason or "Notice given.",
        org_id=lease.org_id,
        session=session,
    )
    return move_out


def record_move_out(
    session: Session,
    *,
    move_out: MoveOut,
    actual_date: dt.date,
    forwarding_address: dict | None = None,
    disposition_days: int = DEFAULT_DISPOSITION_DAYS,
    inspection_id: str | None = None,
    start_turn_on_vacancy: bool = True,
    actor_id: str | None = None,
) -> MoveOut:
    """Record that they actually left, and start the statutory clock.

    The clock starts here, at the move-out, and the due date is *stored*. A
    deadline recomputed on read drifts every time somebody changes the setting;
    a stored one is the date the law will be measured against.
    """
    if move_out.actual_date is not None:
        raise BusinessRuleViolation("That move-out has already been recorded.")
    if move_out.notice_given_at and actual_date < move_out.notice_given_at:
        raise ValidationFailed("A move-out cannot happen before notice was given.")
    if disposition_days < 1:
        raise ValidationFailed("A disposition period must be at least a day.")

    move_out.actual_date = actual_date
    move_out.status = "moved_out"
    move_out.inspection_id = inspection_id or move_out.inspection_id
    move_out.forwarding_address = forwarding_address or move_out.forwarding_address
    move_out.disposition_due_by = actual_date + dt.timedelta(days=disposition_days)
    session.flush()

    lease = session.get(Lease, move_out.lease_id)
    if lease is not None and lease.status in (LeaseStatus.ACTIVE, LeaseStatus.HOLDOVER):
        lease.status = LeaseStatus.TERMINATED
        session.flush()

    # The vacancy clock starts the day the keys come back, not the day somebody
    # remembers to open a turn. Starting it here is what makes days-vacant a
    # measurement rather than an estimate.
    if start_turn_on_vacancy and lease is not None and lease.unit_id:
        from app.services.leasing.turns import start_turn, turn_for_unit

        if turn_for_unit(session, org_id=move_out.org_id, unit_id=lease.unit_id) is None:
            start_turn(
                session,
                org_id=move_out.org_id,
                unit_id=lease.unit_id,
                started_on=actual_date,
                move_out=move_out,
                actor_id=actor_id,
            )

    if not move_out.forwarding_address:
        # Not a refusal - people leave without giving one - but it is the reason
        # a disposition later cannot be delivered, so it is worth noticing now.
        log.warning(
            "move-out recorded with no forwarding address",
            extra={"event": "moveout.no_forwarding_address", "move_out_id": move_out.id},
        )

    record_audit_event(
        action=AuditAction.LEASE_TERMINATED,
        resource_type="MoveOut",
        resource_id=move_out.id,
        severity=AuditSeverity.NOTICE,
        payload={
            "actual_date": actual_date.isoformat(),
            "disposition_due_by": move_out.disposition_due_by.isoformat(),
        },
        reason="Move-out recorded; disposition clock started.",
        org_id=move_out.org_id,
        session=session,
    )
    return move_out


def deductions_from_inspection(session: Session, *, inspection_id: str) -> list[Deduction]:
    """Turn failed, resident-responsible inspection items into deductions.

    This is where a defensible deduction comes from: an item, photographed, on
    a checklist the resident can see, with a cost recorded at the time.
    """
    from app.models.maintenance import InspectionItem, ItemResult

    items = (
        session.execute(
            select(InspectionItem).where(
                InspectionItem.inspection_id == inspection_id,
                InspectionItem.result == ItemResult.FAIL,
                InspectionItem.is_resident_responsible.is_(True),
            )
        )
        .scalars()
        .all()
    )
    return [
        Deduction(
            description=f"{item.section}: {item.name}" + (f" - {item.notes}" if item.notes else ""),
            amount=item.remedy_cost or ZERO,
            inspection_item_id=item.id,
        )
        for item in items
        if item.remedy_cost and item.remedy_cost > ZERO
    ]


def settle_deposit(
    session: Session,
    *,
    move_out: MoveOut,
    deductions: list[Deduction],
    settled_by_id: str,
    as_of: dt.date | None = None,
) -> MoveOut:
    """Account for the deposit: what is withheld, what is returned, and why.

    Refuses to withhold more than is held, and records whether the statutory
    deadline was met - because a late disposition typically forfeits the
    deductions entirely, and that is worth knowing at the moment it happens
    rather than when the claim arrives.
    """
    if move_out.actual_date is None:
        raise BusinessRuleViolation(
            "The deposit cannot be settled before the move-out is recorded."
        )
    if move_out.disposition_sent_at is not None:
        raise BusinessRuleViolation("That deposit has already been settled.")
    if not settled_by_id:
        raise ValidationFailed("A disposition must be attributed to a person.")

    for deduction in deductions:
        if not deduction.description or not deduction.description.strip():
            raise ValidationFailed(
                "Every deduction needs a description. An unexplained withholding is "
                "the one a magistrate disallows."
            )
        if deduction.amount <= ZERO:
            raise ValidationFailed(f"Deduction '{deduction.description}' has no amount.")

    total = quantize_money(sum((d.amount for d in deductions), ZERO))
    held = move_out.deposit_held
    if total > held:
        raise BusinessRuleViolation(
            f"Deductions of {total} exceed the {held} held. Withholding more than "
            "the deposit is a claim against the resident, not a disposition - "
            "raise it as one."
        )

    today = as_of or utcnow().date()
    on_time = move_out.disposition_due_by is None or today <= move_out.disposition_due_by

    refunded = quantize_money(held - total)

    move_out.deposit_deductions = total
    move_out.deposit_refunded = refunded
    move_out.deduction_detail = [d.as_line() for d in deductions]
    move_out.disposition_sent_at = utcnow()
    move_out.status = "settled"
    session.flush()

    # Take the money out of trust as well as accounting for it on the move-out.
    # Recording the disposition without releasing the funds leaves the trust
    # reconciliation reporting the deposit as still owed, for ever.
    if held > ZERO:
        account_id = holding_account_id(session, org_id=move_out.org_id, lease_id=move_out.lease_id)
        if account_id is None:  # pragma: no cover - held > 0 implies an account
            raise BusinessRuleViolation(
                "The deposit balance is not held in any trust account, so there "
                "is nothing to release. Record how it was collected first."
            )
        for amount, kind in (
            (total, DepositMovementKind.APPLIED),
            (refunded, DepositMovementKind.RETURNED),
        ):
            if amount <= ZERO:
                continue
            release_deposit(
                session,
                org_id=move_out.org_id,
                lease_id=move_out.lease_id,
                bank_account_id=account_id,
                amount=amount,
                kind=kind,
                effective_date=today,
                reason=f"Deposit disposition for move-out {move_out.id}.",
                source_type="move_out",
                source_id=move_out.id,
                actor_id=settled_by_id,
            )

    if not on_time:
        log.error(
            "deposit disposition sent after the statutory deadline",
            extra={
                "event": "moveout.disposition_late",
                "move_out_id": move_out.id,
                "due_by": (
                    move_out.disposition_due_by.isoformat() if move_out.disposition_due_by else None
                ),
                "sent": today.isoformat(),
            },
        )

    record_audit_event(
        action=AuditAction.LEASE_TERMINATED,
        resource_type="MoveOut",
        resource_id=move_out.id,
        severity=AuditSeverity.CRITICAL if not on_time else AuditSeverity.NOTICE,
        outcome=AuditOutcome.SUCCESS if on_time else AuditOutcome.FAILURE,
        payload={
            "deposit_held": str(held),
            "deductions": str(total),
            "refunded": str(move_out.deposit_refunded),
            "lines": move_out.deduction_detail,
            "due_by": (
                move_out.disposition_due_by.isoformat() if move_out.disposition_due_by else None
            ),
            "on_time": on_time,
        },
        reason=(
            "Deposit disposition issued."
            if on_time
            else "Deposit disposition issued AFTER the statutory deadline."
        ),
        org_id=move_out.org_id,
        actor_id=settled_by_id,
        session=session,
    )
    return move_out


def overdue_dispositions(
    session: Session, *, org_id: str, as_of: dt.date | None = None
) -> list[MoveOut]:
    """Move-outs whose statutory deadline has passed unsettled.

    The report somebody should be looking at every morning: past this date the
    deductions are usually forfeit, and often with a penalty on top.
    """
    today = as_of or utcnow().date()
    return list(
        session.execute(
            select(MoveOut)
            .where(
                MoveOut.org_id == org_id,
                MoveOut.disposition_sent_at.is_(None),
                MoveOut.disposition_due_by.is_not(None),
                MoveOut.disposition_due_by < today,
            )
            .order_by(MoveOut.disposition_due_by)
        )
        .scalars()
        .all()
    )

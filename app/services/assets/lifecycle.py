"""Asset lifecycle: service history, warranty, and when to stop repairing.

The registry earns its keep by answering three questions at the moment they are
expensive to get wrong.

**Is this under warranty?** Checked *before* a paid repair is dispatched, not
discovered afterwards on an invoice. Recording a service event therefore
resolves cover automatically and marks the event, so "we paid for something
that was covered" becomes visible rather than merely regrettable.

**Is it worth repairing again?** A boiler with four call-outs this year and
three thousand pounds of cumulative cost against a four-thousand-pound
replacement is not a repair decision any more. That comparison is computed from
the service history rather than left to whoever happens to pick up the ticket.

**When does condition become a plan?** Condition is recorded per service event
and rolled onto the asset, so the replacement forecast in :mod:`.capital` is
driven by what the technician saw rather than by age alone.

Everything here reads and writes the history honestly: a service event is a
fact about what happened, and the asset's aggregates are derived from those
facts rather than maintained independently and allowed to drift.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import BusinessRuleViolation, NotFound, ValidationFailed
from app.logging import get_logger
from app.models.asset_graph import (
    Asset,
    AssetServiceEvent,
    AssetStatus,
    ServiceEventType,
    Warranty,
)
from app.models.audit import AuditAction, AuditSeverity
from app.models.types import quantize_money, utcnow
from app.services.audit.recorder import record_audit_event

__all__ = [
    "REPAIR_VS_REPLACE_RATIO",
    "RepairAdvice",
    "WarrantyCheck",
    "check_warranty",
    "record_service",
    "recover_under_warranty",
    "repair_or_replace",
    "retire_asset",
]

log = get_logger("services.assets.lifecycle")

ZERO = Decimal("0")

#: Past this share of replacement cost spent on repairs, replacing is the
#: cheaper answer even before the disruption of the next failure is counted.
REPAIR_VS_REPLACE_RATIO = Decimal("0.50")

#: Repeated failures inside a year say more than the money does.
REPEAT_FAILURE_THRESHOLD = 3


@dataclass(frozen=True)
class WarrantyCheck:
    """Whether a repair should be a claim rather than an invoice."""

    covered: bool
    warranty: Warranty | None = None
    reason: str = ""

    @property
    def claimable_labor(self) -> bool:
        return bool(self.covered and self.warranty and self.warranty.covers_labor)


@dataclass
class RepairAdvice:
    """Repair or replace, with the numbers that produced the answer."""

    recommendation: str  # "repair" | "replace" | "assess"
    reasons: list[str] = field(default_factory=list)
    lifetime_cost: Decimal = ZERO
    replacement_cost: Decimal | None = None
    cost_ratio: Decimal | None = None
    failures_last_year: int = 0
    past_expected_life: bool = False


# ---------------------------------------------------------------------------
# Warranty
# ---------------------------------------------------------------------------


def check_warranty(
    session: Session, *, asset: Asset, on_date: dt.date | None = None
) -> WarrantyCheck:
    """Resolve cover before money is spent.

    This is the single highest-value question the registry answers, and it is
    only valuable if it is asked at dispatch rather than at invoicing.
    """
    reference = on_date or utcnow().date()
    warranties = (
        session.execute(
            select(Warranty).where(Warranty.org_id == asset.org_id, Warranty.asset_id == asset.id)
        )
        .scalars()
        .all()
    )

    covering = [warranty for warranty in warranties if warranty.covers(reference)]
    if not covering:
        expired = [w for w in warranties if w.expires_on < reference]
        if expired:
            latest = max(expired, key=lambda w: w.expires_on)
            return WarrantyCheck(
                covered=False,
                reason=f"Cover from {latest.provider} expired on {latest.expires_on}.",
            )
        return WarrantyCheck(covered=False, reason="No warranty is recorded for this asset.")

    # The one expiring last is the one worth claiming against.
    best = max(covering, key=lambda w: w.expires_on)
    parts = "parts" if best.covers_parts else ""
    labor = "labour" if best.covers_labor else ""
    scope = " and ".join(filter(None, [parts, labor])) or "limited cover"
    return WarrantyCheck(
        covered=True,
        warranty=best,
        reason=f"{best.provider} covers {scope} until {best.expires_on}.",
    )


def recover_under_warranty(
    session: Session, *, event: AssetServiceEvent, actor_id: str | None = None
) -> AssetServiceEvent:
    """Mark a paid repair as recoverable after the fact.

    Separate from recording, because the discovery usually happens later - and
    the audit event says plainly that money was spent on covered work, which is
    the thing worth noticing.
    """
    if event.was_under_warranty:
        return event

    asset = session.get(Asset, event.asset_id)
    if asset is None:
        raise NotFound("That service event has no asset.")

    check = check_warranty(session, asset=asset, on_date=event.performed_on)
    if not check.covered:
        raise BusinessRuleViolation(
            f"That work was not covered on {event.performed_on}. {check.reason}"
        )

    event.was_under_warranty = True
    session.flush()

    record_audit_event(
        action=AuditAction.WORK_ORDER_COMPLETED,
        resource_type="AssetServiceEvent",
        resource_id=event.id,
        resource_label=asset.code,
        severity=AuditSeverity.NOTICE,
        payload={
            "cost": str(event.cost),
            "warranty_provider": check.warranty.provider if check.warranty else None,
            "recoverable": True,
        },
        reason="Paid work identified as covered by warranty.",
        org_id=asset.org_id,
        actor_id=actor_id,
        session=session,
    )
    return event


# ---------------------------------------------------------------------------
# Service history
# ---------------------------------------------------------------------------


def record_service(
    session: Session,
    *,
    asset: Asset,
    event_type: ServiceEventType,
    performed_on: dt.date,
    cost: Decimal = ZERO,
    work_order_id: str | None = None,
    vendor_id: str | None = None,
    performed_by_id: str | None = None,
    condition_after: int | None = None,
    meter_reading: Decimal | None = None,
    meter_unit: str | None = None,
    parts_replaced: list | None = None,
    notes: str | None = None,
) -> AssetServiceEvent:
    """Record what happened, and derive the asset's aggregates from it.

    The aggregates on the asset - service count, lifetime cost, last serviced,
    condition - are maintained here rather than independently, so they cannot
    disagree with the history they summarise.
    """
    if performed_on > utcnow().date():
        raise ValidationFailed("Service cannot be recorded for a future date.")
    if cost < ZERO:
        raise ValidationFailed("A service cost cannot be negative.")
    if condition_after is not None and not 1 <= condition_after <= 5:
        raise ValidationFailed("A condition score runs from 1 (failed) to 5 (as new).")

    check = check_warranty(session, asset=asset, on_date=performed_on)

    event = AssetServiceEvent(
        org_id=asset.org_id,
        asset_id=asset.id,
        event_type=event_type,
        performed_on=performed_on,
        work_order_id=work_order_id,
        vendor_id=vendor_id,
        performed_by_id=performed_by_id,
        cost=quantize_money(cost),
        # Resolved automatically, so "we paid for covered work" is visible in
        # the data rather than only in somebody's memory of the call.
        was_under_warranty=check.covered,
        meter_reading=meter_reading,
        meter_unit=meter_unit,
        condition_after=condition_after,
        parts_replaced=parts_replaced or [],
        notes=notes,
    )
    session.add(event)

    asset.service_count += 1
    asset.lifetime_service_cost = quantize_money(asset.lifetime_service_cost + event.cost)
    if asset.last_serviced_on is None or performed_on > asset.last_serviced_on:
        asset.last_serviced_on = performed_on
        if condition_after is not None:
            asset.condition_score = condition_after
    session.flush()

    if check.covered and event.cost > ZERO:
        log.info(
            "paid work recorded against a covered asset",
            extra={
                "event": "asset.paid_under_warranty",
                "asset_id": asset.id,
                "cost": str(event.cost),
            },
        )
    return event


def failures_since(session: Session, *, asset: Asset, since: dt.date) -> list[AssetServiceEvent]:
    """Breakdown and emergency events, which are what "unreliable" means."""
    return list(
        session.execute(
            select(AssetServiceEvent)
            .where(
                AssetServiceEvent.org_id == asset.org_id,
                AssetServiceEvent.asset_id == asset.id,
                AssetServiceEvent.performed_on >= since,
                # Repairs only. A preventive visit is the plan working, not
                # the asset failing, and counting it would make a
                # well-maintained boiler look like a liability.
                AssetServiceEvent.event_type == ServiceEventType.REPAIR,
            )
            .order_by(AssetServiceEvent.performed_on)
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Repair or replace
# ---------------------------------------------------------------------------


def repair_or_replace(
    session: Session, *, asset: Asset, as_of: dt.date | None = None
) -> RepairAdvice:
    """Whether this is still a repair decision.

    Deliberately advisory and deliberately explained. The recommendation is
    worth little; the three numbers behind it are worth a lot, because they
    turn "it keeps breaking" into something a budget meeting can act on.
    """
    today = as_of or utcnow().date()
    advice = RepairAdvice(
        recommendation="repair",
        lifetime_cost=asset.lifetime_service_cost,
        replacement_cost=asset.replacement_cost,
    )

    recent = failures_since(session, asset=asset, since=today - dt.timedelta(days=365))
    advice.failures_last_year = len(recent)

    target = asset.expected_replacement_on or asset.compute_replacement_date()
    advice.past_expected_life = bool(target and target <= today)

    if asset.replacement_cost and asset.replacement_cost > ZERO:
        advice.cost_ratio = (asset.lifetime_service_cost / asset.replacement_cost).quantize(
            Decimal("0.01")
        )

    triggers = 0
    if advice.cost_ratio is not None and advice.cost_ratio >= REPAIR_VS_REPLACE_RATIO:
        triggers += 1
        advice.reasons.append(
            f"Repairs have cost {advice.cost_ratio:.0%} of replacement "
            f"({asset.lifetime_service_cost} against {asset.replacement_cost})."
        )
    if advice.failures_last_year >= REPEAT_FAILURE_THRESHOLD:
        triggers += 1
        advice.reasons.append(f"{advice.failures_last_year} repairs in the last twelve months.")
    if advice.past_expected_life:
        triggers += 1
        advice.reasons.append(
            f"Past its expected replacement date ({target.isoformat()})."
            if target
            else "Past its expected life."
        )
    if asset.condition_score is not None and asset.condition_score <= 2:
        triggers += 1
        advice.reasons.append(f"Last inspected condition was {asset.condition_score} out of 5.")

    if triggers >= 2:
        advice.recommendation = "replace"
    elif triggers == 1:
        # One signal is a conversation, not a conclusion.
        advice.recommendation = "assess"
    else:
        advice.reasons.append("Nothing in the history argues against another repair.")

    if advice.replacement_cost is None:
        advice.reasons.append(
            "No replacement cost is recorded, so the money side of this is a guess."
        )

    return advice


# ---------------------------------------------------------------------------
# Retirement
# ---------------------------------------------------------------------------


def retire_asset(
    session: Session,
    *,
    asset: Asset,
    retired_on: dt.date | None = None,
    reason: str,
    replaced_by: Asset | None = None,
    actor_id: str | None = None,
) -> Asset:
    """Take an asset out of service.

    Retired rather than deleted: its service history is the evidence behind the
    replacement decision, and behind the next one for the same model.
    """
    if not reason or not reason.strip():
        raise ValidationFailed("Retiring an asset requires a reason.")
    if asset.status == AssetStatus.RETIRED:
        return asset

    when = retired_on or utcnow().date()
    asset.status = AssetStatus.RETIRED
    asset.notes = "\n".join(filter(None, [asset.notes, f"Retired {when.isoformat()}: {reason}"]))
    if replaced_by is not None:
        if replaced_by.org_id != asset.org_id:
            raise ValidationFailed("A replacement must belong to the same organization.")
        asset.attributes = {**(asset.attributes or {}), "replaced_by_asset_id": replaced_by.id}
        replaced_by.attributes = {
            **(replaced_by.attributes or {}),
            "replaces_asset_id": asset.id,
        }

    record_service(
        session,
        asset=asset,
        event_type=(
            ServiceEventType.REPLACEMENT
            if replaced_by is not None
            else ServiceEventType.DECOMMISSION
        ),
        performed_on=when,
        cost=ZERO,
        notes=f"Retired: {reason}",
        performed_by_id=actor_id,
    )
    session.flush()

    record_audit_event(
        action=AuditAction.WORK_ORDER_COMPLETED,
        resource_type="Asset",
        resource_id=asset.id,
        resource_label=asset.code,
        severity=AuditSeverity.NOTICE,
        payload={
            "retired_on": when.isoformat(),
            "lifetime_service_cost": str(asset.lifetime_service_cost),
            "replaced_by": replaced_by.id if replaced_by else None,
        },
        reason=reason,
        org_id=asset.org_id,
        actor_id=actor_id,
        session=session,
    )
    return asset

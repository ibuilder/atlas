"""Capital planning: what needs replacing, when, and what it will cost.

A replacement forecast is a claim about the future, so the honest version is
built from what is actually known and says where it is guessing.

**Age is the weakest signal, and it is the one everybody uses.** A boiler
serviced twice a year in a mild climate outlasts its nominal life; one that has
failed three times does not reach it. So the forecast starts from expected life
and then *moves* on observed condition and failure history, and reports which
inputs it had.

**A missing replacement cost is stated, not invented.** An asset with no cost
recorded appears in the plan with its cost unknown rather than silently
contributing zero to a budget somebody is going to commit to.

**Money in a future year is not money today.** Costs are inflated forward at a
stated rate, and the rate is an argument rather than a constant, because a
five-year plan built at the wrong one is wrong by a quarter.

Nothing here writes. A plan is a reading of the registry, so it can be re-run
after any correction and produce a different answer honestly.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ValidationFailed
from app.logging import get_logger
from app.models.asset_graph import Asset, AssetCriticality, AssetStatus
from app.models.types import quantize_money, utcnow

__all__ = [
    "DEFAULT_INFLATION",
    "CapitalPlan",
    "ForecastEntry",
    "PlanYear",
    "forecast_asset",
    "plan_capital",
]

log = get_logger("services.assets.capital")

ZERO = Decimal("0")

#: Construction inflation, not consumer inflation. An argument rather than a
#: constant: a five-year plan built at the wrong rate is wrong by a quarter.
DEFAULT_INFLATION = Decimal("0.035")

#: Condition drives the forecast away from pure age. A 1 is failed; a 5 is as
#: new. The multipliers move the remaining life, not the total.
CONDITION_MULTIPLIER: dict[int, Decimal] = {
    1: Decimal("0.10"),
    2: Decimal("0.40"),
    3: Decimal("0.75"),
    4: Decimal("1.00"),
    5: Decimal("1.20"),
}

#: Each repair in the last three years pulls the date in by this much of the
#: remaining life. Repeated failure is the strongest early signal there is.
FAILURE_PENALTY = Decimal("0.15")


@dataclass
class ForecastEntry:
    """One asset's predicted replacement."""

    asset: Asset
    replace_in_year: int
    replace_on: dt.date
    #: Today's money.
    base_cost: Decimal | None
    #: Inflated to the year of replacement.
    forecast_cost: Decimal | None
    criticality: AssetCriticality
    confidence: str  # "measured" | "estimated" | "unknown"
    drivers: list[str] = field(default_factory=list)

    @property
    def cost_is_known(self) -> bool:
        return self.base_cost is not None


@dataclass
class PlanYear:
    year: int
    entries: list[ForecastEntry] = field(default_factory=list)

    @property
    def known_cost(self) -> Decimal:
        return quantize_money(sum((e.forecast_cost or ZERO for e in self.entries), ZERO))

    @property
    def unknown_count(self) -> int:
        return sum(1 for entry in self.entries if not entry.cost_is_known)


@dataclass
class CapitalPlan:
    """A multi-year replacement plan, with its own uncertainty stated."""

    start_year: int
    horizon_years: int
    inflation: Decimal
    years: list[PlanYear] = field(default_factory=list)
    #: Assets whose replacement is already overdue.
    overdue: list[ForecastEntry] = field(default_factory=list)
    #: Assets with no basis for a forecast at all.
    unforecastable: list[Asset] = field(default_factory=list)

    @property
    def total_known_cost(self) -> Decimal:
        return quantize_money(sum((year.known_cost for year in self.years), ZERO))

    @property
    def assets_with_unknown_cost(self) -> int:
        return sum(year.unknown_count for year in self.years) + sum(
            1 for entry in self.overdue if not entry.cost_is_known
        )


# ---------------------------------------------------------------------------
# Forecasting one asset
# ---------------------------------------------------------------------------


def forecast_asset(
    session: Session, *, asset: Asset, as_of: dt.date | None = None
) -> ForecastEntry | None:
    """Predict when this asset needs replacing.

    Returns ``None`` when there is no basis at all - no install date, no
    expected life, no explicit replacement date. Guessing in that situation
    would put a number in a budget that nothing supports.
    """
    today = as_of or utcnow().date()
    drivers: list[str] = []

    explicit = asset.expected_replacement_on
    if explicit is not None:
        target = explicit
        confidence = "estimated"
        drivers.append("Replacement date recorded on the asset.")
    else:
        if not asset.installed_on or not asset.expected_life_years:
            return None
        target = asset.compute_replacement_date()
        if target is None:  # pragma: no cover - defensive
            return None
        confidence = "estimated"
        drivers.append(
            f"Installed {asset.installed_on.isoformat()}, "
            f"{asset.expected_life_years}-year expected life."
        )

        remaining = Decimal((target - today).days)
        if remaining > ZERO:
            adjusted = remaining
            if asset.condition_score in CONDITION_MULTIPLIER:
                multiplier = CONDITION_MULTIPLIER[asset.condition_score]
                adjusted *= multiplier
                confidence = "measured"
                drivers.append(
                    f"Condition {asset.condition_score}/5 "
                    f"{'shortens' if multiplier < 1 else 'extends'} the remaining life."
                )

            failures = _recent_failures(session, asset=asset, since=today - dt.timedelta(days=1095))
            if failures:
                penalty = min(Decimal("0.75"), FAILURE_PENALTY * failures)
                adjusted *= Decimal(1) - penalty
                confidence = "measured"
                drivers.append(f"{failures} repair(s) in three years pull the date forward.")

            target = today + dt.timedelta(days=int(adjusted))

    if asset.replacement_cost is None:
        confidence = "unknown" if confidence == "estimated" else confidence
        drivers.append("No replacement cost is recorded, so this contributes nothing to the total.")

    return ForecastEntry(
        asset=asset,
        replace_in_year=target.year,
        replace_on=target,
        base_cost=asset.replacement_cost,
        forecast_cost=None,  # filled in by the plan, which knows the inflation rate
        criticality=asset.criticality,
        confidence=confidence,
        drivers=drivers,
    )


def _recent_failures(session: Session, *, asset: Asset, since: dt.date) -> int:
    from sqlalchemy import func

    from app.models.asset_graph import AssetServiceEvent, ServiceEventType

    return int(
        session.execute(
            select(func.count())
            .select_from(AssetServiceEvent)
            .where(
                AssetServiceEvent.org_id == asset.org_id,
                AssetServiceEvent.asset_id == asset.id,
                AssetServiceEvent.event_type == ServiceEventType.REPAIR,
                AssetServiceEvent.performed_on >= since,
            )
        ).scalar_one()
        or 0
    )


def inflate(amount: Decimal, *, years: int, rate: Decimal) -> Decimal:
    """Compound a cost forward. Never backwards: a past year costs what it cost."""
    if years <= 0:
        return quantize_money(amount)
    return quantize_money(amount * (Decimal(1) + rate) ** years)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def plan_capital(
    session: Session,
    *,
    org_id: str,
    property_id: str | None = None,
    horizon_years: int = 5,
    inflation: Decimal = DEFAULT_INFLATION,
    as_of: dt.date | None = None,
) -> CapitalPlan:
    """Build a replacement plan across a portfolio or one property."""
    if horizon_years < 1 or horizon_years > 30:
        raise ValidationFailed("A capital plan runs between one and thirty years.")
    if inflation < Decimal("-0.5") or inflation > Decimal("0.5"):
        raise ValidationFailed("That inflation rate is not plausible.")

    today = as_of or utcnow().date()
    plan = CapitalPlan(start_year=today.year, horizon_years=horizon_years, inflation=inflation)
    plan.years = [PlanYear(year=today.year + offset) for offset in range(horizon_years)]
    by_year = {year.year: year for year in plan.years}

    query = select(Asset).where(
        Asset.org_id == org_id,
        Asset.deleted_at.is_(None),
        Asset.status != AssetStatus.RETIRED,
    )
    if property_id:
        query = query.where(Asset.property_id == property_id)

    for asset in session.execute(query.order_by(Asset.code)).scalars():
        entry = forecast_asset(session, asset=asset, as_of=today)
        if entry is None:
            plan.unforecastable.append(asset)
            continue

        years_out = entry.replace_in_year - today.year
        entry.forecast_cost = (
            inflate(entry.base_cost, years=max(0, years_out), rate=inflation)
            if entry.base_cost is not None
            else None
        )

        if entry.replace_on <= today:
            plan.overdue.append(entry)
        elif entry.replace_in_year in by_year:
            by_year[entry.replace_in_year].entries.append(entry)
        # Beyond the horizon: deliberately dropped rather than piled into the
        # final year, which would make year five look like a cliff that is not
        # there.

    for year in plan.years:
        year.entries.sort(key=lambda e: (_criticality_rank(e.criticality), e.replace_on))
    plan.overdue.sort(key=lambda e: (_criticality_rank(e.criticality), e.replace_on))

    log.info(
        "capital plan built",
        extra={
            "event": "capital.planned",
            "org_id": org_id,
            "horizon": horizon_years,
            "overdue": len(plan.overdue),
            "unforecastable": len(plan.unforecastable),
        },
    )
    return plan


def _criticality_rank(value: AssetCriticality) -> int:
    order = {
        AssetCriticality.CRITICAL: 0,
        AssetCriticality.HIGH: 1,
        AssetCriticality.MEDIUM: 2,
        AssetCriticality.LOW: 3,
    }
    return order.get(value, 9)


def plan_as_rows(plan: CapitalPlan) -> list[dict]:
    """Flatten a plan for a report or an export."""
    rows: list[dict] = []
    for entry in plan.overdue:
        rows.append(_row(entry, year_label="Overdue"))
    for year in plan.years:
        for entry in year.entries:
            rows.append(_row(entry, year_label=str(year.year)))
    return rows


def _row(entry: ForecastEntry, *, year_label: str) -> dict:
    return {
        "year": year_label,
        "asset": entry.asset.code,
        "name": entry.asset.name,
        "category": str(entry.asset.category),
        "criticality": str(entry.criticality),
        "replace_on": entry.replace_on.isoformat(),
        "base_cost": entry.base_cost,
        "forecast_cost": entry.forecast_cost,
        "confidence": entry.confidence,
        "why": "; ".join(entry.drivers),
    }

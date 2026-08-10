"""KPI projections: computing dashboard metrics ahead of time.

A projection is a cache with a date on it, and caches go wrong in two specific
ways that this module is shaped to avoid.

**A stale projection must be a rebuild, not a correctness problem.** Every
metric is computed purely from operational data for a given date, so any
snapshot can be thrown away and recomputed to exactly the same value. Nothing
is ever *only* in a projection. That is what makes "just rebuild it" a
sufficient answer to any doubt about a dashboard.

**A rate is not a number you can average.** Occupancy across two properties is
not the mean of their two occupancy rates unless they happen to have the same
unit count. Every rate therefore stores its numerator and denominator as well
as its value, so a portfolio roll-up re-divides rather than re-averages. This
is the classic silent reporting error and the reason those two columns exist.

Snapshots are upserted on (metric, scope, date), so recomputing a day is
idempotent and a backfill can run repeatedly without duplicating a series.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.logging import get_logger
from app.models.reporting import KpiSnapshot
from app.models.types import quantize_money, utcnow

__all__ = [
    "METRICS",
    "MetricDefinition",
    "MetricValue",
    "known_metrics",
    "rebuild_series",
    "snapshot_metrics",
]

log = get_logger("services.reporting.projections")

ZERO = Decimal("0")


@dataclass(frozen=True)
class MetricValue:
    """One computed metric.

    ``numerator`` and ``denominator`` are kept for rates so a roll-up can
    re-divide. A portfolio's occupancy is total occupied over total units, not
    the average of each property's percentage.
    """

    numeric_value: Decimal | None = None
    count_value: int | None = None
    numerator: Decimal | None = None
    denominator: Decimal | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricDefinition:
    key: str
    name: str
    description: str
    compute: Callable[..., MetricValue]


METRICS: dict[str, MetricDefinition] = {}


def register(definition: MetricDefinition) -> MetricDefinition:
    if definition.key in METRICS:  # pragma: no cover - registration is static
        raise RuntimeError(f"Metric {definition.key!r} is already registered.")
    METRICS[definition.key] = definition
    return definition


def known_metrics() -> list[str]:
    return sorted(METRICS)


def _rate(numerator: Decimal, denominator: Decimal, detail: dict[str, Any]) -> MetricValue:
    value = (numerator / denominator).quantize(Decimal("0.0001")) if denominator > ZERO else None
    return MetricValue(
        numeric_value=value,
        numerator=quantize_money(numerator),
        denominator=quantize_money(denominator),
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _occupancy_rate(session: Session, *, org_id: str, as_of: dt.date) -> MetricValue:
    from app.models.leasing import Lease, LeaseStatus
    from app.models.org import Unit, UnitStatus

    units = session.execute(
        select(func.count())
        .select_from(Unit)
        .where(
            Unit.org_id == org_id,
            Unit.deleted_at.is_(None),
            # A unit that is down or off the market is not rentable, and
            # counting it as vacant makes occupancy look worse than it is.
            Unit.status.not_in([UnitStatus.DOWN, UnitStatus.OFF_MARKET]),
        )
    ).scalar_one()

    occupied = session.execute(
        select(func.count(func.distinct(Lease.unit_id))).where(
            Lease.org_id == org_id,
            Lease.deleted_at.is_(None),
            Lease.unit_id.is_not(None),
            Lease.status.in_([LeaseStatus.ACTIVE, LeaseStatus.HOLDOVER]),
            Lease.start_date <= as_of,
        )
    ).scalar_one()

    return _rate(
        Decimal(occupied),
        Decimal(units),
        {"occupied_units": occupied, "rentable_units": units},
    )


register(
    MetricDefinition(
        key="occupancy_rate",
        name="Occupancy",
        description="Occupied units over rentable units. Offline units are excluded.",
        compute=_occupancy_rate,
    )
)


def _delinquency_rate(session: Session, *, org_id: str, as_of: dt.date) -> MetricValue:
    from app.models.accounting import Invoice, InvoiceStatus

    billed = session.execute(
        select(func.coalesce(func.sum(Invoice.total), 0)).where(
            Invoice.org_id == org_id,
            Invoice.status != InvoiceStatus.VOID,
            Invoice.issue_date <= as_of,
        )
    ).scalar_one()

    overdue = session.execute(
        select(func.coalesce(func.sum(Invoice.balance), 0)).where(
            Invoice.org_id == org_id,
            Invoice.status.in_([InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID]),
            Invoice.due_date < as_of,
        )
    ).scalar_one()

    return _rate(Decimal(overdue), Decimal(billed), {"overdue": str(overdue)})


register(
    MetricDefinition(
        key="delinquency_rate",
        name="Delinquency",
        description="Overdue balance over everything billed to date.",
        compute=_delinquency_rate,
    )
)


def _sla_compliance(session: Session, *, org_id: str, as_of: dt.date) -> MetricValue:
    from app.models.maintenance import WorkOrder, WorkOrderStatus

    orders = (
        session.execute(
            select(WorkOrder).where(
                WorkOrder.org_id == org_id,
                WorkOrder.deleted_at.is_(None),
                WorkOrder.status.not_in([WorkOrderStatus.CANCELLED, WorkOrderStatus.DRAFT]),
            )
        )
        .scalars()
        .all()
    )
    total = len(orders)
    met = sum(1 for order in orders if order.sla_breached_at is None)
    return _rate(Decimal(met), Decimal(total), {"breached": total - met})


register(
    MetricDefinition(
        key="work_order_sla_compliance",
        name="SLA compliance",
        description="Work orders that have not breached their resolution target.",
        compute=_sla_compliance,
    )
)


def _open_work_orders(session: Session, *, org_id: str, as_of: dt.date) -> MetricValue:
    from app.models.maintenance import WorkOrder, WorkOrderStatus

    count = session.execute(
        select(func.count())
        .select_from(WorkOrder)
        .where(
            WorkOrder.org_id == org_id,
            WorkOrder.deleted_at.is_(None),
            WorkOrder.status.not_in(
                [
                    WorkOrderStatus.VERIFIED,
                    WorkOrderStatus.CANCELLED,
                    WorkOrderStatus.DRAFT,
                ]
            ),
        )
    ).scalar_one()
    return MetricValue(count_value=int(count))


register(
    MetricDefinition(
        key="open_work_orders",
        name="Open work orders",
        description="Work neither closed nor cancelled.",
        compute=_open_work_orders,
    )
)


def _net_operating_income(session: Session, *, org_id: str, as_of: dt.date) -> MetricValue:
    """Income less operating expense for the month ending ``as_of``."""
    from app.models.accounting import Account, AccountType, JournalEntry, JournalLine

    start = as_of.replace(day=1)
    rows = session.execute(
        select(Account.account_type, func.sum(JournalLine.credit), func.sum(JournalLine.debit))
        .join(JournalLine, JournalLine.account_id == Account.id)
        .join(JournalEntry, JournalEntry.id == JournalLine.journal_entry_id)
        .where(
            JournalLine.org_id == org_id,
            JournalEntry.entry_date >= start,
            JournalEntry.entry_date <= as_of,
            Account.account_type.in_([AccountType.REVENUE, AccountType.EXPENSE]),
        )
        .group_by(Account.account_type)
    ).all()

    income = expense = ZERO
    for account_type, credit, debit in rows:
        if account_type == AccountType.REVENUE:
            income += Decimal(credit or 0) - Decimal(debit or 0)
        else:
            expense += Decimal(debit or 0) - Decimal(credit or 0)

    return MetricValue(
        numeric_value=quantize_money(income - expense),
        detail={
            "period_start": start.isoformat(),
            "income": str(quantize_money(income)),
            "expense": str(quantize_money(expense)),
        },
    )


register(
    MetricDefinition(
        key="net_operating_income",
        name="Net operating income",
        description="Revenue less operating expense, month to date.",
        compute=_net_operating_income,
    )
)


# ---------------------------------------------------------------------------
# Snapshotting
# ---------------------------------------------------------------------------


def snapshot_metrics(
    session: Session,
    *,
    org_id: str,
    as_of: dt.date | None = None,
    metric_keys: list[str] | None = None,
    scope_type: str = "organization",
    scope_id: str | None = None,
) -> list[KpiSnapshot]:
    """Compute and store metrics for one date.

    Upserts on (metric, scope, date), so recomputing a day corrects it rather
    than adding a second point to the series.
    """
    day = as_of or utcnow().date()
    keys = metric_keys or known_metrics()
    stored: list[KpiSnapshot] = []

    for key in keys:
        definition = METRICS.get(key)
        if definition is None:
            log.warning("unknown metric requested", extra={"event": "kpi.unknown", "metric": key})
            continue

        try:
            value = definition.compute(session, org_id=org_id, as_of=day)
        except Exception:  # noqa: BLE001 - one bad metric must not lose the rest
            log.exception(
                "metric computation failed",
                extra={"event": "kpi.failed", "metric": key, "org_id": org_id},
            )
            continue

        snapshot = session.execute(
            select(KpiSnapshot).where(
                KpiSnapshot.org_id == org_id,
                KpiSnapshot.metric_key == key,
                KpiSnapshot.scope_type == scope_type,
                (
                    KpiSnapshot.scope_id.is_(None)
                    if scope_id is None
                    else KpiSnapshot.scope_id == scope_id
                ),
                KpiSnapshot.as_of_date == day,
            )
        ).scalar_one_or_none()

        if snapshot is None:
            snapshot = KpiSnapshot(
                org_id=org_id,
                metric_key=key,
                scope_type=scope_type,
                scope_id=scope_id,
                as_of_date=day,
            )
            session.add(snapshot)

        snapshot.numeric_value = value.numeric_value
        snapshot.count_value = value.count_value
        snapshot.numerator = value.numerator
        snapshot.denominator = value.denominator
        snapshot.detail = value.detail
        snapshot.computed_at = utcnow()
        stored.append(snapshot)

    session.flush()
    return stored


def rebuild_series(
    session: Session,
    *,
    org_id: str,
    start: dt.date,
    end: dt.date,
    metric_keys: list[str] | None = None,
) -> int:
    """Recompute a date range from operational data.

    The answer to any doubt about a dashboard: throw the projections away and
    rebuild. Every metric is a pure function of the operational tables, so a
    rebuild reproduces exactly the same numbers.
    """
    if end < start:
        raise ValueError("A rebuild range must end on or after it starts.")

    days = (end - start).days + 1
    written = 0
    for offset in range(days):
        written += len(
            snapshot_metrics(
                session,
                org_id=org_id,
                as_of=start + dt.timedelta(days=offset),
                metric_keys=metric_keys,
            )
        )
    log.info(
        "kpi series rebuilt",
        extra={"event": "kpi.rebuilt", "org_id": org_id, "days": days, "points": written},
    )
    return written


def roll_up(snapshots: list[KpiSnapshot]) -> Decimal | None:
    """Combine same-metric snapshots across scopes correctly.

    Re-divides the summed numerator by the summed denominator. Averaging the
    percentages instead is the classic silent error: two properties at 100% and
    50% occupancy are not 75% occupied unless they are the same size.
    """
    if not snapshots:
        return None
    numerators = [s.numerator for s in snapshots if s.numerator is not None]
    denominators = [s.denominator for s in snapshots if s.denominator is not None]
    if not numerators or not denominators:
        return None
    total = sum(denominators, ZERO)
    if total <= ZERO:
        return None
    return (sum(numerators, ZERO) / total).quantize(Decimal("0.0001"))

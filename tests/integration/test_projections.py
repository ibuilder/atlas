"""KPI projections.

The two acceptance cases: a projection is reconstructible from operational
data, and a stale one is a rebuild rather than a correctness problem.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.models.reporting import KpiSnapshot
from app.services.reporting.projections import (
    known_metrics,
    rebuild_series,
    roll_up,
    snapshot_metrics,
)

pytestmark = pytest.mark.integration

TODAY = dt.date(2026, 3, 31)


def _value(db, org, key, as_of=TODAY):
    return (
        db.session.query(KpiSnapshot)
        .filter(KpiSnapshot.metric_key == key, KpiSnapshot.as_of_date == as_of)
        .one()
    )


# ------------------------------------------------------------------ catalogue


def test_the_catalogue_lists_its_metrics():
    for key in ("occupancy_rate", "delinquency_rate", "work_order_sla_compliance"):
        assert key in known_metrics()


def test_an_unknown_metric_is_skipped_not_fatal(db, org, scope, accounts):
    stored = snapshot_metrics(
        db.session, org_id=org.id, as_of=TODAY, metric_keys=["occupancy_rate", "not_a_metric"]
    )
    db.session.commit()
    assert [s.metric_key for s in stored] == ["occupancy_rate"]


# ------------------------------------------------------------------- values


def test_occupancy_counts_occupied_over_rentable(db, org, scope, unit_record, lease_record):
    lease_record.start_date = dt.date(2026, 1, 1)
    db.session.commit()

    snapshot_metrics(db.session, org_id=org.id, as_of=TODAY, metric_keys=["occupancy_rate"])
    db.session.commit()

    snapshot = _value(db, org, "occupancy_rate")
    assert snapshot.numerator == Decimal("1.0000")
    assert snapshot.denominator >= Decimal("1.0000")
    assert snapshot.numeric_value is not None


def test_a_rate_keeps_its_parts_so_a_rollup_can_redivide(db, org, scope, unit_record, lease_record):
    """Averaging percentages across properties is the classic silent error."""
    lease_record.start_date = dt.date(2026, 1, 1)
    db.session.commit()
    snapshot_metrics(db.session, org_id=org.id, as_of=TODAY, metric_keys=["occupancy_rate"])
    db.session.commit()

    snapshot = _value(db, org, "occupancy_rate")
    assert snapshot.numerator is not None
    assert snapshot.denominator is not None


def test_rolling_up_redivides_rather_than_averaging():
    """100% of ten units and 50% of two is not 75% occupied."""

    class _Fake:
        def __init__(self, numerator, denominator):
            self.numerator = Decimal(numerator)
            self.denominator = Decimal(denominator)

    combined = roll_up([_Fake(10, 10), _Fake(1, 2)])
    assert combined == (Decimal(11) / Decimal(12)).quantize(Decimal("0.0001"))
    assert combined != Decimal("0.7500")


def test_a_count_metric_stores_a_count(db, org, scope, property_record):
    from app.models.maintenance import Priority
    from app.services.maintenance.service import create_work_order

    create_work_order(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        title="Leak",
        description="Leak under the sink.",
        priority=Priority.NORMAL,
    )
    db.session.commit()

    snapshot_metrics(db.session, org_id=org.id, as_of=TODAY, metric_keys=["open_work_orders"])
    db.session.commit()

    snapshot = _value(db, org, "open_work_orders")
    assert snapshot.count_value == 1
    assert snapshot.numeric_value is None


def test_net_operating_income_is_income_less_expense(db, org, scope, accounts):
    from app.services.accounting.chart import AccountCode
    from app.services.accounting.ledger import LineInput, post_journal_entry

    post_journal_entry(
        db.session,
        org_id=org.id,
        entry_date=dt.date(2026, 3, 5),
        description="Rent",
        lines=[
            LineInput(account_id=accounts[AccountCode.CASH_OPERATING].id, debit=Decimal("5000")),
            LineInput(account_id=accounts[AccountCode.RENTAL_INCOME].id, credit=Decimal("5000")),
        ],
    )
    post_journal_entry(
        db.session,
        org_id=org.id,
        entry_date=dt.date(2026, 3, 10),
        description="Repair",
        lines=[
            LineInput(
                account_id=accounts[AccountCode.REPAIRS_MAINTENANCE].id, debit=Decimal("1200")
            ),
            LineInput(account_id=accounts[AccountCode.CASH_OPERATING].id, credit=Decimal("1200")),
        ],
    )
    db.session.commit()

    snapshot_metrics(db.session, org_id=org.id, as_of=TODAY, metric_keys=["net_operating_income"])
    db.session.commit()

    snapshot = _value(db, org, "net_operating_income")
    assert snapshot.numeric_value == Decimal("3800.0000")
    assert snapshot.detail["income"] == "5000.0000"


def test_a_period_with_no_activity_is_zero_not_missing(db, org, scope, accounts):
    snapshot_metrics(db.session, org_id=org.id, as_of=TODAY, metric_keys=["net_operating_income"])
    db.session.commit()
    assert _value(db, org, "net_operating_income").numeric_value == Decimal("0.0000")


def test_a_metric_with_no_denominator_records_no_rate(db, org, scope, accounts):
    """No invoices means no delinquency rate, not a division by zero."""
    snapshot_metrics(db.session, org_id=org.id, as_of=TODAY, metric_keys=["delinquency_rate"])
    db.session.commit()

    snapshot = _value(db, org, "delinquency_rate")
    assert snapshot.numeric_value is None
    assert snapshot.denominator == Decimal("0.0000")


# --------------------------------------------------------------- rebuilding


def test_recomputing_a_day_corrects_it_rather_than_duplicating(db, org, scope, accounts):
    """The acceptance case: a stale projection is a rebuild, not a problem."""
    snapshot_metrics(db.session, org_id=org.id, as_of=TODAY, metric_keys=["open_work_orders"])
    db.session.commit()
    first = _value(db, org, "open_work_orders").count_value

    from app.models.maintenance import Priority
    from app.models.org import Property, PropertyType
    from app.services.maintenance.service import create_work_order

    prop = Property(
        org_id=org.id,
        code="P2",
        name="Second",
        property_type=PropertyType.RESIDENTIAL_MULTI,
        address_line1="2 Test Way",
        city="Testville",
        region="TS",
        postal_code="00001",
    )
    db.session.add(prop)
    db.session.commit()
    create_work_order(
        db.session,
        org_id=org.id,
        property_id=prop.id,
        title="Leak",
        description="Leak.",
        priority=Priority.NORMAL,
    )
    db.session.commit()

    snapshot_metrics(db.session, org_id=org.id, as_of=TODAY, metric_keys=["open_work_orders"])
    db.session.commit()

    assert first == 0
    assert _value(db, org, "open_work_orders").count_value == 1
    assert db.session.query(KpiSnapshot).filter_by(metric_key="open_work_orders").count() == 1


def test_a_series_rebuilds_across_a_range(db, org, scope, accounts):
    written = rebuild_series(
        db.session,
        org_id=org.id,
        start=dt.date(2026, 3, 1),
        end=dt.date(2026, 3, 5),
        metric_keys=["open_work_orders"],
    )
    db.session.commit()

    assert written == 5
    assert db.session.query(KpiSnapshot).filter_by(metric_key="open_work_orders").count() == 5


def test_rebuilding_twice_is_idempotent(db, org, scope, accounts):
    for _ in range(2):
        rebuild_series(
            db.session,
            org_id=org.id,
            start=dt.date(2026, 3, 1),
            end=dt.date(2026, 3, 3),
            metric_keys=["open_work_orders"],
        )
        db.session.commit()

    assert db.session.query(KpiSnapshot).filter_by(metric_key="open_work_orders").count() == 3


def test_a_reversed_range_is_refused(db, org, scope):
    with pytest.raises(ValueError):
        rebuild_series(
            db.session,
            org_id=org.id,
            start=dt.date(2026, 3, 31),
            end=dt.date(2026, 3, 1),
        )


def test_projections_do_not_cross_organizations(db, org, other_org, scope, accounts):
    snapshot_metrics(db.session, org_id=org.id, as_of=TODAY, metric_keys=["open_work_orders"])
    db.session.commit()
    assert db.session.query(KpiSnapshot).filter(KpiSnapshot.org_id == other_org.id).count() == 0

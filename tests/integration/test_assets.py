"""Asset lifecycle and capital planning.

The two things worth proving: warranty is resolved *before* money is spent, and
a forecast says where it is guessing rather than quietly contributing zero to a
budget somebody commits to.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.errors import BusinessRuleViolation, ValidationFailed
from app.models.asset_graph import (
    Asset,
    AssetCategory,
    AssetCriticality,
    AssetStatus,
    ServiceEventType,
    Warranty,
)
from app.services.assets.capital import (
    DEFAULT_INFLATION,
    forecast_asset,
    inflate,
    plan_as_rows,
    plan_capital,
)
from app.services.assets.lifecycle import (
    check_warranty,
    record_service,
    recover_under_warranty,
    repair_or_replace,
    retire_asset,
)

pytestmark = pytest.mark.integration

TODAY = dt.date.today()


@pytest.fixture()
def boiler(db, org, scope, property_record):
    record = Asset(
        org_id=org.id,
        code="BOIL-01",
        name="Main boiler",
        category=AssetCategory.HVAC,
        status=AssetStatus.ACTIVE,
        criticality=AssetCriticality.CRITICAL,
        property_id=property_record.id,
        manufacturer="Vaillant",
        installed_on=TODAY - dt.timedelta(days=365 * 8),
        expected_life_years=15,
        purchase_price=Decimal("4200.00"),
        replacement_cost=Decimal("6000.00"),
    )
    db.session.add(record)
    db.session.commit()
    return record


def _warranty(db, org, asset, *, years=5, labor=True, provider="Vaillant"):
    record = Warranty(
        org_id=org.id,
        asset_id=asset.id,
        provider=provider,
        kind="manufacturer",
        starts_on=TODAY - dt.timedelta(days=30),
        expires_on=TODAY + dt.timedelta(days=365 * years),
        covers_parts=True,
        covers_labor=labor,
    )
    db.session.add(record)
    db.session.commit()
    return record


# ------------------------------------------------------------------ warranty


def test_an_asset_with_no_warranty_is_not_covered(db, org, scope, boiler):
    check = check_warranty(db.session, asset=boiler)
    assert check.covered is False
    assert "No warranty" in check.reason


def test_a_live_warranty_covers(db, org, scope, boiler):
    _warranty(db, org, boiler)
    check = check_warranty(db.session, asset=boiler)

    assert check.covered is True
    assert check.claimable_labor is True
    assert "Vaillant covers parts and labour" in check.reason


def test_an_expired_warranty_says_when_it_lapsed(db, org, scope, boiler):
    warranty = _warranty(db, org, boiler)
    warranty.expires_on = TODAY - dt.timedelta(days=10)
    db.session.commit()

    check = check_warranty(db.session, asset=boiler)
    assert check.covered is False
    assert "expired on" in check.reason


def test_the_longest_cover_is_the_one_worth_claiming(db, org, scope, boiler):
    _warranty(db, org, boiler, years=1, provider="Installer")
    _warranty(db, org, boiler, years=6, provider="Manufacturer")

    check = check_warranty(db.session, asset=boiler)
    assert check.warranty.provider == "Manufacturer"


def test_recording_work_resolves_cover_automatically(db, org, scope, boiler):
    """ "We paid for covered work" should be visible in the data, not remembered."""
    _warranty(db, org, boiler)
    event = record_service(
        db.session,
        asset=boiler,
        event_type=ServiceEventType.REPAIR,
        performed_on=TODAY,
        cost=Decimal("450.00"),
    )
    db.session.commit()
    assert event.was_under_warranty is True


def test_work_outside_cover_is_not_marked(db, org, scope, boiler):
    event = record_service(
        db.session,
        asset=boiler,
        event_type=ServiceEventType.REPAIR,
        performed_on=TODAY,
        cost=Decimal("450.00"),
    )
    db.session.commit()
    assert event.was_under_warranty is False


def test_a_paid_repair_can_be_identified_as_recoverable_later(db, org, scope, boiler):
    event = record_service(
        db.session,
        asset=boiler,
        event_type=ServiceEventType.REPAIR,
        performed_on=TODAY,
        cost=Decimal("450.00"),
    )
    db.session.commit()
    assert event.was_under_warranty is False

    _warranty(db, org, boiler)
    recover_under_warranty(db.session, event=event)
    db.session.commit()
    assert event.was_under_warranty is True


def test_uncovered_work_cannot_be_claimed_retrospectively(db, org, scope, boiler):
    event = record_service(
        db.session,
        asset=boiler,
        event_type=ServiceEventType.REPAIR,
        performed_on=TODAY,
        cost=Decimal("450.00"),
    )
    db.session.commit()
    with pytest.raises(BusinessRuleViolation):
        recover_under_warranty(db.session, event=event)


# ------------------------------------------------------------------- history


def test_recording_service_derives_the_asset_aggregates(db, org, scope, boiler):
    record_service(
        db.session,
        asset=boiler,
        event_type=ServiceEventType.REPAIR,
        performed_on=TODAY - dt.timedelta(days=10),
        cost=Decimal("300.00"),
        condition_after=3,
    )
    record_service(
        db.session,
        asset=boiler,
        event_type=ServiceEventType.PREVENTIVE,
        performed_on=TODAY,
        cost=Decimal("120.00"),
        condition_after=4,
    )
    db.session.commit()

    assert boiler.service_count == 2
    assert boiler.lifetime_service_cost == Decimal("420.0000")
    assert boiler.last_serviced_on == TODAY
    assert boiler.condition_score == 4


def test_an_older_event_does_not_move_the_last_serviced_date(db, org, scope, boiler):
    """History arrives out of order; the aggregate must not follow it backwards."""
    record_service(
        db.session,
        asset=boiler,
        event_type=ServiceEventType.PREVENTIVE,
        performed_on=TODAY,
        cost=Decimal("100.00"),
        condition_after=5,
    )
    record_service(
        db.session,
        asset=boiler,
        event_type=ServiceEventType.REPAIR,
        performed_on=TODAY - dt.timedelta(days=200),
        cost=Decimal("500.00"),
        condition_after=2,
    )
    db.session.commit()

    assert boiler.last_serviced_on == TODAY
    assert boiler.condition_score == 5
    assert boiler.lifetime_service_cost == Decimal("600.0000")


def test_future_dated_service_is_refused(db, org, scope, boiler):
    with pytest.raises(ValidationFailed):
        record_service(
            db.session,
            asset=boiler,
            event_type=ServiceEventType.REPAIR,
            performed_on=TODAY + dt.timedelta(days=1),
        )


def test_an_impossible_condition_score_is_refused(db, org, scope, boiler):
    with pytest.raises(ValidationFailed):
        record_service(
            db.session,
            asset=boiler,
            event_type=ServiceEventType.INSPECTION,
            performed_on=TODAY,
            condition_after=9,
        )


# -------------------------------------------------------- repair or replace


def _repairs(db, boiler, count, cost="500.00"):
    for index in range(count):
        record_service(
            db.session,
            asset=boiler,
            event_type=ServiceEventType.REPAIR,
            performed_on=TODAY - dt.timedelta(days=30 * index),
            cost=Decimal(cost),
        )
    db.session.commit()


def test_a_healthy_asset_is_still_a_repair(db, org, scope, boiler):
    _repairs(db, boiler, 1, "200.00")
    advice = repair_or_replace(db.session, asset=boiler)

    assert advice.recommendation == "repair"
    assert advice.failures_last_year == 1


def test_one_signal_is_a_conversation_not_a_conclusion(db, org, scope, boiler):
    _repairs(db, boiler, 3, "100.00")
    advice = repair_or_replace(db.session, asset=boiler)

    assert advice.recommendation == "assess"
    assert advice.failures_last_year == 3


def test_cost_and_repeat_failure_together_say_replace(db, org, scope, boiler):
    """Four call-outs and half the replacement cost is not a repair decision."""
    _repairs(db, boiler, 4, "800.00")
    advice = repair_or_replace(db.session, asset=boiler)

    assert advice.recommendation == "replace"
    assert advice.cost_ratio >= Decimal("0.50")
    assert any("repairs in the last twelve months" in reason for reason in advice.reasons)


def test_preventive_visits_do_not_count_as_failures(db, org, scope, boiler):
    """A well-maintained boiler must not look like a liability."""
    for index in range(6):
        record_service(
            db.session,
            asset=boiler,
            event_type=ServiceEventType.PREVENTIVE,
            performed_on=TODAY - dt.timedelta(days=30 * index),
            cost=Decimal("100.00"),
        )
    db.session.commit()

    advice = repair_or_replace(db.session, asset=boiler)
    assert advice.failures_last_year == 0


def test_a_missing_replacement_cost_is_stated(db, org, scope, boiler):
    boiler.replacement_cost = None
    db.session.commit()

    advice = repair_or_replace(db.session, asset=boiler)
    assert advice.cost_ratio is None
    assert any("guess" in reason for reason in advice.reasons)


# ---------------------------------------------------------------- retirement


def test_retiring_keeps_the_history(db, org, scope, boiler):
    _repairs(db, boiler, 2)
    retire_asset(db.session, asset=boiler, reason="Beyond economic repair.")
    db.session.commit()

    assert boiler.status == AssetStatus.RETIRED
    assert boiler.service_count == 3  # two repairs plus the decommission record
    assert db.session.get(Asset, boiler.id) is not None


def test_retiring_without_a_reason_is_refused(db, org, scope, boiler):
    with pytest.raises(ValidationFailed):
        retire_asset(db.session, asset=boiler, reason="   ")


def test_a_replacement_is_linked_both_ways(db, org, scope, boiler, property_record):
    successor = Asset(
        org_id=org.id,
        code="BOIL-02",
        name="Replacement boiler",
        category=AssetCategory.HVAC,
        property_id=property_record.id,
    )
    db.session.add(successor)
    db.session.commit()

    retire_asset(db.session, asset=boiler, reason="Replaced.", replaced_by=successor)
    db.session.commit()

    assert boiler.attributes["replaced_by_asset_id"] == successor.id
    assert successor.attributes["replaces_asset_id"] == boiler.id


# ------------------------------------------------------------ capital plan


def test_inflation_compounds_forward_only():
    assert inflate(Decimal("1000"), years=0, rate=Decimal("0.035")) == Decimal("1000.0000")
    assert inflate(Decimal("1000"), years=-3, rate=Decimal("0.035")) == Decimal("1000.0000")
    assert inflate(Decimal("1000"), years=2, rate=Decimal("0.10")) == Decimal("1210.0000")


def test_an_asset_with_no_basis_cannot_be_forecast(db, org, scope, property_record):
    """Guessing here puts a number in a budget that nothing supports."""
    bare = Asset(
        org_id=org.id,
        code="MYSTERY",
        name="Unknown plant",
        category=AssetCategory.OTHER,
        property_id=property_record.id,
    )
    db.session.add(bare)
    db.session.commit()

    assert forecast_asset(db.session, asset=bare) is None


def test_age_alone_produces_an_estimate(db, org, scope, boiler):
    entry = forecast_asset(db.session, asset=boiler)
    assert entry is not None
    assert entry.confidence == "estimated"
    assert entry.replace_on > TODAY
    assert any("expected life" in driver for driver in entry.drivers)


def test_poor_condition_pulls_the_date_forward(db, org, scope, boiler):
    baseline = forecast_asset(db.session, asset=boiler).replace_on

    boiler.condition_score = 2
    db.session.commit()
    worse = forecast_asset(db.session, asset=boiler)

    assert worse.replace_on < baseline
    assert worse.confidence == "measured"
    assert any("Condition 2/5" in driver for driver in worse.drivers)


def test_good_condition_extends_it(db, org, scope, boiler):
    baseline = forecast_asset(db.session, asset=boiler).replace_on

    boiler.condition_score = 5
    db.session.commit()
    better = forecast_asset(db.session, asset=boiler)

    assert better.replace_on > baseline


def test_repeated_failures_pull_the_date_forward(db, org, scope, boiler):
    baseline = forecast_asset(db.session, asset=boiler).replace_on
    _repairs(db, boiler, 3, "200.00")

    stressed = forecast_asset(db.session, asset=boiler)
    assert stressed.replace_on < baseline
    assert stressed.confidence == "measured"


def test_a_missing_cost_is_reported_not_treated_as_zero(db, org, scope, boiler):
    boiler.replacement_cost = None
    db.session.commit()

    entry = forecast_asset(db.session, asset=boiler)
    assert entry.base_cost is None
    assert entry.confidence == "unknown"
    assert any("contributes nothing" in driver for driver in entry.drivers)


def test_a_plan_groups_by_year_and_inflates(db, org, scope, property_record):
    for index, years_left in enumerate((1, 3)):
        db.session.add(
            Asset(
                org_id=org.id,
                code=f"AS-{index}",
                name=f"Asset {index}",
                category=AssetCategory.HVAC,
                property_id=property_record.id,
                expected_replacement_on=TODAY + dt.timedelta(days=365 * years_left + 30),
                replacement_cost=Decimal("1000.00"),
            )
        )
    db.session.commit()

    plan = plan_capital(db.session, org_id=org.id, horizon_years=5, inflation=Decimal("0.10"))

    populated = [year for year in plan.years if year.entries]
    assert len(populated) == 2
    # The nearer one inflates by one year, the further by three.
    costs = sorted(entry.forecast_cost for year in populated for entry in year.entries)
    assert costs[0] == Decimal("1100.0000")
    assert costs[1] == Decimal("1331.0000")


def test_an_overdue_asset_is_separated_from_the_plan(db, org, scope, property_record):
    db.session.add(
        Asset(
            org_id=org.id,
            code="OLD",
            name="Overdue plant",
            category=AssetCategory.HVAC,
            property_id=property_record.id,
            expected_replacement_on=TODAY - dt.timedelta(days=400),
            replacement_cost=Decimal("2000.00"),
        )
    )
    db.session.commit()

    plan = plan_capital(db.session, org_id=org.id)
    assert len(plan.overdue) == 1
    assert plan.overdue[0].asset.code == "OLD"
    assert all(not year.entries for year in plan.years)


def test_unforecastable_assets_are_listed_not_hidden(db, org, scope, property_record):
    db.session.add(
        Asset(
            org_id=org.id,
            code="MYSTERY",
            name="Unknown plant",
            category=AssetCategory.OTHER,
            property_id=property_record.id,
        )
    )
    db.session.commit()

    plan = plan_capital(db.session, org_id=org.id)
    assert [asset.code for asset in plan.unforecastable] == ["MYSTERY"]


def test_a_retired_asset_is_not_in_the_plan(db, org, scope, boiler):
    boiler.status = AssetStatus.RETIRED
    db.session.commit()

    plan = plan_capital(db.session, org_id=org.id)
    assert not any(year.entries for year in plan.years)
    assert plan.unforecastable == []


def test_assets_beyond_the_horizon_are_dropped_not_piled_into_year_five(
    db, org, scope, property_record
):
    """Otherwise the final year looks like a cliff that is not there."""
    db.session.add(
        Asset(
            org_id=org.id,
            code="FAR",
            name="Distant replacement",
            category=AssetCategory.STRUCTURE,
            property_id=property_record.id,
            expected_replacement_on=TODAY + dt.timedelta(days=365 * 20),
            replacement_cost=Decimal("50000.00"),
        )
    )
    db.session.commit()

    plan = plan_capital(db.session, org_id=org.id, horizon_years=5)
    assert plan.total_known_cost == Decimal("0.0000")


def test_the_plan_counts_what_it_does_not_know(db, org, scope, boiler):
    boiler.replacement_cost = None
    boiler.expected_replacement_on = TODAY + dt.timedelta(days=400)
    db.session.commit()

    plan = plan_capital(db.session, org_id=org.id)
    assert plan.assets_with_unknown_cost == 1


def test_an_implausible_horizon_is_refused(db, org, scope):
    with pytest.raises(ValidationFailed):
        plan_capital(db.session, org_id=org.id, horizon_years=99)


def test_an_implausible_inflation_rate_is_refused(db, org, scope):
    with pytest.raises(ValidationFailed):
        plan_capital(db.session, org_id=org.id, inflation=Decimal("3.0"))


def test_a_plan_flattens_for_a_report(db, org, scope, boiler):
    boiler.expected_replacement_on = TODAY + dt.timedelta(days=400)
    db.session.commit()

    rows = plan_as_rows(plan_capital(db.session, org_id=org.id))
    assert rows
    assert rows[0]["asset"] == "BOIL-01"
    assert rows[0]["why"]


def test_the_default_inflation_rate_is_construction_not_consumer():
    assert Decimal("0.03") <= DEFAULT_INFLATION


def test_plans_do_not_cross_organizations(db, org, other_org, scope, boiler):
    plan = plan_capital(db.session, org_id=other_org.id)
    assert plan.unforecastable == []
    assert all(not year.entries for year in plan.years)

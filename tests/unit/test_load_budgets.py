"""The load profile's pass/fail arithmetic.

This is the function that decides whether a load run *passed*, and until it was
extracted from the locustfile it could not be imported without locust installed
- so nothing had ever executed it. A load test whose verdict is wrong is worse
than no load test, because it produces a number somebody quotes.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pytest

from tests.load.budgets import BUDGETS, Measurement, breaches, budget_for

pytestmark = pytest.mark.unit


def test_the_budgets_match_the_checklist():
    """P95 under 300ms for reads and 700ms for writes, as committed to."""
    assert BUDGETS["read"] == 300
    assert BUDGETS["write"] == 700


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("read: dashboard", 300),
        ("write: raise request", 700),
        ("report: rent roll", 5_000),
        ("auth: sign in", None),
        ("nonsense", None),
    ],
)
def test_the_budget_comes_from_the_name_prefix(name, expected):
    assert budget_for(name) == expected


def test_a_run_inside_its_budgets_passes():
    assert (
        breaches(
            [
                Measurement("read: dashboard", requests=500, p95_ms=180),
                Measurement("write: raise request", requests=40, p95_ms=610),
            ]
        )
        == []
    )


def test_a_breach_is_reported_with_its_number():
    found = breaches([Measurement("read: dashboard", requests=500, p95_ms=412)])
    assert len(found) == 1
    assert "412ms" in found[0]
    assert "300ms budget" in found[0]


def test_the_boundary_is_not_a_breach():
    """Exactly at budget is inside it. Otherwise the target is unhittable."""
    assert breaches([Measurement("read: dashboard", requests=10, p95_ms=300)]) == []
    assert breaches([Measurement("read: dashboard", requests=10, p95_ms=301)])


def test_an_endpoint_that_never_ran_is_not_counted_as_passing():
    """A task that never fired has not met its budget - it was not measured.

    Reporting it green is how a profile with a broken task list looks healthy.
    """
    assert breaches([Measurement("read: dashboard", requests=0, p95_ms=99_999)]) == []


def test_an_unbudgeted_endpoint_is_ignored():
    assert breaches([Measurement("auth: sign in", requests=50, p95_ms=9_000)]) == []


def test_a_failure_rate_fails_the_run_on_its_own():
    """A fast error is still an error.

    Latency-only checking reports a green run against a server returning 500s
    in two milliseconds.
    """
    found = breaches([Measurement("read: dashboard", requests=500, p95_ms=12)], fail_ratio=0.05)
    assert len(found) == 1
    assert "failure rate" in found[0]


def test_a_tolerable_failure_rate_does_not():
    assert (
        breaches([Measurement("read: dashboard", requests=500, p95_ms=12)], fail_ratio=0.005) == []
    )


def test_every_breach_in_a_run_is_reported_not_just_the_first():
    """An operator fixing one at a time needs the whole list."""
    found = breaches(
        [
            Measurement("read: dashboard", requests=500, p95_ms=900),
            Measurement("read: trial balance", requests=100, p95_ms=1_200),
            Measurement("write: raise request", requests=40, p95_ms=2_000),
        ],
        fail_ratio=0.5,
    )
    assert len(found) == 4

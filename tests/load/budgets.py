"""The pass/fail arithmetic for a load run, with no dependency on locust.

Extracted from the profile deliberately. This is the code that decides whether
a load test *passed*, and it lived inside a module that cannot be imported
without locust installed - so the one function whose correctness determines
whether the exercise means anything had never been executed by anything.

Nothing here imports locust, so it is testable in the ordinary suite, and the
profile calls it rather than restating it.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["BUDGETS", "Measurement", "breaches", "budget_for"]

#: From the 1.0 checklist. Milliseconds, at the 95th percentile.
BUDGETS: dict[str, int] = {
    "read": 300,
    "write": 700,
    #: Reports touch half the ledger and are run deliberately, not incidentally.
    "report": 5_000,
}

#: A failure rate above this fails the run regardless of latency. A fast error
#: is still an error, and a profile that only measures latency will happily
#: report a green run against a server returning 500s in two milliseconds.
MAX_FAIL_RATIO = 0.01


@dataclass(frozen=True)
class Measurement:
    """One measured endpoint, named ``kind: detail``."""

    name: str
    requests: int
    p95_ms: float


def budget_for(name: str) -> int | None:
    """The budget a measurement is held to, from its name prefix.

    ``None`` where the name carries no recognised prefix - sign-in, for
    instance, which is measured but not budgeted.
    """
    kind = str(name).split(":", 1)[0].strip()
    return BUDGETS.get(kind)


def breaches(measurements: Iterable[Measurement], *, fail_ratio: float = 0.0) -> list[str]:
    """Every budget breach in a run, as sentences. Empty means it passed.

    Endpoints with no requests are skipped rather than treated as passing: a
    task that never ran has not met its budget, it has not been measured, and
    reporting it as green is how a broken profile looks healthy.
    """
    found: list[str] = []

    for measurement in measurements:
        if measurement.requests == 0:
            continue
        budget = budget_for(measurement.name)
        if budget is None:
            continue
        if measurement.p95_ms > budget:
            found.append(
                f"{measurement.name}: P95 {measurement.p95_ms:.0f}ms exceeds the {budget}ms budget"
            )

    if fail_ratio > MAX_FAIL_RATIO:
        found.append(f"failure rate {fail_ratio:.2%} exceeds {MAX_FAIL_RATIO:.0%}")

    return found

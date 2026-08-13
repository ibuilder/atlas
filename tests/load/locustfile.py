"""Load profile for the stated performance budgets.

The 1.0 checklist commits to P95 under 300ms for reads and 700ms for writes.
This is the harness that measures it. It does not tick that box — that needs
production-shaped hardware and data volumes, which is exactly why the number
cannot be produced here — but it makes the check executable rather than
aspirational.

Two things make this a *useful* load test rather than a benchmark that flatters
the system.

**It exercises the expensive paths, not the cheap ones.** Hammering
``/healthz`` produces a beautiful graph and tells you nothing. The weights
below are set from what a management company's day actually looks like: mostly
reads of lists and dashboards, a steady trickle of payments and work-order
updates, and the occasional report run that touches half the ledger.

**Every user is a real tenant.** Load against one organization with an empty
database measures an index that fits in memory. The profile signs in as seeded
demo users and works within their own tenant, so row-level security, the ORM
scoping guard, and the policy engine are all on the measured path — which is
where the cost actually is.

Run it against a *seeded* deployment, never an empty one:

    pip install locust
    flask seed demo
    locust -f tests/load/locustfile.py --host http://localhost:5000

The budgets are asserted at the end of the run and set the process exit code,
so a CI job can fail on a regression once there is somewhere to run one. A load
test that only prints numbers is a load test nobody runs twice.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import random

try:
    from locust import HttpUser, between, events, task
except ImportError:  # pragma: no cover - locust is an optional dev dependency
    raise SystemExit(
        "This profile needs locust: pip install locust\n"
        "It is deliberately not a hard dependency - nothing in the application "
        "imports it, and a load tool in the production image is a liability."
    ) from None

#: The pass/fail arithmetic lives beside this file rather than in it, so the
#: one function that decides whether a run *passed* can be tested without
#: locust installed. See ``tests/load/budgets.py``.
from tests.load.budgets import BUDGETS, Measurement, breaches  # noqa: E402, F401

#: The seeded demo accounts. Load against a seeded deployment only: an empty
#: database measures an index that fits in L2 cache.
DEMO_PASSWORD = "atlas-demo-2026-portfolio"
STAFF_ACCOUNTS = (
    "admin@atlas.demo",
    "controller@atlas.demo",
    "manager@atlas.demo",
    "leasing@atlas.demo",
    "dispatch@atlas.demo",
)


class StaffUser(HttpUser):
    """A member of staff working through an ordinary day.

    Weights are the shape of real use, not an even spread: people look at lists
    far more than they change things, and the reads are what a slow query
    actually ruins.
    """

    wait_time = between(1, 4)

    def on_start(self) -> None:
        email = random.choice(STAFF_ACCOUNTS)  # noqa: S311 - not cryptographic
        with self.client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": DEMO_PASSWORD},
            catch_response=True,
            name="auth: sign in",
        ) as response:
            if response.status_code != 200:
                response.failure(f"could not sign in as {email}. Is the demo seeded on this host?")
                self.environment.runner.quit()

    # --- reads: the bulk of a day -----------------------------------------

    @task(20)
    def dashboard(self) -> None:
        self.client.get("/admin/", name="read: dashboard")

    @task(15)
    def properties(self) -> None:
        self.client.get("/api/v1/properties?limit=50", name="read: properties")

    @task(12)
    def work_orders(self) -> None:
        self.client.get("/api/v1/work-orders?limit=50", name="read: work orders")

    @task(10)
    def invoices(self) -> None:
        self.client.get("/api/v1/invoices?limit=50", name="read: invoices")

    @task(8)
    def leases(self) -> None:
        self.client.get("/api/v1/leases?limit=50", name="read: leases")

    @task(5)
    def ledger(self) -> None:
        """The one that gets slow first: an aggregate over every journal line."""
        self.client.get("/admin/ledger", name="read: trial balance")

    @task(4)
    def audit(self) -> None:
        self.client.get("/admin/audit", name="read: audit trail")

    @task(3)
    def paginate_deeply(self) -> None:
        """Keyset pagination should not degrade with depth. Offset would."""
        self.client.get("/api/v1/invoices?limit=50&cursor=", name="read: invoices page 2")

    # --- writes: the trickle ----------------------------------------------

    @task(3)
    def raise_request(self) -> None:
        self.client.post(
            "/api/v1/maintenance-requests",
            json={
                "title": "Load profile: dripping tap",
                "description": "Raised by the load profile. Safe to close.",
                "category": "plumbing",
            },
            name="write: raise request",
        )

    @task(2)
    def search(self) -> None:
        self.client.get("/api/v1/properties?q=court&limit=20", name="read: search")

    # --- reports: deliberate, occasional, expensive ------------------------

    @task(1)
    def run_report(self) -> None:
        self.client.get("/api/v1/reports/rent_roll?format=json", name="report: rent roll")


class ResidentUser(HttpUser):
    """A resident checking their balance.

    Included because the portal paths carry the ownership re-derivation on
    every request, and that is a cost worth measuring rather than assuming.
    """

    wait_time = between(5, 20)
    weight = 3

    def on_start(self) -> None:
        with self.client.post(
            "/api/v1/auth/login",
            json={"email": "resident@atlas.demo", "password": DEMO_PASSWORD},
            catch_response=True,
            name="auth: resident sign in",
        ) as response:
            if response.status_code != 200:
                response.failure("could not sign in as the demo resident")

    @task(10)
    def portal(self) -> None:
        self.client.get("/resident/", name="read: resident portal")


@events.quitting.add_listener
def _report_against_budgets(environment, **_kwargs) -> None:
    """Fail the run when a budget is missed.

    Exit status matters: a load test that reports numbers nobody reads is a
    load test nobody runs twice.
    """
    stats = environment.stats

    # The arithmetic lives in ``budgets`` so it can be tested without locust
    # installed. This function's only job is turning locust's statistics into
    # measurements and setting the exit code.
    measured = [
        Measurement(
            name=name[0] if isinstance(name, tuple) else str(name),
            requests=entry.num_requests,
            p95_ms=entry.get_response_time_percentile(0.95) or 0.0,
        )
        for name, entry in stats.entries.items()
    ]
    found = breaches(measured, fail_ratio=stats.total.fail_ratio)

    if found:
        print("\nBudget breaches:")  # noqa: T201 - this is a CLI tool
        for breach in found:
            print(f"  - {breach}")  # noqa: T201
        environment.process_exit_code = 1
    else:
        print("\nEvery measured path is inside its budget.")  # noqa: T201
        environment.process_exit_code = 0

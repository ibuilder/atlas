# Load profile

Measures the performance budgets the 1.0 checklist commits to:

| Path | P95 budget |
|---|---|
| Reads | 300 ms |
| Writes | 700 ms |
| Reports | 5 s |

## Running it

Load must run against a **seeded** deployment, and `seed demo` is not the seed
to use. It builds three properties and twenty-six units — enough to walk the
product, and small enough that every query fits in cache and tells you nothing.

```bash
pip install locust
flask seed load --properties 200 --units-per-property 40 --months 24
locust -f tests/load/locustfile.py --host http://localhost:5000
```

`seed load` generates a production-*shaped* organization: deliberately skewed,
because a uniform database measures an index that behaves nothing like the real
thing. A tenth of properties hold four times the units; a twentieth of leases
carry eight times the billing history. That tail is where the slow queries
live, and a flat generator hides it.

It is deterministic — the RNG seed is fixed — so two runs produce the same
database and two results are comparable. A load test you cannot re-run against
the same data is a number, not a measurement.

For reference, on a laptop against SQLite:

| Setting | Produces | Takes |
|---|---|---|
| `--properties 100 --months 24` | 5.3k units, 58k invoices, 227k ledger lines, 58k audit events | ~90s |

Double the properties for roughly double both. Against Postgres on real
hardware it is faster.

The generator bulk-inserts, which goes around the ledger's balance invariant
and the audit chain. It therefore *verifies* both afterwards and refuses to
finish if either fails — a load test against a database that is quietly not
Atlas-shaped measures the wrong system convincingly.

Headless, with a fixed shape, for CI or a recorded run:

```bash
locust -f tests/load/locustfile.py --host http://localhost:5000   --headless --users 50 --spawn-rate 5 --run-time 10m
```

The run exits non-zero if any measured path misses its budget or the failure
rate exceeds 1%.

## What it does and does not tell you

It exercises the paths that actually cost something — dashboards, list
endpoints, the trial balance, the audit trail, and the portal views that
re-derive ownership on every request. Every virtual user signs in as a real
tenant, so row-level security, the ORM scoping guard, and the policy engine are
all on the measured path.

It does **not** tick the 1.0 load-testing condition. That needs
production-shaped hardware and data volumes; this harness makes the check
executable, not satisfied. A number produced on a developer laptop against a
three-property demo is not evidence about a 500-property portfolio, and
recording it as though it were is the failure this repository is trying to
avoid elsewhere.

<sub>SPDX-License-Identifier: MIT</sub>

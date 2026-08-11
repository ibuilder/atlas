# Load profile

Measures the performance budgets the 1.0 checklist commits to:

| Path | P95 budget |
|---|---|
| Reads | 300 ms |
| Writes | 700 ms |
| Reports | 5 s |

## Running it

Load must run against a **seeded** deployment. An empty database measures an
index that fits in cache and tells you nothing about production.

```bash
pip install locust
flask seed demo
locust -f tests/load/locustfile.py --host http://localhost:5000
```

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

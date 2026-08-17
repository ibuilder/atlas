<div align="center">

# Atlas PMOS

**An enterprise property management operating system.**

Leasing · Accounting · Maintenance · Resident & Owner Portals · Documents · Automation · Asset Intelligence
— unified behind one canonical data model, one policy engine, and one audit trail.

[![CI](https://github.com/ibuilder/atlas/actions/workflows/ci.yml/badge.svg)](https://github.com/ibuilder/atlas/actions/workflows/ci.yml)
[![CodeQL](https://github.com/ibuilder/atlas/actions/workflows/codeql.yml/badge.svg)](https://github.com/ibuilder/atlas/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Docs](https://img.shields.io/badge/docs-github%20pages-brightgreen.svg)](https://ibuilder.github.io/atlas/)

[**Live product tour**](https://ibuilder.github.io/atlas/) · [Architecture](docs/ARCHITECTURE.md) · [Domain](docs/DOMAIN.md) · [Deployment](DEPLOYMENT.md) · [ADRs](docs/adr/) · [Runbooks](docs/runbooks/) · [Security](SECURITY.md)

</div>

---

## Why Atlas

Property management software tends to be strong in one place and thin everywhere else — great accounting with weak
maintenance, or a slick resident portal sitting on a shallow ledger. The seams show up as spreadsheets, re-keyed data,
and an audit trail that stops at the module boundary.

Atlas PMOS is built the other way around: **one canonical operations model first, features second.**

| | What most platforms do | What Atlas does |
|---|---|---|
| **Authorization** | permission checks scattered across views | one deny-by-default policy engine, exhaustively tested as a role × action × resource matrix |
| **Audit** | a log table someone can `UPDATE` | append-only events in a per-org **hash chain** — tampering is detectable, not just discouraged |
| **Tenancy** | a `WHERE org_id = ?` you hope nobody forgets | three enforced layers: service scoping, a session guard that *raises* on unscoped tenant queries, and Postgres RLS |
| **Accounting** | a ledger bolted onto operations | double-entry with the balanced invariant enforced in the service **and** in the database; posted entries are immutable, corrections are reversals |
| **Documents** | folders | a document graph — one file linked to a lease, a unit, an inspection, an invoice, and a warranty at once |
| **Assets** | a text field on the unit | an asset registry with service history, warranty windows, and replacement forecasting |

## Quickstart

```bash
git clone https://github.com/ibuilder/atlas.git && cd atlas
make setup     # venv + dependencies + pre-commit hooks
make demo      # migrate, seed a full demo portfolio, and run the app
```

Then open <http://localhost:5000>. `make demo` prints the seeded logins — an admin, an accountant, a maintenance
dispatcher, a resident, an owner, and a vendor, so you can walk every portal from one seed.

Prefer containers:

```bash
docker compose up --build
```

That brings up Postgres, Redis, the web app, a Celery worker, and Celery beat, runs migrations on start, and seeds
the demo tenant. It is a *development* stack: a default secret, HTTPS off, and the database port published, all of
which are conveniences locally and liabilities anywhere else.

To deploy it, use the file that has none of them:

```bash
cp .env.production.example .env.production   # then fill it in; nothing has a working default
docker compose -f docker-compose.prod.yml --env-file .env.production up -d
```

That pulls a published image rather than building, runs migrations as their own step, binds the web port to
loopback for a TLS-terminating proxy to sit in front of, and refuses to start if any secret is unset. See
[DEPLOYMENT.md](DEPLOYMENT.md).

<details>
<summary><b>Manual setup</b></summary>

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev,postgres]"
cp .env.example .env                            # then fill in SECRET_KEY
flask --app wsgi db upgrade
flask --app wsgi seed demo
flask --app wsgi run --debug
```

</details>

## What's in the box

**Platform** — multi-org tenancy with row-level isolation · RBAC + ABAC policy engine · tamper-evident audit ·
versioned `/api/v1` REST API with OpenAPI 3.1 · idempotency keys · cursor pagination · signed webhooks with replay
protection · structured JSON logs with correlation IDs · Prometheus metrics · health, readiness, and migration-status
probes.

**Identity** — local auth with Argon2 · TOTP MFA with single-use recovery codes · device/session management and
revocation · login throttling and lockout · single-use password reset · hashed API tokens · OIDC and SAML 2.0
single sign-on with SCIM directory provisioning.

**Leasing** — leads, guest cards, applications and screening from enquiry to signed lease, lease packets, and
charge schedules driving recurring billing. Renewals, move-outs, and deposit disposition run end to end, with the
statutory disposition clock starting when the move-out is recorded. An embeddable enquiry form drops into an
operator's own website and files leads straight into the funnel. See
[docs/FEATURES.md](docs/FEATURES.md), which states the status of every claim on this page.

**Accounting** — segmented chart of accounts, double-entry journal, trust accounts, AR invoices, AP bills, payments
and payment application, bank reconciliation with an exception queue, period close with approval gating, owner
statements and distributions.

**Maintenance** — intake and habitability-aware triage, work-order lifecycle with actor-attributed timeline, SLA
tracking and breach metrics, vendor assignment and invoice linkage, inspection checklist templates, preventive
maintenance schedules, turn workflows.

**Documents & assets** — pluggable object storage, content-type sniffing and size caps, malware-scan pipeline hook,
signed expiring URLs, polymorphic document links, lease and invoice extraction as reviewable suggestions, asset
registry with warranties, service history, and capital planning.

**Automation** — trigger → condition → actor → action rules with retry policies, escalation paths, human approval
checkpoints, full run history, and a **dry-run harness** so a rule can be proven before it ever touches production data.

See [docs/FEATURES.md](docs/FEATURES.md) for the complete capability matrix, including what is production-complete
versus what is a defined seam awaiting Phase 2/3.

## Architecture at a glance

```
        HTTP ─▶ middleware (correlation id, org resolution, idempotency)
                     │
                  routes ──────▶ schemas (pydantic v2, strict)
                     │
                 services ─────▶ policy engine (deny by default)
                     │      └──▶ audit (hash-chained, append only)
                     │      └──▶ domain events ──▶ Celery ──▶ webhooks
                     │
              repositories ────▶ SQLAlchemy 2.x ──▶ PostgreSQL (+ RLS)
```

A **modular monolith**: hard domain boundaries, event-driven seams, one transaction when the business needs one.
The reasoning — and the conditions under which we'd split it — is in
[ADR-0001](docs/adr/0001-modular-monolith.md).

Business rules never live in route handlers. Every mutating action emits an audit event. Every protected route goes
through the policy engine. Every external integration sits behind an adapter interface. Every background job is
idempotent. These are enforced by tests, not convention — see [tests/security](tests/security/).

## Project layout

```
app/
  config/        environment configs that fail closed in production
  security/      auth, policies, mfa, sessions, tokens
  models/        canonical domain model
  schemas/       pydantic request/response contracts
  services/      business rules, one package per bounded context
  repositories/  persistence abstractions
  api/v1/        versioned REST surface
  web/           admin, resident, owner, and vendor UIs
  tasks/         Celery jobs
  events/        domain event bus
  cli/           operational commands
migrations/      alembic
tests/           unit · integration · contract · security · migration · performance
infra/           docker · k8s · terraform
docs/            architecture, ADRs, runbooks, and the Pages site
```

## Development

```bash
make check      # format, lint, type-check, security scan, and test — everything CI runs
make test       # pytest with coverage
make lint       # ruff + black --check
make typecheck  # mypy
make audit      # bandit + pip-audit
make migrate m="add widget table"
```

Tests default to an in-memory SQLite database so the suite runs anywhere with no services. Point `DATABASE_URL` at
Postgres to run the same suite against the production dialect, including the RLS tests:

```bash
DATABASE_URL=postgresql+psycopg://atlas:atlas@localhost/atlas_test make test
```

## Deployment

Multi-stage, non-root container images; Gunicorn behind an ingress proxy; managed Postgres and Redis; secrets from a
real secret manager. Kubernetes manifests are in [infra/k8s](infra/k8s/) and a Terraform skeleton in
[infra/terraform](infra/terraform/). Operational procedures — auth outage, queue backlog, payment provider outage,
webhook failure, database failover — are in [docs/runbooks](docs/runbooks/).

Production config refuses to start on a weak or missing `SECRET_KEY`, a non-TLS database URL where one is required,
or debug mode left on. Failing closed at boot beats failing open at 3am.

## Contributing

[CONTRIBUTING.md](CONTRIBUTING.md) covers the workflow, commit conventions, and the definition of done. Security
issues go through [SECURITY.md](SECURITY.md) — please don't open a public issue for a vulnerability.

## License

MIT — see [LICENSE](LICENSE).

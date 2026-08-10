# Changelog

All notable changes to Atlas PMOS are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Until 1.0.0, the REST API contract may change between minor versions. Error
codes and the `/api/v1` namespace are already treated as stable.

## [Unreleased]

### Planned
- Webhook delivery loop (the outbox, signing, and backoff schedule are modelled;
  the HTTP dispatcher is not yet implemented).
- Accounts payable services: bill approval routing and disbursement.
- Bank reconciliation workspace with matching suggestions.
- Automation rule execution engine (the schema, dry-run flag, and run history
  are modelled; the evaluator is not yet implemented).
- Inspections and preventive-maintenance generation.
- OIDC and SAML single sign-on.
- Document storage adapters and the malware-scan pipeline.

## [0.1.0] - 2026-08-09

The first release: an enterprise core that runs, migrates, and is tested.

### Added

**Platform**
- Flask application factory with per-environment configuration that **fails
  closed** — production refuses to start on a weak or missing `SECRET_KEY`,
  a non-PostgreSQL database, debug mode, wildcard CORS, or local file storage.
- Three-layer tenant isolation: service-level scoping, an ORM guard that filters
  every tenant query (and refuses unscoped ones in strict mode), and a schema
  invariant test that fails the build if a table carries `org_id` without being
  enrolled.
- Tamper-evident audit trail: append-only events chained by SHA-256 per
  organization, with `verify_chain()` reporting the exact sequence where a
  break begins. Enforced against modification and deletion at the ORM boundary.
- Deny-by-default authorization engine combining RBAC (organization, portfolio,
  and property scopes) with ABAC ownership predicates for portal accounts.
  Unknown actions fail closed. Sensitive actions require a fresh MFA assertion.
- Structured JSON logging with correlation IDs and a PII redaction filter that
  applies to third-party loggers too.
- Prometheus metrics covering request latency, database timing, authentication
  outcomes, ledger postings, payment outcomes, and maintenance SLA breaches.
- Liveness, readiness, and migration-status probes, with readiness refusing
  traffic when a migration is pending.

**Identity**
- Argon2id password hashing with transparent rehash on parameter upgrade, a
  length-first policy following NIST SP 800-63B, and a timing-equalised
  unknown-account path so login cannot be used to enumerate accounts.
- TOTP multi-factor authentication with **replay protection** (a valid code
  cannot be presented twice within its own step) and single-use recovery codes.
- Server-side sessions with individual revocation, idle timeout, and automatic
  invalidation of every session on password change.
- Hashed, prefixed API tokens with optional CIDR allowlists.

**Domain**
- 84-table canonical model spanning organizations, portfolios, properties,
  buildings, units, owners and temporal ownership stakes, leads, applications,
  screening, leases, residents and tenancies, accounting, maintenance, vendors,
  documents, assets, automation, integration, and reporting.
- Double-entry ledger with the balance invariant enforced in the service, by a
  flush-time recomputation from the lines, and by a database `CHECK`. Posted
  entries are immutable; corrections are reversals.
- Accounts receivable: invoicing with ledger posting, payment capture, and
  application (oldest-due-first by default), overpayment retained as credit,
  and processor-webhook deduplication by external identifier.
- Period close with approval gating, and reopening that demands a reason and is
  audited as critical.
- Maintenance intake with habitability detection that overrides under-reported
  urgency, a validated work-order state machine, an actor-attributed timeline,
  SLA deadlines stamped from the policy in force at creation, and a refusal to
  dispatch to a vendor whose insurance has lapsed.

**API and interface**
- Versioned `/api/v1` REST surface with cursor pagination, strict request
  validation that rejects unknown fields, idempotency keys, ETag preconditions,
  and one stable error envelope.
- OpenAPI 3.1 generated from the live URL map, with a server-rendered reference
  page that needs no external scripts.
- Server-rendered admin console plus resident, owner, and vendor portals on a
  dark-first design system built from the Atlas brand tokens.
- Content Security Policy with no `unsafe-inline`, enforced end to end — the
  templates carry no inline styles or scripts.

**Operations**
- Alembic migrations that render Atlas's portable column types correctly for
  both PostgreSQL and SQLite, verified to upgrade and downgrade cleanly.
- Multi-stage, non-root container image; Docker Compose stack with PostgreSQL,
  Redis, web, worker, and beat.
- GitHub Actions: lint, types, security scan, tests across Python 3.11–3.13 on
  SQLite and PostgreSQL, migration upgrade/downgrade/drift check, container
  build with SBOM and vulnerability scan, and CodeQL.
- `flask seed demo` builds a complete demo portfolio — properties, units,
  residents on leases, three months of invoices and payments, maintenance
  running through to completed work orders, and a signed-in account for every
  role.

### Security
- Idempotency keys prevent duplicate payment capture on retry; the same key
  with a different body is rejected rather than silently served.
- Cross-tenant access is reported as `404`, never `403`, so the API cannot be
  used to probe which identifiers exist in other organizations.
- Field-level encryption for MFA seeds, tax identifiers, government IDs, and
  bank details, with comma-separated keys enabling rotation without downtime.
- Aggregate queries (`SELECT count(*)`) are scoped explicitly, closing a gap
  that ORM loader criteria alone do not cover.

[Unreleased]: https://github.com/ibuilder/atlas/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ibuilder/atlas/releases/tag/v0.1.0

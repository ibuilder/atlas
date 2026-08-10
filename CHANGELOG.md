# Changelog

All notable changes to Atlas PMOS are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Until 1.0.0, the REST API contract may change between minor versions. Error
codes and the `/api/v1` namespace are already treated as stable.

## [Unreleased]

### Added

- **Role administration console** (roadmap 4.7). Roles with their permissions
  and holders, and the reverse view - "what can this person do?" is the
  question actually asked. Read-only by design: granting a role from a list is
  one mis-click away from the wrong authority, so the change goes through the
  audited service path and this view exists so the decision is made with the
  current picture in front of you. It also surfaces permissions that no role
  holds, which is what an auditor asks and what a permission matrix answers
  badly.

### Fixed

- **The entire operations console returned 500.** Every admin template imported
  the navigation macro without `with context`, so the macro could not see
  `can()` - which arrives from a context processor - and raised
  `UndefinedError` on render. Every page: dashboard, properties, work orders,
  ledger, audit. It had never been caught because no test asked a console page
  to render. There is now a parameterised test that renders all of them, and a
  new page has to be added to its list.

- **Bulk import with replay** (roadmap 4.4). Properties, units, and vendors
  from CSV, shaped around how this is actually used: somebody exports from
  their old system, half the rows fail on a date format, they fix the
  spreadsheet and upload the whole thing again. So the entire file is validated
  before anything is written - a partial import is worse than a failed one,
  because the operator cannot tell which half landed - and rows are keyed by
  natural business identifiers, so the second upload updates rather than
  duplicating. Errors carry the spreadsheet's row number, because that is the
  number the person is looking at. Two rows claiming the same record is an
  error rather than a decision about which wins. Lives in
  `app.services.imports` rather than `app.services.common`: the primitives
  package is imported *by* services, so it must not import them back.

- **OIDC single sign-on** (roadmap 4.1). Authorization code flow with PKCE.
  The state is a single-use database row rather than a cookie compared to
  itself, so a replayed callback fails instead of establishing a second
  session. The ID token is *verified*, not decoded: signature against the
  provider's published keys, then issuer, audience, expiry, and nonce, with
  `alg: none` and symmetric algorithms refused outright. A provider may only
  speak for its configured email domains, and just-in-time provisioning is
  refused entirely without at least one - otherwise a tenant's own IdP can
  mint an account belonging to somebody else.
- **SAML 2.0** (roadmap 4.2). Signature verification is delegated to `signxml`
  and the module refuses to run without it. Hand-rolling XML-DSIG would mean
  exclusive canonicalisation, reference resolution, and transform handling, and
  a subtly wrong implementation accepts forged assertions while appearing to
  work - so it is not attempted. Verification is against the *configured*
  certificate rather than one embedded in the response, which proves only that
  the sender can sign. Audience restriction, validity window, and single-use
  assertion ids are enforced on top.
- **SCIM 2.0 provisioning** (roadmap 4.3). Deactivation revokes the account's
  sessions in the same operation: marking a user inactive while leaving a live
  session is offboarding that does not offboard, and it is the usual way this
  integration is got wrong. DELETE deactivates rather than removing, because a
  user id appears on ledger entries and audit events. An unsupported filter is
  refused rather than quietly ignored - silently returning everything to a
  query meant to match one person is how a sync deactivates a whole company.
- Migrations that add tenant-scoped tables now call
  `migrations.support.rls.apply_tenant_policies()`. The original RLS migration
  only saw the tables that existed when it ran, so without this a new table
  sits outside the isolation boundary while looking entirely correct.

- **KPI projections** (roadmap 4.6). Five metrics - occupancy, delinquency, SLA
  compliance, open work orders, net operating income - computed nightly into
  `KpiSnapshot` so dashboards stop contending with operational writes. Two
  properties hold: every metric is a pure function of operational data for a
  given date, so a doubtful series is a rebuild rather than a correctness
  problem; and every rate stores its numerator and denominator, so a portfolio
  roll-up re-divides instead of averaging percentages. Two properties at 100%
  and 50% occupancy are not 75% occupied unless they are the same size, and
  that is the reason those columns exist.

- **Bank reconciliation** (roadmap 4.5). Statement import is idempotent over
  overlapping windows, which is the case that actually happens: somebody
  downloads 1-31 March after already loading 1-15. Each line carries a stable
  identity - the bank's reference where there is one, otherwise a fingerprint
  of date, amount, description and an occurrence index - so two genuinely
  identical monthly fees both survive while a re-import of the same file adds
  neither.

  Match suggestions are ranked *and explained*, because an unexplained ranking
  is one nobody trusts and an operator who does not trust it matches everything
  by hand anyway. Automatic matching takes only candidates that are both above
  the confidence threshold and unambiguous: a tie between two payments of the
  same amount on the same day is left for a person, whatever it scores. The
  threshold is calibrated so exact amount on exact date reaches it and nothing
  weaker does.

  Completion is refused while the difference is non-zero, an exception is
  unresolved, or a transaction is neither matched nor deliberately ignored -
  and the unresolved check queries rather than reading an ORM collection, so it
  cannot be defeated by a stale session. Reopening a completed reconciliation
  demands a reason and audits as CRITICAL.

- **Scheduled reports** (roadmap 3.5). A registry of five reports - rent roll,
  trial balance, delinquency ageing, work-order SLA, vendor compliance -
  rendered to CSV, JSON, HTML, PDF, or XLSX. **Recipients resolve at send
  time** from user and role references rather than stored addresses, which is
  the whole point: a departed employee stops receiving the books the moment
  their account is disabled, with no cleanup step to forget. Output is stored
  as a document so retention and access control apply to it, the schedule's
  watermark advances whether or not delivery worked, and repeated failures take
  a schedule out of service.
- PDF is written directly rather than through a headless browser, because a
  report is a table of text and the alternative was a rendering engine in every
  container for the sake of a rent roll. The costs are stated in the module:
  WinAnsi encoding, and a fixed column grid rather than measured text.
- XLSX needs the optional `openpyxl` dependency and says so plainly when it is
  absent, rather than handing back a CSV with the wrong extension.

- **Inspection workflow** (roadmap 3.4). The checklist is copied onto the
  inspection when it is scheduled rather than referenced, so a template edited
  in March cannot change what a February inspection appears to have asked. An
  item flagged `requires_photo` blocks sign-off without evidence linked to it -
  but only when the finding is not a clean pass, because demanding a photo of
  forty working light switches is how a checklist stops being filled in
  honestly. Failed items raise work orders at a priority derived from severity,
  guarded by the item's own reference so one broken window is one job however
  many times the call is made. Offline replay is idempotent by construction:
  findings upsert onto their item, and the device's capture time is kept rather
  than the server's - the finding happened in the flat at 09:00, not at the
  coffee shop at 14:00.

- **Preventive maintenance generation** (roadmap 3.3). Work orders raise inside
  the schedule's lead time, so the work can be booked rather than arriving
  already late, and idempotently by watermark. Two behaviours are choices
  rather than consequences: a job that has not run since March raises one
  gutter clean and not five, and a seasonal schedule that comes due out of
  season is deferred to its window rather than fired in July or dropped.
  Generation is deliberately not recorded as completion - raising the order and
  the boiler actually being serviced are different facts.

- **Approvals workflow** (roadmap 3.2). One request-decide-act path for every
  sensitive action. A requester can never grant their own request. Expiry is
  checked when an approval is *used*, not only when it is granted - an approval
  issued in March does not authorise a payment in September. The payload is
  fingerprinted when the checkpoint is raised and re-checked at the moment of
  action, so a record that moved afterwards no longer carries the decision.
- Bill payment now enforces that property directly: the total is snapshotted at
  approval, and a bill edited from $4,200 to $42,000 afterwards is refused with
  a critical audit event rather than paid on the strength of the old decision.
  Migration `c93f21a5d7e4` adds `bills.approved_total` and backfills it.
- A scheduled sweep lapses approvals nobody decided.

- **Automation rule engine** (roadmap 3.1). Conditions are data, not code: a
  JSON tree walked by a fixed operator table, with no `eval`, no attribute
  access, no regular expressions, and bounded depth - so there is no path from
  a rule to a Python object and nothing to escape from. Actions are registered
  handlers split into a reading `describe` and a writing `apply`; a dry run
  calls only `describe`, which makes "a dry run changes nothing" a structural
  guarantee rather than a flag somebody has to remember to check.

  Rules start inactive and in dry run, and promotion to live is refused unless
  a dry run that did not fail is already on file. Editing the logic of a live
  rule returns it to dry run. Cascades terminate: a rule cannot re-enter its
  own chain, and no chain exceeds three hops. A runaway rule burns its own
  hourly quota, and consecutive failures take it out of service.
- Domain events are now announced through a single function that writes the
  outbox row and dispatches automation rules in the caller's transaction, so
  a webhook and a rule can never disagree about what happened. Work-order
  creation and every lifecycle transition emit.
- ADR-0007 records why the condition language is deliberately small, and what
  that costs.

- **Owner statements and distributions** (roadmap 2.4). Statements resolve
  *temporal* ownership: a property sold mid-period apportions by days held, so
  both the outgoing and incoming owner are paid for exactly the days they owned
  it. Management fee and reserve retention are computed on the statement;
  distributions post a balanced entry and refuse to draw on a trust account or
  to exceed available cash less reserve. Regenerating an issued statement is
  refused rather than silently restating history.
- **Recurring charge generation** (roadmap 2.5). Monthly billing from lease
  charge schedules, idempotent by a per-charge watermark: running the job twice
  bills once, and a job that has not run for three months catches up on all
  three. Partial first and last months prorate to the day against the real
  length of that month.
- **Delinquency sweep** (roadmap 2.6). Staged escalation (late notice → second
  notice → pay-or-quit) honouring each lease's grace period, with the stage
  column as the watermark so a late fee is assessed once per stage rather than
  once per run. Every notice carries a delivery record.
- Scheduled jobs for all three, wired into the beat schedule and isolated
  per tenant: one organization's failure does not stop the sweep.

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

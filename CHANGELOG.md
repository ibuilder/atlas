# Changelog

All notable changes to Atlas PMOS are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Until 1.0.0, the REST API contract may change between minor versions. Error
codes and the `/api/v1` namespace are already treated as stable.

## [Unreleased]

### Added

- **Deposits can be taken and released.** The subledger that backs the trust
  reconciliation now has a surface: `POST /api/v1/deposits/collect` and
  `/release`, a per-lease balance at `GET /api/v1/leases/{id}/deposit` that
  answers as at any date, and a **Deposits** page in the console showing each
  trust account against its own beneficiaries. Collecting and releasing are
  separate permissions - `deposit.collect` sits with the accountants,
  `deposit.release` with the controllers - for the same reason entering a bill
  is split from paying it: money leaving a trust account belongs to somebody
  else. Residents get `deposit.read` and nothing more.
- **`mypy app` is clean and now fails the build.** It was reporting 117 errors
  behind `continue-on-error`, which is the same as not running it. Two were
  real: see below.

- **The three portals now write.** Residents pay an invoice and report a fault
  from the portal rather than only through the API; owners read the statements
  the system has been able to generate since 0.2; vendors accept, start, hold,
  and complete a job with its costs entered on site. Every POST re-derives what
  the caller owns and refuses anything outside it as a 404 - not 403, because
  telling a resident an invoice exists but is not theirs turns the portal into
  an enumerator. A permission check proves nothing here: every resident holds
  `payment.record`.
- **Applications and screening.** Screening is refused without recorded
  consent, and the refusal is audited as CRITICAL - a consumer report pulled
  without consent is a statutory violation, and the only place to prevent it
  reliably is before the request. A denial without stated reasons is refused,
  because those reasons *are* the adverse-action notice. The criteria are
  snapshotted at the decision, so "why was this denied" answers against the
  thresholds in force then. Criminal history routes to individual assessment
  rather than declining automatically, because a blanket bar has been held to
  violate fair-housing law. The service recommends; a person decides.
- **Renewals, move-outs, and deposit disposition.** A renewal's terms are fixed
  when it is offered, so a resident accepts what they were offered rather than
  today's rent, and a lapsed offer cannot be honoured. The statutory
  disposition clock starts at the move-out and the deadline is *stored*, not
  recomputed - a recomputed deadline drifts every time somebody changes the
  setting. Deductions need a description and an amount, come from inspection
  findings where there are any, and cannot exceed the deposit. A late
  disposition is audited as CRITICAL, because past the deadline the deductions
  are usually forfeit.
- **1099 year-end totals.** Cash basis, per vendor, with the threshold applied.
  A vendor over the threshold with no TIN, legal name, or address is reported
  as *blocked* rather than silently dropped - the omission is the expensive
  failure, since the penalty is per form - with backup withholding computed.
  The export carries the last four digits only.
- **Trust three-way reconciliation.** Bank against book against the sum of what
  every beneficiary is owed. The third leg is the one that matters: bank and
  book can agree perfectly while the trust is short, because the shortfall is
  between beneficiaries. Commingled operating activity is reported line by
  line, and a negative held balance is called an error rather than shown as a
  number.
- Two new reports, `tax_1099` and `trust_position`, so both are deliverable
  rather than merely callable.

- A load profile against the stated P95 budgets, which fails the run on a
  breach rather than printing numbers nobody reads, and an adversarial suite
  over the tenancy and authorization boundaries. Neither ticks a 1.0 condition
  — load testing needs production-shaped hardware, and a penetration test needs
  somebody who did not write the code — but both make those checks executable
  rather than aspirational.

### Fixed

- **Open redirect on the login page.** `_safe_next` refused `//evil.test` and
  anything with a scheme, but accepted `/\evil.test` — and browsers normalise
  the backslash, making it protocol-relative and therefore offsite. Found by
  the new attack suite. Backslashes are now rejected and the result is parsed
  as a final check; the login form also no longer echoes an unsafe `next` value
  back into the page.
- **A malformed identifier in a URL returned 500.** The GUID type validates at
  the bind boundary, which is right, but the resulting `ValueError` surfaced as
  an unhandled error — so a path-traversal string in a document URL produced a
  stack-trace-shaped response instead of a 404. There is now an `id` URL
  converter, applied to every `*_id` route segment, so a non-identifier never
  reaches a view. 404 rather than 400: a non-identifier cannot name a record,
  and answering differently for "malformed" and "not yours" hands an attacker
  an oracle.
- `LeaseRenewal.rent_increase` reached for a relationship that was never
  declared, so `hasattr` was always false and it returned `None` for every
  offer ever made. The relationship now exists and the property works.
- **The trust reconciliation's third leg was reading a field nothing wrote.**
  `Lease.deposit_held` was set by the demo seed and by the tests, and by no
  application code path at all — so in any real deployment the beneficiary
  total was zero, the "book versus beneficiaries" difference was the entire
  trust balance, and `shortfall` could never be anything but zero. The one
  thing the module exists to catch was the one thing it could not. There is now
  a deposit subledger (`DepositMovement`, `app/services/accounting/deposits.py`)
  recording every collection and release against a lease *and* a named trust
  account, posted to the ledger in the same call. Two further defects fell out
  with it: beneficiaries were scoped by organization rather than by the account
  being reconciled — so an operator with a trust account per jurisdiction saw a
  phantom shortfall on one and a surplus on the other, and a genuine shortfall
  on one was hidden when the other happened to be over by the same amount — and
  the `as_of` argument was accepted and ignored, so a year-end tie-out run in
  March measured the ledger at 31 December against deposits held today.
- **A deposit disposition never released the money.** `settle_deposit` recorded
  what was withheld and refunded on the move-out and stopped there, so the
  trust went on reporting the deposit as owed to a resident who had been paid
  and had left. Notice also captured `lease.security_deposit` — the contracted
  figure — rather than what was actually collected, which refunded money that
  was never taken.
- **Voided disbursements counted toward 1099 totals.** A stopped cheque still
  summed into a vendor's year, so a vendor genuinely paid $400 could cross the
  $600 threshold on the strength of a payment that never left the bank, and be
  issued a return overstating it.
- **The portal paid a different invoice than the resident chose.**
  `record_payment` retires a lease's open invoices oldest-due-first when given
  no allocation, and the portal gave none — so a resident settling this month's
  rent while last month's was outstanding cleared the wrong one, and the
  confirmation told them they had cleared this one. The overpayment guard was
  checking the balance of the invoice that was not being paid, too.
- A `NaN` in a portal amount field returned 500 rather than a validation
  message. It survives quantization, and Python's decimal raises on ordered
  comparisons against it instead of returning False — so both guards that were
  meant to reject it crashed on it instead. Affected the resident payment form
  and the vendor cost fields.
- An owner with no statements yet got a 500 on `/owner/statements`: the empty
  `IN` list was guarded with a sentinel `""`, which the GUID type validates and
  rejects. Now the query simply does not run.
- `convert_to_lease` treated an explicit security deposit of zero as
  unspecified and substituted a full month's rent, so a lease written with a
  deposit-replacement rider in place of a deposit refunded a month's rent at
  move-out.
- **The configured default rate limit applied to nothing.** The factory set
  `limiter.default_limits` after `init_app` - a plain attribute Flask-Limiter
  never reads - so `RATELIMIT_DEFAULT` was configured, visible in settings, and
  entirely inert. Every unlimited endpoint was unlimited in production too. Now
  set through `app.config` before `init_app`, where the limiter reads it.
  Found by mypy.
- **A reconciliation's cleared balance was never stored.** `refresh_totals`
  assigned `cleared_balance` as an undeclared instance attribute, which
  computed the difference correctly and then discarded the figure that
  difference was derived from. It is now a column, so a completed
  reconciliation can be re-examined rather than re-derived. The existing test
  passed either way because it asserted on the object the service had just
  returned; the new one reloads it. Found by mypy.
- The seeded scheduled report looked up its recipient by email in a dictionary
  keyed by role code. Because the lookup used `.get`, it silently returned
  `None` and the schedule went out addressed to nobody.

## [0.5.0] - 2026-08-10

Four milestones in one release. 0.2.0 through 0.4.0 were developed and merged
but never separately tagged, so this is the first release since 0.1.0 and it
contains all of them: closing the loop (0.2), automation (0.3), enterprise
readiness (0.4), and asset intelligence (0.5).

Every roadmap item through 0.5 is complete with its acceptance criteria met.
Four conditions for 1.0 remain deliberately open in `ROADMAP.md` — a
disaster-recovery drill executed rather than written, load testing, an
independent penetration test, and a 90-day soak. Three need real hardware and
elapsed time; the fourth needs somebody who did not write the code. None can be
satisfied by writing more of it.

### Fixed

- `score_match` in bank reconciliation read `JournalEntry.reference`, a field
  that does not exist - so every transaction where the bank supplied a
  reference raised `AttributeError` instead of scoring. The branch was never
  covered because no test set a reference on a statement line. Found by the
  demo seed running through it. Now reads the entry number and memos, with
  tests either side.
- Two modules compared a local `date.today()` against the services' UTC clock.
  They agree for most of the day and disagree either side of the UTC rollover,
  which is a test that passes all morning and fails in the evening - as one
  did, at 21:30 local. Both now use the same clock the code under test uses.

- The single-sign-on migration was hand-written and did not match what the
  models declare: it missed `delete_reason` from the soft-delete mixin, used a
  plain `String` where the models use the portable enum type, omitted the
  mixin-supplied indexes, and set `ON DELETE CASCADE` where the tenant
  convention is `RESTRICT`. `alembic check` caught it in CI. Regenerated from
  the models, which is what should have happened the first time.
- `ruff` now lints `migrations/` as well as `app/` and `tests/`. Migrations are
  code that runs against production databases, and leaving them outside the
  lint gate is how a hand-written one drifts from the schema unnoticed.

### Added

- Disaster-recovery runbook, and the command it depends on:
  `flask atlas verify-restore` proves a restored database is actually usable -
  the encryption key decrypts real data, every organization's audit chain is
  intact, every ledger balances, and row-level security survived. Row counts
  look correct in every one of those failure modes, which is why row counts are
  not one of the checks. Key recovery is step one of the runbook because losing
  the field-encryption key is the failure that actually happens, and it has no
  recovery path.

### Changed

- **The audit hash now covers `reason`, `resource_label`, and `severity`.**
  Writing the restore-verification tests surfaced that it did not: the stated
  reason for a rejected approval, which record an event referred to, and
  whether something was CRITICAL or INFO could all be altered without breaking
  the chain. A trail that is tamper-evident about the *shape* of an event and
  silent about its *substance* is not much of a trail. Purely diagnostic
  columns - correlation id, IP address, user agent - remain outside the hash on
  purpose: they are context attached by the transport, not assertions about
  what happened.

  This changes the hash format. Chains written by earlier versions will not
  verify against this one; there are no production deployments, and doing it
  now costs a re-seed rather than a re-chaining migration.

- **Space hierarchy** (roadmap 5.4). Site, building, floor, unit, room, riser -
  assembled in one query rather than one round trip per node, because the page
  that shows a two-hundred-room building is the page people leave open all day.
  Answers "what does this serve?", and rolls area and equipment upward. Two
  invariants are enforced rather than assumed: a space cannot become its own
  ancestor, and cannot move to another property. The first would turn every
  traversal into an infinite loop from a single mis-set parent - exactly the
  edit a bulk import makes - and the second would make every roll-up above it
  wrong in a way nobody notices until a cost report is questioned. External
  geometry references are stored opaquely, because interpreting an IFC GUID
  belongs to the system that produced it.
- **Document intelligence** (roadmap 5.3), in the shape ADR-0006 requires.
  Lease, invoice, and insurance-certificate extraction produce *suggestions*,
  never facts. Nothing reaches a lease or the ledger until a person accepts it,
  and the accept is attributed and audited alongside the sentence the value was
  read from - so "why does it say £3,100?" answers with a name and a quote
  rather than a shrug. A reviewer can correct a misread digit rather than only
  accept or reject, which is the common case. A missing field is reported
  rather than omitted, and a weak label like a bare "total" - which matches
  subtotal and tax too - gets a confidence that forces a look. An ambiguous
  date such as 12/04/2026 keeps its reading but drops below the review
  threshold instead of silently booking a payment a month out. The extractors
  are deterministic matchers and say so; a model-backed one would slot in
  behind the same interface and be bound by the same rule.

- **Asset lifecycle** (roadmap 5.1). Warranty is resolved when work is
  *recorded* rather than discovered later on an invoice, so "we paid for
  something that was covered" is visible in the data instead of merely
  regrettable - and a paid repair can still be identified as recoverable
  afterwards, which is when the discovery usually happens. The asset's
  aggregates are derived from its service history rather than maintained
  alongside it, so they cannot drift; an event arriving out of order does not
  drag the last-serviced date backwards. Retirement keeps the history, because
  it is the evidence behind the next replacement decision.
- **Repair-or-replace advice.** Cumulative repair cost against replacement
  cost, repeat failures within twelve months, expected life, and last recorded
  condition. One signal is "assess", two are "replace" - and preventive visits
  are not counted as failures, or a well-maintained boiler would look like a
  liability. The recommendation matters less than the three numbers returned
  with it, which turn "it keeps breaking" into something a budget meeting can
  act on.
- **Capital planning** (roadmap 5.2). A multi-year forecast that starts from
  expected life and then moves on observed condition and failure history,
  reporting which inputs it actually had. A missing replacement cost is stated
  rather than silently contributing zero to a budget somebody commits to.
  Costs inflate forward at a rate that is an argument rather than a constant.
  Assets beyond the horizon are dropped rather than piled into the final year,
  which would show a cliff that is not there. Exposed as a `capital_plan`
  report.

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

[Unreleased]: https://github.com/ibuilder/atlas/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/ibuilder/atlas/compare/v0.1.0...v0.5.0
[0.1.0]: https://github.com/ibuilder/atlas/releases/tag/v0.1.0

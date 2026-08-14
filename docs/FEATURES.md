# Capability status

An honest map of what is built, what is modelled but not yet wired, and what is
only a defined seam. Written this way deliberately: a roadmap that reads as a
feature list is how a buyer discovers the gap during implementation.

**Legend**

| | Meaning |
|---|---|
| **Complete** | Implemented, exercised by tests, and usable end to end. |
| **Partial** | Working core with named gaps, listed explicitly. |
| **Modelled** | Schema, migrations, and invariants exist; no service logic yet. |
| **Seam** | An interface and an ADR. No implementation. |
| **No surface** | Service logic implemented and tested, with no route, view, or job that reaches it. The capability exists in the codebase and not in the product. |

`tests/unit/test_service_reachability.py` enforces the last row: it fails the
build when a module becomes unreachable without being recorded, and again when
a recorded one is fixed and the entry is left behind. The table below and that
file have to agree.

---

## Platform

| Capability | Status | Notes |
|---|---|---|
| Bulk import with replay | **No surface** | Properties, units, and vendors from CSV. The whole file is validated before anything is written, and `plan_import()` reads only - which is the point of it, and nobody can ask for a plan. |
| Multi-organization tenancy | **Complete** | Three enforcement layers: service scoping, an ORM guard, and PostgreSQL row-level security applied by migration. A build-failing invariant catches a tenant table that escapes any of them. |
| RBAC + ABAC authorization | **Complete** | Organization, portfolio, and property scopes; portal ownership predicates; exhaustive test matrix. |
| Tamper-evident audit trail | **Complete** | Per-organization hash chain, verification endpoint, scheduled integrity check. |
| Versioned REST API | **Complete** | Cursor pagination, idempotency keys, ETags, stable error envelope. |
| OpenAPI 3.1 | **Complete** | Generated from the live URL map, so it cannot drift. |
| Structured logging + correlation IDs | **Complete** | With PII redaction covering third-party loggers. |
| Prometheus metrics | **Complete** | RED metrics plus business counters. |
| Health / readiness / migration probes | **Complete** | Readiness refuses traffic on a pending migration. |

## Identity and access

| Capability | Status | Notes |
|---|---|---|
| Role administration console | **Complete** | Roles, their permissions, and who holds them; plus the reverse view, which is the question actually asked - "what can this person do?". Read-only: granting a role goes through the audited service path, because granting from a list is one mis-click from the wrong authority. Surfaces permissions no role holds, which a permission matrix answers badly. |
| OIDC single sign-on | **Complete** | Authorization code flow with PKCE. The state is a single-use database row, so a replayed callback cannot establish a second session. The ID token is *verified* - signature against the published keys, then issuer, audience, expiry and nonce - and `alg: none` or a symmetric algorithm is refused outright. |
| SAML 2.0 | **Complete** | Signature verification is delegated to `signxml` and the module refuses to run without it: a subtly wrong XML-DSIG implementation accepts forged assertions while appearing to work. Verified against the *configured* certificate, not one embedded in the response. Audience restriction, validity window, and single-use assertion ids are all enforced. |
| SCIM 2.0 provisioning | **No surface** | Deactivation revokes sessions in the same operation. Unreachable in the way that matters most for SCIM: it is an HTTP contract, and there are no endpoints, so no identity provider can drive it. |
| Local authentication | **Complete** | Argon2id, transparent rehash, timing-equalised unknown-account path. |
| TOTP multi-factor | **Complete** | Includes replay protection within a step, and single-use recovery codes. |
| Session management | **Complete** | Server-side, individually revocable, idle timeout, invalidated on credential change. |
| API tokens | **Complete** | Hashed, prefixed, optional CIDR allowlist. |
| Password reset | **Complete** | Single-use, superseding, does not reveal account existence. |
| Role administration UI | **Complete** | Roles and assignments are provisioned and enforced; no management screen. |

## Portfolio

| Capability | Status | Notes |
|---|---|---|
| Organizations, portfolios, properties, units | **Complete** | Full CRUD via API, listing in the console. |
| Buildings and spaces | **Complete** | See the space hierarchy above: nestable, traversable, and the tree invariants are enforced. |
| Owners and temporal ownership stakes | **Complete** | Stakes are time-bounded, and a transfer closes the outgoing one the day before rather than editing it - so a statement for any earlier period still resolves the owners who actually held the asset then. The invariant enforced is that a transfer preserves the total: a property owned at all totals exactly 100%, checked after every move, because a transfer that drops four percent does not fail, it silently under-distributes for ever. |

## Leasing

| Capability | Status | Notes |
|---|---|---|
| Lead capture | **Complete** | API and speed-to-lead measurement. |
| Applications and screening | **Complete** | The funnel runs from an enquiry to a tenancy through both the console and the API. Screening is refused without recorded consent, and the consent evidence is read from the connection rather than from the request body - an address the submitter can dictate is not evidence that anybody agreed. Every decision requires a reason, approvals included, because on a denial that text is the adverse-action notice and requiring it only there is what makes denials look arbitrary. The criteria are snapshotted at the decision, so "why was this denied" answers against the thresholds in force at the time rather than whatever they later became; criminal history routes to individual assessment rather than an automatic decline. A decision is refused once the application is withdrawn, lapsed, or already converted. |
| Lease creation and activation | **Complete** | Includes overlap prevention — a unit cannot be double-let. |
| Renewals, move-outs, deposit disposition | **No surface** | A renewal's terms are fixed when offered; the statutory disposition clock starts at the move-out and the deadline is stored rather than recomputed; deductions need evidence and cannot exceed the deposit; settling releases the funds from trust. No surface reaches any of it - a move-out can only be recorded from code. |
| E-sign | **Complete** | The envelope lifecycle behind a provider adapter, reachable from all three portals: a signer sees what is waiting for them, is shown the consent wording, and types their name - which is what makes the stored consent record evidence rather than a field. Staff raise, send, and void through `/api/v1/envelopes` behind `esign.manage`; signing needs no permission, because a signer is authorised by being named on the envelope. The document's SHA-256 is pinned at send and re-checked before completion, so a file swapped underneath an open envelope voids it. Completion requires *every* signer, and an hourly sweep lapses envelopes nobody completed. Simple electronic signature, not qualified - see ADR-0008. |

## Accounting

| Capability | Status | Notes |
|---|---|---|
| Chart of accounts | **Complete** | Property-management default chart, seeded per organization. |
| Double-entry ledger | **Complete** | Invariant enforced in service, at flush, and by database `CHECK`. |
| Posted-entry immutability and reversal | **Complete** | Editing or deleting a posted entry raises. |
| Trial balance | **Complete** | Exposed via API and console. |
| Accounts receivable | **Complete** | Invoicing, payment capture, application, overpayment credit, void. |
| Period close and reopen | **Complete** | Approval-gated close; reopening demands a reason and audits as critical. |
| Accounts payable | **No surface** | Bill recording with ledger posting, threshold-based approval routing, separation of duties enforced by identity, and disbursement. Money coming in has three surfaces; money going out has none. |
| Bank reconciliation | **No surface** | Idempotent statement import over overlapping windows, ranked and explained match suggestions, exceptions, and a sign-off that refuses to complete while anything disagrees. Exercised only by the demo seed; there is no workspace to drive it from. |
| Trust accounting | **Complete** | Structural separation plus the **three-way reconciliation** a regulator actually asks for: bank against book against the sum of what every beneficiary is owed. The third leg is the one that matters - bank and book can agree perfectly while the trust is short, because the shortfall is between beneficiaries. Commingled operating activity is reported line by line, and a negative held balance is called an error rather than shown as a number. |
| Owner statements and distributions | **Complete** | Ownership is resolved *temporally*: a property sold mid-period apportions by days held, so both owners are paid for the days they owned it. Management fee and reserve retention computed; distribution refuses to exceed available cash less reserve, or to draw on a trust account. |
| Recurring charge generation | **Complete** | Monthly billing from lease charge schedules, idempotent by watermark, with day-accurate proration for partial first and last months. |
| Delinquency and late fees | **Complete** | Staged escalation honouring each lease's grace period; a late fee is assessed once per stage, not once per run. |
| 1099 / tax reporting | **Complete** | Cash-basis totals per vendor for a calendar year, with the threshold applied. A vendor over the threshold with no TIN, legal name, or address is **reported as blocked** rather than silently dropped - the omission is the expensive failure - with backup withholding computed. The export carries the last four digits only; a spreadsheet of TINs is a breach waiting to be emailed. |

## Maintenance

| Capability | Status | Notes |
|---|---|---|
| Request intake and triage | **Complete** | Habitability detection overrides under-reported urgency. |
| Work-order lifecycle | **Complete** | Validated state machine with an actor-attributed timeline. |
| SLA tracking and breach detection | **Complete** | Deadlines stamped at creation; scheduled escalation job. |
| Vendor dispatch with compliance gate | **Complete** | Refuses to assign work to a vendor whose insurance has lapsed. |
| Inspections | **No surface** | The checklist is copied onto the inspection at scheduling, so a template edited afterwards cannot rewrite what a completed inspection recorded, and findings raise work orders. Reachable only from the demo seed. |
| Preventive maintenance | **Complete** | Work orders raise inside the schedule's lead time, idempotent by watermark. A missed gap generates once rather than once per cycle missed, and a seasonal schedule that comes due out of season is deferred to its window rather than raised or lost. Generation is deliberately not recorded as completion. |
| Turn management | **Complete** | A turn record spans vacated to rent-ready, opened automatically when a move-out is recorded so the clock starts when the keys come back rather than when somebody remembers to open one. Steps are records with an optional work order, not a checklist in a notes field, because a stalled turn has to say which step it stalled on. A turn cannot be marked ready while a required step is outstanding; a step may be skipped, but never silently - the reason is mandatory and lands in the audit payload. The board reports days-vacant from completed turns only, since an average that counts turns still running flatters itself as they get worse. |

## Residents, owners, vendors

| Capability | Status | Notes |
|---|---|---|
| Resident portal | **Complete** | Balance, lease, invoices, and request history, plus paying an invoice and reporting a fault from the portal itself. An empty amount box pays the balance; overpayment is refused, because a typo there mints a credit somebody has to chase. |
| Owner portal | **Complete** | Properties, receivables, open work, and the statements themselves - list and detail, with the arithmetic and the day-weighted ownership share that produced them. |
| Vendor portal | **Complete** | Assigned work and compliance standing, plus accepting, starting, holding, and completing a job with its labour and material costs entered on site. Cancelling, reassigning, and verifying stay with the management company. |
| Messaging and notices | **Complete** | Delinquency notices are issued with delivery evidence and statutory response deadlines. General threads are anchored to what they are about (a lease, a work order, an application) and reach all three portals. Visibility is a property of the thread rather than of the reader: an internal thread is excluded by the query the portals must use, not by a template filter one refactor away from being dropped. |

## Documents and assets

| Capability | Status | Notes |
|---|---|---|
| Document graph (polymorphic links) | **Complete** | One object, many relationships; deduplicated by content digest. |
| Upload, validation, storage | **Complete** | Magic-byte sniffing, extension allowlist, size cap, generated keys, local and S3 adapters. |
| Quarantine and scanning | **Complete** | Quarantine on arrival, scan, release or hold, failing closed when the scanner is unreachable. The reference deployment runs ClamAV and the application waits on its health check rather than releasing unscanned files; `flask atlas verify-scanner` proves it end to end with EICAR and refuses to pass a scanner that did not answer. The structural scanner remains the default for development and says plainly that it is not a virus scanner. |
| Signed expiring retrieval | **Complete** | Time-limited tokens, attributable to the actor they were issued to, refused for quarantined objects. |
| Retention and legal hold | **Complete** | Retention derived from document category; a hold outranks every rule. |
| Space hierarchy | **No surface** | Site / building / floor / unit / room / riser, nestable, with assets located in it and area rolled up. Exercised only by the demo seed; there is no way to build or amend a hierarchy from the product. |
| Document intelligence | **No surface** | Lease, invoice, and insurance-certificate extraction as *suggestions* a person accepts or rejects, per ADR-0006 - nothing is believed until somebody says so. No surface reaches either the extraction or the review that is the whole safeguard. |
| Asset registry, warranties, service history | **No surface** | Warranty is resolved when work is raised rather than looked up afterwards, and service history drives repair-or-replace. Reachable only from the demo seed. |
| Repair-or-replace advice | **Complete** | Computed from cumulative repair cost against replacement cost, repeat failures in twelve months, expected life, and last recorded condition. One signal is "assess"; two are "replace". The recommendation matters less than the numbers behind it, which are returned with it. |
| Capital planning | **Complete** | Multi-year replacement forecast that starts from expected life and then *moves* on observed condition and failure history, and reports which it used. A missing replacement cost is stated rather than contributing zero to a budget. Costs inflate forward at a stated rate. Assets beyond the horizon are dropped rather than piled into the final year, which would show a cliff that is not there. Available as a report. |

## Automation, reporting, integration

| Capability | Status | Notes |
|---|---|---|
| Rule engine | **Complete** | Conditions are a JSON tree walked by a fixed operator table - no `eval`, no attribute access, no regular expressions, bounded depth. Actions are registered handlers split into `describe` and `apply`, so a dry run *cannot* mutate: the writing half is never called. Rules start inactive and in dry run, and promotion requires a dry run on file that did not fail. Cascades are bounded by depth and by a chain that refuses a rule re-entry. Hourly throttle and auto-disable after consecutive failures. |
| Approvals with separation of duties | **Complete** | One request-decide-act path for every sensitive action. A requester can never grant their own request. Expiry is checked when the approval is *used*, not only when granted, so one issued in March cannot authorise a payment in September. The payload is fingerprinted at request time and re-checked at use, so a record edited after approval loses it - a bill approved at $4,200 and edited to $42,000 must be approved again. |
| Transactional outbox | **Complete** | Events written in the caller's transaction; a separate dispatcher publishes. Domain events are announced through one function that feeds both the outbox and the rule engine, so the two cannot drift. |
| Signed webhooks with retry and DLQ | **Complete** | HMAC-SHA256 with the timestamp inside the signed string, exponential backoff to a 6h ceiling, dead-lettering, endpoint auto-disable, operator replay, and SSRF protection on customer-supplied URLs. |
| Inbound webhook deduplication | **Complete** | Payment capture is idempotent by provider event id. |
| KPI dashboards | **Complete** | Computed live, and projected nightly into `KpiSnapshot` so dashboards stop contending with operational writes. Every metric is a pure function of operational data, so a doubtful series is a rebuild rather than a correctness problem. Rates store their numerator and denominator, so a portfolio roll-up re-divides instead of averaging percentages - the classic silent reporting error. |
| Scheduled reports | **Complete** | A registry of five reports (rent roll, trial balance, delinquency ageing, work-order SLA, vendor compliance) rendered to CSV, JSON, HTML, PDF, or XLSX. **Recipients resolve at send time** from user and role references, so a departed employee stops receiving the books the moment their account is disabled. Output is stored as a document, so retention and access control apply to it. Repeated failures take a schedule out of service. |

## Background jobs

| Job | Status |
|---|---|
| SLA breach escalation | **Complete** |
| Vendor compliance refresh | **Complete** |
| Audit chain verification | **Complete** |
| Expired session and idempotency purge | **Complete** |
| Recurring charge generation | **Complete** |
| Delinquency sweep | **Complete** |
| Owner statement generation | **Complete** |
| Stale approval expiry | **Complete** |
| Scheduled report delivery | **Complete** |
| Nightly KPI projection | **Complete** |
| Preventive maintenance generation | **Complete** |
| Webhook dispatch | **Complete** |

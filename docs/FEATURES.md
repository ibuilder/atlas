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

---

## Platform

| Capability | Status | Notes |
|---|---|---|
| Bulk import with replay | **Complete** | Properties, units, and vendors from CSV. The whole file is validated before anything is written, so a file with errors is rejected in full with a per-row report against the *spreadsheet's* row numbers - a partial import is worse than a failed one. Rows are keyed by natural business identifiers, so fixing the spreadsheet and re-uploading updates rather than duplicating. `plan_import()` reads only. |
| Multi-organization tenancy | **Complete** | Three enforcement layers: service scoping, an ORM guard, and PostgreSQL row-level security applied by migration. A build-failing invariant catches a tenant table that escapes any of them. |
| RBAC + ABAC authorization | **Complete** | Organization, portfolio, and property scopes; portal ownership predicates; exhaustive test matrix. |
| Tamper-evident audit trail | **Complete** | Per-organization hash chain, verification endpoint, scheduled integrity check. |
| Versioned REST API | **Complete** | Cursor pagination, idempotency keys, ETags, stable error envelope. |
| OpenAPI 3.1 | **Complete** | Generated from the live URL map, so it cannot drift. |
| Structured logging + correlation IDs | **Complete** | With PII redaction covering third-party loggers. |
| Prometheus metrics | **Complete** | RED metrics plus business counters. |
| Health / readiness / migration probes | **Complete** | Readiness refuses traffic on a pending migration. |
| SSO (OIDC, SAML) | **Seam** | `User.idp_issuer` / `idp_subject` exist; no protocol implementation. |
| SCIM provisioning | **Seam** | Deferred by design; see the roadmap. |

## Identity and access

| Capability | Status | Notes |
|---|---|---|
| Role administration console | **Complete** | Roles, their permissions, and who holds them; plus the reverse view, which is the question actually asked - "what can this person do?". Read-only: granting a role goes through the audited service path, because granting from a list is one mis-click from the wrong authority. Surfaces permissions no role holds, which a permission matrix answers badly. |
| OIDC single sign-on | **Complete** | Authorization code flow with PKCE. The state is a single-use database row, so a replayed callback cannot establish a second session. The ID token is *verified* - signature against the published keys, then issuer, audience, expiry and nonce - and `alg: none` or a symmetric algorithm is refused outright. |
| SAML 2.0 | **Complete** | Signature verification is delegated to `signxml` and the module refuses to run without it: a subtly wrong XML-DSIG implementation accepts forged assertions while appearing to work. Verified against the *configured* certificate, not one embedded in the response. Audience restriction, validity window, and single-use assertion ids are all enforced. |
| SCIM 2.0 provisioning | **Complete** | Deactivation revokes sessions in the same operation - inactive with a live session is offboarding that does not offboard. DELETE deactivates rather than removing, because a user id appears on ledger entries. An unsupported filter is refused rather than silently matching everything. |
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
| Buildings and spaces | **Modelled** | Schema present; no dedicated service or UI. |
| Owners and temporal ownership stakes | **Partial** | Owners are creatable and drive the owner portal; stake transfer has no workflow. |
| Bulk import with per-row errors and replay | **Modelled** | `ImportJob` schema exists; no importer. |

## Leasing

| Capability | Status | Notes |
|---|---|---|
| Lead capture | **Complete** | API and speed-to-lead measurement. |
| Applications and screening | **Modelled** | Schema captures decision factors and consent; no workflow or provider adapter. |
| Lease creation and activation | **Complete** | Includes overlap prevention — a unit cannot be double-let. |
| Renewals, move-outs, deposit disposition | **Modelled** | Schema encodes statutory deadlines; no workflow. |
| E-sign | **Seam** | `esign_envelope_id` and a backend setting; no provider integration. |

## Accounting

| Capability | Status | Notes |
|---|---|---|
| Chart of accounts | **Complete** | Property-management default chart, seeded per organization. |
| Double-entry ledger | **Complete** | Invariant enforced in service, at flush, and by database `CHECK`. |
| Posted-entry immutability and reversal | **Complete** | Editing or deleting a posted entry raises. |
| Trial balance | **Complete** | Exposed via API and console. |
| Accounts receivable | **Complete** | Invoicing, payment capture, application, overpayment credit, void. |
| Period close and reopen | **Complete** | Approval-gated close; reopening demands a reason and audits as critical. |
| Accounts payable | **Complete** | Bill recording with ledger posting, threshold-based approval routing, separation of duties enforced by identity, disbursement, and duplicate-invoice prevention. |
| Bank reconciliation | **Complete** | Statement import is idempotent over overlapping windows - the bank's reference where there is one, otherwise a fingerprint with an occurrence index, so two genuinely identical fees both survive while a re-import adds neither. Match suggestions are ranked *and explained*; automatic matching takes only candidates that are both confident and unambiguous, leaving ties for a person. Completion is refused while the difference is non-zero, an exception is unresolved, or a transaction is neither matched nor deliberately ignored. Reopening is audited as CRITICAL. |
| Trust accounting | **Partial** | Structural separation, flags, and constraints are enforced, and a trust account is refused as the source of an operating disbursement; no dedicated trust reconciliation yet. |
| Owner statements and distributions | **Complete** | Ownership is resolved *temporally*: a property sold mid-period apportions by days held, so both owners are paid for the days they owned it. Management fee and reserve retention computed; distribution refuses to exceed available cash less reserve, or to draw on a trust account. |
| Recurring charge generation | **Complete** | Monthly billing from lease charge schedules, idempotent by watermark, with day-accurate proration for partial first and last months. |
| Delinquency and late fees | **Complete** | Staged escalation honouring each lease's grace period; a late fee is assessed once per stage, not once per run. |
| 1099 / tax reporting | **Modelled** | `is_1099_reportable` flags present; no year-end export. |

## Maintenance

| Capability | Status | Notes |
|---|---|---|
| Request intake and triage | **Complete** | Habitability detection overrides under-reported urgency. |
| Work-order lifecycle | **Complete** | Validated state machine with an actor-attributed timeline. |
| SLA tracking and breach detection | **Complete** | Deadlines stamped at creation; scheduled escalation job. |
| Vendor dispatch with compliance gate | **Complete** | Refuses to assign work to a vendor whose insurance has lapsed. |
| Inspections | **Complete** | The checklist is *copied* onto the inspection at scheduling, so a template edited afterwards cannot rewrite what a completed inspection appears to have asked. An item flagged `requires_photo` blocks sign-off without linked evidence - but only when the finding is not a clean pass, since photographing forty working light switches is how a checklist stops being filled in honestly. Failed items raise work orders, guarded by the item's own reference so one broken window is one job. Offline replay is idempotent by construction. |
| Preventive maintenance | **Complete** | Work orders raise inside the schedule's lead time, idempotent by watermark. A missed gap generates once rather than once per cycle missed, and a seasonal schedule that comes due out of season is deferred to its window rather than raised or lost. Generation is deliberately not recorded as completion. |
| Turn management | **Modelled** | Represented through work orders; no turn-specific templates. |

## Residents, owners, vendors

| Capability | Status | Notes |
|---|---|---|
| Resident portal | **Partial** | Balance, lease, invoices, and request history render; payment submission is API-only. |
| Owner portal | **Partial** | Properties, receivables, and open work render; statements generate and can be issued, but the portal does not yet render them. |
| Vendor portal | **Partial** | Assigned work and compliance standing render; no field update UI. |
| Messaging and notices | **Partial** | Delinquency notices are issued with delivery evidence and statutory response deadlines; general messaging threads remain modelled only. |

## Documents and assets

| Capability | Status | Notes |
|---|---|---|
| Document graph (polymorphic links) | **Complete** | One object, many relationships; deduplicated by content digest. |
| Upload, validation, storage | **Complete** | Magic-byte sniffing, extension allowlist, size cap, generated keys, local and S3 adapters. |
| Quarantine and scanning | **Partial** | The pipeline is complete — quarantine on arrival, scan, release or hold. The default scanner performs *structural* checks only (EICAR, active content); a ClamAV adapter is included but a real deployment must configure one. |
| Signed expiring retrieval | **Complete** | Time-limited tokens, attributable to the actor they were issued to, refused for quarantined objects. |
| Retention and legal hold | **Complete** | Retention derived from document category; a hold outranks every rule. |
| OCR and extraction | **Seam** | Fields and status modelled; no pipeline. |
| Space hierarchy | **Complete** | Site / building / floor / unit / room / riser, nestable, assembled in one query. Answers "what does this serve?" and rolls area and equipment up through it. A space cannot become its own ancestor and cannot move to another property - the first would make every traversal an infinite loop, the second would make every roll-up above it wrong in a way nobody notices until a cost report is questioned. External geometry references (IFC GUID, scan room id) are stored opaquely on purpose. |
| Document intelligence | **Complete** | Lease, invoice, and insurance-certificate extraction as *suggestions*, per ADR-0006. Nothing is written until a person accepts it, and accepting is attributed and audited with the sentence the value was read from - so "why does it say £3,100?" answers with a name and a quote. A reviewer can correct a misread digit rather than only accept or reject. Ambiguous dates (12/04/2026) keep their reading but drop below the review threshold rather than being guessed. Deterministic matchers today; a model-backed extractor would slot in behind the same interface and the same rule. |
| Asset registry, warranties, service history | **Complete** | Warranty is resolved when work is *recorded*, not discovered on an invoice, so paying for covered work is visible in the data. The asset's aggregates are derived from its service history rather than maintained separately, so they cannot drift from it. Retirement keeps the history, because it is the evidence behind the next replacement decision. |
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

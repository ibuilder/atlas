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
| Local authentication | **Complete** | Argon2id, transparent rehash, timing-equalised unknown-account path. |
| TOTP multi-factor | **Complete** | Includes replay protection within a step, and single-use recovery codes. |
| Session management | **Complete** | Server-side, individually revocable, idle timeout, invalidated on credential change. |
| API tokens | **Complete** | Hashed, prefixed, optional CIDR allowlist. |
| Password reset | **Complete** | Single-use, superseding, does not reveal account existence. |
| Role administration UI | **Modelled** | Roles and assignments are provisioned and enforced; no management screen. |

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
| Bank reconciliation | **Modelled** | Reconciliation and exception schema exist; no matching engine. |
| Trust accounting | **Partial** | Structural separation, flags, and constraints are enforced, and a trust account is refused as the source of an operating disbursement; no dedicated trust reconciliation yet. |
| Owner statements and distributions | **Modelled** | Schema complete; no generator. |
| 1099 / tax reporting | **Modelled** | `is_1099_reportable` flags present; no year-end export. |

## Maintenance

| Capability | Status | Notes |
|---|---|---|
| Request intake and triage | **Complete** | Habitability detection overrides under-reported urgency. |
| Work-order lifecycle | **Complete** | Validated state machine with an actor-attributed timeline. |
| SLA tracking and breach detection | **Complete** | Deadlines stamped at creation; scheduled escalation job. |
| Vendor dispatch with compliance gate | **Complete** | Refuses to assign work to a vendor whose insurance has lapsed. |
| Inspections | **Modelled** | Templates, items, and offline-capture fields are modelled; no workflow. |
| Preventive maintenance | **Modelled** | Schedules and calendar-correct recurrence are modelled; no generator. |
| Turn management | **Modelled** | Represented through work orders; no turn-specific templates. |

## Residents, owners, vendors

| Capability | Status | Notes |
|---|---|---|
| Resident portal | **Partial** | Balance, lease, invoices, and request history render; payment submission is API-only. |
| Owner portal | **Partial** | Properties, receivables, and open work render; statements are not generated. |
| Vendor portal | **Partial** | Assigned work and compliance standing render; no field update UI. |
| Messaging and notices | **Modelled** | Threads, messages, delivery evidence, and statutory deadlines are modelled; no service. |

## Documents and assets

| Capability | Status | Notes |
|---|---|---|
| Document graph (polymorphic links) | **Complete** | One object, many relationships; deduplicated by content digest. |
| Upload, validation, storage | **Complete** | Magic-byte sniffing, extension allowlist, size cap, generated keys, local and S3 adapters. |
| Quarantine and scanning | **Partial** | The pipeline is complete — quarantine on arrival, scan, release or hold. The default scanner performs *structural* checks only (EICAR, active content); a ClamAV adapter is included but a real deployment must configure one. |
| Signed expiring retrieval | **Complete** | Time-limited tokens, attributable to the actor they were issued to, refused for quarantined objects. |
| Retention and legal hold | **Complete** | Retention derived from document category; a hold outranks every rule. |
| OCR and extraction | **Seam** | Fields and status modelled; no pipeline. |
| Asset registry, warranties, service history | **Modelled** | Includes warranty lookup and replacement forecasting logic on the model. |

## Automation, reporting, integration

| Capability | Status | Notes |
|---|---|---|
| Rule engine | **Modelled** | Rules, runs, steps, dry-run flag, throttles, and auto-disable are modelled; no evaluator. |
| Approvals with separation of duties | **Modelled** | `can_be_decided_by()` enforces requester ≠ approver; no workflow. |
| Transactional outbox | **Complete** | Events written in the caller's transaction; a separate dispatcher publishes. |
| Signed webhooks with retry and DLQ | **Complete** | HMAC-SHA256 with the timestamp inside the signed string, exponential backoff to a 6h ceiling, dead-lettering, endpoint auto-disable, operator replay, and SSRF protection on customer-supplied URLs. |
| Inbound webhook deduplication | **Complete** | Payment capture is idempotent by provider event id. |
| KPI dashboards | **Complete** | Computed live; the snapshot projection table is **Modelled**. |
| Scheduled reports | **Modelled** | Schedule and run history are modelled; no generator. |

## Background jobs

| Job | Status |
|---|---|
| SLA breach escalation | **Complete** |
| Vendor compliance refresh | **Complete** |
| Audit chain verification | **Complete** |
| Expired session and idempotency purge | **Complete** |
| Recurring charge generation | **Modelled** (scheduled, not implemented) |
| Delinquency sweep | **Modelled** (scheduled, not implemented) |
| Preventive maintenance generation | **Modelled** (scheduled, not implemented) |
| Webhook dispatch | **Complete** |

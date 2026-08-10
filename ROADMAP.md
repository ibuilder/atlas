# Roadmap

Sequenced by dependency and by leverage, not by how impressive each item sounds.
Every item states its **acceptance criteria**, because "done" for a property
platform is a testable condition, not a feeling.

Sizing is relative: **S** ≈ a service plus tests, **M** ≈ several services and an
API surface, **L** ≈ a subsystem with its own operational story.

Status here mirrors [`docs/FEATURES.md`](docs/FEATURES.md); that file is the
authority on what exists today.

---

## 0.1.0 — Enterprise core ✅ *shipped*

Identity, tenancy, audit, the ledger, maintenance, the API, and the operational
scaffolding. Three isolation layers, a tamper-evident audit chain, and a demo
that reconciles.

---

## 0.2.0 — Close the loop ✅ *shipped*

The theme: every workflow that currently *stops* at a boundary should cross it.
Documents exist but cannot be uploaded; bills exist but cannot be paid; events
are recorded but never leave the system. These are the gaps that make the
current release a core rather than a product.

| # | Item | Size | Depends on |
|---|---|---|---|
| 2.1 | **Document storage and upload** ✅ | M | — |
| 2.2 | **Webhook delivery** ✅ | S | — |
| 2.3 | **Accounts payable: approval and disbursement** ✅ | M | — |
| 2.4 | **Owner statements and distributions** ✅ | M | 2.3 |
| 2.5 | **Recurring charge generation** ✅ | S | — |
| 2.6 | **Delinquency sweep and notices** ✅ | S | 2.5 |

**2.1 Document storage and upload.**
Local and S3 adapters behind one interface. Content-type sniffing against the
declared type, extension allowlist, size cap, SHA-256 deduplication, quarantine
until scanned, signed expiring URLs, and the polymorphic link graph exposed.
*Accepts when:* an uploaded file is quarantined on arrival, is not servable until
cleared, is addressable from a lease and a work order simultaneously, and a
signed URL expires; a file whose bytes disagree with its declared content type is
rejected.

**2.2 Webhook delivery.**
The outbox publisher and the HTTP dispatcher: HMAC-SHA256 signatures with a
timestamped signing string, exponential backoff, dead-lettering, endpoint
auto-disable, and operator replay.
*Accepts when:* a delivery is signed and verifiable by a receiver, a failing
endpoint backs off then dead-letters rather than retrying forever, a dead-lettered
delivery can be replayed, and one event is never delivered twice to one endpoint.

**2.3 Accounts payable.**
Bill approval routing by amount threshold, separation of duties on approval, and
disbursement posting to the ledger against the correct bank account.
*Accepts when:* a bill above threshold cannot be paid without a distinct
approver, payment posts a balanced entry, trust accounts refuse operating
disbursements, and a duplicate vendor invoice number is rejected.

**2.4 Owner statements and distributions.**
Period statement generation resolving *temporal* ownership, management fee
calculation, reserve retention, and distribution posting.
*Accepts when:* a statement for a period where ownership changed apportions
correctly to both owners, and distribution never exceeds available cash less
reserve.

**2.5 Recurring charge generation.**
Idempotent monthly billing from lease charge schedules, with proration for
partial first and last months.
*Accepts when:* running the job twice produces one invoice, a mid-month move-in
prorates to the day, and a lease ending mid-cycle bills only the days occupied.

**2.6 Delinquency sweep.**
Configurable grace periods, staged escalation, late-fee assessment, and notice
issuance with delivery evidence.
*Accepts when:* a grace period is honoured, a late fee is assessed once per
cycle, and every notice carries a delivery record.

---

## 0.3.0 — Automate ✅ *shipped*

The theme: reduce manual orchestration. This is where the operational
differentiation lives, and it is deliberately *after* the loop is closed —
automating a workflow that cannot complete is worse than not automating it.

| # | Item | Size | Depends on |
|---|---|---|---|
| 3.1 | **Automation rule engine** ✅ | L | 0.2 |
| 3.2 | **Approvals workflow** ✅ | S | 3.1 |
| 3.3 | **Preventive maintenance generation** ✅ | S | — |
| 3.4 | **Inspections** ✅ | M | 2.1 |
| 3.5 | **Scheduled reports** ✅ | M | 2.1 |

**3.1 Automation rule engine.**
A restricted condition evaluator — never `eval` — an action registry, retry and
escalation policies, throttling, auto-disable on repeated failure, and full run
history. **Dry-run is the headline feature**: a rule executes against real data,
records everything it would have done, and changes nothing.
*Accepts when:* a dry run produces a complete step-by-step record with zero
writes to domain tables, a malicious condition expression cannot reach the
interpreter, a runaway rule disables itself, and every action is audited.

**3.2 Approvals.**
Threshold-triggered human checkpoints with separation of duties already modelled
on `Approval.can_be_decided_by()`.
*Accepts when:* a requester cannot approve their own request, an expired approval
blocks the action, and the approved payload is snapshotted so the approver's
decision cannot be changed under them.

**3.3 Preventive maintenance.**
Work-order generation from schedules with lead time, seasonal windows, and
calendar-correct recurrence.
*Accepts when:* re-running generates no duplicate for a cycle, and a schedule
restricted to winter months does not fire in July.

**3.4 Inspections.**
Checklist execution, photo evidence, offline-tolerant replay, and automatic
work-order creation from failed items.
*Accepts when:* a completed inspection renders against the template *version*
used, a failed item can raise a work order, and an offline capture replays
without duplicating.

**3.5 Scheduled reports.**
Report registry, generation to PDF/CSV/XLSX, delivery to resolved recipients.
*Accepts when:* recipients resolve at send time so a departed employee stops
receiving the books.

---

## 0.4.0 — Enterprise readiness

The theme: what a 500-property operator's IT function asks for before signing.

| # | Item | Size | Depends on |
|---|---|---|---|
| 4.1 | **OIDC single sign-on** | M | — |
| 4.2 | **SAML 2.0** | M | 4.1 |
| 4.3 | **SCIM provisioning** | M | 4.1 |
| 4.4 | **Bulk import with replay** | M | 2.1 |
| 4.5 | **Bank reconciliation workspace** | L | — |
| 4.6 | **Reporting projections** | M | — |
| 4.7 | **Role administration UI** | S | — |

**4.5 Bank reconciliation** is the largest single item here and the one operators
judge an accounting system by: statement import, candidate matching with a
confidence score, an exception queue, and a completion that locks the period
range.
*Accepts when:* a statement re-imported over an overlapping window creates no
duplicates, matching suggestions are ranked, and a reconciliation cannot complete
with an unresolved difference.

**4.6 Reporting projections** populates `KpiSnapshot` so dashboards stop
contending with operational writes.
*Accepts when:* a projection is reconstructible from operational data, and a
stale projection is a rebuild rather than a correctness problem.

---

## 0.5.0 — Asset intelligence

The strategic bet from ADR-0005, built once the operational chain is complete.

| # | Item | Size |
|---|---|---|
| 5.1 | Asset lifecycle services and warranty claim workflow | M |
| 5.2 | Capital planning: replacement forecasting across a portfolio | M |
| 5.3 | Document intelligence: OCR, lease abstraction, invoice extraction | L |
| 5.4 | Space hierarchy and external geometry references | M |

**5.3** is where AI first becomes defensible: extraction is a *suggestion* against
a document a human can check, which is the shape ADR-0006 requires.

---

## 1.0.0 — General availability

Not a feature milestone. The conditions under which we would put a customer's
portfolio on this:

- [ ] Every 0.2 and 0.3 item complete, with acceptance criteria met
- [ ] Load tested to the stated budgets: P95 < 300ms reads, < 700ms writes
- [ ] Backup restore drill executed and documented, including key recovery
- [ ] A penetration test against the authorization and tenancy boundaries
- [ ] Disaster-recovery runbook executed end to end, not just written
- [ ] 90-day soak on a real portfolio with no unexplained ledger variance
- [ ] Zero `Modelled` or `Seam` entries in `docs/FEATURES.md` for anything the
      marketing surface claims

---

## Explicitly not planned

Stated so the absence reads as a decision rather than an oversight.

- **A mobile application.** The portals are responsive and inspections are the
  only genuinely field-bound workflow. Revisit when offline capture proves
  insufficient as a web application.
- **A general-purpose report builder.** Operators ask for it and then use six
  reports. A registry of well-made reports beats a builder that produces bad ones.
- **Autonomous AI action.** ADR-0006. Suggestion with a human checkpoint, or
  nothing.
- **Multi-currency within one organization.** Cross-currency ledgers need
  translation, revaluation, and a reporting currency — a subsystem, not a column.
  Per-organization currency is supported today.

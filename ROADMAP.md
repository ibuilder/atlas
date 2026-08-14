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

## 0.4.0 — Enterprise readiness ✅ *shipped*

The theme: what a 500-property operator's IT function asks for before signing.

| # | Item | Size | Depends on |
|---|---|---|---|
| 4.1 | **OIDC single sign-on** ✅ | M | — |
| 4.2 | **SAML 2.0** ✅ | M | 4.1 |
| 4.3 | **SCIM provisioning** ✅ | M | 4.1 |
| 4.4 | **Bulk import with replay** ✅ | M | 2.1 |
| 4.5 | **Bank reconciliation workspace** ✅ | L | — |
| 4.6 | **Reporting projections** ✅ | M | — |
| 4.7 | **Role administration UI** ✅ | S | — |

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

## 0.5.0 — Asset intelligence ✅ *shipped*

The strategic bet from ADR-0005, built once the operational chain is complete.

| # | Item | Size |
|---|---|---|
| 5.1 | Asset lifecycle services and warranty claim workflow ✅ | M |
| 5.2 | Capital planning: replacement forecasting across a portfolio ✅ | M |
| 5.3 | Document intelligence: OCR, lease abstraction, invoice extraction ✅ | L |
| 5.4 | Space hierarchy and external geometry references ✅ | M |

**5.3** is where AI first becomes defensible: extraction is a *suggestion* against
a document a human can check, which is the shape ADR-0006 requires.

---

## 0.6.0 — Make it operable

Ten service modules are implemented, tested, and reachable by nothing a user can
get to. `tests/unit/test_service_reachability.py` enumerates them and fails the
build if the list grows; `docs/FEATURES.md` marks them **No surface**. This
milestone is the surfaces, and nothing else — no new domain logic, because the
domain logic is already written and already covered.

The sequencing is by consequence rather than by size. Each item is done when the
capability can be exercised end to end by a signed-in human, its module leaves
the reachability list, and its FEATURES row honestly reads **Complete**.

| # | Item | Size |
|---|---|---|
| ~~6.1~~ | ~~Leasing funnel: application, screening, decision, conversion~~ | **Shipped** |
| ~~6.2~~ | ~~Move-outs, renewals, and deposit disposition~~ | **Shipped** |
| ~~6.3~~ | ~~Accounts payable: bills, approval, disbursement~~ | **Shipped** |
| 6.4 | Bank reconciliation workspace | M |
| ~~6.5~~ | ~~Inspections: schedule, perform, complete~~ | **Shipped** |
| 6.6 | SCIM 2.0 endpoints and the SSO login routes | M |
| 6.7 | Bulk import: upload, plan, apply | S |
| 6.8 | Document extraction review queue | S |
| 6.9 | Spaces and asset lifecycle management | M |

**6.1 — shipped.** The funnel used to stop at leads, which made the several
services behind it — consent before screening, criteria snapshotted at the
decision, individual assessment for criminal history — protections on a road
nobody could drive. It now runs from intake to tenancy through the console and
through `/api/v1/applications`. Two things came out of building it that the
service had not been asked before: consent is read from the connection rather
than from the request body, because an address the submitter can dictate is not
evidence anybody agreed; and a decision is now refused once an application is
withdrawn, lapsed, or already converted, which the old "not already decided"
test let through — approving over a live tenancy included.

Still open, and deliberately: there is no *public* self-serve intake form. An
application is taken by a signed-in agent or driven by an authenticated
integration. A public form is an unauthenticated write surface and wants its own
rate limiting, abuse handling, and identity story rather than a route bolted
onto this one.

**6.2 — shipped.** It carried the most legal weight of anything on this list:
the statutory disposition clock starts at the move-out, and a move-out could
only be recorded from code — so the deadline that decides whether deductions
are forfeit depended on somebody running a script. Both the console and the API
now record it, and the deadline is stored at that moment rather than recomputed
on read. The disposition board leads with what is overdue. Renewals shipped with
it because they share the same lease-end conversation.

**6.3** because money coming in has three surfaces and money going out has none.
Separation of duties is enforced by the service already; the surface has to keep
the two roles apart rather than reintroducing a screen where one person can
record and pay.

**6.4** and **6.5** are both currently exercised only by the demo seed. The
reconciliation workspace is the larger of the two: statement upload, the ranked
match suggestions with their reasons, exceptions, and a sign-off that refuses
while anything disagrees.

**6.6** is the one where the missing surface *is* the feature. SCIM is an HTTP
contract, so an identity provider has nothing to call, and the OIDC and SAML
services have no login route to drive them. Until this ships, "single sign-on"
means the code exists.

**6.7** to **6.9** are smaller and independent, and can be picked up in any
order once the four above are done. **6.8** matters more than its size suggests:
extraction produces *suggestions* per ADR-0006, and the accept-or-reject review
is the whole safeguard — shipping extraction without the review would be the
one thing that ADR forbids.

### Acceptance for the milestone

- [ ] `NO_SURFACE` and `SEED_ONLY` in `tests/unit/test_service_reachability.py`
      are both empty, and the guard is left in place so the next module cannot
      arrive unreachable.
- [ ] `docs/FEATURES.md` has no **No surface** rows.
- [ ] Every new surface is covered the way the portal write surfaces are: a
      happy path, a refusal the service raises and the surface must not swallow,
      and the same request aimed at another tenant's record returning 404.

### What this milestone deliberately does not do

Add capability. Every service in it is already written, tested, and — in nine
cases out of ten — already exercised against realistic data by the demo seed.
If something here turns out to need new domain logic, that is a finding worth
recording rather than a licence to widen the milestone.

---

## 1.0.0 — General availability

Not a feature milestone. The conditions under which we would put a customer's
portfolio on this:

- [x] Every 0.2, 0.3, 0.4, and 0.5 item complete, with acceptance criteria met
- [x] Zero `Modelled` or `Seam` entries in `docs/FEATURES.md` for anything the
      marketing surface claims — the README now points at that table and states
      plainly where a capability is modelled rather than built
- [x] Disaster-recovery runbook **written**, with the verification it depends on
      implemented and tested: `flask atlas verify-restore` proves the encryption
      key decrypts, every audit chain is intact, every ledger balances, and
      row-level security survived the restore. See
      [docs/runbooks/disaster-recovery.md](docs/runbooks/disaster-recovery.md).
- [ ] **Disaster-recovery drill executed** against a real restore, and the
      timing table in that runbook filled in. Written is not executed, and the
      drill is the point.
- [ ] **Load tested** to the stated budgets: P95 < 300ms reads, < 700ms writes.
      Needs production-shaped hardware and data volumes. The harness exists and
      fails the run on a breach — see [tests/load/](tests/load/README.md) — so
      what is missing is somewhere to run it, not something to run. The verdict
      logic is now separated from the locust profile and covered by the ordinary
      suite (`tests/unit/test_load_budgets.py`), because it previously could not
      be imported without locust installed and had therefore never executed —
      a load test whose pass/fail arithmetic is wrong is worse than none.
- [ ] **Penetration test** against the authorization and tenancy boundaries, by
      somebody who did not write them. `tests/security/test_attack_surface.py`
      is the starting point that test should not have to rediscover; it is
      explicitly *not* a substitute, because a suite written by the author
      tests the attacks the author thought of.
- [ ] **90-day soak** on a real portfolio with no unexplained ledger variance.

The last four are deliberately unticked. Three of them require a running
deployment, real hardware, and elapsed calendar time; the fourth requires an
independent party. They cannot be satisfied by writing more code, and ticking
them on the strength of a test suite would be the exact dishonesty this
checklist exists to prevent.

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

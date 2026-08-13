# Architecture decision records

Each record states the decision, the reasoning at the time, what it costs, and
what would make us revisit it. A decision without a stated cost is advocacy.

| # | Decision | Status |
|---|---|---|
| [0001](#adr-0001) | Modular monolith before microservices | Accepted |
| [0002](#adr-0002) | PostgreSQL as the system of record | Accepted |
| [0003](#adr-0003) | Celery for background work | Accepted |
| [0004](#adr-0004) | Centralised policy-based authorization | Accepted |
| [0005](#adr-0005) | Document and asset graphs as the strategic differentiator | Accepted |
| [0006](#adr-0006) | AI deferred behind governance controls | Accepted |
| [0007](#adr-0007) | Automation rules are data, and dry run is structural | Accepted |
| [0008](#adr-0008) | Typed-name e-signature, with the artifact pinned | Accepted |

---

## ADR-0001 — Modular monolith before microservices {#adr-0001}

**Context.** Atlas spans a dozen bounded contexts. The instinct at this scope is
to start with services. Property operations, though, are unusually
transactional: recording a payment posts to the ledger, updates an invoice
balance, clears a delinquency stage, and writes an audit event — and all four
must be true together or none of them.

**Decision.** One deployable with hard internal boundaries and event-driven
seams. Contexts communicate through services and a transactional outbox, never
through each other's tables.

**Consequences.** We get real transactions across contexts, one deployment, one
place to enforce tenancy and audit, and no distributed-tracing prerequisite
before the first customer. We pay for it with a shared failure domain, a
codebase that requires discipline to keep from tangling, and coarse scaling.

**Revisit when** one context's write volume or deploy cadence genuinely diverges.
Documents and reporting are the likely first extractions — the outbox already
gives them an asynchronous boundary.

---

## ADR-0002 — PostgreSQL as the system of record {#adr-0002}

**Context.** The workload is relational and financial: double-entry ledgers,
temporal ownership, and reporting that slices by property, portfolio, period,
and owner simultaneously.

**Decision.** PostgreSQL, using its specific capabilities rather than treating it
as a generic store: `NUMERIC` for money, native `UUID`, `JSONB` for
configuration, partial and composite indexes, `CHECK` constraints as invariants,
and row-level security as an isolation layer.

**Consequences.** Correctness guarantees live in the database, where they cannot
be bypassed by a new code path. The cost is portability — SQLite is supported for
development and testing only, via type decorators that keep the *Python*
behaviour identical while the storage differs.

**Revisit when** a genuinely non-relational workload appears. Document search and
time-series telemetry are the plausible candidates, and both would be additions
alongside PostgreSQL, not replacements.

---

## ADR-0003 — Celery for background work {#adr-0003}

**Context.** OCR, statement generation, webhook delivery, recurring billing, and
SLA escalation must survive process restarts and be individually retryable.

**Decision.** Celery with Redis, `acks_late`, and `reject_on_worker_lost`, so a
worker that dies mid-task has its job redelivered rather than dropped. Queues are
routed by workload so document processing cannot starve webhook delivery.

**Consequences.** Mature scheduling, routing, and retry semantics, and an
operational surface people already know. The cost is a hard requirement that
**every task be idempotent** — at-least-once delivery guarantees a job runs twice
eventually. That is enforced by convention and by design review, and it is the
single most likely place for a subtle bug.

**Revisit when** the workload becomes stream-shaped rather than task-shaped, or
if exactly-once semantics become genuinely necessary rather than merely
convenient.

---

## ADR-0004 — Centralised policy-based authorization {#adr-0004}

**Context.** Atlas has staff roles, portfolio-scoped managers, and three portal
audiences who may only ever see their own records. Scattering checks across
views means the policy cannot be enumerated, cannot be tested exhaustively, and
gains a hole the first time someone adds a route in a hurry.

**Decision.** One engine. `evaluate(context, action, resource)` combines RBAC
(scoped to organization, portfolio, or property) with ABAC ownership predicates,
and denies by default. An unknown action fails closed. Sensitive actions demand a
fresh MFA assertion regardless of which role granted them.

**Consequences.** The policy is a data structure, so the test suite walks the
whole role × action × resource matrix. Every check pays a resolution cost, which
is why the context is built once per request and cached. Templates may hide
controls the viewer cannot use — that is courtesy; the engine is the enforcement.

**Revisit when** customers need to author their own policies. That is a rules
language, not more roles, and it should be a deliberate project.

---

## ADR-0005 — Document and asset graphs as the strategic differentiator {#adr-0005}

**Context.** The market is crowded on leasing, accounting, maintenance, and
portals. It is thin on connecting a file to the thing it is evidence *about*, and
thinner still on treating equipment as a first-class record.

**Decision.** Store each document once and link it to anything through a
generalised edge table. Model equipment as assets with service history,
warranties, expected life, and replacement forecasting.

**Consequences.** One certificate of insurance can be the vendor's compliance
record, an attachment on four work orders, and evidence in a claim — with one
expiry date rather than four copies that drift. Assets answer the question that
actually matters at 2am: *is this still under warranty, and who serviced it
last?* The cost is that a polymorphic edge table has no referential integrity to
the far side, so link validity is a service-layer responsibility, and queries
across it need care.

**Revisit when** the edge table becomes a query bottleneck. The answer then is
materialised per-entity views, not denormalising the graph away.

---

## ADR-0006 — AI deferred behind governance controls {#adr-0006}

**Context.** Every competitor markets AI. Atlas touches fair-housing decisions,
money movement, and habitability — three areas where a plausible-sounding wrong
answer is not a bad user experience but a legal exposure.

**Decision.** No AI in the 0.1.0 release. `FEATURE_AI_COPILOT` defaults off. When
it arrives it will be *suggestion only*, behind a human approval checkpoint, with
confidence and reasoning surfaced, prompt and response logged immutably where
permitted, tenant data isolated, and a per-tenant off switch.

**Consequences.** We forgo a checkbox competitors have. In exchange, no
automated decision can be taken that nobody approved, and the governance
infrastructure — approvals with separation of duties, dry-run execution, an
immutable audit chain — is being built for automation anyway, so AI inherits it
rather than needing it retrofitted.

**Revisit when** a specific workflow has a measurable error cost and a human
checkpoint that genuinely reduces it. Invoice coding and maintenance triage are
the strongest candidates. "Add AI" is not a trigger; a benchmarked workflow is.

---

## ADR-0007 — Automation rules are data, and dry run is structural {#adr-0007}

**Context.** A rule engine lets somebody who cannot deploy code cause arbitrary
changes to production data. Two failure modes follow directly. The first is
injection: if conditions are expressed in anything evaluated as code, a rule
author is a remote code execution away from the database. The second is the
untested rule that goes live and sends four hundred pay-or-quit notices.

**Decision.** Conditions are a JSON tree walked by a fixed operator table — no
`eval`, no attribute access, no regular expressions, and bounded depth. Actions
are registered handlers split into two functions: `describe`, which reads, and
`apply`, which writes. A dry run calls only `describe`.

**Consequences.** The sandbox holds by construction rather than by review: there
is no path from a condition to a Python object, so there is nothing to escape
from. Dry run cannot mutate, because in dry run the mutating function is never
called — as opposed to the usual design, where one function checks a `dry_run`
flag and the guarantee lasts exactly until somebody forgets to check it.

The costs are real. Every action is written twice, and the two halves can drift:
`describe` can claim something `apply` does not do. The operator vocabulary is
deliberately small, so some conditions are not expressible and have to become
code. And omitting regular expressions means pattern matching is limited to
prefix, suffix, and substring — chosen because a catastrophically backtracking
pattern is a denial of service that looks like a typo.

**Revisit when** rule authors repeatedly hit the vocabulary's edges. The answer
then is more operators, each with bounded cost — not a general expression
language.



---

## ADR-0008 — Typed-name e-signature, with the artifact pinned {#adr-0008}

**Context.** A lease has to be signed. The options are to integrate a signature
provider (DocuSign, Dropbox Sign), to implement certificate-backed digital
signatures, or to capture signature *in* Atlas and keep the evidence ourselves.

The first cannot be the only option: it puts a per-envelope cost and a
third-party dependency in the path of the most ordinary thing a management
company does, and small operators will not pay it. The second is a different
product — issuing and validating certificates is a trust business, not a
property one.

What actually determines whether a signature stands up is narrower than any of
those choices, and it is the same in all three: can you produce evidence of who
signed, when, from where, what they were shown, and that the document on file is
the document they saw. Statutes in the ESIGN/UETA family, and eIDAS at the
"simple electronic signature" level, ask for that record rather than for
cryptography.

**Decision.** Implement the envelope *lifecycle* in Atlas behind a provider
adapter, with a working built-in provider that captures a typed-name signature
plus a consent record — typed name, IP address, user agent, timestamp, and the
consent wording shown at the time.

Pin the artifact: the document's SHA-256 is captured when the envelope is sent
and re-checked before it completes. If it differs, the envelope voids and the
completion is refused.

Call it what it is. The built-in provider produces a simple electronic
signature, not an advanced or qualified one, and says so.

**Consequences.** The evidence a dispute actually turns on is captured at the
moment and cannot be reconstructed afterwards, which is the failure mode of
bolting this on later. "They signed it" and "they signed *this*" stop being
different claims, because a document swapped underneath an open envelope fails
loudly instead of quietly inheriting somebody's consent — the one attack this
design specifically defeats.

The costs. A typed-name signature carries less weight than a
certificate-backed one where an adversary disputes identity rather than intent;
Atlas mitigates identity with portal authentication, which is weaker than
government-ID verification and should not be described otherwise. Jurisdictions
requiring a qualified signature for residential tenancies are not served by the
built-in provider at all. And the `http` provider is deliberately unimplemented
rather than half-implemented: every vendor's API differs enough that a generic
client would be wrong for all of them, and a stub that silently succeeds is
worse than one that refuses.

**Revisit when** a deployment needs advanced or qualified signatures, or when
identity disputes appear in practice rather than in theory. The answer then is a
provider implementation behind the existing adapter — the lifecycle, the consent
record, and the artifact check do not change.

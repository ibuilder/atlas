# Runbooks

Written for someone woken at 3am who did not build this. Each starts with how to
confirm the problem, then how to reduce impact, then how to fix it. Diagnosis
before heroics.

## Severity

| | Meaning | Response |
|---|---|---|
| **SEV1** | Money at risk, data exposed, or the platform is down | Page immediately, incident channel, comms within 30 min |
| **SEV2** | A core workflow is broken for many tenants | Page during business hours, hourly updates |
| **SEV3** | Degraded but working, or one tenant affected | Next business day |

**Escalate to SEV1 immediately, regardless of blast radius:** any suspected
cross-tenant data exposure, any audit chain integrity failure, any unexplained
ledger imbalance.

---

## Authentication outage

**Confirm.** `atlas_auth_attempts_total{outcome="success"}` at zero while
attempts continue. `/readyz` still healthy rules out a total outage.

1. Check `/readyz` — a failing database check makes this a database incident.
2. Check Redis. Rate limiting and sessions depend on it; if it is unreachable,
   the limiter fails closed.
3. Look for a spike in `outcome="locked"`. A credential-stuffing run locks real
   accounts as a side effect — that is the lockout working, not a bug.
4. Check for a recent deploy that changed `SECRET_KEY`. Rotating it invalidates
   every session cookie at once and looks exactly like an auth outage.

**Reduce impact.** If lockouts are collateral damage from an attack, block the
source at the edge rather than raising `LOGIN_MAX_ATTEMPTS`.

**Never** disable MFA or authentication to restore service. An outage is
recoverable; an unauthenticated window is not.

---

## Queue backlog

**Confirm.** `atlas_queue_depth` climbing; `atlas_job_runs_total{outcome="retry"}`
rising.

1. Are workers alive? `docker compose ps worker` or the pod status.
2. Which queue? Routing means `documents` can back up while `webhooks` is fine.
3. Check `outcome="failure"` by task. One poisonous message retrying forever
   looks like a capacity problem and is not.
4. Scale workers for genuine volume; fix or drop the message for a poison pill.

**Safe to do.** Restart workers. Every task is idempotent, so redelivery is
expected and harmless — that property is exactly what makes this a low-risk
action.

---

## Payment provider outage

**Confirm.** `atlas_payments_total{outcome="failed"}` spiking; provider status
page.

1. Payment capture is idempotent by provider event id, so replayed webhooks
   after recovery will **not** double-charge.
2. Do **not** manually re-record payments during an outage. When the provider
   replays its backlog you will have two records, one of them invisible to
   reconciliation.
3. If residents cannot pay, extend the late-fee grace period at the organization
   level rather than suppressing the delinquency sweep — the sweep is what
   creates the audit trail for any waiver.

**After recovery.** Reconcile: compare `payments` against the provider's
settlement report for the window. Investigate any gap before closing the period.

---

## Webhook delivery failures

**Confirm.** `atlas_webhook_deliveries_total{outcome="failed"}` rising; endpoints
with a high `consecutive_failures`.

1. Failures against one endpoint are the customer's problem; failures across many
   are ours.
2. Endpoints auto-disable after sustained failure. Re-enabling before the
   receiver is fixed just refills the queue.
3. Dead-lettered deliveries are retained deliberately — replay them once the
   receiver is confirmed healthy.

---

## Database failover

**Confirm.** `/readyz` returning 503 with `database: error`; connection errors in
the logs.

1. Confirm the managed instance has actually failed over.
2. Application replicas recover on their own — `pool_pre_ping` discards stale
   connections rather than serving errors from them.
3. If they do not recover, restart replicas to rebuild the pool.
4. Check for in-flight transactions lost at the cut. Ledger postings are atomic,
   so a lost transaction leaves **no** partial entry — verify rather than assume.

**After recovery.** Run the trial balance. Debits must equal credits. If they do
not, treat it as SEV1 and stop write traffic to accounting.

---

## Audit chain integrity failure

**This is SEV1 regardless of how it was found.**

**Confirm.** `flask atlas verify-audit --org <slug>` reports `intact: false`, with
a `failure` of `content_modified`, `sequence_gap`, or `broken_link`.

1. **Do not "fix" the chain.** Rewriting hashes destroys the only evidence.
2. Record the reported `at_sequence` and `event_id`.
3. Identify who has direct database write access and correlate with the
   surrounding events.
4. Preserve a database snapshot before any remediation.
5. Involve whoever owns compliance. This is a control failure, not a bug.

A break means a row was altered or removed after the fact — the ORM refuses both,
so it did not come through the application.

---

## Suspected cross-tenant exposure

**SEV1.**

1. Capture the correlation ID and the request path.
2. Query the audit trail for that correlation ID across organizations.
3. Check for `security.tenant_isolation` entries — a caught attempt logs at
   CRITICAL and returns `404`.
4. If a query genuinely returned another tenant's data, restrict the affected
   endpoint at the edge before investigating further.
5. Run `flask atlas check-schema`. A table carrying `org_id` without being
   enrolled in tenant scoping is the most likely cause.

---

## Restoring from backup

Practise this **before** needing it. An untested backup is a hypothesis.

1. Restore to a **new** instance. Never over the live one.
2. `alembic current` — confirm the schema revision matches the application.
3. Run the trial balance and `verify-audit` for the largest tenants.
4. Only then repoint the application.

Note: restoring the database without the matching `FIELD_ENCRYPTION_KEY` leaves
every encrypted column unreadable. Back up the key separately, in a secret
manager, and confirm you can retrieve it as part of the drill.

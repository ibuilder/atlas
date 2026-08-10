# Architecture

## Shape

Atlas is a **modular monolith**: one deployable, hard internal boundaries,
event-driven seams where a service would eventually be extracted.

```
              ┌─────────────────────────────────────────┐
   HTTP ─────▶│ middleware                              │
              │  correlation id → org resolution →      │
              │  idempotency → CSRF                     │
              └──────────────────┬──────────────────────┘
                                 ▼
              ┌─────────────────────────────────────────┐
              │ routes            thin: HTTP ⇄ service  │
              │   └─ schemas      pydantic, strict      │
              └──────────────────┬──────────────────────┘
                                 ▼
              ┌─────────────────────────────────────────┐
              │ services          business rules,       │
              │                   transactions          │
              │   ├─ policy engine   deny by default    │
              │   ├─ audit           hash-chained       │
              │   └─ domain events → outbox → webhooks  │
              └──────────────────┬──────────────────────┘
                                 ▼
              ┌─────────────────────────────────────────┐
              │ models            SQLAlchemy 2.x        │
              │   └─ tenancy guard  injects org scope   │
              └──────────────────┬──────────────────────┘
                                 ▼
                      PostgreSQL (+ row-level security)
```

Dependencies point one way. A model never imports a service; a service never
imports a route. Where a lower layer needs something from a higher one, the
import is deferred into the function — which is also what keeps the application
factory working.

## Bounded contexts

`iam` · `orgs` · `crm_leasing` · `resident` · `accounting` · `maintenance` ·
`vendor` · `documents` · `asset_graph` · `automation` · `integration` ·
`reporting`

Each owns its models, services, and schemas. Cross-context reads go through the
other context's service, not its tables.

## The three ideas worth understanding

### 1. Tenant isolation is layered

One mechanism is one mistake away from a breach.

| Layer | Mechanism | Defeated by |
|---|---|---|
| 1 | Services resolve and pass an explicit organization scope | A service that forgets |
| 2 | ORM guard injects `org_id` into every tenant query, and refuses unscoped ones in strict mode | A raw SQL path |
| 3 | PostgreSQL row-level security | Connecting as the table owner |

Plus a fourth that is not a runtime layer at all: `assert_tenant_coverage()`
fails the build if a table carries `org_id` without subclassing `TenantModel`.
That is the mistake most likely to be made, and the one least likely to be
noticed in review — a model with `org_id: Mapped[str]` looks identical to one
that inherits it and behaves completely differently.

Layer 2 has a subtlety worth naming: SQLAlchemy's loader criteria only reach
statements that load an ORM *entity*. A bare `SELECT count(*)` selects none, so
aggregates get an explicit predicate instead. Dashboards are built almost
entirely from that query shape.

### 2. The audit trail is chained, not just written

Each event stores `SHA256(previous_hash ‖ canonical_json(fields))`. Altering or
removing any row breaks every hash after it, and `verify_chain()` reports the
exact sequence where the break begins. A scheduled job runs it nightly.

This does not prevent tampering — someone with write access can still damage the
trail. It makes tampering **undeniable**, which is the property compliance
actually asks for. Sequence allocation happens under a row lock on a per-tenant
chain head, so concurrent writers cannot fork the chain.

### 3. The ledger cannot silently drift

Double entry is enforced three times, on purpose:

1. The service refuses an unbalanced entry.
2. A flush-time listener recomputes both totals *from the lines*, so the
   denormalised columns cannot disagree with the rows they summarise.
3. A database `CHECK` refuses to store a posted entry whose totals differ.

Posted entries are immutable at the ORM boundary. Corrections are reversals:
the books then show what happened *and* what was believed at the time.

## Request lifecycle

1. **Correlation** — adopt or mint an ID; bind the ambient context that logging,
   auditing, and the tenancy guard all read.
2. **Authentication** — session cookie (server-side session validated) or bearer
   token (hashed lookup, CIDR check).
3. **Organization resolution** — home organization, or a requested one if the
   user object confirms access. An unauthorised switch is a `404`.
4. **Idempotency** — claim the key, or replay the stored response.
5. **Authorization** — `require(action, resource)`.
6. **Service** — one transaction, audit event, domain event to the outbox.
7. **Response** — error envelope on failure, security headers, correlation echo.

## Data model conventions

- **UUIDv7 primary keys.** Time-ordered, so inserts stay at the right edge of
  the index instead of scattering across it.
- **Money is `Decimal`.** `NUMERIC(20,4)` on PostgreSQL; scaled integers on
  SQLite, because SQLite's float fallback makes `0.10 + 0.20` equal
  `0.30000000000000004` in a ledger test.
- **Datetimes are timezone-aware.** Naive values are rejected at the column.
- **Soft deletion everywhere.** Financial and lease history must stay
  reconstructible; purging is a separate, deliberate act.
- **Actor columns are not foreign keys.** Attribution has to survive a user row
  being erased for a data-subject request.

## Where this design would change

Stated so the trade-offs are inspectable rather than implied:

- **Extract a service** when one context's write volume or deploy cadence
  genuinely diverges. Documents and reporting are the likely first candidates;
  the outbox already gives them an asynchronous boundary.
- **Add read models** when dashboards start contending with operational writes.
  `KpiSnapshot` exists for exactly this and is not yet populated.
- **Split the database** only after RLS, connection limits, and read replicas
  have been exhausted. Cross-context transactional consistency is the main thing
  a monolith buys, and property operations use it constantly — a payment posts
  to the ledger, updates an invoice, and closes a delinquency in one transaction.

## Decisions

See [ADRs](adr/README.md) for why the modular monolith, why PostgreSQL, why
Celery, why a policy engine, why the document and asset graphs are the strategic
bet, and why AI is deferred behind governance.

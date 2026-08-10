# Contributing

## Getting set up

```bash
make setup          # venv, dependencies, pre-commit hooks
cp .env.example .env
make demo           # migrate, seed a full portfolio, run the app
```

Tests run against in-memory SQLite by default and need no services. Before
opening a pull request, run the same suite against PostgreSQL — a few behaviours
(native `UUID`, `NUMERIC`, row-level security) only exist there:

```bash
DATABASE_URL=postgresql+psycopg://atlas:atlas@localhost/atlas_test make test
```

## The rules that are not negotiable

These are enforced by tests, not by review, because review is where tired people
approve things.

1. **No business rules in route handlers.** Routes translate HTTP into a service
   call and back. If a handler contains an `if` about the domain, it belongs in
   a service.
2. **Every mutating action emits an audit event.** Through
   `record_audit_event`, never by writing the table directly.
3. **Every protected route goes through the policy engine.** `require(...)`, not
   an ad-hoc role check. A new permission goes in the catalogue with a
   `sensitive` flag decided deliberately.
4. **Every tenant-scoped table subclasses `TenantModel`.** A table with `org_id`
   that does not is outside query scoping and outside row-level security — and
   looks completely correct in a diff. `make check-schema` fails the build.
5. **Every external integration sits behind an adapter interface.** The domain
   never imports a vendor SDK.
6. **Every background job is idempotent.** At-least-once delivery guarantees a
   job runs twice eventually. Use a watermark, not a memory of having run.
7. **Money is `Decimal`, never `float`.** It crosses the API as a string.
8. **Datetimes are timezone-aware.** Naive datetimes are rejected at the column
   boundary.
9. **Posted ledger entries are immutable.** Corrections are reversals.
10. **Every returned error uses the standard envelope** with a stable code.

## Schema changes

```bash
make migrate m="add widget table"   # autogenerate
# read the generated file - autogenerate is a first draft, not an answer
make upgrade
make downgrade                      # prove it reverses before you commit
```

CI runs `alembic check` and fails on a model edited without a migration.
Custom column types must render as themselves — `migrations/env.py` handles
this, and a new type decorator needs adding to `_ATLAS_TYPES` there.

## Tests

| Layer | What belongs there |
|---|---|
| `tests/unit` | Pure logic: policy evaluation, hashing, money, the audit chain. |
| `tests/integration` | Database-backed workflows across services. |
| `tests/security` | Isolation, authorization boundaries, HTTP hardening. |
| `tests/contract` | External adapter shapes. |

A bug fix comes with a test that fails without it. A security fix comes with a
test that performs the attack.

## Commits and pull requests

[Conventional Commits](https://www.conventionalcommits.org/):
`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `perf:`, `build:`.

A pull request should say what changed and why, note any migration, and call out
anything a reviewer should look at twice. If it touches authorization, tenancy,
or the ledger, say so in the title — those get a second reviewer.

## Definition of done

- [ ] Tests pass on SQLite and PostgreSQL
- [ ] `make check` is clean
- [ ] Migration included, and reverses
- [ ] Audit events emitted for new mutations
- [ ] Permissions added to the catalogue with a considered `sensitive` flag
- [ ] `docs/FEATURES.md` updated if capability status changed
- [ ] `CHANGELOG.md` updated under Unreleased

# Deployment

How to run Atlas somewhere that matters. The reference deployment is
`docker-compose.yml`; anything below that says **must** is enforced by the
application refusing to boot, and everything else is a decision you are making
whether or not you notice.

For what to do when it breaks, see [runbooks/](docs/runbooks/README.md). For
backup and restore specifically, see
[disaster-recovery.md](docs/runbooks/disaster-recovery.md).

---

## 1. What it needs

| Component | Why | Notes |
|---|---|---|
| PostgreSQL 14+ | System of record | **Required** outside development â€” see Â§3 |
| Redis 7+ | Celery broker, rate-limit and cache backend | |
| A malware scanner | Uploads are quarantined until one clears them | ClamAV in the reference compose |
| Object storage or a volume | Documents | Local path by default; S3-compatible supported |
| SMTP | Notices, statements, portal mail | `console` backend prints instead, for development |

Three process types, all from the same image:

- **web** â€” Gunicorn behind your TLS terminator
- **worker** â€” Celery worker
- **beat** â€” Celery beat, **exactly one instance**

> Running two `beat` processes runs every scheduled job twice. The jobs are
> idempotent by watermark, so this is survivable rather than catastrophic â€” but
> it doubles the load and makes the logs lie about how often things happen.

---

## 2. Configuration

Everything is environment variables, read once at startup into a validated
settings object. `ATLAS_ENV` selects the profile: `development`, `testing`,
`staging`, or `production`.

### Secrets that must be set

| Variable | What it protects | If it changes |
|---|---|---|
| `SECRET_KEY` | Sessions, CSRF tokens, signed URLs | Everyone is signed out; signed links break |
| `FIELD_ENCRYPTION_KEY` | Encrypted columns â€” bank details, TINs | **Encrypted data is unrecoverable.** Back it up separately from the database |
| `WEBHOOK_SIGNING_SECRET` | Outbound webhook signatures | Consumers reject deliveries until updated |

`FIELD_ENCRYPTION_KEY` is a urlsafe base64 32-byte Fernet key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**Store it somewhere the database backup is not.** A backup and the key to read
it, in one place, is one compromise rather than two â€” and a backup without the
key is a restore that produces unreadable columns. The disaster-recovery runbook
recovers the key *first*, before the database, for that reason.

### What production refuses to boot on

These are `ConfigError` at startup, not warnings:

- `SECRET_KEY` or `FIELD_ENCRYPTION_KEY` unset, a known placeholder, or under 32 characters
- `DEBUG` or `TESTING` enabled
- `DATABASE_URL` pointing at SQLite
- `SESSION_COOKIE_SECURE` disabled
- `CSRF_ENABLED` disabled
- `CELERY_TASK_ALWAYS_EAGER` enabled
- `*` in `CORS_ALLOWED_ORIGINS`
- `STORAGE_BACKEND=local` — object storage is required; local disk is not
  durable across replicas, and a restore that comes back without the documents
  has failed quietly
- `MAIL_BACKEND=console` — delinquency notices carry statutory response
  deadlines, and a backend that prints them to a log has not sent them
- `MFA_REQUIRED_FOR_PRIVILEGED` disabled
- A non-PostgreSQL `DATABASE_URL`, or a Redis URL that is really an in-memory cache

Failing to start is cheaper than failing open. If one of these bites during a
deploy, it has just prevented the incident.

### Worth setting deliberately

| Variable | Default | Consider |
|---|---|---|
| `MALWARE_SCANNER` | `structural` | `clamav` for anything accepting resident uploads |
| `CLAMAV_HOST` / `CLAMAV_PORT` | `127.0.0.1` / `3310` | |
| `RATELIMIT_DEFAULT` | `600 per minute` | Applied through config, before `init_app` |
| `MAIL_BACKEND` | `console` | `smtp` |
| `ESIGN_BACKEND` | `mock` | `mock` signs inside Atlas; `http` needs a provider client you supply |
| `STORAGE_BACKEND` | local | S3-compatible |
| `LOG_FORMAT` | `console` | `json` for anything shipping to a log aggregator |
| `FORCE_HTTPS` | on when deployed | Leave it on |

---

## 3. The database

**Do not connect as a superuser.** This is the single most important line in
this document. Tenant isolation's third layer is PostgreSQL row-level security,
and **a superuser bypasses RLS unconditionally** â€” silently, with no error. Two
of the three isolation layers still hold, but the one that catches everything
else does not.

Create a role that is not a superuser and does not own the tables:

```sql
CREATE ROLE atlas_app LOGIN PASSWORD 'â€¦' NOSUPERUSER NOCREATEDB NOCREATEROLE;
GRANT CONNECT ON DATABASE atlas TO atlas_app;
GRANT USAGE ON SCHEMA public TO atlas_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO atlas_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO atlas_app;
```

Run migrations as a role that *can* alter the schema, and run the application as
`atlas_app`. `FORCE ROW LEVEL SECURITY` is applied by migration, so the table
owner is subject to the policy too â€” but ownership still carries DDL rights the
application has no business holding.

### Migrations

```bash
alembic upgrade head
```

Run it before the web process accepts traffic. The reference compose does this
in the `web` command. Every migration in this repository has a tested
`downgrade`, and CI proves the whole chain applies and reverses.

**A migration that adds a tenant-scoped table must call
`apply_tenant_policies()`.** The original RLS migration walked the tables that
existed when it ran; a table added afterwards has no policy and looks entirely
correct. `app.models.registry` fails the test suite if a tenant table has no
policy, which turns "somebody forgot" into a red build rather than a breach.

---

## 4. First run

### The quickest whole deployment

```bash
cp .env.production.example .env.production   # fill it in; nothing has a working default
docker compose -f docker-compose.prod.yml --env-file .env.production up -d
```

Deliberately a separate file from `docker-compose.yml` rather than an override
on top of it. Every convenience in the development stack â€” a default secret,
HTTPS off, the database port published â€” is a liability once the thing is
reachable, and a forgotten `-f` that silently deploys those defaults is a
failure nobody sees. A separate file cannot be half-applied.

It pulls a published image rather than building, runs migrations as their own
container so two replicas cannot race the same one, and binds the web port to
loopback: put a TLS-terminating proxy in front of it.

Then create the organization and its administrator, as below.

### By hand

```bash
alembic upgrade head

# The organization first: create-admin needs one to put the administrator in.
flask atlas create-org --name "Your Company" --slug your-company

# Prompts for the password rather than taking it on the command line.
flask atlas create-admin --org your-company --email you@example.com --name "Your Name"

flask atlas check-schema        # tenant coverage, audit columns, indexed FKs
flask atlas verify-scanner      # proves EICAR is detected by whatever is configured
```

`create-org` provisions the tenant's roles and its chart of accounts. It exists
because writing this guide showed that it did not: the only way to create an
organization was `flask seed demo`, so a real deployment could not be
provisioned without the demo accounts and their published password.

`verify-scanner` matters more than it looks. "A ClamAV adapter is included" and
"this deployment scans uploads" are different claims, and only one is worth
anything. It fails loudly if the daemon did not answer, and distinguishes that
from a scanner that answered wrongly.

Enrol MFA on the administrator before using the account for anything.

**Do not run `flask seed demo` anywhere real.** It creates accounts with a
published password.

---

## 5. Health, metrics, logs

| Endpoint | Use |
|---|---|
| `/healthz` | Liveness. The process is up; no dependencies touched |
| `/readyz` | Readiness. Checks the database and Redis |
| `/healthz/ping` | Bare liveness, registered by the factory |
| `/metrics` | Prometheus |

Point your orchestrator's liveness probe at `/healthz` and readiness at
`/readyz`. Using the deep check for liveness restarts the web process every time
the database hiccups, which converts a brief database problem into an outage.

Set `LOG_FORMAT=json`. Every log line carries a correlation id that also appears
in the error envelope returned to the caller, so a user's screenshot of an error
is enough to find the request.

Metrics worth alerting on: `atlas_maintenance_sla_breaches_total`,
`atlas_reconciliation_exceptions`, failed `atlas_job_runs_total`, and the audit
chain verification job's outcome. A broken audit chain is the one that should
page somebody.

---

## 6. Scheduled work

Beat runs, among others:

| Job | Cadence | What silence means |
|---|---|---|
| `atlas.audit.verify_chains` | Daily | Nobody is checking the audit trail is intact |
| `atlas.maintenance.escalate_sla_breaches` | Frequent | Statutory response deadlines pass unnoticed |
| `atlas.billing.generate_recurring_charges` | Monthly | Rent is not invoiced |
| `atlas.collections.sweep_delinquency` | Daily | Delinquency never escalates |
| `atlas.esign.expire_envelopes` | Hourly | An open envelope can be signed a year later |
| `atlas.webhooks.dispatch_pending` | Frequent | Integrations go quiet |
| `atlas.documents.scan` | On upload | Uploads stay quarantined |

All are idempotent by watermark, so a missed window is caught up rather than
skipped, and a double run does not double-charge.

---

## 7. Upgrading

1. Read the [CHANGELOG](CHANGELOG.md). Breaking changes are called out.
2. Back up, and verify the backup â€” see the DR runbook.
3. `alembic upgrade head` with the application stopped or in maintenance mode.
4. Roll the web processes, then workers, then beat.
5. `flask atlas verify-restore` if the upgrade touched anything structural: it
   proves the encryption key still decrypts, every audit chain is intact, every
   ledger balances, and RLS survived.

Before 1.0 the REST API contract may change between minor versions. Error codes
and the `/api/v1` namespace are already treated as stable.

---

## 8. What this deployment does not do for you

Stated plainly, because finding out later is worse:

- **TLS.** Terminate it in front. `FORCE_HTTPS` sets the headers and assumes
  you did.
- **Backups.** The runbook says how; nothing here runs them on a schedule.
- **Secret management.** Environment variables are read once at boot. Use your
  platform's secret store to put them there.
- **Horizontal scale beyond one beat.** Web and workers scale out; beat does not.
- **The four 1.0 conditions** in [ROADMAP.md](ROADMAP.md) â€” a disaster-recovery
  drill actually executed, load testing on production-shaped hardware, an
  independent penetration test, and a 90-day soak. They are unticked because
  they are unmet, not because they are unimportant.

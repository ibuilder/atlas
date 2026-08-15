# Disaster recovery

A runbook that has never been executed is a document, not a capability. This
one is written to be *run*, on a schedule, against a real restore — and the
checklist at the end is the evidence that it was.

The single most common way this goes wrong is not losing the database. It is
restoring the database and discovering that the field-encryption key is gone,
at which point every encrypted column is permanently unreadable and the backup
is a very large file of nothing. **Key recovery is step one here for that
reason.**

---

## What must survive

| Asset | Where it lives | Loss means |
|---|---|---|
| PostgreSQL cluster | Managed instance or self-hosted volume | Everything. |
| `ATLAS_FIELD_ENCRYPTION_KEY` | Secret manager, *not* the database | Encrypted columns are unreadable forever. |
| `ATLAS_SECRET_KEY` | Secret manager | Sessions and signed URLs invalidate; recoverable by rotation. |
| Document object store | S3 bucket or local volume | Leases, certificates, inspection photos. |
| Audit chain heads | In the database | The chain cannot be verified past the gap. |

`ATLAS_FIELD_ENCRYPTION_KEY` is the one with no recovery path. It is a Fernet
key; there is no way to derive it, no way to brute-force it, and no support
call that gets it back. It must be stored somewhere the database backup is not,
and it must be part of the restore drill — because a drill that skips it does
not test the failure that actually happens.

---

## Recovery objectives

| Measure | Target | How it is met |
|---|---|---|
| RPO — data loss | ≤ 5 minutes | Continuous WAL archiving. Nightly base backup alone gives 24h, which is not the target. |
| RTO — time to serve | ≤ 60 minutes | Restore, migrate, verify, cut over. The drill below is timed against this. |
| Audit continuity | Zero gaps | `verify_chain` must pass for every organization after restore. |

These are targets, not measurements, until a drill records the actual numbers.
The checklist has a place to write them down.

---

## Backup

### Database

```bash
pg_dump --format=custom --no-owner --no-privileges \
  --file="atlas-$(date -u +%Y%m%dT%H%M%SZ).dump" "$DATABASE_URL"
```

Custom format because it restores selectively and in parallel. `--no-owner`
because the restore target rarely has the same role names, and a restore that
fails on a missing role at 3am is an avoidable half hour.

WAL archiving is what turns a 24-hour RPO into a 5-minute one. A nightly dump
without it does not meet the objective above.

### Secrets

Export from the secret manager, not from a running container's environment — a
container's environment reflects what it was *given*, which may already be
stale relative to the source of truth.

```bash
# Record the key identifiers and versions, never the values, in the drill log.
echo "field_encryption_key_version=$(vault kv metadata get -format=json secret/atlas | jq .data.current_version)"
```

### Documents

If storage is S3, enable versioning and cross-region replication and this step
is a configuration check rather than a copy. If storage is a local volume, it
is a copy, and it is the step most likely to be forgotten because the database
feels like the important part.

---

## Restore

### 1. Recover the encryption key first

Before restoring anything. If the key cannot be recovered, the rest of the
procedure produces a database whose encrypted columns are gone, and it is
better to know that in minute one than in minute fifty.

```bash
export ATLAS_FIELD_ENCRYPTION_KEY="$(vault kv get -field=field_encryption_key secret/atlas)"
python -c "
from cryptography.fernet import Fernet
import os
Fernet(os.environ['ATLAS_FIELD_ENCRYPTION_KEY'].encode())
print('key is well-formed')
"
```

A well-formed key is not necessarily the *right* key. Step 4 proves that.

### 2. Restore the database

```bash
createdb atlas_restored
pg_restore --dbname=atlas_restored --no-owner --jobs=4 atlas-<timestamp>.dump
```

### 3. Bring the schema to head

A backup is at whatever migration was current when it was taken. Deploying
newer code against it without migrating is the second most common restore
failure.

```bash
DATABASE_URL=postgresql://.../atlas_restored alembic upgrade head
```

### 4. Verify — this is the part that matters

```bash
DATABASE_URL=postgresql://.../atlas_restored flask --app wsgi atlas verify-restore
```

It checks four things, and a restore is not complete until all four pass.
Exits non-zero on any failure, so it can gate a cut-over rather than being
read and nodded at:

1. **The encryption key is the right one.** Decrypts a known encrypted column.
   A wrong key fails here rather than silently returning ciphertext.
2. **The audit chain is intact for every organization.** `verify_chain` walks
   the hash chain and reports the exact sequence number of any break. A restore
   that loses audit continuity is a restore that cannot be attested to.
3. **The ledger balances.** Total debits equal total credits, per organization.
   If this fails, the restore is partial, whatever the row counts say.
4. **Row-level security is enabled on every tenant table.** RLS policies are
   schema objects and a restore can drop them; a database that comes back
   without them looks entirely correct and isolates nothing.

### 5. Cut over

Point `DATABASE_URL` at the restored database, start one instance, run the
verification again against the live configuration, then scale up. Sessions
will have been invalidated if `ATLAS_SECRET_KEY` changed; that is expected and
users simply sign in again.

---

## The drill

Quarterly, against a real restore into a scratch database. Not a review of this
document — an execution of it, timed.

| Field | Value |
|---|---|
| Date | |
| Performed by | |
| Backup used (timestamp) | |
| Key recovered from | |
| Time to key recovery | |
| Time to database restored | |
| Time to migrations at head | |
| Time to all four checks passing | |
| **Total RTO** | |
| Measured RPO (newest transaction present) | |
| Checks 1–4 all passed | ☐ |
| Issues found | |
| Runbook corrections needed | |

The last row is the point of the exercise. A drill that finds nothing to
correct has usually not been run properly.

---

## Failure modes seen in practice

**The key was in the same secret store as the database credentials, and the
store was the thing that was lost.** Store the field-encryption key somewhere
with a different blast radius.

**The restore succeeded and RLS did not come back.** Policies are schema
objects. `pg_restore` on a database created without them leaves a cluster that
serves every tenant's data to every tenant, and nothing in the application
complains. Check 4 exists for this.

**Migrations were run before the key was recovered.** Some migrations touch
encrypted columns. Recover the key first; the order in this runbook is not
arbitrary.

**The documents were not in the backup at all.** The database is the part
people think of. A lease nobody can produce is still a lost lease.

---

<sub>SPDX-License-Identifier: MIT</sub>

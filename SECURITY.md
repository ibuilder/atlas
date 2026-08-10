# Security

## Reporting a vulnerability

Please **do not open a public issue**. Report privately through
[GitHub Security Advisories](https://github.com/ibuilder/atlas/security/advisories/new).

Include what you can: affected version or commit, reproduction steps, impact,
and any proof of concept. Expect an acknowledgement within three working days
and an assessment within ten. We will credit you in the advisory unless you
prefer otherwise, and we will not pursue action against good-faith research that
stays within your own deployment.

## What Atlas enforces

| Control | Where |
|---|---|
| Tenant isolation | Service scoping, an ORM guard, and a build-failing schema invariant. Cross-tenant access returns `404`, never `403`. |
| Deny-by-default authorization | `app/security/policies.py`. Unknown actions fail closed. |
| Tamper-evident audit | Per-organization SHA-256 chain; modification or deletion is refused and detectable. |
| Password storage | Argon2id, tuned by configuration, rehashed transparently on upgrade. |
| MFA | TOTP with replay protection; sensitive actions require a *fresh* assertion. |
| Session control | Server-side, individually revocable; all sessions die on password change. |
| Field encryption | MFA seeds, tax identifiers, government IDs, bank details. Keys rotate without downtime. |
| Transport and headers | HSTS, a CSP with no `unsafe-inline`, `nosniff`, `DENY` framing, isolated cross-origin policies. |
| Input validation | Strict schemas that reject unknown fields; HTML stripped at the boundary. |
| Rate limiting | Per identity when authenticated, per IP otherwise. |
| Idempotency | Retried writes replay; the same key with a different body is rejected. |
| Outbound webhooks | HMAC-SHA256 signatures with the timestamp inside the signed string; customer-supplied URLs are refused when they resolve to private, loopback, or link-local addresses. |
| Secrets | Configuration only, typed as `SecretStr`. Production refuses to start on a weak or placeholder secret. |

## Deployment expectations

Atlas fails closed at startup, but it cannot enforce the environment around it.

- Terminate TLS in front of the app and set `TRUSTED_PROXY_COUNT` to the **real**
  number of proxies. A larger number lets a client forge its own source IP,
  which silently defeats rate limiting and IP allowlists.
- Supply `SECRET_KEY` and `FIELD_ENCRYPTION_KEY` from a secret manager. Losing
  the encryption key makes encrypted columns unrecoverable; there is no
  backdoor, by design.
- Set `METRICS_TOKEN`. `/metrics` exposes tenant counts and revenue-shaped
  counters.
- Enable PostgreSQL row-level security (`DB_ENABLE_RLS=true`) **and connect as a
  role that is actually subject to it.** This is the single easiest way to
  deploy Atlas with RLS enabled, looking correct, and doing nothing:
  - A **superuser bypasses every policy unconditionally**, and `FORCE ROW LEVEL
    SECURITY` does not change that. The default user created by the official
    `postgres` image is a superuser.
  - A **table owner** bypasses policies unless `FORCE` is set. Atlas sets it, so
    ownership alone is survivable — but a superuser is not.
  - Create a dedicated `NOBYPASSRLS` role for the application, grant it only
    `SELECT, INSERT, UPDATE, DELETE` on the schema, and run migrations as a
    separate, more privileged role.

  Verify it rather than assuming: connect as the application role and run
  `SELECT count(*) FROM properties` with no tenant variable set. It must return
  zero.

  The policy is suspended only for a deliberate act: an explicit `unscoped()`
  block, or a system context that has not yet chosen a tenant (provisioning,
  seeding, migrations). Merely having no organization bound is **not** grounds
  for a bypass — that case is denied, so a query that forgets to establish a
  tenant returns nothing rather than everything.
- Restrict database network access. Isolation layers 1 and 2 live in the
  application; only RLS protects against a direct connection.
- Back up and **test restoring**. An untested backup is a hypothesis.

## Known limitations in 0.1.0

Stated plainly rather than left to be discovered:

- **No SSO.** `idp_issuer` and `idp_subject` exist on `User`; no protocol
  implementation. Enterprise deployments needing SAML or OIDC should wait.
- **The bundled malware scanner is not virus detection.** Upload, quarantine,
  release, and retrieval are implemented, and the scanner is pluggable — a
  ClamAV adapter ships and fails closed when the daemon is unreachable. The
  *default* adapter performs structural checks only: the EICAR test file,
  embedded JavaScript, and Office macros. It will not catch a novel threat.
  Configure a real scanner before accepting untrusted uploads.
- **The automation engine does not execute.** Rules can be stored but not run,
  so no automated action can fire unexpectedly.

## Cryptography

Argon2id for passwords. SHA-256 for high-entropy tokens (there is nothing to
brute-force, and constant-time lookup by digest is required). Fernet
(AES-128-CBC + HMAC-SHA256) for field encryption. HMAC-SHA256 for webhook
signatures. SHA-256 for the audit chain.

No custom cryptographic constructions. Everything comes from `cryptography`,
`argon2-cffi`, or the standard library.

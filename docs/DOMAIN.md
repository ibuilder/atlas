# The domain

What a second engineer needs before changing anything. `ARCHITECTURE.md` covers
how the code is arranged; this covers what the records *mean* and which
invariants are load-bearing — the knowledge that otherwise lives in module
docstrings and is only found by opening the right file.

Read in order. Each section ends with **what breaks** if the rule is ignored,
because that is the part worth remembering.

---

## 1. Tenancy

Every business row belongs to exactly one organization. That is enforced three
times, deliberately, because each layer fails differently:

| Layer | What it catches | Where |
|---|---|---|
| Service scoping | The ordinary case: a query written without a filter | `org_id=` on every service function |
| ORM guard | A query that escaped the service layer | `with_loader_criteria` in `app/models/base.py` |
| Row-level security | Anything that reaches the database another way | Applied by migration; see `migrations/support/rls.py` |

Subclassing `TenantModel` is what enrols a table in all three.
`app.models.registry.assert_tenant_coverage` **fails the build** if a table has
an `org_id` column and is not a `TenantModel`, so the enrolment cannot be
forgotten quietly.

Two details that surprise people:

**The RLS predicate has a bypass.** `atlas.bypass_rls = on` exists for
provisioning and for restore verification, where there is no tenant yet. It is
set deliberately and never from request handling.

**A PostgreSQL superuser bypasses RLS unconditionally.** The application must
not connect as one. This is the single most important line in the deployment
guide and the easiest to get wrong.

**Cross-tenant access returns 404, never 403.** A 403 confirms the record
exists, which turns any endpoint into an oracle for enumerating another
tenant's data. Every portal and API route follows this.

> **What breaks:** a table that skips `TenantModel` looks correct in every test
> that uses one organization, and leaks the moment a second one exists.

---

## 2. Money and time

**Money is `Decimal`, never float.** The `Money` column type is `NUMERIC` on
PostgreSQL and scaled integers on SQLite, so the same arithmetic holds on both.
`quantize_money()` rounds `ROUND_HALF_UP` rather than Python's default
`ROUND_HALF_EVEN`, because that is what accounting convention and every
tenant's calculator expect.

Guard non-finite input at the boundary. `Decimal("NaN")` survives
`quantize_money`, and every *ordered* comparison against it raises rather than
returning `False` — so a `<= 0` check does not reject NaN, it crashes on it.
Use `amount.is_finite()`.

**Time is timezone-aware UTC.** `utcnow()` is the only sanctioned source of
"now". A local `date.today()` compared against a UTC service clock agrees for
most of the day and disagrees either side of the rollover, which is a test that
passes all morning and fails at 21:30.

**Identifiers are UUIDv7**, presented as `str` everywhere. Uniform string
identifiers avoid a family of bugs where a `UUID` object and its string form
fail to compare equal across a cache, a context variable, or a JSON boundary.

> **What breaks:** float money is wrong by a cent per few thousand operations,
> and nobody finds it until a trial balance is out by an amount too small to
> explain and too large to ignore.

---

## 3. The ledger

Double entry, enforced rather than assumed:

- Every journal entry stores its own debit and credit totals.
- A database `CHECK` refuses a *posted* entry whose totals differ.
- A flush-time listener recomputes both from the lines, so the denormalised
  figures cannot drift from the rows they summarise.

Two mechanisms, because the ledger being wrong is not a bug you find in a test —
it is one you find in an audit.

**Posted entries are immutable.** Corrections are a reversal plus a new entry.
Mutating history is how a reconciled month silently stops reconciling.

**Control accounts require `system_posting=True`.** Accounts receivable, the
deposit liability, and the trust cash accounts are maintained by services, not
by hand, and posting to them directly is refused.

> **What breaks:** an entry posted around the service leaves the control account
> disagreeing with the subledger it summarises, and the difference is found by
> whoever runs the next reconciliation.

---

## 4. The audit trail

Per-organization hash chain:

```
entry_hash = SHA256( previous_hash || canonical_json(event fields) )
```

Each organization has its own chain and its own monotonic sequence, with a
locked chain-head row so concurrent writers cannot interleave. `verify_chain()`
recomputes the whole thing; `flask atlas verify-restore` runs it as part of
proving a restore.

**What is inside the hash matters.** It covers the action, actor, resource,
payload, *reason*, resource label, and severity. An earlier version omitted the
reason — which meant a rejected approval's stated justification could be
rewritten afterwards without breaking the chain. If you add a field somebody
would want to argue about later, it belongs in the hash.

Audit at the granularity somebody asks questions at. Message *threads* are
audited on open, assign, and resolve, not once per reply: one chain entry per
message buries everything else in it.

> **What breaks:** a field outside the hash is a field an insider can change
> without detection, which is the entire property the chain exists to provide.

---

## 5. Leasing and tenancy

The chain is `Lead → Application → Lease → Tenancy → MoveOut`, with `Turn`
running alongside the gap between one tenancy and the next.

**A lease and a tenancy are different things.** A lease is the contract; a
tenancy is one person's relationship to it. Two residents on one lease are two
tenancies, and financial responsibility is per tenancy.

**Renewal terms are fixed when offered.** A resident accepts *that* offer, not
whatever the asking rent has become since. A lapsed offer cannot be honoured.

**The statutory disposition clock starts at the move-out and the deadline is
stored**, not recomputed. A recomputed deadline drifts every time somebody
changes the setting; a stored one is the date the law will be measured against.
A late disposition is audited as CRITICAL because past the deadline the
deductions are usually forfeit entirely.

**Deductions need evidence.** A description and an amount, sourced from
inspection findings where there are any, and never exceeding what is held.

> **What breaks:** settling against `lease.security_deposit` (what the contract
> says) rather than what was collected refunds money that was never taken. This
> was a real bug; the fix was the deposit subledger below.

---

## 6. Deposits and trust

A trust account holds money the operator does not own. Two questions must be
answerable at any moment:

1. **How much is in the account?** The general ledger answers this.
2. **Who is it owed to, and how much each?** Nothing answers this unless
   something records it.

The second is the *beneficiary ledger*, and it is what makes a three-way
reconciliation three-way. Bank and book can agree perfectly while the operator
is short, because both measure the same pile of money from the same side. Only
the sum of what every resident is individually owed can contradict them.

`DepositMovement` is that record: signed amounts (positive took money in,
negative let it out), against a lease **and** a named trust account, on an
effective date. It is movements rather than a balance for two reasons, and both
are the difference between a reconciliation and a number:

- **A balance cannot be asked about the past.** Tying out a year end in March
  needs what was held on 31 December.
- **A balance does not say where the money is.** An operator with an account per
  jurisdiction needs each reconciled against the deposits *that account* holds.

Every movement is recorded *and* posted to the ledger in the same call. There is
no public function that does one without the other.

> **What breaks:** this is exactly what went wrong. `Lease.deposit_held` was
> read by the reconciliation and written by nothing, so the beneficiary total was
> always zero, the "book versus beneficiaries" difference was the entire trust
> balance, and `shortfall` could never be non-zero. The one thing the module
> existed to catch was the one thing it could not.

---

## 7. Ownership

Ownership is time-bounded stakes, not a current percentage, because every
question worth asking about it is a question about a date.

A transfer **closes** the outgoing stake the day *before* it takes effect and
**opens** the incoming one on the day of it. The two therefore never both cover
the transfer date, and the day-weighted apportionment in
`accounting/statements.py` splits a mid-period transfer correctly with no
special case.

**The invariant is on the total.** A property owned at all totals exactly 100%,
checked after every transfer. Zero is allowed — a managed property with no
equity record on file is ordinary — but anything strictly between is a share
nobody holds.

> **What breaks:** a transfer that moves 96% where 100% was held does not fail.
> It silently under-distributes every owner statement from that date, and nobody
> notices until an owner adds up a year of them.

---

## 8. Maintenance and turns

**A request and a work order are separate records.** One report can spawn three
work orders across three trades; three reports of the same leak collapse into
one. Fusing them — the shortcut almost every simple system takes — makes both of
those ordinary situations unrepresentable.

**Habitability is a first-class field**, not another priority level. No-heat,
no-water, and no-power carry statutory response deadlines, and that clock must
not be adjustable by whoever is triaging at the time.

**A turn measures the vacancy, not the work.** It opens when the keys come back
— `record_move_out` starts one — because the clock that matters starts then
rather than when somebody remembers to raise a job. A turn cannot be marked
ready while a required step is outstanding; a step may be skipped, but never
silently, and the reason lands in the audit payload.

> **What breaks:** a unit advertised as ready before it is produces a cancelled
> move-in and a refunded holding deposit, not a saved day.

---

## 9. Documents and signatures

**A document is stored once and linked to everything it relates to.** One
certificate of insurance is simultaneously the vendor's compliance record, an
attachment on four work orders, and evidence in a claim. Copying it four times
means four things to expire, three of which nobody updates.

Uploads are quarantined until scanned, the storage key is never derived from the
user-supplied filename, and retrieval goes through a signed expiring URL — so a
leaked link is a time-boxed leak of one document rather than a directory. The
scanner **fails closed**: unreachable means the document stays quarantined.

For signatures, three rules make one evidence rather than decoration:

- **A signature is on a specific artifact.** The document's SHA-256 is pinned
  when the envelope is sent and re-checked before completion. Otherwise "they
  signed it" and "they signed *this*" are different claims and only the weaker
  one is provable.
- **Consent evidence is captured at the moment.** Typed name, IP, user agent,
  timestamp, and the wording shown. None of it can be reconstructed afterwards.
- **An envelope completes only when every signer has signed.** A lease executed
  on one of two required signatures is not executed.

A signer is authorised by being *named on the envelope* — a fact about the
envelope rather than a permission anyone can be granted — so their identity
comes from the signed-in account, never from the request.

> **What breaks:** see ADR-0008. A consent record nobody was shown is not
> consent to anything, which is why the portal signing page is part of the
> feature and not decoration on it.

---

## 10. Authorization

Deny by default. An unknown action fails closed rather than open.

Permissions are `<domain>.<action>` from a **closed vocabulary in code**: tenants
compose roles from it but cannot invent verbs the engine has never heard of.
Sensitive permissions force a fresh MFA assertion regardless of which role
grants them.

Scopes are organization, portfolio, and property, plus ownership predicates for
the portals. **A permission check is not enough on a portal route**: every
resident holds `payment.record`, so the question is never "may they pay?" but
"is this their invoice?" — which is why every portal POST re-derives what the
caller owns.

Separation of duties is enforced by identity, not by convention: the person who
enters a bill must not be the person who pays it, and `deposit.collect` is a
different grant from `deposit.release`.

---

## 11. Things that are not obvious

- **Idempotency is by watermark** for every scheduled job. A sweep that runs
  twice must not double-charge.
- **Dry run is structural.** Automation actions are split into `describe`
  (reads) and `apply` (writes); a dry run calls only `describe`. The guarantee
  holds because the mutating function is never called, not because a flag was
  checked.
- **Reachability is enforced.** `tests/unit/test_service_reachability.py` fails
  the build when a service module has no route, view, command, or job. The demo
  seed does not count. This exists because correct, tested, unreachable code has
  shipped here more than once.
- **`docs/FEATURES.md` is the authority on what exists.** It has a `No surface`
  status for capabilities that are implemented and unreachable, and it has to
  agree with the guard above.

---

## Where to look

| Question | File |
|---|---|
| How is the code arranged? | [ARCHITECTURE.md](ARCHITECTURE.md) |
| What exists, honestly? | [FEATURES.md](FEATURES.md) |
| Why was it built that way? | [adr/README.md](adr/README.md) |
| How do I run it in anger? | [../DEPLOYMENT.md](../DEPLOYMENT.md) |
| What do I do when it breaks? | [runbooks/](runbooks/README.md) |
| What is planned? | [../ROADMAP.md](../ROADMAP.md) |

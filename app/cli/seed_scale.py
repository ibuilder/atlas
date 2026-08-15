"""Production-shaped data, for measuring things that only break at volume.

This is not the demo seed with a bigger number passed to it. Two differences
matter, and both exist because of what a load test is *for*.

**Skew, not volume.** A uniformly generated database measures an index that
behaves nothing like production. Real portfolios are lopsided: a handful of
properties hold most of the units, a handful of leases carry most of the ledger
lines, and most rows are quiet. A query plan tuned against a flat distribution
falls over the first time it meets a tenant with four hundred invoices. So the
generator is deliberately unequal, and the shape is stated below rather than
left to emerge.

**Determinism.** The RNG is seeded from a fixed value, so two runs produce the
same database and two load results are comparable. A load test you cannot
re-run against the same data is a number, not a measurement.

Speed forces one compromise, taken openly. Bulk inserts bypass the service
layer, which is where the ledger's balance invariant and the audit chain live.
Rather than trust that the generator got them right, this command *verifies*
them afterwards with the same checks a disaster restore uses — the trial
balance must be zero per organization and every audit chain must walk clean.
If the generator is wrong, the seed fails rather than handing a load test a
database that quietly is not Atlas-shaped.

What it does not do: it does not make the data real. Every name is synthetic
and every amount is invented. It answers "how does this perform against this
shape", not "how does this perform against your portfolio".

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal
from typing import Any

import click
from flask.cli import AppGroup, with_appcontext

from app.context import system_context, use_context
from app.extensions import current_session, db
from app.models.types import utcnow, uuid7_str

__all__ = ["register_scale_commands"]

#: Fixed so two runs are comparable. Changing it changes the database, which is
#: a decision rather than an accident.
RNG_SEED = 20260101

#: The shape. Stated as constants so a reader can see what "production-shaped"
#: was taken to mean, and argue with it.
#:
#: A tenth of properties hold a third of the units. Most leases bill quietly
#: month after month; a few carry disputes, part-payments, and a long tail of
#: adjustments. That tail is where the slow queries live.
LARGE_PROPERTY_SHARE = 0.10
LARGE_PROPERTY_MULTIPLIER = 4
BUSY_LEASE_SHARE = 0.05
BUSY_LEASE_MULTIPLIER = 8

OCCUPANCY = 0.92

#: Batched so one transaction does not hold a hundred thousand pending objects.
BATCH = 2_000


def register_scale_commands(group: AppGroup) -> None:
    """Attach the scale command to the existing ``seed`` group."""
    group.add_command(seed_load)


@click.command("load")
# Added to the `seed` group after the fact rather than declared on it, so the
# app context Flask's own group decorator would have supplied has to be asked
# for explicitly.
@with_appcontext
@click.option("--slug", default="loadtest", help="Organization slug to create.")
@click.option("--properties", default=200, help="How many properties.")
@click.option("--units-per-property", default=40, help="Average units per property.")
@click.option("--months", default=24, help="Months of billing and ledger history.")
@click.option(
    "--verify/--no-verify",
    default=True,
    help="Prove the ledger balances and the audit chains walk. Leave this on.",
)
def seed_load(
    slug: str, properties: int, units_per_property: int, months: int, verify: bool
) -> None:
    """Generate a production-shaped organization for load testing.

    Deliberately a separate command from ``seed demo``. The demo is meant to be
    read by a person walking the product; this is meant to be unreadable and
    large, and conflating the two produces something that is bad at both.
    """
    from app.models.org import Organization

    existing = db.session.query(Organization).filter(Organization.slug == slug).one_or_none()
    if existing is not None:
        click.echo(f"Organization {slug!r} already exists. Drop it first, or use another slug.")
        raise SystemExit(1)

    rng = random.Random(RNG_SEED)  # noqa: S311 - shaping test data, not security
    started = utcnow()

    organization = _create_org(slug)
    with use_context(system_context("seed-load", org_id=organization.id)):
        counts = _generate(
            organization,
            rng=rng,
            property_count=properties,
            units_per_property=units_per_property,
            months=months,
        )
        db.session.commit()

        if verify:
            click.echo("")
            click.secho("  Verifying the invariants bulk insert bypassed...", bold=True)
            _verify(organization)

    elapsed = (utcnow() - started).total_seconds()
    click.echo("")
    click.secho(f"  Load organization ready in {elapsed:.0f}s.", bold=True)
    click.echo(f"  Organization : {organization.name} ({organization.slug})")
    for label, value in counts.items():
        click.echo(f"  {label:<13}: {value:,}")
    click.echo("")
    click.secho(
        "  Synthetic data. It answers 'how does this perform against this shape',",
        fg="yellow",
    )
    click.secho("  not 'how does this perform against your portfolio'.", fg="yellow")


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _create_org(slug: str):  # noqa: ANN202
    from app.models.org import OrganizationStatus
    from app.services.iam.provisioning import create_organization

    with use_context(system_context("seed-load")):
        organization = create_organization(
            current_session(),
            name="Load Test Portfolio",
            slug=slug,
            legal_name="Load Test Portfolio LLC",
            status=OrganizationStatus.ACTIVE,
            timezone="America/New_York",
            city="Brooklyn",
            region="NY",
            country="US",
        )
        db.session.commit()
    return organization


def _generate(
    organization,  # noqa: ANN001
    *,
    rng: random.Random,
    property_count: int,
    units_per_property: int,
    months: int,
) -> dict[str, int]:
    from app.services.accounting.chart import seed_chart_of_accounts

    accounts = seed_chart_of_accounts(current_session(), organization.id)
    db.session.commit()

    users = _users(organization)
    properties = _properties(organization, rng=rng, count=property_count)
    units = _units(organization, properties, rng=rng, average=units_per_property)
    residents, leases = _leases(organization, units, rng=rng, months=months)
    invoices, payments, entries, lines = _billing(
        organization, leases, accounts, rng=rng, months=months
    )
    work_orders = _work_orders(organization, properties, units, rng=rng, months=months)
    _portal_resident(organization, residents)
    # Scaled off write volume rather than off property count: audit events
    # track what happened, and a portfolio with two years of billing behind it
    # has an audit table shaped by the billing, not by the number of buildings.
    audit_events = _audit(organization, properties, rng=rng, count=invoices)

    return {
        "Users": len(users),
        "Properties": len(properties),
        "Units": len(units),
        "Residents": len(residents),
        "Leases": len(leases),
        "Invoices": invoices,
        "Payments": payments,
        "Journal": entries,
        "Ledger lines": lines,
        "Work orders": work_orders,
        "Audit events": audit_events,
    }


def _users(organization) -> list:  # noqa: ANN001, ANN201
    """The accounts the load profile signs in as.

    Same addresses and password as the demo seed, because the locust profile
    hard-codes them and two sets of credentials is one set nobody updates.
    """
    from app.cli.seed import DEMO_PASSWORD, DEMO_USERS
    from app.services.iam.provisioning import create_user

    created = []
    for email, name, role, _description in DEMO_USERS:
        created.append(
            create_user(
                current_session(),
                org_id=organization.id,
                email=email,
                full_name=name,
                password=DEMO_PASSWORD,
                role_codes=[role],
            )
        )
    db.session.commit()
    return created


def _properties(organization, *, rng: random.Random, count: int) -> list[dict[str, Any]]:  # noqa: ANN001
    """Properties, a tenth of them large enough to skew every list query."""
    from app.models.org import Property, PropertyType

    kinds = list(PropertyType)
    rows = []
    for index in range(count):
        rows.append(
            {
                "id": uuid7_str(),
                "org_id": organization.id,
                "code": f"P{index:05d}",
                # "Court" appears in a predictable share of names because the
                # load profile searches for it; a search that matches nothing
                # measures the planner's fast path and nothing else.
                "name": f"{_place(rng)} {'Court' if index % 3 == 0 else _suffix(rng)}",
                "property_type": rng.choice(kinds),
                "address_line1": f"{rng.randint(1, 400)} {_place(rng)} Road",
                "city": "Brooklyn",
                "region": "NY",
                "postal_code": f"11{rng.randint(200, 249)}",
                "country": "US",
                # max(1, ...) so the skew survives a small run. int(4 * 0.10)
                # is zero, which would quietly hand a smoke test a perfectly
                # flat portfolio — the one shape this generator exists to
                # avoid producing.
                "is_large": index < max(1, int(count * LARGE_PROPERTY_SHARE)),
            }
        )

    _insert(Property, [{k: v for k, v in row.items() if k != "is_large"} for row in rows])
    return rows


def _units(  # noqa: ANN201
    organization,  # noqa: ANN001
    properties: list[dict[str, Any]],
    *,
    rng: random.Random,
    average: int,
) -> list[dict[str, Any]]:
    from app.models.org import Unit, UnitStatus

    rows = []
    for property_row in properties:
        count = average * (LARGE_PROPERTY_MULTIPLIER if property_row["is_large"] else 1)
        count = max(1, int(rng.gauss(count, count * 0.2)))
        for number in range(count):
            rows.append(
                {
                    "id": uuid7_str(),
                    "org_id": organization.id,
                    "property_id": property_row["id"],
                    "unit_number": f"{number // 10 + 1}{chr(65 + number % 10)}",
                    "bedrooms": rng.choice([0, 1, 1, 2, 2, 2, 3]),
                    "market_rent": Decimal(rng.randrange(1400, 4200, 25)),
                    "status": UnitStatus.OCCUPIED,
                }
            )

    _insert(Unit, rows)
    return rows


def _leases(  # noqa: ANN201
    organization,  # noqa: ANN001
    units: list[dict[str, Any]],
    *,
    rng: random.Random,
    months: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from app.models.leasing import Lease, LeaseStatus
    from app.models.resident import Resident, ResidentStatus

    today = utcnow().date()
    residents: list[dict[str, Any]] = []
    leases: list[dict[str, Any]] = []

    for index, unit in enumerate(units):
        if rng.random() > OCCUPANCY:
            continue
        resident_id = uuid7_str()
        residents.append(
            {
                "id": resident_id,
                "org_id": organization.id,
                "first_name": _given(rng),
                "last_name": _family(rng),
                "email": f"resident{index:06d}@load.invalid",
                "status": ResidentStatus.CURRENT,
            }
        )
        start = today - dt.timedelta(days=rng.randint(30, months * 30))
        leases.append(
            {
                "id": uuid7_str(),
                "org_id": organization.id,
                "lease_number": f"LSE-L{index:06d}",
                "property_id": unit["property_id"],
                "unit_id": unit["id"],
                "status": LeaseStatus.ACTIVE,
                "start_date": start,
                "end_date": start + dt.timedelta(days=364),
                "rent_amount": unit["market_rent"],
                "security_deposit": unit["market_rent"],
                # The tail that costs money to query.
                "is_busy": rng.random() < BUSY_LEASE_SHARE,
            }
        )

    _insert(Resident, residents)
    _insert(Lease, [{k: v for k, v in row.items() if k != "is_busy"} for row in leases])
    return residents, leases


def _billing(  # noqa: ANN201
    organization,  # noqa: ANN001
    leases: list[dict[str, Any]],
    accounts: dict,
    *,
    rng: random.Random,
    months: int,
) -> tuple[int, int, int, int]:
    """Invoices, payments, and the balanced ledger behind them.

    Every journal entry is generated already balanced, and the whole set is
    checked at the end. Posting each one through the ledger service would be
    honest and would also take hours; generating them balanced and *proving* it
    is the same guarantee at a hundredth of the cost.
    """
    from app.models.accounting import (
        Invoice,
        InvoiceStatus,
        JournalEntry,
        JournalLine,
        Payment,
        PaymentMethod,
        PaymentStatus,
    )
    from app.services.accounting.chart import AccountCode

    rent_income = accounts[AccountCode.RENTAL_INCOME].id
    receivable = accounts[AccountCode.ACCOUNTS_RECEIVABLE].id
    cash = accounts[AccountCode.CASH_OPERATING].id

    today = utcnow().date()
    invoices: list[dict[str, Any]] = []
    payments: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    counter = 0

    for lease in leases:
        span = months * (BUSY_LEASE_MULTIPLIER if lease["is_busy"] else 1)
        span = min(span, months * BUSY_LEASE_MULTIPLIER)
        for month in range(span):
            issued = today - dt.timedelta(days=30 * (month + 1))
            if issued < lease["start_date"]:
                break
            counter += 1
            amount = Decimal(lease["rent_amount"])
            invoice_id = uuid7_str()

            # Most invoices are paid. The unpaid tail is what makes the
            # delinquency and receivables queries do real work.
            roll = rng.random()
            if roll < 0.86:
                status, balance, paid = InvoiceStatus.PAID, Decimal("0"), amount
            elif roll < 0.94:
                status, balance, paid = (
                    InvoiceStatus.PARTIALLY_PAID,
                    amount / 2,
                    amount / 2,
                )
            else:
                status, balance, paid = InvoiceStatus.OPEN, amount, Decimal("0")

            invoices.append(
                {
                    "id": invoice_id,
                    "org_id": organization.id,
                    "invoice_number": f"INV-L{counter:08d}",
                    "lease_id": lease["id"],
                    "property_id": lease["property_id"],
                    "issue_date": issued,
                    "due_date": issued + dt.timedelta(days=5),
                    "status": status,
                    "subtotal": amount,
                    "total": amount,
                    "amount_paid": paid,
                    "balance": balance,
                }
            )

            entry_id = uuid7_str()
            entries.append(
                {
                    "id": entry_id,
                    "org_id": organization.id,
                    "entry_number": f"JE-L{counter:08d}",
                    "entry_date": issued,
                    "description": "Rent billed",
                    "is_posted": True,
                    "posted_at": utcnow(),
                    "property_id": lease["property_id"],
                }
            )
            lines.extend(
                _balanced(
                    organization, entry_id, debit=receivable, credit=rent_income, amount=amount
                )
            )

            if paid > 0:
                payment_id = uuid7_str()
                payments.append(
                    {
                        "id": payment_id,
                        "org_id": organization.id,
                        "payment_number": f"PAY-L{counter:08d}",
                        "lease_id": lease["id"],
                        "received_date": issued + dt.timedelta(days=rng.randint(0, 12)),
                        "amount": paid,
                        "unapplied_amount": Decimal("0"),
                        "method": PaymentMethod.ACH,
                        "status": PaymentStatus.SETTLED,
                    }
                )
                paid_entry_id = uuid7_str()
                entries.append(
                    {
                        "id": paid_entry_id,
                        "org_id": organization.id,
                        "entry_number": f"JE-LP{counter:08d}",
                        "entry_date": issued + dt.timedelta(days=3),
                        "description": "Rent received",
                        "is_posted": True,
                        "posted_at": utcnow(),
                        "property_id": lease["property_id"],
                    }
                )
                lines.extend(
                    _balanced(
                        organization, paid_entry_id, debit=cash, credit=receivable, amount=paid
                    )
                )

    _insert(Invoice, invoices)
    _insert(Payment, payments)
    _insert(JournalEntry, entries)
    _insert(JournalLine, lines)
    return len(invoices), len(payments), len(entries), len(lines)


def _balanced(
    organization,  # noqa: ANN001
    entry_id: str,
    *,
    debit: str,
    credit: str,
    amount: Decimal,
) -> list[dict[str, Any]]:
    """One debit and one matching credit. The invariant, by construction."""
    return [
        {
            "id": uuid7_str(),
            "org_id": organization.id,
            "journal_entry_id": entry_id,
            "line_number": 1,
            "account_id": debit,
            "debit": amount,
            "credit": Decimal("0"),
        },
        {
            "id": uuid7_str(),
            "org_id": organization.id,
            "journal_entry_id": entry_id,
            "line_number": 2,
            "account_id": credit,
            "debit": Decimal("0"),
            "credit": amount,
        },
    ]


def _work_orders(  # noqa: ANN201
    organization,  # noqa: ANN001
    properties: list[dict[str, Any]],
    units: list[dict[str, Any]],
    *,
    rng: random.Random,
    months: int,
) -> int:
    from app.models.maintenance import Priority, WorkOrder, WorkOrderStatus

    statuses = list(WorkOrderStatus)
    rows = []
    for index in range(len(units) // 3):
        unit = rng.choice(units)
        raised = utcnow() - dt.timedelta(days=rng.randint(0, months * 30))
        rows.append(
            {
                "id": uuid7_str(),
                "org_id": organization.id,
                "work_order_number": f"WO-L{index:07d}",
                "property_id": unit["property_id"],
                "unit_id": unit["id"],
                "title": (
                    title := rng.choice(
                        [
                            "Leaking tap",
                            "Heating fault",
                            "Door lock",
                            "Window seal",
                            "Extractor noise",
                        ]
                    )
                ),
                "description": f"{title} reported by the resident.",
                "status": rng.choice(statuses),
                "priority": rng.choice(list(Priority)),
                # The queue sorts on this and the dashboard counts breaches
                # against it, so it has to be populated or both go free.
                "resolution_due_at": raised + dt.timedelta(days=rng.randint(1, 14)),
            }
        )

    _insert(WorkOrder, rows)
    return len(rows)


def _portal_resident(organization, residents: list[dict[str, Any]]) -> None:  # noqa: ANN001
    """The account the load profile's resident signs in as.

    The locust profile hard-codes ``resident@atlas.demo``. Without it here, its
    ResidentUser fails every request and the portal path — which re-derives
    ownership on every load, and is therefore one of the more expensive things
    measured — silently goes unmeasured.
    """
    from app.cli.seed import DEMO_PASSWORD
    from app.models.iam import UserType
    from app.services.iam.provisioning import create_user

    if not residents:
        return
    first = residents[0]
    create_user(
        current_session(),
        org_id=organization.id,
        email="resident@atlas.demo",
        full_name=f"{first['first_name']} {first['last_name']}",
        password=DEMO_PASSWORD,
        user_type=UserType.RESIDENT,
        resident_id=first["id"],
    )
    db.session.commit()


def _audit(  # noqa: ANN201
    organization,  # noqa: ANN001
    properties: list[dict[str, Any]],
    *,
    rng: random.Random,
    count: int,
) -> int:
    """A hash-linked audit chain, built in one pass rather than one call each.

    The chain is sequential by construction — each entry hashes the one before
    it — so it can be computed in a loop and inserted at once. It is then
    walked by ``verify_chain`` like any other, which is what makes taking the
    shortcut defensible.
    """
    from app.models.audit import (
        GENESIS_HASH,
        AuditAction,
        AuditEvent,
        AuditOutcome,
        AuditSeverity,
        compute_entry_hash,
    )
    from app.services.audit.recorder import _chain_head

    head = _chain_head(current_session(), organization.id)
    sequence = head.last_sequence
    previous = head.last_hash or GENESIS_HASH

    rows = []
    for index in range(count):
        sequence += 1
        occurred = utcnow() - dt.timedelta(minutes=rng.randint(0, 60 * 24 * 400))
        target = rng.choice(properties)
        action = AuditAction.PROPERTY_UPDATED
        payload = {"field": "name", "index": index}
        entry_hash = compute_entry_hash(
            previous_hash=previous,
            org_id=organization.id,
            sequence=sequence,
            action=action,
            resource_type="Property",
            resource_id=target["id"],
            resource_label=target["code"],
            actor_id=None,
            occurred_at=occurred,
            outcome=str(AuditOutcome.SUCCESS),
            severity=str(AuditSeverity.INFO),
            payload=payload,
            reason=None,
        )
        rows.append(
            {
                "id": uuid7_str(),
                "org_id": organization.id,
                "sequence": sequence,
                "occurred_at": occurred,
                "action": action,
                "severity": AuditSeverity.INFO,
                "outcome": AuditOutcome.SUCCESS,
                "resource_type": "Property",
                "resource_id": target["id"],
                "resource_label": target["code"],
                "actor_type": "system",
                "source": "seed-load",
                "payload": payload,
                "previous_hash": previous,
                "entry_hash": entry_hash,
            }
        )
        previous = entry_hash

    _insert(AuditEvent, rows)
    head.last_sequence = sequence
    head.last_hash = previous
    db.session.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Proving the shortcut was safe
# ---------------------------------------------------------------------------


def _verify(organization) -> None:  # noqa: ANN001
    """The same checks a disaster restore runs, for the same reason.

    Bulk insert went around the service layer, so the invariants the service
    layer enforces are unproven until something walks them. Row counts look
    right in every failure mode this catches, which is exactly why row counts
    are not one of the checks.
    """
    from sqlalchemy import func, select

    from app.models.accounting import JournalLine
    from app.services.audit.recorder import verify_chain

    debits, credits = db.session.execute(
        select(
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        ).where(JournalLine.org_id == organization.id)
    ).one()
    if Decimal(str(debits)) != Decimal(str(credits)):
        raise click.ClickException(
            f"The generated ledger does not balance: {debits} debits against {credits} "
            "credits. The seed is wrong, and a load test against it would be measuring "
            "something that is not Atlas."
        )
    click.echo(f"    Ledger balances: {debits} debits, {credits} credits.")

    result = verify_chain(current_session(), org_id=organization.id)
    if not result.get("intact", False):
        raise click.ClickException(f"The generated audit chain does not verify: {result}")
    click.echo(f"    Audit chain intact across {result['events_checked']:,} events.")


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _insert(model: Any, rows: list[dict[str, Any]]) -> None:
    """Batched core insert.

    The ORM would give every row an identity map entry and a flush listener
    pass; at this volume that is the difference between minutes and hours.
    """
    if not rows:
        return
    for start in range(0, len(rows), BATCH):
        db.session.execute(model.__table__.insert(), rows[start : start + BATCH])
    db.session.flush()


_PLACES = (
    "Harrow",
    "Kestrel",
    "Marlow",
    "Brackenford",
    "Elmsworth",
    "Whitcombe",
    "Ashgate",
    "Redhaven",
    "Norbury",
    "Thornfield",
    "Larkspur",
    "Winterbourne",
)
_SUFFIXES = ("House", "Place", "Terrace", "Gardens", "Mews", "Rise", "Yard")
_GIVEN = (
    "Dana",
    "Rosa",
    "Amir",
    "Jordan",
    "Priya",
    "Sam",
    "Alex",
    "Chris",
    "Rowan",
    "Nia",
    "Tomas",
    "Beatriz",
    "Kwame",
    "Ingrid",
    "Hassan",
    "Mei",
)
_FAMILY = (
    "Okonkwo",
    "Villanueva",
    "Haddad",
    "Pike",
    "Raman",
    "Okafor",
    "Moreau",
    "Nakamura",
    "Ellis",
    "Vale",
    "Whitfield",
    "Sandoval",
    "Bergstrom",
    "Ferreira",
)


def _place(rng: random.Random) -> str:
    return rng.choice(_PLACES)


def _suffix(rng: random.Random) -> str:
    return rng.choice(_SUFFIXES)


def _given(rng: random.Random) -> str:
    return rng.choice(_GIVEN)


def _family(rng: random.Random) -> str:
    return rng.choice(_FAMILY)

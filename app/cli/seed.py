"""Demo data.

Builds a small but *complete* management company: properties with units,
residents on leases, invoices with payments applied, maintenance running through
to completed work orders, a vendor with live insurance, and a signed-in user for
every role so each portal can be walked from one seed.

:mod:`app.cli.seed_operations` then layers on everything built after the core -
a space hierarchy with equipment in it, an automation rule that has served its
dry run, a completed reconciliation, an inspection that raised work, owner
statements across a mid-period transfer. Split in two because the first half is
the company and the second half is how it is run, and the two read better apart.

Deliberately a CLI command rather than application bootstrap. Seed data that
loads itself on startup ends up in production exactly once, and that once is
enough.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import click
from flask.cli import AppGroup

from app.context import system_context, use_context
from app.extensions import current_session, db

seed_cli = AppGroup("seed", help="Seed data for demos and local development.")

__all__ = ["seed_cli"]

DEMO_PASSWORD = "atlas-demo-2026-portfolio"

DEMO_USERS: tuple[tuple[str, str, str, str], ...] = (
    ("admin@atlas.demo", "Rowan Ellis", "org_admin", "Organization administrator"),
    ("controller@atlas.demo", "Priya Raman", "controller", "Controller"),
    ("accountant@atlas.demo", "Sam Okafor", "accountant", "Accountant"),
    ("manager@atlas.demo", "Dana Whitfield", "property_manager", "Property manager"),
    ("leasing@atlas.demo", "Alex Moreau", "leasing_agent", "Leasing agent"),
    ("dispatch@atlas.demo", "Chris Nakamura", "maintenance_dispatcher", "Maintenance dispatcher"),
    ("auditor@atlas.demo", "Jordan Vale", "auditor", "Auditor (read-only)"),
)


@seed_cli.command("demo")
@click.option("--slug", default="northlight", help="Organization slug to create.")
@click.option("--force", is_flag=True, help="Re-seed even if the organization exists.")
def seed_demo(slug: str, force: bool) -> None:
    """Create a fully populated demo organization."""
    from app.models.org import Organization

    existing = db.session.query(Organization).filter(Organization.slug == slug).one_or_none()
    if existing is not None and not force:
        click.echo(f"Organization {slug!r} already exists. Use --force to add more data.")
        _print_credentials(slug)
        return

    organization = existing or _create_org(slug)

    with use_context(system_context("seed", org_id=organization.id)):
        accounts = _seed_accounts(organization)
        users = _seed_users(organization)
        properties, units = _seed_portfolio(organization)
        vendor = _seed_vendor(organization)
        residents, leases = _seed_leases(organization, units)
        _seed_financials(organization, leases, accounts)
        _seed_maintenance(organization, properties, units, leases, vendor, users)
        db.session.commit()

        # Everything built after the core: spaces and equipment, preventive
        # schedules, an inspection, automation, banking, ownership, reporting.
        from app.cli.seed_operations import seed_operations

        operations = seed_operations(
            organization, properties, units, leases, vendor, users, accounts
        )
        db.session.commit()

    click.echo("")
    click.secho("  Atlas demo ready.", bold=True)
    click.echo(f"  Organization : {organization.name} ({organization.slug})")
    click.echo(f"  Properties   : {len(properties)}")
    click.echo(f"  Units        : {len(units)}")
    click.echo(f"  Residents    : {len(residents)}")
    click.echo(f"  Leases       : {len(leases)}")
    click.echo("")
    click.echo(f"  Spaces       : {operations['spaces']} (nested hierarchy on Harrow Court)")
    click.echo(f"  Assets       : {operations['assets']} with warranty and service history")
    click.echo(f"  PM schedules : {operations['pm_schedules']} (one seasonal)")
    click.echo(f"  Inspections  : {operations['inspections']} completed, work raised from findings")
    click.echo(
        f"  Automation   : {operations['automation_rules']} rules (one live, one in dry run)"
    )
    click.echo(f"  Reconciled   : {operations['reconciliations']} bank statement period")
    click.echo(
        f"  Statements   : {operations['owner_statements']} owner statements across a transfer"
    )
    click.echo(f"  Reports      : {operations['scheduled_reports']} scheduled")
    click.echo(
        f"  SSO          : {operations['identity_providers']} provider configured (inactive)"
    )
    click.echo(f"  KPI history  : {operations['kpi_points']} snapshots over 15 days")
    click.echo(
        f"  Messages     : {operations['message_threads']} threads (one internal, "
        "invisible in the portal)"
    )
    click.echo(f"  Turns        : {operations['turns']} (one finished, one running late)")
    click.echo(
        f"  E-signature  : {operations['envelopes']} envelopes - one executed, "
        "one waiting in the resident portal"
    )
    click.echo(
        f"  Applications : {operations['applications']} "
        "(one awaiting a decision, one conditional, one denied)"
    )
    _print_credentials(slug)


def _print_credentials(slug: str) -> None:
    click.echo("")
    click.secho("  Sign in at http://localhost:5000/auth/login", bold=True)
    click.echo(f"  Password for every demo account: {DEMO_PASSWORD}")
    click.echo("")
    for email, name, _role, description in DEMO_USERS:
        click.echo(f"    {email:<28} {description} ({name})")
    click.echo(f"    {'resident@atlas.demo':<28} Resident portal")
    click.echo(f"    {'owner@atlas.demo':<28} Owner portal")
    click.echo(f"    {'vendor@atlas.demo':<28} Vendor portal")
    click.echo("")
    click.secho(
        "  Demo credentials only. Never provision these in a real environment.", fg="yellow"
    )


def _create_org(slug: str):  # noqa: ANN202
    from app.models.org import OrganizationStatus
    from app.services.iam.provisioning import create_organization

    with use_context(system_context("seed")):
        organization = create_organization(
            current_session(),
            name="Northlight Property Group",
            slug=slug,
            legal_name="Northlight Property Group LLC",
            status=OrganizationStatus.ACTIVE,
            timezone="America/New_York",
            city="Brooklyn",
            region="NY",
            country="US",
        )
        db.session.commit()
    return organization


def _seed_accounts(organization):  # noqa: ANN001, ANN202
    from app.services.accounting.chart import seed_chart_of_accounts

    accounts = seed_chart_of_accounts(current_session(), organization.id)
    db.session.flush()
    return accounts


def _seed_users(organization):  # noqa: ANN001, ANN202
    from app.services.iam.provisioning import create_user

    created = {}
    for email, name, role, _description in DEMO_USERS:
        if db.session.query(_user_model()).filter_by(email=email).first():
            continue
        created[role] = create_user(
            current_session(),
            org_id=organization.id,
            email=email,
            full_name=name,
            password=DEMO_PASSWORD,
            role_codes=[role],
        )
    db.session.flush()
    return created


def _user_model():  # noqa: ANN202
    from app.models.iam import User

    return User


def _seed_portfolio(organization):  # noqa: ANN001, ANN202
    from app.models.org import (
        Portfolio,
        Property,
        PropertyType,
        Unit,
        UnitStatus,
    )

    portfolio = Portfolio(
        org_id=organization.id,
        name="Brooklyn Core",
        code="BKC",
        description="Stabilised multifamily across Kings County.",
    )
    db.session.add(portfolio)
    db.session.flush()

    blueprints = [
        ("HAR", "Harrow Court", "112 Harrow Street", "Brooklyn", "NY", "11216", 1928, 12),
        ("MER", "Meridian Row", "48 Meridian Avenue", "Brooklyn", "NY", "11221", 1964, 8),
        ("KLN", "Kiln Yard Lofts", "9 Kiln Yard", "Brooklyn", "NY", "11237", 2004, 6),
    ]

    properties = []
    units = []
    for code, name, line1, city, region, postal, year, unit_count in blueprints:
        record = Property(
            org_id=organization.id,
            portfolio_id=portfolio.id,
            code=code,
            name=name,
            property_type=PropertyType.RESIDENTIAL_MULTI,
            address_line1=line1,
            city=city,
            region=region,
            postal_code=postal,
            year_built=year,
            total_units=unit_count,
        )
        db.session.add(record)
        db.session.flush()
        properties.append(record)

        for index in range(unit_count):
            floor = index // 2 + 1
            bedrooms = 1 + (index % 3)
            unit = Unit(
                org_id=organization.id,
                property_id=record.id,
                unit_number=f"{floor}{'ABCDEF'[index % 2]}",
                floor=floor,
                bedrooms=bedrooms,
                bathrooms=Decimal("1.0") if bedrooms < 3 else Decimal("2.0"),
                square_feet=520 + bedrooms * 220,
                market_rent=Decimal(2200 + bedrooms * 450),
                deposit_amount=Decimal(2200 + bedrooms * 450),
                status=UnitStatus.VACANT_READY,
            )
            db.session.add(unit)
            units.append(unit)

    db.session.flush()
    return properties, units


def _seed_vendor(organization):  # noqa: ANN001, ANN202
    from app.models.vendor import (
        ComplianceKind,
        ComplianceStatus,
        Vendor,
        VendorCompliance,
        VendorStatus,
        VendorTrade,
    )

    vendor = Vendor(
        org_id=organization.id,
        code="APEX",
        name="Apex Mechanical",
        vendor_type="contractor",
        status=VendorStatus.ACTIVE,
        email="dispatch@apexmech.demo",
        phone="+1-718-555-0142",
        accepts_emergency=True,
        is_preferred=True,
        hourly_rate=Decimal("135.00"),
        compliance_status=ComplianceStatus.VALID,
        compliance_expires_at=dt.date.today() + dt.timedelta(days=210),
    )
    db.session.add(vendor)
    db.session.flush()

    db.session.add_all(
        [
            VendorTrade(org_id=organization.id, vendor_id=vendor.id, trade="hvac", is_primary=True),
            VendorTrade(org_id=organization.id, vendor_id=vendor.id, trade="plumbing"),
            VendorCompliance(
                org_id=organization.id,
                vendor_id=vendor.id,
                kind=ComplianceKind.CERTIFICATE_OF_INSURANCE,
                status=ComplianceStatus.VALID,
                carrier_name="Harbour Mutual",
                policy_number="HM-4471-B",
                coverage_amount=Decimal("2000000"),
                issued_on=dt.date.today() - dt.timedelta(days=150),
                expires_at=dt.date.today() + dt.timedelta(days=210),
            ),
        ]
    )
    db.session.flush()
    return vendor


def _seed_leases(organization, units):  # noqa: ANN001, ANN202
    from app.models.leasing import Lease, LeaseStatus
    from app.models.org import UnitStatus
    from app.models.resident import Resident, ResidentStatus, Tenancy, TenancyRole
    from app.models.sequences import SequenceKey
    from app.services.common.numbering import next_number

    names = [
        ("Imani", "Brooks"),
        ("Tomas", "Lindqvist"),
        ("Grace", "Oyelaran"),
        ("Wei", "Zhang"),
        ("Nadia", "Haddad"),
        ("Ruth", "Kowalski"),
        ("Ezra", "Mbeki"),
        ("Sofia", "Duarte"),
        ("Kai", "Andersen"),
        ("Lucia", "Ferrari"),
        ("Omar", "Haddadi"),
        ("Nina", "Petrova"),
    ]

    today = dt.date.today()
    residents = []
    leases = []

    # Occupy roughly three quarters of the portfolio, so occupancy is a real
    # number rather than a suspiciously perfect one.
    for index, unit in enumerate(units[: int(len(units) * 0.75)]):
        first, last = names[index % len(names)]
        resident = Resident(
            org_id=organization.id,
            first_name=first,
            last_name=f"{last}",
            email=f"{first.lower()}.{last.lower()}{index}@resident.demo",
            phone=f"+1-718-555-{2000 + index:04d}",
            status=ResidentStatus.CURRENT,
        )
        db.session.add(resident)
        db.session.flush()
        residents.append(resident)

        start = today.replace(day=1) - dt.timedelta(days=30 * (index % 10))
        lease = Lease(
            org_id=organization.id,
            lease_number=next_number(current_session(), SequenceKey.LEASE, org_id=organization.id),
            property_id=unit.property_id,
            unit_id=unit.id,
            status=LeaseStatus.ACTIVE,
            start_date=start,
            end_date=start + dt.timedelta(days=364),
            move_in_date=start,
            rent_amount=unit.market_rent or Decimal("2400"),
            security_deposit=unit.deposit_amount or Decimal("2400"),
            # ``deposit_held`` is deliberately not set here. It is maintained
            # by the deposit subledger, and seeding it directly is what let the
            # trust reconciliation look populated while nothing filled it.
            billing_day=1,
        )
        db.session.add(lease)
        db.session.flush()
        leases.append(lease)

        db.session.add(
            Tenancy(
                org_id=organization.id,
                lease_id=lease.id,
                resident_id=resident.id,
                role=TenancyRole.PRIMARY,
                started_at=start,
            )
        )
        unit.status = UnitStatus.OCCUPIED

    db.session.flush()
    _seed_portal_users(organization, residents)
    return residents, leases


def _seed_portal_users(organization, residents):  # noqa: ANN001
    """One signed-in account per portal, so all four surfaces are walkable."""
    from app.models.iam import User, UserType
    from app.models.org import OwnerEntity, OwnershipStake, OwnerType, Property
    from app.models.vendor import Vendor
    from app.services.iam.provisioning import create_user

    if not db.session.query(User).filter_by(email="resident@atlas.demo").first() and residents:
        create_user(
            current_session(),
            org_id=organization.id,
            email="resident@atlas.demo",
            full_name=residents[0].full_name,
            password=DEMO_PASSWORD,
            user_type=UserType.RESIDENT,
            resident_id=residents[0].id,
        )

    owner = db.session.query(OwnerEntity).filter_by(org_id=organization.id).first()
    if owner is None:
        owner = OwnerEntity(
            org_id=organization.id,
            code="NLC",
            name="Northlight Capital Partners",
            owner_type=OwnerType.PARTNERSHIP,
            email="owner@atlas.demo",
            is_1099_required=True,
        )
        db.session.add(owner)
        db.session.flush()

        for record in db.session.query(Property).filter_by(org_id=organization.id):
            db.session.add(
                OwnershipStake(
                    org_id=organization.id,
                    property_id=record.id,
                    owner_entity_id=owner.id,
                    percentage=Decimal("100.0000"),
                    effective_from=dt.date.today() - dt.timedelta(days=900),
                    is_primary_contact=True,
                )
            )
        db.session.flush()

    if not db.session.query(User).filter_by(email="owner@atlas.demo").first():
        create_user(
            current_session(),
            org_id=organization.id,
            email="owner@atlas.demo",
            full_name="Northlight Capital Partners",
            password=DEMO_PASSWORD,
            user_type=UserType.OWNER,
            owner_entity_id=owner.id,
        )

    vendor = db.session.query(Vendor).filter_by(org_id=organization.id).first()
    if vendor and not db.session.query(User).filter_by(email="vendor@atlas.demo").first():
        create_user(
            current_session(),
            org_id=organization.id,
            email="vendor@atlas.demo",
            full_name="Apex Mechanical Dispatch",
            password=DEMO_PASSWORD,
            user_type=UserType.VENDOR,
            vendor_id=vendor.id,
        )

    db.session.flush()


def _seed_financials(organization, leases, accounts):  # noqa: ANN001
    """Three months of rent, mostly paid, some deliberately overdue."""
    from app.models.accounting import PaymentMethod
    from app.services.accounting.chart import AccountCode
    from app.services.accounting.receivables import ChargeInput, issue_invoice, record_payment

    rent_account = accounts[AccountCode.RENTAL_INCOME]
    today = dt.date.today()

    for index, lease in enumerate(leases):
        for month_offset in (2, 1, 0):
            issue_date = _month_start(today, -month_offset)
            invoice = issue_invoice(
                current_session(),
                org_id=organization.id,
                charges=[
                    ChargeInput(
                        description=f"Rent - {issue_date.strftime('%B %Y')}",
                        amount=lease.rent_amount,
                        account_id=rent_account.id,
                        service_period_start=issue_date,
                        service_period_end=_month_end(issue_date),
                    )
                ],
                issue_date=issue_date,
                due_date=issue_date,
                lease=lease,
                property_id=lease.property_id,
                unit_id=lease.unit_id,
                period_start=issue_date,
                period_end=_month_end(issue_date),
            )

            # Every fifth resident falls behind on the current month, so the
            # delinquency figures on the dashboard are non-trivial.
            is_delinquent = index % 5 == 0 and month_offset == 0
            if not is_delinquent:
                record_payment(
                    current_session(),
                    org_id=organization.id,
                    amount=invoice.total,
                    method=PaymentMethod.ACH,
                    received_date=issue_date + dt.timedelta(days=2),
                    lease_id=lease.id,
                    property_id=lease.property_id,
                    reference=f"ACH-{invoice.invoice_number}",
                )

    db.session.flush()


def _seed_maintenance(organization, properties, units, leases, vendor, users):  # noqa: ANN001
    """A spread of maintenance work, including one habitability emergency."""
    from app.models.maintenance import Priority, WorkOrderStatus
    from app.services.maintenance.service import (
        create_request,
        create_work_order,
        transition_work_order,
    )

    scenarios = [
        (
            "No hot water in the bathroom",
            "Hot water stopped overnight; the boiler is not firing.",
            "plumbing",
            Priority.NORMAL,
        ),
        ("Kitchen tap dripping", "Constant drip from the mixer tap.", "plumbing", Priority.LOW),
        (
            "Hallway light out",
            "The second floor hallway light has failed.",
            "electrical",
            Priority.NORMAL,
        ),
        (
            "Heating not reaching the bedroom",
            "Radiator stays cold while the rest of the flat heats.",
            "hvac",
            Priority.HIGH,
        ),
        (
            "Front door lock sticking",
            "The main entry lock jams intermittently.",
            "general",
            Priority.HIGH,
        ),
    ]

    for index, (title, description, trade, priority) in enumerate(scenarios):
        lease = leases[index % len(leases)] if leases else None
        request = create_request(
            current_session(),
            org_id=organization.id,
            property_id=lease.property_id if lease else properties[0].id,
            unit_id=lease.unit_id if lease else None,
            lease_id=lease.id if lease else None,
            title=title,
            description=description,
            category=trade,
            priority=priority,
            permission_to_enter=index % 2 == 0,
            source="portal",
        )

        work_order = create_work_order(
            current_session(),
            org_id=organization.id,
            property_id=request.property_id,
            title=title,
            description=description,
            request=request,
            unit_id=request.unit_id,
            trade=trade,
            priority=request.effective_priority(),
        )

        # Walk each one a different distance down the lifecycle so the queue
        # shows a realistic spread rather than a wall of identical rows.
        transition_work_order(
            current_session(),
            work_order=work_order,
            target=WorkOrderStatus.ASSIGNED,
            vendor_id=vendor.id,
            actor_label="Chris Nakamura",
            note="Dispatched to Apex Mechanical.",
        )
        if index % 3 != 2:
            transition_work_order(
                current_session(),
                work_order=work_order,
                target=WorkOrderStatus.IN_PROGRESS,
                actor_label="Apex Mechanical",
                note="Technician on site.",
            )
        if index % 3 == 0:
            transition_work_order(
                current_session(),
                work_order=work_order,
                target=WorkOrderStatus.COMPLETED,
                actor_label="Apex Mechanical",
                labor_hours=Decimal("2.5"),
                labor_cost=Decimal("337.50"),
                material_cost=Decimal("84.20"),
                resolution_notes="Replaced the thermocouple and verified operation.",
                resident_visible=True,
            )

    db.session.flush()


def _month_start(reference: dt.date, offset_months: int) -> dt.date:
    total = reference.month - 1 + offset_months
    year = reference.year + total // 12
    month = total % 12 + 1
    return dt.date(year, month, 1)


def _month_end(start: dt.date) -> dt.date:
    if start.month == 12:
        return dt.date(start.year, 12, 31)
    return dt.date(start.year, start.month + 1, 1) - dt.timedelta(days=1)

"""Demo data for everything built after the core: 0.2 through 0.5.

The original seed builds a management company. This builds the *operations* on
top of it - the parts that only mean anything once there is a portfolio to run:
a space hierarchy with equipment in it, an automation rule that has served its
dry run, a reconciliation that actually balances, an inspection that raised a
work order, an owner statement for a period where ownership changed.

Two rules govern everything here.

**The demo must stay honest.** Every journal entry it posts is balanced, so the
trial balance still agrees afterwards; every audit event goes through the
recorder, so the chain still verifies. A demo that has to be excluded from the
integrity checks is a demo of a different system.

**Nothing is faked past the interface.** The automation rule is promoted the
way a real one is - dry run first, then promotion - rather than by setting a
flag. The reconciliation is completed through the service, so it is subject to
the same refusals. What you see in the demo is what the code does.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import click

from app.extensions import db
from app.models.types import utcnow

__all__ = ["seed_operations"]

#: UTC, matching the services this seeds through. A local date near the UTC
#: rollover would hand them a "future" service date and be refused.
TODAY = utcnow().date()


def seed_operations(
    organization, properties, units, leases, vendor, users, accounts
):  # noqa: ANN001, ANN201
    """Layer the 0.2-0.5 features onto an existing demo organization."""
    summary: dict[str, int] = {}

    spaces = _seed_spaces(organization, properties[0], units)
    summary["spaces"] = len(spaces)

    assets = _seed_assets(organization, properties[0], spaces)
    summary["assets"] = len(assets)

    summary["pm_schedules"] = _seed_preventive(organization, properties, assets)
    summary["inspections"] = _seed_inspection(organization, properties[0], units, leases)
    summary["automation_rules"] = _seed_automation(organization, properties[0])
    summary["reconciliations"] = _seed_reconciliation(organization, accounts)
    summary["owner_statements"] = _seed_ownership(organization, properties[0], accounts)
    summary["scheduled_reports"] = _seed_reports(organization, users)
    summary["identity_providers"] = _seed_sso(organization)
    summary["kpi_points"] = _seed_projections(organization)

    db.session.flush()
    return summary


# ---------------------------------------------------------------------------
# Spaces and equipment
# ---------------------------------------------------------------------------


def _seed_spaces(organization, prop, units):  # noqa: ANN001, ANN202
    """Site / level / unit / room, plus the riser that serves them."""
    from app.models.asset_graph import SpaceKind
    from app.services.assets.spaces import create_space

    site = create_space(
        db.session,
        org_id=organization.id,
        property_id=prop.id,
        code="HAR-SITE",
        name="Harrow Court",
        kind=SpaceKind.COMMON_AREA,
    )
    riser = create_space(
        db.session,
        org_id=organization.id,
        property_id=prop.id,
        code="HAR-RISER-A",
        name="Riser A",
        kind=SpaceKind.MECHANICAL,
        parent=site,
    )
    plant = create_space(
        db.session,
        org_id=organization.id,
        property_id=prop.id,
        code="HAR-PLANT",
        name="Boiler room",
        kind=SpaceKind.MECHANICAL,
        parent=site,
        level=0,
    )

    created = [site, riser, plant]
    for level_number in (1, 2):
        level = create_space(
            db.session,
            org_id=organization.id,
            property_id=prop.id,
            code=f"HAR-L{level_number}",
            name=f"Level {level_number}",
            kind=SpaceKind.CIRCULATION,
            parent=site,
            level=level_number,
        )
        created.append(level)

        for unit in [u for u in units if u.property_id == prop.id and u.floor == level_number]:
            flat = create_space(
                db.session,
                org_id=organization.id,
                property_id=prop.id,
                code=f"HAR-{unit.unit_number}",
                name=f"Flat {unit.unit_number}",
                kind=SpaceKind.ROOM,
                parent=level,
                unit_id=unit.id,
                area_sqft=Decimal(str(unit.square_feet or 700)),
            )
            kitchen = create_space(
                db.session,
                org_id=organization.id,
                property_id=prop.id,
                code=f"HAR-{unit.unit_number}-K",
                name="Kitchen",
                kind=SpaceKind.ROOM,
                parent=flat,
                unit_id=unit.id,
                area_sqft=Decimal("110.00"),
            )
            created.extend([flat, kitchen])

    db.session.flush()
    return created


def _seed_assets(organization, prop, spaces):  # noqa: ANN001, ANN202
    """A boiler worth replacing, a lift worth watching, and extractor fans."""
    from app.models.asset_graph import (
        Asset,
        AssetCategory,
        AssetCriticality,
        ServiceEventType,
        Warranty,
    )
    from app.services.assets.lifecycle import record_service

    plant = next(space for space in spaces if space.code == "HAR-PLANT")
    kitchens = [space for space in spaces if space.code.endswith("-K")]

    boiler = Asset(
        org_id=organization.id,
        code="HAR-BOIL-01",
        name="Main boiler",
        category=AssetCategory.HVAC,
        criticality=AssetCriticality.CRITICAL,
        property_id=prop.id,
        space_id=plant.id,
        manufacturer="Vaillant",
        model_number="ecoTEC 938",
        serial_number="VL-938-114772",
        installed_on=TODAY - dt.timedelta(days=365 * 13),
        expected_life_years=15,
        purchase_price=Decimal("8400.00"),
        replacement_cost=Decimal("11500.00"),
    )
    lift = Asset(
        org_id=organization.id,
        code="HAR-LIFT-01",
        name="Passenger lift",
        category=AssetCategory.ELEVATOR,
        criticality=AssetCriticality.HIGH,
        property_id=prop.id,
        installed_on=TODAY - dt.timedelta(days=365 * 6),
        expected_life_years=25,
        purchase_price=Decimal("62000.00"),
        replacement_cost=Decimal("78000.00"),
        condition_score=4,
    )
    db.session.add_all([boiler, lift])
    db.session.flush()

    # The lift is still covered; the boiler's cover lapsed years ago, which is
    # what makes its repair history expensive.
    db.session.add(
        Warranty(
            org_id=organization.id,
            asset_id=lift.id,
            provider="Kone Service",
            policy_number="KS-2026-8841",
            kind="service_contract",
            starts_on=TODAY - dt.timedelta(days=200),
            expires_on=TODAY + dt.timedelta(days=520),
            covers_parts=True,
            covers_labor=True,
            claim_phone="+1-555-0140",
        )
    )
    db.session.flush()

    assets = [boiler, lift]
    for index, kitchen in enumerate(kitchens[:6]):
        fan = Asset(
            org_id=organization.id,
            code=f"HAR-FAN-{index + 1:02d}",
            name="Kitchen extractor",
            category=AssetCategory.APPLIANCE,
            criticality=AssetCriticality.LOW,
            property_id=prop.id,
            space_id=kitchen.id,
            unit_id=kitchen.unit_id,
            installed_on=TODAY - dt.timedelta(days=365 * 4),
            expected_life_years=10,
            replacement_cost=Decimal("340.00"),
        )
        db.session.add(fan)
        assets.append(fan)
    db.session.flush()

    # Four call-outs in a year against an eleven-and-a-half-thousand-pound
    # replacement: the repair-or-replace advice has something real to say.
    for months_ago, cost in ((11, "1450.00"), (7, "980.00"), (4, "1620.00"), (1, "2100.00")):
        record_service(
            db.session,
            asset=boiler,
            event_type=ServiceEventType.REPAIR,
            performed_on=TODAY - dt.timedelta(days=30 * months_ago),
            cost=Decimal(cost),
            notes="Heat exchanger fault; parts and labour.",
            condition_after=2,
        )
    record_service(
        db.session,
        asset=lift,
        event_type=ServiceEventType.PREVENTIVE,
        performed_on=TODAY - dt.timedelta(days=45),
        cost=Decimal("0.00"),
        notes="Quarterly service under contract.",
        condition_after=4,
    )

    db.session.flush()
    return assets


def _seed_preventive(organization, properties, assets) -> int:  # noqa: ANN001
    """Schedules that will generate work, including a seasonal one."""
    from app.models.maintenance import PreventiveMaintenanceSchedule, Priority

    boiler = next(asset for asset in assets if asset.code == "HAR-BOIL-01")
    schedules = [
        PreventiveMaintenanceSchedule(
            org_id=organization.id,
            name="Annual boiler service",
            description="Statutory annual service and safety check.",
            property_id=boiler.property_id,
            asset_id=boiler.id,
            trade="hvac",
            priority=Priority.HIGH,
            interval_unit="month",
            interval_value=12,
            next_due_on=TODAY + dt.timedelta(days=20),
            lead_time_days=30,
            estimated_cost=Decimal("420.00"),
        ),
        PreventiveMaintenanceSchedule(
            org_id=organization.id,
            name="Gutter clearance",
            description="Autumn gutter and downpipe clearance.",
            property_id=properties[0].id,
            trade="grounds",
            priority=Priority.LOW,
            interval_unit="month",
            interval_value=6,
            # Restricted to autumn, so out of season it defers rather than fires.
            active_months=[10, 11],
            next_due_on=TODAY,
            lead_time_days=14,
            estimated_cost=Decimal("260.00"),
        ),
    ]
    db.session.add_all(schedules)
    db.session.flush()
    return len(schedules)


# ---------------------------------------------------------------------------
# Inspections
# ---------------------------------------------------------------------------


def _seed_inspection(organization, prop, units, leases) -> int:  # noqa: ANN001
    """A completed move-out inspection that raised real work."""
    from app.models.maintenance import InspectionKind, InspectionTemplate, ItemResult
    from app.services.maintenance.inspections import (
        ItemFinding,
        complete_inspection,
        raise_work_orders_from_findings,
        record_finding,
        schedule_inspection,
        start_inspection,
    )

    template = InspectionTemplate(
        org_id=organization.id,
        code="MOVE_OUT",
        name="Move-out condition report",
        kind=InspectionKind.MOVE_OUT,
        version=1,
        sections=[
            {
                "section": "Kitchen",
                "items": [{"name": "Worktops"}, {"name": "Extractor"}],
            },
            {
                "section": "Living",
                "items": [{"name": "Flooring"}, {"name": "Windows"}],
            },
            {"section": "Safety", "items": [{"name": "Smoke alarm"}]},
        ],
    )
    db.session.add(template)
    db.session.flush()

    lease = next((lease for lease in leases if lease.property_id == prop.id), None)
    inspection = schedule_inspection(
        db.session,
        org_id=organization.id,
        kind=InspectionKind.MOVE_OUT,
        property_id=prop.id,
        unit_id=lease.unit_id if lease else units[0].id,
        lease_id=lease.id if lease else None,
        template=template,
        scheduled_for=None,
    )
    start_inspection(db.session, inspection=inspection)

    # One failure with a cost, so the deposit deduction has evidence behind it.
    findings = {
        "Worktops": (ItemResult.PASS, None, None),
        "Extractor": (ItemResult.PASS, None, None),
        "Flooring": (ItemResult.FAIL, "medium", Decimal("380.00")),
        "Windows": (ItemResult.PASS, None, None),
        "Smoke alarm": (ItemResult.PASS, None, None),
    }
    for item in inspection.items:
        result, severity, cost = findings[item.name]
        record_finding(
            db.session,
            inspection=inspection,
            finding=ItemFinding(
                item_id=item.id,
                result=result,
                severity=severity,
                notes="Scorch damage beyond fair wear." if cost else None,
                remedy_cost=cost,
                is_resident_responsible=bool(cost),
            ),
        )

    raise_work_orders_from_findings(db.session, inspection=inspection)
    complete_inspection(db.session, inspection=inspection, inspector_signed=True)
    db.session.flush()
    return 1


# ---------------------------------------------------------------------------
# Automation
# ---------------------------------------------------------------------------


def _seed_automation(organization, prop) -> int:  # noqa: ANN001
    """A rule that earned its promotion the way a real one does."""
    from app.services.automation.engine import (
        activate_rule,
        create_rule,
        dispatch_event,
        promote_rule_to_live,
        run_rule,
    )

    escalation = create_rule(
        db.session,
        org_id=organization.id,
        code="escalate-emergencies",
        name="Escalate emergency work orders",
        description=(
            "Any work order raised at emergency priority gets a timeline note so "
            "the on-call dispatcher sees it without opening the record."
        ),
        trigger_event="work_order.created",
        conditions=[{"field": "priority", "op": "eq", "value": "emergency"}],
        actions=[
            {
                "type": "add_work_order_note",
                "params": {
                    "note": "Emergency: escalated to the on-call dispatcher.",
                    "resident_visible": False,
                },
            }
        ],
        max_runs_per_hour=200,
    )
    activate_rule(db.session, rule=escalation)

    # A dry run against a real work order, which is what promotion requires.
    from app.models.maintenance import WorkOrder

    sample = (
        db.session.query(WorkOrder)
        .filter(WorkOrder.org_id == organization.id, WorkOrder.property_id == prop.id)
        .first()
    )
    if sample is not None:
        run_rule(
            db.session,
            rule=escalation,
            event_type="work_order.created",
            payload={"priority": "emergency"},
            subject_type="work_order",
            subject_id=sample.id,
            force_dry_run=True,
        )
        promote_rule_to_live(db.session, rule=escalation)

        # And one live run, so the console shows a real execution with steps.
        dispatch_event(
            db.session,
            org_id=organization.id,
            event_type="work_order.created",
            payload={"priority": "emergency"},
            subject_type="work_order",
            subject_id=sample.id,
        )

    # A second rule left deliberately in dry run, because that is the state a
    # new rule should be found in.
    create_rule(
        db.session,
        org_id=organization.id,
        code="notify-on-completion",
        name="Announce completed work",
        description="Publishes a webhook event when work is verified.",
        trigger_event="work_order.completed",
        conditions=[],
        actions=[{"type": "emit_event", "params": {"event_type": "work_order.completed"}}],
    )

    db.session.flush()
    return 2


# ---------------------------------------------------------------------------
# Banking
# ---------------------------------------------------------------------------


def _seed_reconciliation(organization, accounts) -> int:  # noqa: ANN001
    """A completed reconciliation, matched through the real matcher."""
    from sqlalchemy import select

    from app.models.accounting import (
        BankAccount,
        BankAccountType,
        JournalEntry,
        JournalLine,
    )
    from app.models.leasing import Lease, LeaseStatus
    from app.services.accounting.chart import AccountCode
    from app.services.accounting.deposits import collect_deposit
    from app.services.accounting.reconciliation import (
        StatementLine,
        auto_match,
        complete_reconciliation,
        import_statement,
        open_reconciliation,
    )

    operating = BankAccount(
        org_id=organization.id,
        code="OPER",
        name="Operating account",
        account_type=BankAccountType.OPERATING,
        gl_account_id=accounts[AccountCode.CASH_OPERATING].id,
        institution_name="Kings County Bank",
    )
    trust = BankAccount(
        org_id=organization.id,
        code="TRUST",
        name="Security deposit trust",
        account_type=BankAccountType.TRUST,
        gl_account_id=accounts[AccountCode.CASH_TRUST].id,
        institution_name="Kings County Bank",
        is_trust=True,
    )
    db.session.add_all([operating, trust])
    db.session.flush()

    # Take every active lease's deposit into trust, through the same service
    # the application uses. Without this the demo shows a trust account holding
    # money that no beneficiary is recorded as owed, which is exactly the
    # exception the three-way reconciliation exists to raise.
    active_leases = (
        db.session.execute(
            select(Lease).where(
                Lease.org_id == organization.id,
                Lease.status == LeaseStatus.ACTIVE,
                Lease.security_deposit > Decimal("0"),
            )
        )
        .scalars()
        .all()
    )
    for lease in active_leases:
        collect_deposit(
            db.session,
            org_id=organization.id,
            lease_id=lease.id,
            bank_account_id=trust.id,
            amount=lease.security_deposit,
            effective_date=lease.start_date,
            reason="Deposit taken at move-in.",
        )
    db.session.flush()

    # Reconcile last month, so the window is closed and the figures are stable.
    period_end = TODAY.replace(day=1) - dt.timedelta(days=1)
    period_start = period_end.replace(day=1)

    rows = db.session.execute(
        select(JournalLine, JournalEntry)
        .join(JournalEntry, JournalEntry.id == JournalLine.journal_entry_id)
        .where(
            JournalLine.org_id == organization.id,
            JournalLine.account_id == accounts[AccountCode.CASH_OPERATING].id,
            JournalEntry.entry_date >= period_start,
            JournalEntry.entry_date <= period_end,
        )
    ).all()

    lines = [
        StatementLine(
            posted_date=entry.entry_date,
            amount=line.debit - line.credit,
            description=(entry.description or "Transfer")[:120].upper(),
            reference=entry.entry_number,
            external_id=f"KCB-{line.id[:12]}",
        )
        for line, entry in rows
    ]
    if not lines:
        return 0

    import_statement(
        db.session,
        org_id=organization.id,
        bank_account_id=operating.id,
        lines=lines,
    )
    auto_match(db.session, org_id=organization.id, bank_account_id=operating.id)

    closing = sum((line.amount for line in lines), Decimal("0"))
    reconciliation = open_reconciliation(
        db.session,
        org_id=organization.id,
        bank_account_id=operating.id,
        statement_start=period_start,
        statement_end=period_end,
        opening_balance=Decimal("0.00"),
        closing_balance=closing,
    )
    try:
        complete_reconciliation(
            db.session,
            reconciliation=reconciliation,
            completed_by_id=None,
            notes="Seeded demo reconciliation.",
        )
    except Exception:  # noqa: BLE001
        # Left open rather than forced: an unbalanced reconciliation that
        # completes anyway would be a demo of the wrong system.
        click.echo("    (reconciliation left open - statement and ledger disagree)")

    db.session.flush()
    return 1


# ---------------------------------------------------------------------------
# Ownership and statements
# ---------------------------------------------------------------------------


def _seed_ownership(organization, prop, accounts) -> int:  # noqa: ANN001
    """Two owners and a mid-period transfer, which is the interesting case."""
    from app.models.org import OwnerEntity, OwnershipStake, OwnerType
    from app.services.accounting.statements import generate_statement

    outgoing = OwnerEntity(
        org_id=organization.id,
        code="OWN-KESTREL",
        name="Kestrel Holdings LLC",
        owner_type=OwnerType.COMPANY,
        reserve_amount=Decimal("2500.00"),
    )
    incoming = OwnerEntity(
        org_id=organization.id,
        code="OWN-ALDER",
        name="Alder Trust",
        owner_type=OwnerType.TRUST,
        reserve_amount=Decimal("2500.00"),
    )
    db.session.add_all([outgoing, incoming])
    db.session.flush()

    period_end = TODAY.replace(day=1) - dt.timedelta(days=1)
    period_start = period_end.replace(day=1)
    handover = period_start + dt.timedelta(days=13)

    db.session.add_all(
        [
            OwnershipStake(
                org_id=organization.id,
                property_id=prop.id,
                owner_entity_id=outgoing.id,
                percentage=Decimal("100.00"),
                effective_from=dt.date(2019, 1, 1),
                effective_to=handover,
            ),
            OwnershipStake(
                org_id=organization.id,
                property_id=prop.id,
                owner_entity_id=incoming.id,
                percentage=Decimal("100.00"),
                effective_from=handover + dt.timedelta(days=1),
            ),
        ]
    )
    db.session.flush()

    produced = 0
    for owner in (outgoing, incoming):
        try:
            generate_statement(
                db.session,
                org_id=organization.id,
                owner_entity_id=owner.id,
                property_id=prop.id,
                period_start=period_start,
                period_end=period_end,
            )
            produced += 1
        except Exception:  # noqa: BLE001 - a demo statement is not worth failing the seed
            click.echo(f"    (statement for {owner.code} skipped)")

    db.session.flush()
    return produced


# ---------------------------------------------------------------------------
# Reporting, SSO, projections
# ---------------------------------------------------------------------------


def _seed_reports(organization, users) -> int:  # noqa: ANN001
    """Schedules that resolve their recipients at send time."""
    from app.models.reporting import ReportFormat, ScheduledReport
    from app.models.types import utcnow

    controller = users.get("controller@atlas.demo")
    schedules = [
        ScheduledReport(
            org_id=organization.id,
            name="Weekly rent roll",
            report_code="rent_roll",
            description="Every occupied unit with its lease terms.",
            schedule="0 7 * * 1",
            format=ReportFormat.CSV,
            recipients=([{"type": "user", "id": controller.id}] if controller is not None else []),
            is_active=True,
            next_run_at=utcnow() + dt.timedelta(days=1),
        ),
        ScheduledReport(
            org_id=organization.id,
            name="Monthly delinquency ageing",
            report_code="delinquency",
            description="Overdue balances by ageing bucket.",
            schedule="0 6 1 * *",
            format=ReportFormat.PDF,
            recipients=[{"type": "role", "code": "controller"}],
            is_active=True,
            next_run_at=utcnow() + dt.timedelta(days=3),
        ),
        ScheduledReport(
            org_id=organization.id,
            name="Capital plan",
            report_code="capital_plan",
            description="Five-year replacement forecast, inflated forward.",
            schedule="0 6 1 * *",
            format=ReportFormat.PDF,
            parameters={"horizon_years": 5},
            recipients=[{"type": "role", "code": "org_admin"}],
            is_active=True,
            next_run_at=utcnow() + dt.timedelta(days=3),
        ),
    ]
    db.session.add_all(schedules)
    db.session.flush()
    return len(schedules)


def _seed_sso(organization) -> int:  # noqa: ANN001
    """A configured provider, left inactive.

    Active would be a lie: there is no identity provider behind it in a demo,
    and a sign-in button that cannot work is worse than none.
    """
    from app.models.sso import IdentityProvider, SsoProtocol

    db.session.add(
        IdentityProvider(
            org_id=organization.id,
            code="northlight-entra",
            name="Northlight (Microsoft Entra ID)",
            protocol=SsoProtocol.OIDC,
            is_active=False,
            issuer="https://login.microsoftonline.com/00000000-0000-0000-0000-000000000000/v2.0",
            client_id="atlas-demo-client",
            discovery_url=(
                "https://login.microsoftonline.com/00000000-0000-0000-0000-000000000000"
                "/v2.0/.well-known/openid-configuration"
            ),
            allowed_email_domains=["atlas.demo"],
            jit_provisioning=True,
            default_role_code="property_manager",
            groups_claim="groups",
            group_role_map={"Atlas-Admins": "org_admin", "Atlas-Accounting": "accountant"},
            scim_enabled=False,
        )
    )
    db.session.flush()
    return 1


def _seed_projections(organization) -> int:  # noqa: ANN001
    """A fortnight of KPI history, so the dashboard has a trend not a dot."""
    from app.services.reporting.projections import snapshot_metrics

    points = 0
    for days_ago in range(14, -1, -1):
        points += len(
            snapshot_metrics(
                db.session, org_id=organization.id, as_of=TODAY - dt.timedelta(days=days_ago)
            )
        )
    db.session.flush()
    return points

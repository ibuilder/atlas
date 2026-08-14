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

from app.extensions import current_session, db
from app.models.types import utcnow

__all__ = ["seed_operations"]

#: UTC, matching the services this seeds through. A local date near the UTC
#: rollover would hand them a "future" service date and be refused.
TODAY = utcnow().date()


def seed_operations(organization, properties, units, leases, vendor, users, accounts):  # noqa: ANN001, ANN201
    """Layer the 0.2-0.5 features onto an existing demo organization."""
    summary: dict[str, int] = {}

    spaces = _seed_spaces(organization, properties[0], units)
    summary["spaces"] = len(spaces)

    assets = _seed_assets(organization, properties[0], spaces)
    summary["assets"] = len(assets)

    summary["pm_schedules"] = _seed_preventive(organization, properties, assets)
    summary["inspections"] = _seed_inspection(organization, properties[0], units, leases)
    summary["automation_rules"] = _seed_automation(organization, properties[0])
    summary["reconciliations"] = _seed_reconciliation(organization, accounts, users)
    summary["owner_statements"] = _seed_ownership(organization, properties[0], accounts)
    summary["scheduled_reports"] = _seed_reports(organization, users)
    summary["identity_providers"] = _seed_sso(organization)
    summary["kpi_points"] = _seed_projections(organization)
    summary["message_threads"] = _seed_messages(organization, properties, leases, users)
    summary["turns"] = _seed_turns(organization, units, users)
    summary["envelopes"] = _seed_esign(organization, leases, users)
    summary["applications"] = _seed_applications(organization, properties[0], units, users)
    summary["tenancy_endings"] = _seed_tenancy_endings(organization, leases, accounts, users)
    summary["bills"] = _seed_payables(organization, vendor, accounts, users)

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
        current_session(),
        org_id=organization.id,
        property_id=prop.id,
        code="HAR-SITE",
        name="Harrow Court",
        kind=SpaceKind.COMMON_AREA,
    )
    riser = create_space(
        current_session(),
        org_id=organization.id,
        property_id=prop.id,
        code="HAR-RISER-A",
        name="Riser A",
        kind=SpaceKind.MECHANICAL,
        parent=site,
    )
    plant = create_space(
        current_session(),
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
            current_session(),
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
                current_session(),
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
                current_session(),
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
            current_session(),
            asset=boiler,
            event_type=ServiceEventType.REPAIR,
            performed_on=TODAY - dt.timedelta(days=30 * months_ago),
            cost=Decimal(cost),
            notes="Heat exchanger fault; parts and labour.",
            condition_after=2,
        )
    record_service(
        current_session(),
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
        current_session(),
        org_id=organization.id,
        kind=InspectionKind.MOVE_OUT,
        property_id=prop.id,
        unit_id=lease.unit_id if lease else units[0].id,
        lease_id=lease.id if lease else None,
        template=template,
        scheduled_for=None,
    )
    start_inspection(current_session(), inspection=inspection)

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
            current_session(),
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

    raise_work_orders_from_findings(current_session(), inspection=inspection)
    complete_inspection(current_session(), inspection=inspection, inspector_signed=True)
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
        current_session(),
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
    activate_rule(current_session(), rule=escalation)

    # A dry run against a real work order, which is what promotion requires.
    from app.models.maintenance import WorkOrder

    sample = (
        db.session.query(WorkOrder)
        .filter(WorkOrder.org_id == organization.id, WorkOrder.property_id == prop.id)
        .first()
    )
    if sample is not None:
        run_rule(
            current_session(),
            rule=escalation,
            event_type="work_order.created",
            payload={"priority": "emergency"},
            subject_type="work_order",
            subject_id=sample.id,
            force_dry_run=True,
        )
        promote_rule_to_live(current_session(), rule=escalation)

        # And one live run, so the console shows a real execution with steps.
        dispatch_event(
            current_session(),
            org_id=organization.id,
            event_type="work_order.created",
            payload={"priority": "emergency"},
            subject_type="work_order",
            subject_id=sample.id,
        )

    # A second rule left deliberately in dry run, because that is the state a
    # new rule should be found in.
    create_rule(
        current_session(),
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


def _seed_reconciliation(organization, accounts, users) -> int:  # noqa: ANN001
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
            current_session(),
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
        current_session(),
        org_id=organization.id,
        bank_account_id=operating.id,
        lines=lines,
    )
    auto_match(current_session(), org_id=organization.id, bank_account_id=operating.id)

    closing = sum((line.amount for line in lines), Decimal("0"))
    reconciliation = open_reconciliation(
        current_session(),
        org_id=organization.id,
        bank_account_id=operating.id,
        statement_start=period_start,
        statement_end=period_end,
        opening_balance=Decimal("0.00"),
        closing_balance=closing,
    )
    # A sign-off is attributed to a person. The controller is the role that
    # holds it in the seeded organization, and in a real one.
    controller = users["controller"]

    try:
        complete_reconciliation(
            current_session(),
            reconciliation=reconciliation,
            completed_by_id=controller.id,
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
                current_session(),
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

    controller = users["controller"]
    schedules = [
        ScheduledReport(
            org_id=organization.id,
            name="Weekly rent roll",
            report_code="rent_roll",
            description="Every occupied unit with its lease terms.",
            schedule="0 7 * * 1",
            format=ReportFormat.CSV,
            recipients=[{"type": "user", "id": controller.id}],
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
                current_session(), org_id=organization.id, as_of=TODAY - dt.timedelta(days=days_ago)
            )
        )
    db.session.flush()
    return points


# ---------------------------------------------------------------------------
# Messaging, turns, and a signed lease
# ---------------------------------------------------------------------------


def _seed_messages(organization, properties, leases, users) -> int:  # noqa: ANN001
    """Conversations, including one the resident must never see.

    The internal thread is the point of seeding this at all: a demo where every
    thread is visible proves nothing about the rule that matters. Sign in as the
    resident and it is absent; sign in as staff and it is there.
    """
    from sqlalchemy import select

    from app.models.maintenance import WorkOrder
    from app.models.org import OwnerEntity
    from app.models.resident import CommunicationChannel, MessageDirection, Tenancy
    from app.services.notifications.messaging import (
        assign_thread,
        open_thread,
        post_message,
        resolve_thread,
        set_status,
    )

    manager = users["property_manager"]
    lease = leases[0]
    tenancy = (
        db.session.execute(
            select(Tenancy).where(Tenancy.org_id == organization.id, Tenancy.lease_id == lease.id)
        )
        .scalars()
        .first()
    )
    resident_id = tenancy.resident_id if tenancy is not None else None

    created = 0

    # 1. An ordinary resident conversation, still open.
    tap = open_thread(
        current_session(),
        org_id=organization.id,
        title="Kitchen tap is dripping",
        subject_type="lease",
        subject_id=lease.id,
        property_id=lease.property_id,
        unit_id=lease.unit_id,
        resident_id=resident_id,
        actor_id=manager.id,
    )
    post_message(
        current_session(),
        thread=tap,
        body="The kitchen mixer has been dripping since the weekend. It is getting worse.",
        sender_label="Resident",
        direction=MessageDirection.INBOUND,
    )
    post_message(
        current_session(),
        thread=tap,
        body="Thanks for letting us know. A plumber can come Thursday morning - does that suit?",
        sender_label=manager.full_name,
        sender_user_id=manager.id,
    )
    assign_thread(current_session(), thread=tap, assignee_id=manager.id, actor_id=manager.id)
    set_status(current_session(), thread=tap, status="pending", actor_id=manager.id)
    created += 1

    # 2. Internal, on the same lease. Never visible in the resident portal.
    internal = open_thread(
        current_session(),
        org_id=organization.id,
        title="Renewal approach for this tenancy",
        subject_type="lease",
        subject_id=lease.id,
        resident_id=resident_id,
        is_internal=True,
        actor_id=manager.id,
    )
    post_message(
        current_session(),
        thread=internal,
        body=(
            "Good payer, no arrears in eighteen months. Recommend holding the "
            "increase to 3% rather than the 5% the model suggests."
        ),
        sender_label=manager.full_name,
        sender_user_id=manager.id,
        direction=MessageDirection.INTERNAL,
    )
    created += 1

    # 3. Addressed to the owner rather than to a property - which is the case
    #    that used to be invisible to them.
    owner = (
        db.session.execute(select(OwnerEntity).where(OwnerEntity.org_id == organization.id))
        .scalars()
        .first()
    )
    if owner is not None:
        distribution = open_thread(
            current_session(),
            org_id=organization.id,
            title="Your distribution timing",
            subject_type="owner",
            subject_id=owner.id,
            actor_id=manager.id,
        )
        post_message(
            current_session(),
            thread=distribution,
            body="Distributions now clear on the 5th rather than the 8th. Nothing else changes.",
            sender_label=users["controller"].full_name,
            sender_user_id=users["controller"].id,
            channel=CommunicationChannel.EMAIL,
        )
        created += 1

    # 4. A vendor thread on their own job, resolved.
    order = (
        db.session.execute(
            select(WorkOrder).where(
                WorkOrder.org_id == organization.id, WorkOrder.vendor_id.is_not(None)
            )
        )
        .scalars()
        .first()
    )
    if order is not None:
        access = open_thread(
            current_session(),
            org_id=organization.id,
            title=f"Access for {order.work_order_number}",
            subject_type="work_order",
            subject_id=order.id,
            actor_id=manager.id,
        )
        post_message(
            current_session(),
            thread=access,
            body="Key safe code is on the job. Resident works from home Tuesdays.",
            sender_label=manager.full_name,
            sender_user_id=manager.id,
        )
        post_message(
            current_session(),
            thread=access,
            body="Understood - we will attend Tuesday.",
            sender_label="Apex Mechanical",
            direction=MessageDirection.INBOUND,
        )
        resolve_thread(current_session(), thread=access, actor_id=manager.id)
        created += 1

    db.session.flush()
    return created


def _seed_turns(organization, units, users) -> int:  # noqa: ANN001
    """One turn finished, one still running.

    A finished one so the board has a days-vacant figure to average, and a
    running one so the board has something on it. The running one keeps a
    required step outstanding, so the refusal is demonstrable rather than
    described.
    """
    from app.models.org import UnitStatus
    from app.services.leasing.turns import complete_step, mark_ready, skip_step, start_turn

    manager = users["property_manager"]
    candidates = [
        unit
        for unit in units
        if unit.org_id == organization.id
        and unit.status in (UnitStatus.VACANT_READY, UnitStatus.VACANT_NOT_READY)
    ][:2]
    if len(candidates) < 2:
        return 0

    # Finished eleven days after the keys came back, with one step skipped and
    # a reason on the record.
    finished = start_turn(
        current_session(),
        org_id=organization.id,
        unit_id=candidates[0].id,
        started_on=TODAY - dt.timedelta(days=40),
        actor_id=manager.id,
    )
    for step in finished.steps:
        if not step.is_required:
            continue
        if step.name == "Flooring clean or replace":
            skip_step(
                current_session(),
                step=step,
                reason="Vinyl laid last year; cleaned rather than replaced.",
                actor_id=manager.id,
            )
        else:
            complete_step(current_session(), step=step, actor_id=manager.id)
    mark_ready(
        current_session(),
        turn=finished,
        ready_on=TODAY - dt.timedelta(days=29),
        actor_id=manager.id,
    )

    # Still running, and past its target, so the board shows what late looks like.
    running = start_turn(
        current_session(),
        org_id=organization.id,
        unit_id=candidates[1].id,
        started_on=TODAY - dt.timedelta(days=18),
        actor_id=manager.id,
    )
    for step in running.steps[:3]:
        complete_step(current_session(), step=step, actor_id=manager.id)

    db.session.flush()
    return 2


def _seed_esign(organization, leases, users) -> int:  # noqa: ANN001
    """A lease executed electronically, with the consent record behind it."""
    from sqlalchemy import select

    from app.models.documents import Document, DocumentCategory, ScanStatus
    from app.models.iam import User
    from app.models.resident import Resident, Tenancy
    from app.services.documents.esign import (
        SignerInput,
        create_envelope,
        record_signature,
        send_envelope,
        sha256_of,
    )

    lease = leases[0]
    tenancy = (
        db.session.execute(
            select(Tenancy).where(Tenancy.org_id == organization.id, Tenancy.lease_id == lease.id)
        )
        .scalars()
        .first()
    )
    if tenancy is None:
        return 0
    resident = db.session.get(Resident, tenancy.resident_id)
    if resident is None:
        return 0

    # The address the resident *signs in with*, not the contact address on their
    # record. `awaiting_signature` matches strictly on the signed-in address, so
    # an envelope addressed anywhere else is invisible to them - which is the
    # constraint the service warns about, and the demo should not walk into it.
    portal_user = (
        db.session.execute(
            select(User).where(User.org_id == organization.id, User.resident_id == resident.id)
        )
        .scalars()
        .first()
    )
    signer_email = portal_user.email if portal_user is not None else resident.email
    if not signer_email:
        return 0

    # A stand-in for the rendered agreement. The digest is of real bytes, so the
    # artifact check the envelope performs is checking something.
    body = (
        f"RESIDENTIAL TENANCY AGREEMENT\n"
        f"Lease {lease.lease_number}\n"
        f"Term {lease.start_date} to {lease.end_date}\n"
        f"Rent {lease.rent_amount} per month\n"
    ).encode()

    document = Document(
        org_id=organization.id,
        name=f"Lease agreement {lease.lease_number}",
        original_filename=f"{lease.lease_number}.txt",
        storage_key=f"documents/demo/{lease.lease_number}.txt",
        content_type="text/plain",
        size_bytes=len(body),
        checksum_sha256=sha256_of(body),
        category=DocumentCategory.LEASE,
        scan_status=ScanStatus.CLEAN,
    )
    db.session.add(document)
    db.session.flush()

    manager = users["property_manager"]
    envelope = create_envelope(
        current_session(),
        org_id=organization.id,
        document_id=document.id,
        title=f"Lease agreement {lease.lease_number}",
        reference=f"ENV-{lease.lease_number}",
        signers=[
            SignerInput(name=resident.full_name, email=signer_email, role="resident"),
            SignerInput(name=manager.full_name, email=manager.email, role="landlord"),
        ],
        subject_type="lease",
        subject_id=lease.id,
        actor_id=manager.id,
    )
    send_envelope(current_session(), envelope=envelope, actor_id=manager.id)

    for name, address in (
        (resident.full_name, signer_email),
        (manager.full_name, manager.email),
    ):
        record_signature(
            current_session(),
            envelope=envelope,
            email=address,
            typed_name=name,
            ip_address="203.0.113.42",
            user_agent="Mozilla/5.0 (demo portal)",
        )

    # And one still waiting, so the signing page has something on it. Without
    # this the demo shows a completed envelope and an empty portal, which
    # demonstrates the record but not the act.
    pending_body = (
        f"PARKING ADDENDUM\nLease {lease.lease_number}\nBay 14, from next month\n"
    ).encode()
    pending_document = Document(
        org_id=organization.id,
        name=f"Parking addendum {lease.lease_number}",
        original_filename=f"{lease.lease_number}-parking.txt",
        storage_key=f"documents/demo/{lease.lease_number}-parking.txt",
        content_type="text/plain",
        size_bytes=len(pending_body),
        checksum_sha256=sha256_of(pending_body),
        category=DocumentCategory.LEASE,
        scan_status=ScanStatus.CLEAN,
    )
    db.session.add(pending_document)
    db.session.flush()

    pending = create_envelope(
        current_session(),
        org_id=organization.id,
        document_id=pending_document.id,
        title=f"Parking addendum {lease.lease_number}",
        reference=f"ENV-{lease.lease_number}-PARK",
        signers=[SignerInput(name=resident.full_name, email=signer_email, role="resident")],
        subject_type="lease",
        subject_id=lease.id,
        actor_id=manager.id,
    )
    send_envelope(current_session(), envelope=pending, actor_id=manager.id)

    db.session.flush()
    return 2


def _seed_applications(organization, prop, units, users) -> int:  # noqa: ANN001
    """Three applications: one waiting, one approved with conditions, one denied.

    A denial is seeded on purpose. The funnel's hardest surface to get right is
    the one where somebody is told no, and a demo that only ever approves shows
    none of the machinery that exists for it - the recorded reasons, the
    criteria snapshotted at the decision, the individual assessment.
    """
    from app.models.org import UnitStatus
    from app.services.leasing.applications import (
        add_applicant,
        approve_application,
        create_application,
        deny_application,
        record_consent,
        record_screening,
        request_screening,
        submit_application,
    )

    manager = users["property_manager"]
    vacant = [
        unit
        for unit in units
        if unit.org_id == organization.id and unit.status == UnitStatus.VACANT_READY
    ]
    session = current_session()

    def _open(  # noqa: ANN202
        first,  # noqa: ANN001
        last,  # noqa: ANN001
        income,  # noqa: ANN001
        unit,  # noqa: ANN001
        days_ago,  # noqa: ANN001
        credit=None,  # noqa: ANN001
        recommendation=None,  # noqa: ANN001
        factors=None,  # noqa: ANN001
    ):
        application = create_application(
            session,
            org_id=organization.id,
            property_id=prop.id,
            unit_id=unit.id if unit else None,
            desired_move_in=TODAY + dt.timedelta(days=30),
            lease_term_months=12,
            quoted_rent=Decimal("2150.00"),
            application_fee=Decimal("45.00"),
            actor_id=manager.id,
        )
        application.created_at = utcnow() - dt.timedelta(days=days_ago)
        applicant = add_applicant(
            session,
            application=application,
            first_name=first,
            last_name=last,
            email=f"{first.lower()}.{last.lower()}@example.invalid",
            monthly_income=income,
            employer_name="Meridian Labs",
        )
        # Consent before screening, with the address it was given from. The
        # demo records a plausible one; the surfaces read it from the request.
        record_consent(session, applicant=applicant, ip_address="198.51.100.23")
        submit_application(session, application=application, actor_id=manager.id)
        if recommendation is not None:
            screening = request_screening(
                session, application=application, applicant=applicant, provider="demo-bureau"
            )
            record_screening(
                session,
                screening=screening,
                recommendation=recommendation,
                credit_score=credit,
                verified_monthly_income=income,
                provider_reference="DEMO-0001",
                factors=factors,
            )
        return application

    from app.models.leasing import ScreeningRecommendation

    # Still open, so the console has something waiting on a person.
    _open("Rosa", "Villanueva", Decimal("7400.00"), vacant[0] if vacant else None, 3)

    conditional = _open(
        "Amir",
        "Haddad",
        Decimal("5600.00"),
        vacant[1] if len(vacant) > 1 else None,
        11,
        credit=648,
        recommendation=ScreeningRecommendation.APPROVE_WITH_CONDITIONS,
    )
    approve_application(
        session,
        application=conditional,
        decided_by_id=manager.id,
        conditions={"conditions": ["Guarantor required", "Two months' deposit"]},
        reason="Verified income is 2.6x the rent, below the 3.0x threshold; a guarantor closes the gap.",
    )

    denied = _open(
        "Jordan",
        "Pike",
        Decimal("3100.00"),
        None,
        20,
        credit=571,
        recommendation=ScreeningRecommendation.DECLINE,
        # A decline without structured factors is recorded but flagged: no
        # adverse-action notice can be written from "declined".
        factors=[
            {"code": "income_ratio", "detail": "Verified income is 1.4x the quoted rent."},
            {"code": "credit_score", "detail": "Score of 571 against a 620 minimum."},
        ],
    )
    deny_application(
        session,
        application=denied,
        decided_by_id=manager.id,
        reasons=[
            "Verified income is 1.4x the quoted rent, below the 3.0x threshold in force.",
            "Credit score of 571 is below the 620 minimum in force.",
        ],
    )

    db.session.flush()
    return 3


def _seed_tenancy_endings(organization, leases, accounts, users) -> int:  # noqa: ANN001
    """A renewal on offer, and a move-out settled inside its deadline.

    A settled one rather than an overdue one: the board is meant to be empty,
    and a demo that ships with a statutory breach on it teaches the wrong
    reflex about what that red banner means.
    """
    from sqlalchemy import select

    from app.models.accounting import BankAccount
    from app.models.leasing import LeaseStatus
    from app.services.accounting.deposits import collect_deposit, deposit_balance
    from app.services.leasing.tenancy import (
        Deduction,
        give_notice,
        offer_renewal,
        record_move_out,
        settle_deposit,
    )

    manager = users["property_manager"]
    session = current_session()
    active = [
        lease
        for lease in leases
        if lease.org_id == organization.id and lease.status == LeaseStatus.ACTIVE
    ]
    if len(active) < 2:
        return 0

    # One renewal on offer, priced above the current rent and open for 30 days.
    renewing = active[0]
    offer_renewal(
        session,
        lease=renewing,
        offered_rent=(renewing.rent_amount * Decimal("1.04")).quantize(Decimal("0.01")),
        proposed_start=renewing.end_date + dt.timedelta(days=1),
        proposed_end=renewing.end_date + dt.timedelta(days=366),
        actor_id=manager.id,
    )

    # One tenancy ended and settled. The deposit is collected first because a
    # disposition settles against what was taken, not against the contract.
    leaving = active[1]
    trust = (
        session.execute(
            select(BankAccount).where(
                BankAccount.org_id == organization.id, BankAccount.is_trust.is_(True)
            )
        )
        .scalars()
        .first()
    )
    if trust is None:
        return 1

    if deposit_balance(session, org_id=organization.id, lease_id=leaving.id) <= Decimal("0"):
        collect_deposit(
            session,
            org_id=organization.id,
            lease_id=leaving.id,
            bank_account_id=trust.id,
            amount=leaving.security_deposit or leaving.rent_amount,
            effective_date=leaving.start_date,
        )

    left_on = TODAY - dt.timedelta(days=12)
    move_out = give_notice(
        session,
        lease=leaving,
        notice_date=left_on - dt.timedelta(days=30),
        scheduled_date=left_on,
        reason="Bought a house.",
    )
    record_move_out(
        session,
        move_out=move_out,
        actual_date=left_on,
        forwarding_address={"raw": "44 Kestrel Row, Northlight"},
        start_turn_on_vacancy=False,
        actor_id=manager.id,
    )
    settle_deposit(
        session,
        move_out=move_out,
        deductions=[
            Deduction(description="Carpet clean, living room", amount=Decimal("145.00")),
            Deduction(description="Two keys not returned", amount=Decimal("40.00")),
        ],
        settled_by_id=manager.id,
    )

    db.session.flush()
    return 2


def _seed_payables(organization, vendor, accounts, users) -> int:  # noqa: ANN001
    """Three bills: one paid, one approved and due, one blocked on approval.

    The blocked one is recorded by the accountant on purpose. Signing in as
    that accountant and finding the approve button gone - with the reason
    stated - is the only way the separation-of-duties rule is visible in a demo
    at all.
    """
    from sqlalchemy import select

    from app.models.accounting import BankAccount
    from app.services.accounting.chart import AccountCode
    from app.services.accounting.payables import (
        BillLineInput,
        approve_bill,
        pay_bill,
        record_bill,
    )

    session = current_session()
    clerk = users["accountant"]
    controller = users["controller"]
    operating = (
        session.execute(
            select(BankAccount).where(
                BankAccount.org_id == organization.id, BankAccount.is_trust.is_(False)
            )
        )
        .scalars()
        .first()
    )

    def _bill(description, amount, days_ago, account_code, invoice):  # noqa: ANN001, ANN202
        bill = record_bill(
            session,
            org_id=organization.id,
            vendor_id=vendor.id,
            bill_date=TODAY - dt.timedelta(days=days_ago),
            due_date=TODAY - dt.timedelta(days=days_ago) + dt.timedelta(days=30),
            lines=[
                BillLineInput(
                    description=description,
                    amount=amount,
                    account_id=accounts[account_code].id,
                )
            ],
            vendor_invoice_number=invoice,
            actor_id=clerk.id,
        )
        # The separation rule reads created_by_id, and the seed runs as the
        # system rather than as a person - so attribution has to be stated.
        bill.created_by_id = clerk.id
        session.flush()
        return bill

    settled = _bill(
        "Boiler service, Harrow Court",
        Decimal("640.00"),
        75,
        AccountCode.REPAIRS_MAINTENANCE,
        "NPS-20418",
    )
    approve_bill(session, bill=settled, approver_id=controller.id, note="Matches the work order.")
    if operating is not None:
        pay_bill(
            session,
            bill=settled,
            bank_account_id=operating.id,
            amount=settled.total,
            paid_date=TODAY - dt.timedelta(days=50),
            check_number="10418",
            actor_id=controller.id,
        )

    due = _bill(
        "Landscaping, quarterly",
        Decimal("1180.00"),
        40,
        AccountCode.REPAIRS_MAINTENANCE,
        "NPS-20502",
    )
    approve_bill(session, bill=due, approver_id=controller.id)

    # Left pending on purpose, and recorded by the accountant, so the demo can
    # show the refusal rather than describe it.
    _bill(
        "Roof inspection and flashing repair",
        Decimal("3450.00"),
        6,
        AccountCode.REPAIRS_MAINTENANCE,
        "NPS-20577",
    )

    db.session.flush()
    return 3

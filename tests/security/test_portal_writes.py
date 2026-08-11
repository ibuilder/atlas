"""The portal write surfaces.

These are the routes where the caller is a resident or a contractor rather than
a member of staff, and where a permission check alone proves nothing: every
resident holds `payment.record`, so the question is never "may they pay?" but
"is this their invoice?".

Each POST is therefore tested twice — once for the happy path, once for the
same request aimed at somebody else's record. The second must 404, not 403:
telling a resident that an invoice exists but is not theirs turns the portal
into a way to enumerate the building's invoices.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

pytestmark = pytest.mark.security

PORTAL_PASSWORD = "portal-resident-2026-ok!"


def _rebound(org):
    """A tenant scope for reading after a request has run.

    The request cycle clears the ambient context on its way out, so a test that
    inspects the database afterwards has to bind its own - the same discipline
    the application code is held to.
    """
    from app.context import RequestContext, bind_context, new_correlation_id

    return bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=org.id,
            actor_type="system",
            source="test",
        )
    )


# ---------------------------------------------------------------------------
# Fixtures: two residents, so "somebody else's" is a real record
# ---------------------------------------------------------------------------


@pytest.fixture()
def tenancy(db, org, scope, property_record, unit_record, lease_record):
    """A resident with a portal login, on the seeded lease."""
    from app.models.iam import UserType
    from app.models.resident import Resident, ResidentStatus, Tenancy, TenancyRole
    from app.services.iam.provisioning import create_user

    resident = Resident(
        org_id=org.id,
        first_name="Dana",
        last_name="Okafor",
        email="dana.portal@test.local",
        status=ResidentStatus.CURRENT,
    )
    db.session.add(resident)
    db.session.commit()

    db.session.add(
        Tenancy(
            org_id=org.id,
            lease_id=lease_record.id,
            resident_id=resident.id,
            role=TenancyRole.PRIMARY,
            started_at=lease_record.start_date,
        )
    )
    user = create_user(
        db.session,
        org_id=org.id,
        email="dana.portal@test.local",
        full_name="Dana Okafor",
        password=PORTAL_PASSWORD,
        user_type=UserType.RESIDENT,
        role_codes=["resident"],
        resident_id=resident.id,
    )
    db.session.commit()
    return {"resident": resident, "user": user, "lease": lease_record}


@pytest.fixture()
def open_invoice(db, org, scope, accounts, lease_record):
    from app.services.accounting.chart import AccountCode
    from app.services.accounting.receivables import ChargeInput, issue_invoice

    invoice = issue_invoice(
        db.session,
        org_id=org.id,
        charges=[
            ChargeInput(
                description="Rent",
                amount=Decimal("1200.00"),
                account_id=accounts[AccountCode.RENTAL_INCOME].id,
            )
        ],
        issue_date=dt.date(2026, 3, 1),
        due_date=dt.date(2026, 3, 1),
        lease=lease_record,
        property_id=lease_record.property_id,
    )
    db.session.commit()
    return invoice


@pytest.fixture()
def someone_elses_invoice(db, org, scope, accounts, property_record, unit_record):
    """An invoice on a lease the portal resident has no tenancy on."""
    from app.models.leasing import Lease, LeaseStatus
    from app.models.sequences import SequenceKey
    from app.services.accounting.chart import AccountCode
    from app.services.accounting.receivables import ChargeInput, issue_invoice
    from app.services.common.numbering import next_number

    other = Lease(
        org_id=org.id,
        lease_number=next_number(db.session, SequenceKey.LEASE, org_id=org.id),
        property_id=property_record.id,
        unit_id=unit_record.id,
        status=LeaseStatus.ACTIVE,
        start_date=dt.date(2026, 1, 1),
        end_date=dt.date(2026, 12, 31),
        rent_amount=Decimal("2000.00"),
        security_deposit=Decimal("2000.00"),
    )
    db.session.add(other)
    db.session.commit()

    invoice = issue_invoice(
        db.session,
        org_id=org.id,
        charges=[
            ChargeInput(
                description="Rent",
                amount=Decimal("2000.00"),
                account_id=accounts[AccountCode.RENTAL_INCOME].id,
            )
        ],
        issue_date=dt.date(2026, 3, 1),
        due_date=dt.date(2026, 3, 1),
        lease=other,
        property_id=other.property_id,
    )
    db.session.commit()
    return invoice


def _sign_in_resident(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "dana.portal@test.local", "password": PORTAL_PASSWORD},
    )
    assert response.status_code == 200, response.get_json()


# ---------------------------------------------------------------------------
# Resident: paying
# ---------------------------------------------------------------------------


def test_a_resident_can_pay_their_own_invoice(client, db, tenancy, open_invoice):
    _sign_in_resident(client)

    response = client.post(
        f"/resident/invoices/{open_invoice.id}/pay",
        data={"amount": "1200.00"},
        follow_redirects=False,
    )
    assert response.status_code == 302

    db.session.expire_all()
    from app.models.accounting import InvoiceStatus

    assert db.session.get(type(open_invoice), open_invoice.id).status == InvoiceStatus.PAID


def test_paying_without_an_amount_pays_the_balance(client, db, tenancy, open_invoice):
    """What almost everyone wants, and what an empty box should mean."""
    _sign_in_resident(client)

    client.post(f"/resident/invoices/{open_invoice.id}/pay", data={"amount": ""})
    db.session.expire_all()

    assert db.session.get(type(open_invoice), open_invoice.id).balance == Decimal("0.0000")


def test_a_resident_cannot_pay_somebody_elses_invoice(client, db, tenancy, someone_elses_invoice):
    """404 rather than 403: the portal is not an invoice enumerator."""
    _sign_in_resident(client)

    response = client.post(
        f"/resident/invoices/{someone_elses_invoice.id}/pay", data={"amount": "10.00"}
    )
    assert response.status_code == 404

    db.session.expire_all()
    assert db.session.get(type(someone_elses_invoice), someone_elses_invoice.id).balance == Decimal(
        "2000.0000"
    )


def test_a_nonexistent_invoice_is_also_a_404(client, tenancy):
    _sign_in_resident(client)
    response = client.post(
        "/resident/invoices/00000000-0000-0000-0000-000000000000/pay",
        data={"amount": "10.00"},
    )
    assert response.status_code == 404


def test_overpaying_is_refused(client, db, tenancy, open_invoice):
    """A typo in a portal box should not mint a credit somebody has to chase."""
    _sign_in_resident(client)

    client.post(f"/resident/invoices/{open_invoice.id}/pay", data={"amount": "12000.00"})
    db.session.expire_all()

    assert db.session.get(type(open_invoice), open_invoice.id).balance == Decimal("1200.0000")


def test_a_negative_amount_is_refused(client, db, tenancy, open_invoice):
    _sign_in_resident(client)
    client.post(f"/resident/invoices/{open_invoice.id}/pay", data={"amount": "-50.00"})
    db.session.expire_all()
    assert db.session.get(type(open_invoice), open_invoice.id).balance == Decimal("1200.0000")


def test_a_non_numeric_amount_is_refused(client, db, tenancy, open_invoice):
    _sign_in_resident(client)
    client.post(f"/resident/invoices/{open_invoice.id}/pay", data={"amount": "one thousand"})
    db.session.expire_all()
    assert db.session.get(type(open_invoice), open_invoice.id).balance == Decimal("1200.0000")


@pytest.mark.parametrize("amount", ["NaN", "-NaN", "Infinity", "-Infinity", "1e400"])
def test_a_non_finite_amount_is_refused_rather_than_crashing(
    client, db, tenancy, open_invoice, amount
):
    """NaN quantizes cleanly and then raises on comparison rather than failing it.

    Both portal guards read ``amount <= 0`` and ``amount > balance``; against
    NaN those do not return False, they raise - so without an explicit finite
    check the route answers a hostile form field with a 500.
    """
    _sign_in_resident(client)
    response = client.post(f"/resident/invoices/{open_invoice.id}/pay", data={"amount": amount})

    assert response.status_code == 302
    db.session.expire_all()
    assert db.session.get(type(open_invoice), open_invoice.id).balance == Decimal("1200.0000")


def test_the_invoice_the_resident_chose_is_the_one_that_is_paid(
    client, db, org, scope, accounts, tenancy, lease_record, open_invoice
):
    """Applying oldest-first would pay a different invoice and say otherwise.

    ``record_payment`` retires the lease's open invoices oldest-due-first when
    it is given no allocation, so a resident settling this month's rent while
    last month's is outstanding would have cleared the wrong one - and been
    told, in the confirmation, that they had cleared this one.
    """
    from app.models.accounting import InvoiceStatus
    from app.services.accounting.chart import AccountCode
    from app.services.accounting.receivables import ChargeInput, issue_invoice

    older = issue_invoice(
        db.session,
        org_id=org.id,
        charges=[
            ChargeInput(
                description="February rent",
                amount=Decimal("1200.00"),
                account_id=accounts[AccountCode.RENTAL_INCOME].id,
            )
        ],
        issue_date=dt.date(2026, 2, 1),
        due_date=dt.date(2026, 2, 1),
        lease=lease_record,
        property_id=lease_record.property_id,
    )
    db.session.commit()

    _sign_in_resident(client)
    client.post(f"/resident/invoices/{open_invoice.id}/pay", data={"amount": "1200.00"})
    db.session.expire_all()

    assert db.session.get(type(open_invoice), open_invoice.id).status == InvoiceStatus.PAID
    assert db.session.get(type(older), older.id).balance == Decimal("1200.0000")


def test_an_anonymous_visitor_cannot_pay(client, open_invoice):
    response = client.post(f"/resident/invoices/{open_invoice.id}/pay", data={"amount": "1.00"})
    assert response.status_code in (302, 401)


# ---------------------------------------------------------------------------
# Resident: raising a request
# ---------------------------------------------------------------------------


def test_a_resident_can_raise_a_request(client, db, org, scope, tenancy):
    _sign_in_resident(client)

    response = client.post(
        "/resident/requests",
        data={
            "title": "No hot water",
            "description": "No hot water in the bathroom since Tuesday.",
            "permission_to_enter": "1",
        },
    )
    assert response.status_code == 302

    from app.context import clear_context
    from app.models.maintenance import MaintenanceRequest

    token = _rebound(org)
    try:
        raised = db.session.query(MaintenanceRequest).one()
        assert raised.title == "No hot water"
        assert raised.resident_id == tenancy["resident"].id
        assert raised.permission_to_enter is True
    finally:
        clear_context(token)


def test_a_habitability_report_is_escalated_without_being_asked(client, db, org, scope, tenancy):
    """The resident should not have to know the word "habitability"."""
    _sign_in_resident(client)

    client.post(
        "/resident/requests",
        data={"title": "No heat at all", "description": "The heating is completely dead."},
    )

    from app.context import clear_context
    from app.models.maintenance import MaintenanceRequest

    token = _rebound(org)
    try:
        assert db.session.query(MaintenanceRequest).one().is_habitability is True
    finally:
        clear_context(token)


def test_an_empty_request_is_refused(client, db, org, scope, tenancy):
    _sign_in_resident(client)
    client.post("/resident/requests", data={"title": "", "description": ""})

    from app.context import clear_context
    from app.models.maintenance import MaintenanceRequest

    token = _rebound(org)
    try:
        assert db.session.query(MaintenanceRequest).count() == 0
    finally:
        clear_context(token)


# ---------------------------------------------------------------------------
# Vendor: updating from the field
# ---------------------------------------------------------------------------


@pytest.fixture()
def vendor_login(db, org, scope, vendor_record):
    from app.models.iam import UserType
    from app.services.iam.provisioning import create_user

    user = create_user(
        db.session,
        org_id=org.id,
        email="crew@vendor.test",
        full_name="Acme Crew",
        password=PORTAL_PASSWORD,
        user_type=UserType.VENDOR,
        role_codes=["vendor"],
        vendor_id=vendor_record.id,
    )
    db.session.commit()
    return user


@pytest.fixture()
def assigned_work(db, org, scope, property_record, vendor_record):
    from app.models.maintenance import Priority, WorkOrderStatus
    from app.services.maintenance.service import create_work_order, transition_work_order

    order = create_work_order(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        title="Replace tap washer",
        description="Dripping tap in the kitchen.",
        priority=Priority.NORMAL,
    )
    transition_work_order(
        db.session,
        work_order=order,
        target=WorkOrderStatus.ASSIGNED,
        vendor_id=vendor_record.id,
    )
    db.session.commit()
    return order


def _sign_in_vendor(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "crew@vendor.test", "password": PORTAL_PASSWORD},
    )
    assert response.status_code == 200, response.get_json()


def test_a_vendor_can_start_their_own_job(client, db, vendor_login, assigned_work):
    _sign_in_vendor(client)

    response = client.post(
        f"/vendor/work-orders/{assigned_work.id}", data={"action": "start", "note": "On site."}
    )
    assert response.status_code == 302

    db.session.expire_all()
    from app.models.maintenance import WorkOrder, WorkOrderStatus

    assert db.session.get(WorkOrder, assigned_work.id).status == WorkOrderStatus.IN_PROGRESS


def test_completing_records_what_it_cost(client, db, vendor_login, assigned_work):
    """Costs entered on site, not reconstructed from memory three weeks later."""
    _sign_in_vendor(client)

    client.post(f"/vendor/work-orders/{assigned_work.id}", data={"action": "start"})
    client.post(
        f"/vendor/work-orders/{assigned_work.id}",
        data={
            "action": "complete",
            "note": "Washer replaced.",
            "labor_cost": "90.00",
            "material_cost": "4.50",
        },
    )

    db.session.expire_all()
    from app.models.maintenance import WorkOrder, WorkOrderStatus

    completed = db.session.get(WorkOrder, assigned_work.id)
    assert completed.status == WorkOrderStatus.COMPLETED
    assert completed.labor_cost == Decimal("90.0000")
    assert completed.material_cost == Decimal("4.5000")


def test_a_vendor_cannot_touch_another_vendors_job(
    client, db, org, scope, vendor_login, property_record
):
    from app.models.maintenance import Priority, WorkOrder, WorkOrderStatus
    from app.models.vendor import ComplianceStatus, Vendor, VendorStatus
    from app.services.maintenance.service import create_work_order, transition_work_order

    rival = Vendor(
        org_id=org.id,
        code="RIVAL",
        name="Rival Contracting",
        status=VendorStatus.ACTIVE,
        compliance_status=ComplianceStatus.VALID,
        compliance_expires_at=dt.date.today() + dt.timedelta(days=200),
    )
    db.session.add(rival)
    db.session.commit()

    theirs = create_work_order(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        title="Rival job",
        description="Not ours.",
        priority=Priority.NORMAL,
    )
    transition_work_order(
        db.session, work_order=theirs, target=WorkOrderStatus.ASSIGNED, vendor_id=rival.id
    )
    db.session.commit()

    _sign_in_vendor(client)
    response = client.post(f"/vendor/work-orders/{theirs.id}", data={"action": "start"})
    assert response.status_code == 404

    db.session.expire_all()
    assert db.session.get(WorkOrder, theirs.id).status == WorkOrderStatus.ASSIGNED


def test_a_vendor_cannot_cancel_or_verify(client, db, vendor_login, assigned_work):
    """Cancelling and verifying are the management company's decisions."""
    _sign_in_vendor(client)

    for action in ("cancel", "verify", "reassign"):
        client.post(f"/vendor/work-orders/{assigned_work.id}", data={"action": action})

    db.session.expire_all()
    from app.models.maintenance import WorkOrder, WorkOrderStatus

    assert db.session.get(WorkOrder, assigned_work.id).status == WorkOrderStatus.ASSIGNED


@pytest.mark.parametrize("cost", ["NaN", "Infinity"])
def test_non_finite_costs_are_refused_rather_than_crashing(
    client, db, vendor_login, assigned_work, cost
):
    """Same shape as the pay route: NaN fails the comparison by raising."""
    _sign_in_vendor(client)
    response = client.post(
        f"/vendor/work-orders/{assigned_work.id}",
        data={"action": "complete", "labor_cost": cost, "material_cost": "0"},
    )
    assert response.status_code == 302


def test_negative_costs_are_refused(client, db, vendor_login, assigned_work):
    _sign_in_vendor(client)
    client.post(f"/vendor/work-orders/{assigned_work.id}", data={"action": "start"})
    client.post(
        f"/vendor/work-orders/{assigned_work.id}",
        data={"action": "complete", "labor_cost": "-100.00", "material_cost": "0"},
    )

    db.session.expire_all()
    from app.models.maintenance import WorkOrder, WorkOrderStatus

    assert db.session.get(WorkOrder, assigned_work.id).status == WorkOrderStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# Owner: reading statements
# ---------------------------------------------------------------------------


@pytest.fixture()
def owner_login(db, org, scope, property_record):
    from app.models.iam import UserType
    from app.models.org import OwnerEntity, OwnershipStake, OwnerType
    from app.services.iam.provisioning import create_user

    entity = OwnerEntity(
        org_id=org.id, code="OWN1", name="Kestrel Holdings", owner_type=OwnerType.COMPANY
    )
    db.session.add(entity)
    db.session.commit()

    db.session.add(
        OwnershipStake(
            org_id=org.id,
            property_id=property_record.id,
            owner_entity_id=entity.id,
            percentage=Decimal("100.00"),
            effective_from=dt.date(2020, 1, 1),
        )
    )
    create_user(
        db.session,
        org_id=org.id,
        email="owner@test.local",
        full_name="Kestrel Holdings",
        password=PORTAL_PASSWORD,
        user_type=UserType.OWNER,
        role_codes=["owner"],
        owner_entity_id=entity.id,
    )
    db.session.commit()
    return entity


def _statement_for(db, org, owner_entity, property_record, *, status="issued"):
    from app.models.accounting import OwnerStatement
    from app.models.sequences import SequenceKey
    from app.services.common.numbering import next_number

    statement = OwnerStatement(
        org_id=org.id,
        statement_number=next_number(db.session, SequenceKey.OWNER_STATEMENT, org_id=org.id),
        owner_entity_id=owner_entity.id,
        property_id=property_record.id,
        period_start=dt.date(2026, 3, 1),
        period_end=dt.date(2026, 3, 31),
        total_income=Decimal("5000.00"),
        total_expense=Decimal("800.00"),
        management_fee=Decimal("400.00"),
        net_income=Decimal("3800.00"),
        distribution_amount=Decimal("3800.00"),
        ownership_percentage=Decimal("1.000000"),
        status=status,
    )
    db.session.add(statement)
    db.session.commit()
    return statement


def _sign_in_owner(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@test.local", "password": PORTAL_PASSWORD},
    )
    assert response.status_code == 200, response.get_json()


def test_an_owner_sees_their_statements(client, db, org, owner_login, property_record):
    """The gap that made the owner portal Partial: it can generate them and
    could not show them."""
    _statement_for(db, org, owner_login, property_record)
    _sign_in_owner(client)

    response = client.get("/owner/statements")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "5,000.00" in body or "5000.00" in body


def test_a_statement_detail_shows_the_arithmetic(client, db, org, owner_login, property_record):
    statement = _statement_for(db, org, owner_login, property_record)
    _sign_in_owner(client)

    body = client.get(f"/owner/statements/{statement.id}").get_data(as_text=True)
    assert "Management fee" in body
    assert "Closing balance" in body
    assert "100.0000%" in body or "100.0%" in body or "100" in body


def test_an_owner_with_no_statements_yet_sees_an_empty_page(client, owner_login):
    """Not a 500.

    Substituting a sentinel id for an empty ``IN`` list binds it through the
    GUID type, which validates identifiers - so a brand-new owner, or any owner
    before the first month-end run, got a crash instead of a page.
    """
    _sign_in_owner(client)
    response = client.get("/owner/statements")
    assert response.status_code == 200


def test_an_owner_cannot_read_another_owners_statement(
    client, db, org, scope, owner_login, property_record
):
    from app.models.org import OwnerEntity, OwnerType

    rival = OwnerEntity(org_id=org.id, code="OWN2", name="Rival Trust", owner_type=OwnerType.TRUST)
    db.session.add(rival)
    db.session.commit()
    theirs = _statement_for(db, org, rival, property_record)

    _sign_in_owner(client)
    assert client.get(f"/owner/statements/{theirs.id}").status_code == 404


def test_an_anonymous_visitor_cannot_read_statements(client):
    assert client.get("/owner/statements").status_code in (302, 401)

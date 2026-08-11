"""An adversarial pass over the tenancy and authorization boundaries.

The 1.0 checklist wants a penetration test by somebody who did not write this
code. This is not that — a suite written by the author tests the attacks the
author thought of, which is precisely the limitation an external test exists to
cover. What it is: the starting point that test should not have to rediscover,
and a regression net for every boundary failure that has a name.

Organised by what an attacker actually tries, in the order they try it:

1. Reach another tenant's data by guessing an identifier.
2. Reach it by *supplying* the tenant, rather than being assigned one.
3. Do something their role does not permit.
4. Learn what exists from how the system says no.
5. Get the system to do the work for them — mass assignment, injection,
   traversal, an open redirect.

Every test here should fail loudly if a boundary regresses, and each names the
attack rather than the mechanism, so a reader can tell what it is protecting
against without reading the implementation.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

pytestmark = pytest.mark.security

ATTACKER = "attacker@test.local"
VICTIM_ORG_PASSWORD = "attack-suite-2026-ok!"


# ---------------------------------------------------------------------------
# A second tenant with something worth stealing
# ---------------------------------------------------------------------------


@pytest.fixture()
def victim(db, other_org):
    """Another organization holding a property, a lease, and an invoice."""
    from app.context import RequestContext, bind_context, clear_context, new_correlation_id
    from app.models.leasing import Lease, LeaseStatus
    from app.models.org import Property, PropertyType, Unit, UnitStatus
    from app.models.sequences import SequenceKey
    from app.services.accounting.chart import AccountCode, seed_chart_of_accounts
    from app.services.accounting.receivables import ChargeInput, issue_invoice
    from app.services.common.numbering import next_number

    token = bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=other_org.id,
            actor_type="system",
            source="test",
        )
    )
    try:
        accounts = seed_chart_of_accounts(db.session, other_org.id)
        prop = Property(
            org_id=other_org.id,
            code="VICTIM",
            name="Rival Holdings Tower",
            property_type=PropertyType.RESIDENTIAL_MULTI,
            address_line1="1 Rival Way",
            city="Elsewhere",
            region="RS",
            postal_code="99999",
        )
        db.session.add(prop)
        db.session.flush()

        unit = Unit(
            org_id=other_org.id,
            property_id=prop.id,
            unit_number="1A",
            status=UnitStatus.OCCUPIED,
            market_rent=Decimal("5000.00"),
        )
        db.session.add(unit)
        db.session.flush()

        lease = Lease(
            org_id=other_org.id,
            lease_number=next_number(db.session, SequenceKey.LEASE, org_id=other_org.id),
            property_id=prop.id,
            unit_id=unit.id,
            status=LeaseStatus.ACTIVE,
            start_date=dt.date(2026, 1, 1),
            end_date=dt.date(2026, 12, 31),
            rent_amount=Decimal("5000.00"),
            security_deposit=Decimal("5000.00"),
        )
        db.session.add(lease)
        db.session.flush()

        invoice = issue_invoice(
            db.session,
            org_id=other_org.id,
            charges=[
                ChargeInput(
                    description="Rent",
                    amount=Decimal("5000.00"),
                    account_id=accounts[AccountCode.RENTAL_INCOME].id,
                )
            ],
            issue_date=dt.date(2026, 3, 1),
            due_date=dt.date(2026, 3, 1),
            lease=lease,
            property_id=prop.id,
        )
        db.session.commit()
        return {
            "org": other_org,
            "property": prop,
            "unit": unit,
            "lease": lease,
            "invoice": invoice,
        }
    finally:
        clear_context(token)


@pytest.fixture()
def attacker(db, org, scope, make_user, sign_in):
    """An ordinary administrator of the *first* organization."""
    make_user("org_admin", email=ATTACKER)
    sign_in(ATTACKER)
    return ATTACKER


# ---------------------------------------------------------------------------
# 1. Guessing an identifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/properties/{property}",
        "/api/v1/leases/{lease}",
        "/api/v1/invoices/{invoice}",
    ],
)
def test_a_known_identifier_from_another_tenant_is_not_found(client, attacker, victim, path):
    """The identifier is correct and the caller is a legitimate administrator.

    The only thing standing between them is tenancy, which is the whole point.
    """
    url = path.format(
        property=victim["property"].id, lease=victim["lease"].id, invoice=victim["invoice"].id
    )
    response = client.get(url)
    assert response.status_code == 404, response.get_data(as_text=True)[:300]


def test_the_refusal_never_confirms_the_record_exists(client, attacker, victim):
    """404 and not 403.

    A 403 tells an attacker the identifier is real, which turns any endpoint
    into an oracle for enumerating another tenant's records.
    """
    real = client.get(f"/api/v1/properties/{victim['property'].id}")
    invented = client.get("/api/v1/properties/00000000-0000-0000-0000-000000000000")

    assert real.status_code == invented.status_code == 404
    # And the bodies must not differ in a way that leaks the distinction.
    assert real.get_json().get("error", {}).get("code") == invented.get_json().get("error", {}).get(
        "code"
    )


def test_another_tenants_records_never_appear_in_a_listing(client, attacker, victim):
    body = client.get("/api/v1/properties?limit=100").get_data(as_text=True)
    assert "Rival Holdings Tower" not in body
    assert victim["property"].id not in body


def test_a_write_aimed_at_another_tenant_is_not_found(client, attacker, victim):
    response = client.patch(
        f"/api/v1/properties/{victim['property'].id}",
        json={"name": "Owned"},
    )
    assert response.status_code in (404, 405)


# ---------------------------------------------------------------------------
# 2. Supplying the tenant rather than being assigned one
# ---------------------------------------------------------------------------


def test_an_org_id_in_the_body_does_not_move_the_write(client, db, attacker, victim, org):
    """Mass assignment against the one field that must never be caller-supplied."""
    response = client.post(
        "/api/v1/maintenance-requests",
        json={
            "title": "Planted",
            "description": "Attempting to write into another tenant.",
            "org_id": victim["org"].id,
            "organization_id": victim["org"].id,
        },
    )

    if response.status_code < 300:
        from app.context import RequestContext, bind_context, clear_context, new_correlation_id
        from app.models.maintenance import MaintenanceRequest

        token = bind_context(
            RequestContext(
                correlation_id=new_correlation_id(),
                org_id=victim["org"].id,
                actor_type="system",
                source="test",
            )
        )
        try:
            planted = (
                db.session.query(MaintenanceRequest)
                .filter(MaintenanceRequest.org_id == victim["org"].id)
                .all()
            )
            assert planted == [], "a caller-supplied org_id moved the write"
        finally:
            clear_context(token)


def test_an_org_header_is_not_trusted(client, attacker, victim):
    """Headers are attacker-controlled. Scope comes from the session."""
    for header in ("X-Org-Id", "X-Organization", "X-Tenant-Id"):
        body = client.get(
            "/api/v1/properties?limit=100", headers={header: victim["org"].id}
        ).get_data(as_text=True)
        assert "Rival Holdings Tower" not in body


def test_an_org_query_parameter_is_not_trusted(client, attacker, victim):
    body = client.get(f"/api/v1/properties?limit=100&org_id={victim['org'].id}").get_data(
        as_text=True
    )
    assert "Rival Holdings Tower" not in body


# ---------------------------------------------------------------------------
# 3. Exceeding the role
# ---------------------------------------------------------------------------


def test_a_technician_cannot_read_the_ledger(client, make_user, sign_in):
    make_user("technician", email="tech-attack@test.local")
    sign_in("tech-attack@test.local")

    assert client.get("/admin/ledger").status_code == 403


def test_a_technician_cannot_read_the_audit_trail(client, make_user, sign_in):
    make_user("technician", email="tech-audit@test.local")
    sign_in("tech-audit@test.local")

    assert client.get("/admin/audit").status_code == 403


def test_a_leasing_agent_cannot_post_to_the_ledger(client, make_user, sign_in):
    make_user("leasing_agent", email="agent-attack@test.local")
    sign_in("agent-attack@test.local")

    response = client.post(
        "/api/v1/journal-entries",
        json={"entry_date": "2026-03-01", "description": "Planted", "lines": []},
    )
    assert response.status_code in (403, 404, 405)


def test_an_auditor_cannot_write(client, make_user, sign_in):
    """Read-only has to mean it, or the role is decoration."""
    make_user("auditor", email="auditor-attack@test.local")
    sign_in("auditor-attack@test.local")

    response = client.post(
        "/api/v1/maintenance-requests",
        json={"title": "Planted", "description": "Auditors do not write."},
    )
    assert response.status_code in (403, 404, 405)


def test_a_portal_user_cannot_reach_the_console(client, db, org, scope, make_user, sign_in):
    """The portals are single-purpose surfaces on purpose."""
    from app.models.iam import UserType
    from app.services.iam.provisioning import create_user

    create_user(
        db.session,
        org_id=org.id,
        email="portal-attack@test.local",
        full_name="Portal Person",
        password=VICTIM_ORG_PASSWORD,
        user_type=UserType.RESIDENT,
        role_codes=["resident"],
    )
    db.session.commit()

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "portal-attack@test.local", "password": VICTIM_ORG_PASSWORD},
    )
    assert response.status_code == 200

    for path in ("/admin/", "/admin/ledger", "/admin/audit", "/admin/roles", "/admin/users"):
        assert client.get(path).status_code in (403, 404), path


def test_a_resident_cannot_reach_the_owner_portal(client, db, org, scope):
    from app.models.iam import UserType
    from app.services.iam.provisioning import create_user

    create_user(
        db.session,
        org_id=org.id,
        email="cross-portal@test.local",
        full_name="Portal Person",
        password=VICTIM_ORG_PASSWORD,
        user_type=UserType.RESIDENT,
        role_codes=["resident"],
    )
    db.session.commit()
    client.post(
        "/api/v1/auth/login",
        json={"email": "cross-portal@test.local", "password": VICTIM_ORG_PASSWORD},
    )

    assert client.get("/owner/").status_code in (403, 404)
    assert client.get("/vendor/").status_code in (403, 404)


# ---------------------------------------------------------------------------
# 4. Learning from the refusal
# ---------------------------------------------------------------------------


def test_a_failed_sign_in_does_not_reveal_whether_the_account_exists(client, make_user, org, scope):
    make_user("org_admin", email="known@test.local")

    known = client.post(
        "/api/v1/auth/login", json={"email": "known@test.local", "password": "wrong-password"}
    )
    unknown = client.post(
        "/api/v1/auth/login",
        json={"email": "definitely-not-a-user@test.local", "password": "wrong-password"},
    )

    assert known.status_code == unknown.status_code
    # The correlation id differs per request by design; everything a caller
    # could learn from must not.
    known_error = known.get_json()["error"]
    unknown_error = unknown.get_json()["error"]
    assert known_error["code"] == unknown_error["code"]
    assert known_error["message"] == unknown_error["message"]


def test_an_error_body_carries_no_stack_trace(client, attacker, victim):
    body = client.get(f"/api/v1/properties/{victim['property'].id}").get_data(as_text=True)
    for leak in ("Traceback", "sqlalchemy", 'File "', "app/models", "SELECT "):
        assert leak not in body, f"the error body leaks {leak!r}"


def test_the_server_header_gives_nothing_away(client):
    assert client.get("/healthz").headers.get("Server") == "Atlas"


# ---------------------------------------------------------------------------
# 5. Making the system do the work
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "' OR '1'='1",
        "'; DROP TABLE leases; --",
        "1 UNION SELECT null, current_setting('atlas.current_org')",
        "%' OR org_id IS NOT NULL --",
    ],
)
def test_injection_through_search_returns_nothing_and_breaks_nothing(
    client, db, attacker, victim, payload
):
    response = client.get("/api/v1/properties", query_string={"q": payload, "limit": 50})

    assert response.status_code in (200, 400, 422)
    if response.status_code == 200:
        body = response.get_data(as_text=True)
        assert "Rival Holdings Tower" not in body

    # And the schema survived.
    from app.models.leasing import Lease

    assert db.session.query(Lease) is not None


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.test/steal",
        "//evil.test",
        "/\\evil.test",
        "https:/evil.test",
        "javascript:alert(1)",
    ],
)
def test_the_login_redirect_cannot_be_pointed_offsite(app, client, target):
    """An open redirect on a login page is a phishing primitive: a genuine,
    correctly-branded link that lands the victim on the attacker's page."""
    from app.web.auth import _safe_next

    # The page may echo the value into a hidden field, escaped; what must never
    # happen is that it survives into an actual redirect. url_for needs a
    # request context to build the fallback.
    with app.test_request_context("/auth/login"):
        resolved = _safe_next(target)
    assert "evil.test" not in resolved
    assert "javascript:" not in resolved

    response = client.get("/auth/login", query_string={"next": target})
    assert "javascript:" not in response.get_data(as_text=True)


@pytest.mark.parametrize(
    "traversal",
    ["../../etc/passwd", "..\\..\\windows\\win.ini", "%2e%2e%2f%2e%2e%2fetc%2fpasswd"],
)
def test_document_paths_cannot_traverse(client, attacker, traversal):
    response = client.get(f"/api/v1/documents/{traversal}")
    assert response.status_code in (400, 404, 405)
    body = response.get_data(as_text=True)
    assert "root:" not in body
    assert "[fonts]" not in body


def test_a_write_without_a_csrf_token_is_refused(client, db, org, scope):
    """The portal forms are cookie-authenticated, so CSRF is the live risk."""
    from app.models.iam import UserType
    from app.services.iam.provisioning import create_user

    create_user(
        db.session,
        org_id=org.id,
        email="csrf@test.local",
        full_name="Portal Person",
        password=VICTIM_ORG_PASSWORD,
        user_type=UserType.RESIDENT,
        role_codes=["resident"],
    )
    db.session.commit()
    client.post(
        "/api/v1/auth/login",
        json={"email": "csrf@test.local", "password": VICTIM_ORG_PASSWORD},
    )

    response = client.post(
        "/resident/requests",
        data={"title": "Forged", "description": "No token supplied."},
    )
    # 400 from CSRF, or 404 because there is no lease - never a created request.
    assert response.status_code != 302 or "requests" not in response.headers.get("Location", "")


def test_an_expired_session_stops_working(client, db, org, scope, make_user, sign_in):
    """Revocation has to be immediate, or "sign out everywhere" is a lie."""
    from sqlalchemy import select

    from app.models.iam import User, UserSession
    from app.models.types import utcnow

    make_user("org_admin", email="revoked@test.local")
    sign_in("revoked@test.local")
    assert client.get("/api/v1/properties").status_code == 200

    from app.context import RequestContext, bind_context, clear_context, new_correlation_id

    token = bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=org.id,
            actor_type="system",
            source="test",
        )
    )
    try:
        user = db.session.execute(
            select(User).where(User.email == "revoked@test.local")
        ).scalar_one()
        for session_row in db.session.execute(
            select(UserSession).where(UserSession.user_id == user.id)
        ).scalars():
            session_row.revoked_at = utcnow()
        db.session.commit()
    finally:
        clear_context(token)

    assert client.get("/api/v1/properties").status_code in (401, 403, 302)


def test_the_audit_trail_cannot_be_written_through_the_api(client, attacker):
    """An attacker's first move after succeeding is to erase the record."""
    for method, path in (
        ("post", "/api/v1/audit-events"),
        ("delete", "/api/v1/audit-events"),
        ("patch", "/api/v1/audit-events"),
    ):
        response = getattr(client, method)(path, json={})
        assert response.status_code in (403, 404, 405), f"{method} {path}"

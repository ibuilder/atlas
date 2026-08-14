"""The leasing funnel over the API.

The console covers a leasing agent taking an application by phone. This covers
the other half: a listings site or a partner portal driving the same funnel,
which is where the consent and decision rules have to hold just as firmly
because nobody is watching the screen.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

pytestmark = pytest.mark.integration

MOVE_IN = dt.date(2026, 9, 1)
START = dt.date(2026, 9, 1)
END = dt.date(2027, 8, 31)


@pytest.fixture()
def agent(db, org, scope, make_user, sign_in):
    make_user("property_manager", email="funnel@test.local")
    sign_in("funnel@test.local")
    return "funnel@test.local"


@pytest.fixture()
def opened(client, property_record, unit_record, agent):
    """A draft application with one applicant, created through the API."""
    response = client.post(
        "/api/v1/applications",
        json={
            "property_id": property_record.id,
            "unit_id": unit_record.id,
            "desired_move_in": MOVE_IN.isoformat(),
            "lease_term_months": 12,
            "quoted_rent": "2100.00",
            "application_fee": "50.00",
        },
    )
    assert response.status_code == 201, response.get_json()
    application_id = response.get_json()["id"]

    response = client.post(
        f"/api/v1/applications/{application_id}/applicants",
        json={
            "first_name": "Rosa",
            "last_name": "Villanueva",
            "email": "rosa@example.test",
            "monthly_income": "7400.00",
            "employer_name": "Meridian Labs",
        },
    )
    assert response.status_code == 201, response.get_json()
    return application_id, response.get_json()["id"]


def test_the_funnel_runs_end_to_end(client, db, org, opened):
    application_id, applicant_id = opened

    assert client.post(f"/api/v1/applicants/{applicant_id}/consent").status_code == 200
    assert client.post(f"/api/v1/applications/{application_id}/submit").status_code == 200

    response = client.post(
        f"/api/v1/applications/{application_id}/screenings",
        json={"applicant_id": applicant_id, "provider": "manual"},
    )
    assert response.status_code == 201, response.get_json()
    screening_id = response.get_json()["id"]

    assert (
        client.post(
            f"/api/v1/screenings/{screening_id}",
            json={
                "recommendation": "approve",
                "credit_score": 742,
                "verified_monthly_income": "7400.00",
            },
        ).status_code
        == 200
    )

    response = client.post(
        f"/api/v1/applications/{application_id}/decision",
        json={
            "decision": "approve",
            "reason": "Verified income is 3.5x the quoted rent and the report is clean.",
        },
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "approved"

    response = client.post(
        f"/api/v1/applications/{application_id}/lease",
        json={
            "start_date": START.isoformat(),
            "end_date": END.isoformat(),
            "rent_amount": "2100.00",
            "security_deposit": "2100.00",
        },
    )
    assert response.status_code == 201, response.get_json()
    assert Decimal(response.get_json()["rent_amount"]) == Decimal("2100.00")

    # And once it is a tenancy, it is past ruling on. A second decision here
    # would rewrite the basis of a lease somebody is already living under.
    again = client.post(
        f"/api/v1/applications/{application_id}/decision",
        json={"decision": "deny", "reason": "On reflection we have changed our minds."},
    )
    assert again.status_code in (409, 422)


def test_the_detail_endpoint_carries_the_assessment(client, opened):
    """A caller deciding without the assessment is deciding without the criteria."""
    application_id, _ = opened

    body = client.get(f"/api/v1/applications/{application_id}").get_json()
    assert body["assessment"]["recommendation"]
    assert "applicants" in body and len(body["applicants"]) == 1
    assert "screenings" in body


def test_consent_is_taken_from_the_connection_not_the_body(client, db, org, opened):
    """A caller-supplied address would make the evidence worth nothing."""
    from app.context import RequestContext, bind_context, clear_context, new_correlation_id
    from app.models.leasing import Applicant

    _, applicant_id = opened
    response = client.post(
        f"/api/v1/applicants/{applicant_id}/consent",
        json={"ip_address": "203.0.113.9", "consent_ip": "203.0.113.9"},
        environ_overrides={"REMOTE_ADDR": "198.51.100.77"},
    )
    assert response.status_code == 200

    db.session.expire_all()
    token = bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=org.id,
            actor_type="system",
            source="test",
        )
    )
    try:
        assert db.session.get(Applicant, applicant_id).screening_consent_ip == "198.51.100.77"
    finally:
        clear_context(token)


def test_a_screening_without_consent_is_refused(client, opened):
    """The service's rule, surfaced as a refusal rather than a 500."""
    application_id, applicant_id = opened
    client.post(f"/api/v1/applications/{application_id}/submit")

    response = client.post(
        f"/api/v1/applications/{application_id}/screenings",
        json={"applicant_id": applicant_id},
    )
    assert response.status_code in (409, 422)
    assert b"consent" in response.data.lower()


def test_a_decision_without_a_reason_is_rejected_at_the_schema(client, opened):
    application_id, _ = opened
    client.post(f"/api/v1/applications/{application_id}/submit")

    response = client.post(
        f"/api/v1/applications/{application_id}/decision",
        json={"decision": "deny"},
    )
    assert response.status_code == 422


def test_an_unapproved_application_cannot_become_a_lease(client, opened):
    application_id, _ = opened
    client.post(f"/api/v1/applications/{application_id}/submit")

    response = client.post(
        f"/api/v1/applications/{application_id}/lease",
        json={"start_date": START.isoformat(), "end_date": END.isoformat()},
    )
    assert response.status_code in (409, 422)


def test_a_lease_that_ends_before_it_starts_is_rejected(client, opened):
    application_id, _ = opened

    response = client.post(
        f"/api/v1/applications/{application_id}/lease",
        json={"start_date": END.isoformat(), "end_date": START.isoformat()},
    )
    assert response.status_code == 422


def test_an_applicant_from_another_application_is_not_screenable(
    client, db, org, property_record, opened
):
    """The applicant id is checked against *this* application, not just the tenant."""
    application_id, _ = opened

    other = client.post(
        "/api/v1/applications", json={"property_id": property_record.id}
    ).get_json()["id"]
    stranger = client.post(
        f"/api/v1/applications/{other}/applicants",
        json={"first_name": "Sam", "last_name": "Kettle"},
    ).get_json()["id"]

    client.post(f"/api/v1/applicants/{stranger}/consent")
    client.post(f"/api/v1/applications/{application_id}/submit")

    response = client.post(
        f"/api/v1/applications/{application_id}/screenings",
        json={"applicant_id": stranger},
    )
    assert response.status_code == 404


def test_a_withdrawn_application_cannot_be_decided(client, opened):
    application_id, _ = opened
    client.post(f"/api/v1/applications/{application_id}/submit")

    assert (
        client.post(
            f"/api/v1/applications/{application_id}/withdraw",
            json={"reason": "Took another place"},
        ).status_code
        == 200
    )
    response = client.post(
        f"/api/v1/applications/{application_id}/decision",
        json={"decision": "approve", "reason": "We would still have housed them."},
    )
    assert response.status_code in (409, 422)


def test_another_tenants_application_is_not_found(client, db, org, other_org, agent):
    from app.context import RequestContext, bind_context, clear_context, new_correlation_id
    from app.models.org import Property, PropertyType
    from app.services.leasing.applications import create_application

    token = bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=other_org.id,
            actor_type="system",
            source="test",
        )
    )
    try:
        prop = Property(
            org_id=other_org.id,
            code="RIV3",
            name="Rival Three",
            property_type=PropertyType.RESIDENTIAL_MULTI,
            address_line1="3 Rival Way",
            city="Elsewhere",
            region="RS",
            postal_code="99999",
        )
        db.session.add(prop)
        db.session.flush()
        theirs = create_application(db.session, org_id=other_org.id, property_id=prop.id)
        db.session.commit()
        theirs_id = theirs.id
    finally:
        clear_context(token)

    assert client.get(f"/api/v1/applications/{theirs_id}").status_code == 404
    assert client.post(f"/api/v1/applications/{theirs_id}/submit").status_code == 404


def test_the_list_filters_by_status_and_property(client, db, org, property_record, opened):
    application_id, _ = opened
    client.post(f"/api/v1/applications/{application_id}/submit")

    body = client.get("/api/v1/applications?status=submitted").get_json()
    assert [row["id"] for row in body["data"]] == [application_id]

    body = client.get("/api/v1/applications?status=denied").get_json()
    assert body["data"] == []

    body = client.get(f"/api/v1/applications?property_id={property_record.id}").get_json()
    assert application_id in [row["id"] for row in body["data"]]


def test_a_role_without_decide_cannot_decide(client, opened, make_user, sign_in):
    application_id, _ = opened
    client.post(f"/api/v1/applications/{application_id}/submit")

    make_user("leasing_agent", email="api-agent-only@test.local")
    sign_in("api-agent-only@test.local")

    response = client.post(
        f"/api/v1/applications/{application_id}/decision",
        json={"decision": "approve", "reason": "They seem lovely and I would like to."},
    )
    assert response.status_code == 403


def test_a_quoted_rent_below_zero_is_refused(client, property_record, agent):
    response = client.post(
        "/api/v1/applications",
        json={"property_id": property_record.id, "quoted_rent": "-1.00"},
    )
    assert response.status_code == 422

"""The leasing funnel, from the console.

Three things are worth guarding here and they are not ordinary CRUD:

Consent is read from the connection, never from the form. A screening ordered
without recorded consent is a statutory violation, and an address the operator
could type is not evidence that anybody agreed to anything.

A decision needs a reason on every outcome. On a denial that text *is* the
adverse-action notice; requiring it on approvals too is what stops the denials
beside it looking arbitrary.

And a conditional approval must name its conditions rather than quietly
degrading to a plain one - "approved subject to a guarantor" and "approved" are
different tenancies.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

pytestmark = pytest.mark.security

MOVE_IN = dt.date(2026, 6, 1)


def _rebound(org):
    """A tenant scope for reading after a request has run."""
    from app.context import RequestContext, bind_context, new_correlation_id

    return bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=org.id,
            actor_type="system",
            source="test",
        )
    )


@pytest.fixture()
def agent(db, org, scope, make_user, sign_in):
    """Holds APPLICATION_MANAGE, SCREENING_ORDER, and APPLICATION_DECIDE."""
    make_user("property_manager", email="leasing@test.local")
    sign_in("leasing@test.local")
    return "leasing@test.local"


@pytest.fixture()
def application(db, org, scope, property_record, unit_record):
    from app.services.leasing.applications import add_applicant, create_application

    record = create_application(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        unit_id=unit_record.id,
        desired_move_in=MOVE_IN,
        quoted_rent=Decimal("2000.00"),
    )
    add_applicant(
        db.session,
        application=record,
        first_name="Dana",
        last_name="Okonkwo",
        email="dana@example.test",
        monthly_income=Decimal("7000.00"),
    )
    db.session.commit()
    return record


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_the_funnel_lists_what_has_come_in(client, application, agent):
    response = client.get("/admin/applications")
    assert response.status_code == 200
    assert application.application_number.encode() in response.data


def test_the_detail_page_shows_the_assessment_and_the_consent_state(client, application, agent):
    response = client.get(f"/admin/applications/{application.id}")
    assert response.status_code == 200
    assert b"Dana" in response.data
    # No consent has been recorded, so the page must say so rather than
    # leaving the column ambiguous.
    assert b"Record consent" in response.data


def test_another_tenants_application_is_not_found(client, db, org, other_org, agent):
    """404, not 403: a 403 confirms the record exists to someone who cannot see it."""
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
            code="RIV2",
            name="Rival Two",
            property_type=PropertyType.RESIDENTIAL_MULTI,
            address_line1="2 Rival Way",
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

    assert client.get(f"/admin/applications/{theirs_id}").status_code == 404
    assert (
        client.post(
            f"/admin/applications/{theirs_id}/decision",
            data={"action": "approve", "reason": "Looks fine to me, thanks."},
        ).status_code
        == 404
    )


# ---------------------------------------------------------------------------
# Intake
# ---------------------------------------------------------------------------


def test_an_application_can_be_taken_from_the_console(client, db, org, property_record, agent):
    from app.context import clear_context
    from app.models.leasing import Application, ApplicationStatus

    response = client.post(
        "/admin/applications",
        data={
            "property_id": property_record.id,
            "desired_move_in": MOVE_IN.isoformat(),
            "lease_term_months": "12",
            "quoted_rent": "1850.00",
        },
    )
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        from sqlalchemy import select

        record = db.session.execute(
            select(Application).where(Application.org_id == org.id)
        ).scalar_one()
        assert record.status == ApplicationStatus.DRAFT
        assert record.quoted_rent == Decimal("1850.00")
    finally:
        clear_context(token)


@pytest.mark.parametrize("rent", ["NaN", "Infinity", "one thousand"])
def test_a_rent_that_is_not_a_number_is_refused_rather_than_crashing(
    client, property_record, agent, rent
):
    """NaN survives Decimal() and then raises on the service's own range check."""
    response = client.post(
        "/admin/applications",
        data={"property_id": property_record.id, "quoted_rent": rent},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"is not an amount" in response.data


def test_consent_records_the_connection_not_the_form(client, db, org, application, agent):
    """The submitter does not get to choose what the evidence says."""
    from app.context import clear_context
    from app.models.leasing import Applicant

    applicant_id = application.applicants[0].id
    response = client.post(
        f"/admin/applicants/{applicant_id}/consent",
        data={"ip_address": "203.0.113.9"},
        environ_overrides={"REMOTE_ADDR": "198.51.100.4"},
    )
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        reloaded = db.session.get(Applicant, applicant_id)
        assert reloaded.screening_consent_at is not None
        assert reloaded.screening_consent_ip == "198.51.100.4"
    finally:
        clear_context(token)


def test_a_draft_can_be_submitted(client, db, org, application, agent):
    from app.context import clear_context
    from app.models.leasing import Application, ApplicationStatus

    response = client.post(f"/admin/applications/{application.id}/submit")
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert db.session.get(Application, application.id).status != ApplicationStatus.DRAFT
    finally:
        clear_context(token)


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def test_a_decision_without_a_reason_is_refused(client, db, org, application, agent):
    from app.context import clear_context
    from app.models.leasing import Application

    client.post(f"/admin/applications/{application.id}/submit")
    response = client.post(
        f"/admin/applications/{application.id}/decision",
        data={"action": "deny", "reason": "   "},
        follow_redirects=True,
    )
    assert b"needs a reason" in response.data

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert not db.session.get(Application, application.id).is_decided
    finally:
        clear_context(token)


def test_a_conditional_approval_must_name_its_conditions(client, db, org, application, agent):
    """Otherwise it silently becomes an unconditional one."""
    from app.context import clear_context
    from app.models.leasing import Application

    client.post(f"/admin/applications/{application.id}/submit")
    response = client.post(
        f"/admin/applications/{application.id}/decision",
        data={
            "action": "approve_with_conditions",
            "reason": "Income verified at 3.5x the quoted rent.",
            "conditions": "  \n ",
        },
        follow_redirects=True,
    )
    assert b"what the conditions are" in response.data

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert not db.session.get(Application, application.id).is_decided
    finally:
        clear_context(token)


def test_an_approval_with_conditions_keeps_them(client, db, org, application, agent):
    from app.context import clear_context
    from app.models.leasing import Application, ApplicationStatus

    client.post(f"/admin/applications/{application.id}/submit")
    response = client.post(
        f"/admin/applications/{application.id}/decision",
        data={
            "action": "approve_with_conditions",
            "reason": "Income verified at 3.5x the quoted rent.",
            "conditions": "Guarantor required\nTwo months' deposit",
        },
    )
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        reloaded = db.session.get(Application, application.id)
        assert reloaded.status == ApplicationStatus.CONDITIONALLY_APPROVED
        assert reloaded.decision_conditions["conditions"] == [
            "Guarantor required",
            "Two months' deposit",
        ]
    finally:
        clear_context(token)


def test_a_denial_keeps_the_reason_it_will_have_to_answer_for(client, db, org, application, agent):
    from app.context import clear_context
    from app.models.leasing import Application, ApplicationStatus

    client.post(f"/admin/applications/{application.id}/submit")
    reason = "Verified income is 1.8x the rent, below the 3.0x threshold in force."
    response = client.post(
        f"/admin/applications/{application.id}/decision",
        data={"action": "deny", "reason": reason},
    )
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        reloaded = db.session.get(Application, application.id)
        assert reloaded.status == ApplicationStatus.DENIED
        assert reason in reloaded.decision_reason
    finally:
        clear_context(token)


def test_deciding_twice_is_refused_by_the_service(client, db, org, application, agent):
    """And the console shows the refusal rather than swallowing it."""
    client.post(f"/admin/applications/{application.id}/submit")
    client.post(
        f"/admin/applications/{application.id}/decision",
        data={"action": "deny", "reason": "Eviction within the last three years."},
    )
    response = client.post(
        f"/admin/applications/{application.id}/decision",
        data={"action": "approve", "reason": "On reflection, we would like to house them."},
        follow_redirects=True,
    )
    assert b"already" in response.data.lower()


# ---------------------------------------------------------------------------
# Who may
# ---------------------------------------------------------------------------


def test_a_role_without_decide_cannot_decide(client, db, org, application, make_user, sign_in):
    """Reading the funnel and ruling on it are deliberately separate grants."""
    make_user("leasing_agent", email="agent-only@test.local")
    sign_in("agent-only@test.local")

    response = client.post(
        f"/admin/applications/{application.id}/decision",
        data={"action": "approve", "reason": "They seem lovely and I would like to."},
    )
    assert response.status_code == 403


def test_an_anonymous_visitor_cannot_reach_the_funnel(client, application):
    assert client.get("/admin/applications").status_code in (302, 401)
    assert client.post(
        f"/admin/applications/{application.id}/decision",
        data={"action": "approve", "reason": "Nobody asked me but here we are."},
    ).status_code in (302, 401)

"""Issuing and retiring enquiry-form keys from the console.

The service refuses the dangerous things; these tests are about the surface not
quietly undoing that. Three in particular:

A key configures an anonymous write path into the CRM, so issuing one is gated
on managing integrations rather than on anything a leasing agent holds.

A property id from another tenant must answer 404, not a validation message. A
distinguishable refusal confirms the id was real, which is the enumeration
oracle the rest of the console is careful to avoid.

And the snippet has to be the iframe the service builds, from this deployment's
own host. A console that hand-rolled the markup would be a second place for the
embed contract to drift out of agreement with itself.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.security

ORIGIN = "https://www.maplecourt.example"


def _rebound(org):
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
def integrator(db, org, scope, make_user, sign_in):
    """Holds INTEGRATION_MANAGE."""
    make_user("org_admin", email="admin@test.local")
    sign_in("admin@test.local")
    return "admin@test.local"


@pytest.fixture()
def agent(db, org, scope, make_user, sign_in):
    """A leasing agent: works leads, does not configure public write surfaces."""
    make_user("leasing_agent", email="agent@test.local")
    sign_in("agent@test.local")
    return "agent@test.local"


def _forms(org):
    from app.extensions import db as _db
    from app.models.leasing import EmbedForm

    token = _rebound(org)
    try:
        return _db.session.query(EmbedForm).all()
    finally:
        from app.context import clear_context

        clear_context(token)


def test_an_administrator_can_publish_a_form(client, org, integrator, property_record):
    response = client.post(
        "/admin/embed-forms",
        data={
            "label": "Maple Court listing",
            "allowed_origins": f"{ORIGIN}/rentals\nhttps://blog.maplecourt.example",
            "property_id": property_record.id,
        },
        follow_redirects=True,
    )
    assert response.status_code == 200

    forms = _forms(org)
    assert len(forms) == 1
    # Pasted with a path; stored as the origin a browser actually sends.
    assert forms[0].allowed_origins == [ORIGIN, "https://blog.maplecourt.example"]
    assert forms[0].property_id == property_record.id


def test_a_leasing_agent_cannot_publish_one(client, org, agent):
    """Working leads and opening an anonymous write path are different jobs."""
    response = client.post(
        "/admin/embed-forms",
        data={"label": "Sneaky", "allowed_origins": ORIGIN},
    )
    assert response.status_code in (302, 403)
    assert _forms(org) == []


def test_a_form_without_origins_is_refused_with_a_reason(client, org, integrator):
    response = client.post(
        "/admin/embed-forms",
        data={"label": "Open to all", "allowed_origins": "   "},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert _forms(org) == []


def test_another_tenants_property_is_not_found(client, db, org, other_org, integrator):
    """404 rather than a validation error: a different answer for a real id
    belonging to somebody else is an enumeration oracle."""
    from app.context import clear_context
    from app.models.org import Property, PropertyType

    token = _rebound(other_org)
    try:
        theirs = Property(
            org_id=other_org.id,
            name="Rival Tower",
            code="RIV",
            property_type=PropertyType.RESIDENTIAL_MULTI,
            address_line1="1 Rival Way",
            city="Springfield",
            region="IL",
            postal_code="62701",
        )
        db.session.add(theirs)
        db.session.commit()
        foreign_id = theirs.id
    finally:
        clear_context(token)

    response = client.post(
        "/admin/embed-forms",
        data={"label": "Cross tenant", "allowed_origins": ORIGIN, "property_id": foreign_id},
    )
    assert response.status_code == 404
    assert _forms(org) == []


def test_the_listing_shows_the_snippet_the_service_builds(client, db, org, scope, integrator):
    """One source for the embed contract, not two.

    Asserted against the *escaped* markup on purpose. The snippet sits in a
    textarea, so Jinja escapes it and the browser hands back the original text
    when somebody copies it. Rendering it raw would inject a live iframe into
    the console instead of showing an operator what to paste.
    """
    from markupsafe import escape

    from app.services.leasing import embeds

    form = embeds.create_embed_form(
        db.session, org_id=org.id, label="Listing", allowed_origins=[ORIGIN]
    )
    db.session.commit()
    expected = embeds.snippet_for(form, base_url="http://localhost/")

    body = client.get("/admin/embed-forms").data.decode()
    assert str(escape(expected)) in body
    assert "<iframe" not in body, "the snippet is being rendered as live markup"


def test_the_public_key_is_shown_rather_than_masked(client, db, org, scope, integrator):
    """Unlike the SCIM token beside it. This key ships in a public page's
    source, and pretending otherwise teaches the wrong lesson about the one
    that is genuinely secret."""
    from app.services.leasing import embeds

    form = embeds.create_embed_form(
        db.session, org_id=org.id, label="Listing", allowed_origins=[ORIGIN]
    )
    db.session.commit()

    assert form.public_key in client.get("/admin/embed-forms").data.decode()


def test_a_form_can_be_paused_and_resumed(client, db, org, scope, integrator):
    from app.services.leasing import embeds

    form = embeds.create_embed_form(
        db.session, org_id=org.id, label="Listing", allowed_origins=[ORIGIN]
    )
    db.session.commit()

    client.post(f"/admin/embed-forms/{form.id}", data={"action": "pause"}, follow_redirects=True)
    assert client.get(f"/embed/f/{form.public_key}").status_code == 404

    client.post(f"/admin/embed-forms/{form.id}", data={"action": "resume"}, follow_redirects=True)
    assert client.get(f"/embed/f/{form.public_key}").status_code == 200


def test_revoking_from_the_console_is_terminal(client, db, org, scope, integrator):
    from app.services.leasing import embeds

    form = embeds.create_embed_form(
        db.session, org_id=org.id, label="Listing", allowed_origins=[ORIGIN]
    )
    db.session.commit()

    client.post(f"/admin/embed-forms/{form.id}", data={"action": "revoke"}, follow_redirects=True)
    assert client.get(f"/embed/f/{form.public_key}").status_code == 404

    # And resuming does not bring it back.
    client.post(f"/admin/embed-forms/{form.id}", data={"action": "resume"}, follow_redirects=True)
    assert client.get(f"/embed/f/{form.public_key}").status_code == 404


def test_another_tenants_form_cannot_be_revoked(client, db, org, other_org, scope, integrator):
    """The key belongs to somebody else; from here it does not exist."""
    from app.context import clear_context
    from app.services.leasing import embeds

    token = _rebound(other_org)
    try:
        theirs = embeds.create_embed_form(
            db.session, org_id=other_org.id, label="Theirs", allowed_origins=[ORIGIN]
        )
        db.session.commit()
        foreign_id, foreign_key = theirs.id, theirs.public_key
    finally:
        clear_context(token)

    assert client.post(f"/admin/embed-forms/{foreign_id}", data={"action": "revoke"}).status_code == 404
    # Still serving, which is the point: the refusal changed nothing.
    assert client.get(f"/embed/f/{foreign_key}").status_code == 200


def test_an_unknown_action_changes_nothing(client, db, org, scope, integrator):
    from app.services.leasing import embeds

    form = embeds.create_embed_form(
        db.session, org_id=org.id, label="Listing", allowed_origins=[ORIGIN]
    )
    db.session.commit()

    client.post(f"/admin/embed-forms/{form.id}", data={"action": "delete"}, follow_redirects=True)
    assert client.get(f"/embed/f/{form.public_key}").status_code == 200

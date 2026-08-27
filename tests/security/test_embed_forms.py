"""The embeddable enquiry form: Atlas's only anonymous write surface.

Everything else in the system refuses before it reads. This route reads first,
by design, so the checks that keep it from becoming an open relay into somebody
else's CRM are the whole feature rather than decoration around it.

Five properties are asserted here, and each has a specific failure in mind:

*The key decides the tenant.* A submission cannot name an organization, so
there is nothing to forge. The test is that the captured lead lands in the
key's organization and that the neighbouring one stays empty.

*An unknown key and a revoked key answer identically.* Anything else turns the
endpoint into an oracle for "does this operator use Atlas", answerable by
iterating.

*Framing is allow-listed.* A snippet lifted from one operator's page source
must not render on another site, and the global `X-Frame-Options: DENY` must
not survive onto this route - nor go missing from any other.

*Bots are dropped silently.* A rejected submission gets the same thank-you page
a person gets, because telling automation which control caught it is free
tuning for the next attempt.

*Nothing screening-grade is collected.* The form takes a name and a way to make
contact. Income, employment, and date of birth stay behind authentication, so a
cloned marketing page is never a route to them.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
import time

import pytest

pytestmark = pytest.mark.security

ORIGIN = "https://maplecourt.example.org"
OTHER_ORIGIN = "https://not-your-site.example.net"


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
def embed_form(db, org, scope, property_record):
    from app.services.leasing import embeds

    form = embeds.create_embed_form(
        db.session,
        org_id=org.id,
        label="Maple Court listing page",
        allowed_origins=[ORIGIN],
        property_id=property_record.id,
    )
    db.session.commit()
    return form


def _rendered_token(
    app,
    public_key,
    *,
    age_seconds: float = 30.0,
    salt="atlas.embed.render",
    origin: str | None = ORIGIN,
):
    """Mint the signed render marker the form would have carried.

    Built rather than scraped so a test can place it at an arbitrary age
    without sleeping, which is the only way to exercise the fill-time floor.

    ``origin`` is what the server recorded from the referrer when it rendered
    the form. It lives inside the signature precisely because the submission
    cannot be asked for it - see the module docstring.
    """
    from itsdangerous import URLSafeTimedSerializer

    serializer = URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=salt)
    return serializer.dumps({"k": public_key, "t": time.time() - age_seconds, "o": origin})


def _submission(app, public_key, **overrides):
    payload = {
        "first_name": "Dana",
        "last_name": "Whitfield",
        "email": "dana@example.com",
        "phone": "",
        "desired_move_in": "",
        "message": "Is the two-bed still available?",
        "rendered_at": _rendered_token(app, public_key),
    }
    payload.update(overrides)
    return payload


def _leads(org):
    from app.extensions import db as _db
    from app.models.leasing import Lead

    token = _rebound(org)
    try:
        return _db.session.query(Lead).all()
    finally:
        from app.context import clear_context

        clear_context(token)


# ------------------------------------------------------------------ rendering


def test_a_live_key_renders_the_form(client, embed_form):
    response = client.get(f"/embed/f/{embed_form.public_key}")
    assert response.status_code == 200
    assert b"Register your interest" in response.data


@pytest.mark.parametrize("key", ["pk_live_deadbeef", "nonsense", ""])
def test_an_unknown_key_is_not_found(client, embed_form, key):
    assert client.get(f"/embed/f/{key}").status_code == 404


def test_a_revoked_key_answers_exactly_like_an_unknown_one(client, db, org, scope, embed_form):
    """Otherwise the endpoint confirms which keys were ever real."""
    from app.services.leasing import embeds

    unknown = client.get("/embed/f/pk_live_neverexisted")

    embeds.revoke_embed_form(db.session, form=embed_form)
    db.session.commit()
    revoked = client.get(f"/embed/f/{embed_form.public_key}")

    assert revoked.status_code == unknown.status_code == 404


def test_a_disabled_key_stops_serving(client, db, org, scope, embed_form):
    from app.services.leasing import embeds

    embeds.update_embed_form(db.session, form=embed_form, enabled=False)
    db.session.commit()
    assert client.get(f"/embed/f/{embed_form.public_key}").status_code == 404


# -------------------------------------------------------------------- framing


def test_the_form_may_be_framed_only_by_its_own_origins(client, embed_form):
    response = client.get(f"/embed/f/{embed_form.public_key}")
    policy = response.headers["Content-Security-Policy"]

    assert f"frame-ancestors {ORIGIN}" in policy
    assert OTHER_ORIGIN not in policy


def test_the_embed_response_carries_no_x_frame_options(client, embed_form):
    """`DENY` and a frame-ancestors allowlist disagree, and which one a client
    honours depends on the client. The stale header is removed rather than left
    to precedence."""
    response = client.get(f"/embed/f/{embed_form.public_key}")
    assert "X-Frame-Options" not in response.headers


def test_every_other_route_still_refuses_framing(client):
    """The regression this feature could plausibly cause.

    Relaxing the global policy instead of overriding it per response would make
    the console clickjackable, and nothing about the embed working would say so.
    """
    response = client.get("/")
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in response.headers.get("Content-Security-Policy", "")


def test_the_form_declares_no_script_of_its_own(client, embed_form):
    """It has none, so saying so costs nothing and removes a whole class of
    injection from a page that renders inside somebody else's site."""
    policy = client.get(f"/embed/f/{embed_form.public_key}").headers["Content-Security-Policy"]
    assert "script-src 'none'" in policy


# ----------------------------------------------------------------- submission


def test_a_submission_becomes_a_lead_in_the_keys_organization(app, client, org, embed_form):
    response = client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(app, embed_form.public_key),
    )
    assert response.status_code == 200
    assert b"Thank you" in response.data

    leads = _leads(org)
    assert len(leads) == 1
    assert leads[0].first_name == "Dana"
    assert leads[0].source == "embed"
    assert leads[0].org_id == org.id


def test_a_lead_never_lands_in_a_neighbouring_organization(app, client, org, other_org, embed_form):
    """The key is the only thing that names a tenant, and it is not user input."""
    client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(app, embed_form.public_key),
    )
    assert len(_leads(org)) == 1
    assert _leads(other_org) == []


@pytest.mark.parametrize(
    ("label", "headers"),
    [
        # Firefox omits Origin on a same-origin POST.
        ("no Origin header at all", {}),
        # Chrome sends Atlas's own origin, because that is where the iframe
        # document lives. Never the embedding site.
        ("Atlas's own origin", {"Origin": "http://localhost"}),
    ],
)
def test_a_real_browser_submission_is_accepted(app, client, org, embed_form, label, headers):
    """The regression test for the bug this whole surface shipped with.

    The form is served from Atlas and posts back to Atlas, so the submission is
    same-origin and the `Origin` header is either absent or Atlas's own host.
    An earlier version compared that header against the *embedding site's*
    allowlist, which no browser ever satisfies, so every genuine enquiry
    returned 404 while the suite stayed green - because the tests forged a
    header no browser sends.

    Both shapes below are what really arrives, and both must capture a lead.
    """
    response = client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(app, embed_form.public_key),
        headers=headers,
    )
    assert response.status_code == 200, f"{label} was rejected"
    assert len(_leads(org)) == 1, f"{label} captured no lead"


def test_a_form_rendered_somewhere_unlisted_is_dropped(app, client, org, embed_form):
    """The origin check that can actually mean something.

    Taken from the referrer when the form was rendered and sealed into the
    signed token, so it describes where the form really was rather than what
    the submitter claims. `frame-ancestors` should have refused this already;
    this is the server-side backstop for a client that ignored it.
    """
    response = client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(
            app,
            embed_form.public_key,
            rendered_at=_rendered_token(app, embed_form.public_key, origin=OTHER_ORIGIN),
        ),
    )
    assert response.status_code == 200
    assert _leads(org) == []


def test_a_withheld_referrer_still_captures_the_lead(app, client, org, embed_form):
    """Privacy tooling and referrer policies suppress it routinely. Attribution
    is worth having and not worth losing an enquiry over."""
    client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(
            app,
            embed_form.public_key,
            rendered_at=_rendered_token(app, embed_form.public_key, origin=None),
        ),
    )
    leads = _leads(org)
    assert len(leads) == 1
    # Falls back to the form's label rather than recording nothing.
    assert leads[0].source_detail == "Maple Court listing page"


def test_the_recorded_origin_cannot_be_asserted_by_the_submitter(app, client, org, embed_form):
    """A form field named after it must not be believed."""
    client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(app, embed_form.public_key, origin=OTHER_ORIGIN),
    )
    leads = _leads(org)
    assert len(leads) == 1
    assert leads[0].source_detail == ORIGIN


def test_a_missing_contact_route_is_refused_with_a_reason(app, client, org, embed_form):
    response = client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(app, embed_form.public_key, email="", phone=""),
    )
    assert response.status_code == 400
    assert b"email address or a phone number" in response.data
    assert _leads(org) == []


def test_a_nameless_submission_is_refused(app, client, org, embed_form):
    response = client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(app, embed_form.public_key, first_name=""),
    )
    assert response.status_code == 400
    assert _leads(org) == []


# ----------------------------------------------------------- automation traps


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("honeypot", {"company_website": "https://spam.example"}),
        ("no token at all", {"rendered_at": ""}),
        ("token that was never signed", {"rendered_at": "not.a.real.token"}),
    ],
)
def test_an_automated_submission_is_dropped(app, client, org, embed_form, label, overrides):
    response = client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(app, embed_form.public_key, **overrides),
    )
    # Same page a person gets. A bot that learns it was caught is a bot whose
    # author tunes around the control next time.
    assert response.status_code == 200
    assert b"Thank you" in response.data
    assert _leads(org) == [], f"{label} produced a lead"


def test_a_submission_faster_than_a_human_is_dropped(app, client, org, embed_form):
    fresh = _rendered_token(app, embed_form.public_key, age_seconds=0.0)
    response = client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(app, embed_form.public_key, rendered_at=fresh),
    )
    assert response.status_code == 200
    assert _leads(org) == []


def test_a_token_minted_for_another_form_is_dropped(app, client, db, org, scope, embed_form):
    """Otherwise one rendered form supplies tokens for every other key."""
    from app.services.leasing import embeds

    second = embeds.create_embed_form(
        db.session, org_id=org.id, label="Other page", allowed_origins=[ORIGIN]
    )
    db.session.commit()

    response = client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(
            app, embed_form.public_key, rendered_at=_rendered_token(app, second.public_key)
        ),
    )
    assert response.status_code == 200
    assert _leads(org) == []


def test_a_token_signed_with_the_wrong_salt_is_dropped(app, client, org, embed_form):
    """The salt scopes the signature to this purpose. Without it any other
    signed value in the application would be accepted here."""
    forged = _rendered_token(app, embed_form.public_key, salt="atlas.something.else")
    response = client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(app, embed_form.public_key, rendered_at=forged),
    )
    assert response.status_code == 200
    assert _leads(org) == []


# ------------------------------------------------------------- what it stores


def test_the_form_asks_for_nothing_screening_grade(client, embed_form):
    """The compliance argument for a public form, kept honest by a test.

    If a later change adds an income or date-of-birth field here, it puts
    regulated data behind an anonymous endpoint and this should fail first.
    """
    body = client.get(f"/embed/f/{embed_form.public_key}").data.lower()
    for forbidden in (b"ssn", b"social security", b"date of birth", b"income", b"employer"):
        assert forbidden not in body, f"the public form collects {forbidden!r}"


def test_the_captured_lead_records_where_it_came_from(app, client, org, embed_form):
    """An operator deciding where to spend needs to know which page produced it."""
    client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(app, embed_form.public_key),
    )
    lead = _leads(org)[0]
    assert lead.source_detail == ORIGIN
    assert lead.attributes["embed_form_id"] == embed_form.id


def test_the_property_comes_from_the_key_not_the_submission(
    app, client, org, embed_form, property_record
):
    """A key scoped to a property pins every lead to it, so a crafted post
    cannot file an enquiry against a property the form does not advertise."""
    client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(app, embed_form.public_key, property_id="anything-at-all"),
    )
    assert _leads(org)[0].property_id == property_record.id


def test_a_submission_advances_the_forms_counters(app, client, db, org, scope, embed_form):
    client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(app, embed_form.public_key),
    )
    db.session.refresh(embed_form)
    assert embed_form.submission_count == 1
    assert embed_form.last_submission_at is not None


def test_an_overlong_field_is_refused_rather_than_truncated(app, client, org, embed_form):
    """Silently cutting a phone number to fit stores a lead nobody can call."""
    response = client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(app, embed_form.public_key, phone="9" * 200),
    )
    assert response.status_code == 400
    assert _leads(org) == []


# --------------------------------------------------------------- key lifecycle


def test_a_key_cannot_be_issued_without_an_origin(db, org, scope):
    """An empty allowlist meaning "anybody" is how an embed becomes a relay."""
    from app.errors import ValidationFailed
    from app.services.leasing import embeds

    with pytest.raises(ValidationFailed, match="allowed origin"):
        embeds.create_embed_form(db.session, org_id=org.id, label="Open", allowed_origins=[])


@pytest.mark.parametrize(
    ("pasted", "expected"),
    [
        ("https://Example.com/rent/", "https://example.com"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("https://example.com?utm=1", "https://example.com"),
    ],
)
def test_a_pasted_url_is_reduced_to_the_origin_a_browser_sends(pasted, expected):
    """Operators paste what is in the address bar; browsers send only the
    origin. An allowlist storing the pasted form never matches."""
    from app.services.leasing import embeds

    assert embeds.normalize_origin(pasted) == expected


@pytest.mark.parametrize("bad", ["example.com", "", "   ", "javascript:alert(1)"])
def test_an_unusable_origin_is_refused(bad):
    from app.errors import ValidationFailed
    from app.services.leasing import embeds

    with pytest.raises(ValidationFailed):
        embeds.normalize_origin(bad)


def test_revocation_is_terminal(db, org, scope, embed_form):
    """The snippet is pasted into a page the operator may no longer control,
    so a leaked key must not be resurrectable."""
    from app.errors import ValidationFailed
    from app.services.leasing import embeds

    embeds.revoke_embed_form(db.session, form=embed_form)
    db.session.commit()

    with pytest.raises(ValidationFailed, match="revoked"):
        embeds.update_embed_form(db.session, form=embed_form, enabled=True)


def test_the_snippet_is_an_iframe_pointing_at_atlas(embed_form):
    """A script tag would render the form into the operator's DOM, where their
    own XSS could read what an applicant is typing."""
    from app.services.leasing import embeds

    snippet = embeds.snippet_for(embed_form, base_url="https://atlas.example.com/")
    assert snippet.startswith("<iframe")
    assert f"https://atlas.example.com/embed/f/{embed_form.public_key}" in snippet
    assert "<script" not in snippet


def test_the_public_key_is_marked_as_publishable(embed_form):
    """Somebody grepping their own repository for leaked credentials should be
    able to tell at a glance that this one was always meant to be readable."""
    assert embed_form.public_key.startswith("pk_live_")


def test_two_keys_never_collide(db, org, scope):
    from app.services.leasing import embeds

    keys = {
        embeds.create_embed_form(
            db.session, org_id=org.id, label=f"Page {n}", allowed_origins=[ORIGIN]
        ).public_key
        for n in range(25)
    }
    assert len(keys) == 25


def test_a_desired_move_in_date_survives_the_round_trip(app, client, org, embed_form):
    client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(app, embed_form.public_key, desired_move_in="2026-09-01"),
    )
    assert _leads(org)[0].desired_move_in == dt.date(2026, 9, 1)


def test_an_unparseable_date_does_not_cost_the_lead(app, client, org, embed_form):
    """A move-in date is a nicety. Refusing an enquiry over it loses a customer
    to a date picker disagreement."""
    client.post(
        f"/embed/f/{embed_form.public_key}",
        data=_submission(app, embed_form.public_key, desired_move_in="next spring"),
    )
    leads = _leads(org)
    assert len(leads) == 1
    assert leads[0].desired_move_in is None


def test_a_property_from_another_tenant_is_refused(db, org, other_org, scope):
    """The hazard a docstring used to guard.

    PostgreSQL does not apply row-level security to foreign-key checks, so a
    reference to somebody else's property would insert cleanly rather than fail
    closed - and the ORM guard would not object either, because the write is
    correctly scoped to *this* organization. The id has to be verified, not
    trusted, even though no caller passes one today.
    """
    from app.context import clear_context
    from app.errors import NotFound
    from app.models.org import Property, PropertyType
    from app.services.leasing import embeds

    token = _rebound(other_org)
    try:
        theirs = Property(
            org_id=other_org.id,
            name="Rival Tower",
            code="RIV2",
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

    # A portfolio-wide key: nothing pins the property, so the argument is used.
    portfolio_key = embeds.create_embed_form(
        db.session, org_id=org.id, label="Portfolio", allowed_origins=[ORIGIN]
    )
    db.session.commit()

    with pytest.raises(NotFound):
        embeds.capture_lead(
            db.session,
            form=portfolio_key,
            first_name="Dana",
            email="dana@example.com",
            property_id=foreign_id,
        )


def test_a_property_from_this_tenant_is_accepted(db, org, scope, property_record):
    """The other half: the check must not refuse a legitimate id."""
    from app.services.leasing import embeds

    portfolio_key = embeds.create_embed_form(
        db.session, org_id=org.id, label="Portfolio", allowed_origins=[ORIGIN]
    )
    db.session.commit()

    lead = embeds.capture_lead(
        db.session,
        form=portfolio_key,
        first_name="Dana",
        email="dana@example.com",
        property_id=property_record.id,
    )
    assert lead.property_id == property_record.id

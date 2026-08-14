"""SCIM 2.0 over HTTP, and the credential the directory presents.

The value of SCIM is offboarding, and the thing that has to hold is that a
`PATCH active: false` disables the account *and revokes its sessions*. Marking a
user inactive while leaving a live session token is offboarding that does not
offboard.

The transport carries its own weight here. The tenant comes from the token and
nothing else: a directory that could name its own tenant could deactivate a
different company's staff.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.security

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


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
def provider(db, org, scope):
    from app.models.sso import IdentityProvider, SsoProtocol

    record = IdentityProvider(
        org_id=org.id,
        code="acme-okta",
        name="Acme Okta",
        protocol=SsoProtocol.OIDC,
        is_active=True,
        scim_enabled=True,
        jit_provisioning=True,
        allowed_email_domains=["acme.test"],
        default_role_code="auditor",
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture()
def scim_token(db, org, scope, provider):
    from app.services.iam.scim import issue_scim_token

    token = issue_scim_token(db.session, provider=provider)
    db.session.commit()
    return token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _new_user(username: str = "dana@acme.test") -> dict:
    return {
        "schemas": [USER_SCHEMA],
        "userName": username,
        "name": {"givenName": "Dana", "familyName": "Okonkwo"},
        "emails": [{"value": username, "primary": True}],
        "active": True,
    }


# ---------------------------------------------------------------------------
# The credential
# ---------------------------------------------------------------------------


def test_the_token_is_stored_hashed_and_never_recoverable(db, org, scope, provider):
    from app.security.crypto import hash_token
    from app.services.iam.scim import issue_scim_token

    token = issue_scim_token(db.session, provider=provider)
    db.session.commit()

    assert provider.scim_token_hash == hash_token(token)
    assert token not in (provider.scim_token_hash or "")
    assert provider.scim_token_fingerprint


def test_a_token_cannot_be_issued_for_a_disabled_integration(db, org, scope, provider):
    """A live credential for an integration nobody watches is the worst kind."""
    from app.errors import ValidationFailed
    from app.services.iam.scim import issue_scim_token

    provider.scim_enabled = False
    db.session.commit()

    with pytest.raises(ValidationFailed):
        issue_scim_token(db.session, provider=provider)


def test_no_token_is_401_not_403(client):
    """A directory retries a 5xx and gives up on a 401. Both matter."""
    response = client.get("/scim/v2/Users")
    assert response.status_code == 401
    assert response.get_json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]


def test_an_unrecognised_token_is_refused(client):
    response = client.get("/scim/v2/Users", headers=_auth("scim_nonsense"))
    assert response.status_code == 401


def test_a_revoked_token_stops_working(client, db, org, scope, provider, scim_token):
    from app.services.iam.scim import revoke_scim_token

    assert client.get("/scim/v2/Users", headers=_auth(scim_token)).status_code == 200

    revoke_scim_token(db.session, provider=provider)
    db.session.commit()

    assert client.get("/scim/v2/Users", headers=_auth(scim_token)).status_code == 401


def test_turning_scim_off_stops_the_token_working(client, db, org, scope, provider, scim_token):
    """A credential that outlives the integration is the one nobody notices."""
    provider.scim_enabled = False
    db.session.commit()

    assert client.get("/scim/v2/Users", headers=_auth(scim_token)).status_code == 401


def test_an_inactive_provider_cannot_call(client, db, org, scope, provider, scim_token):
    provider.is_active = False
    db.session.commit()

    assert client.get("/scim/v2/Users", headers=_auth(scim_token)).status_code == 401


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------


def test_the_service_config_says_what_is_actually_supported(client, scim_token):
    """Claiming a capability here that the endpoints lack fails a sync halfway."""
    body = client.get("/scim/v2/ServiceProviderConfig", headers=_auth(scim_token)).get_json()
    assert body["patch"]["supported"] is True
    assert body["bulk"]["supported"] is False
    assert body["sort"]["supported"] is False


def test_a_user_is_provisioned_and_then_found_by_username(client, db, org, scim_token):
    response = client.post("/scim/v2/Users", json=_new_user(), headers=_auth(scim_token))
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    assert body["userName"] == "dana@acme.test"
    assert body["active"] is True
    assert response.headers["Location"].endswith(body["id"])

    listed = client.get(
        '/scim/v2/Users?filter=userName eq "dana@acme.test"', headers=_auth(scim_token)
    ).get_json()
    assert listed["totalResults"] == 1
    assert listed["Resources"][0]["id"] == body["id"]


def test_a_filter_we_do_not_understand_is_refused_not_ignored(client, scim_token):
    """Returning everybody to a query meant to match one person is how a sync
    decides to deactivate the entire company."""
    response = client.get('/scim/v2/Users?filter=displayName co "a"', headers=_auth(scim_token))
    assert response.status_code == 400
    assert response.get_json()["scimType"] == "invalidValue"


def test_deactivation_revokes_live_sessions(client, db, org, scope, provider, scim_token):
    """The single most common way this integration is got wrong."""
    from sqlalchemy import select

    from app.context import clear_context
    from app.models.iam import User, UserSession, UserStatus

    created = client.post("/scim/v2/Users", json=_new_user(), headers=_auth(scim_token)).get_json()

    token_ctx = _rebound(org)
    try:
        from app.services.iam.session_service import create_user_session

        # No test_request_context: pushing one runs the app's teardown, which
        # clears the tenant binding this block depends on. create_user_session
        # already guards its use of `request`.
        user = db.session.get(User, created["id"])
        create_user_session(user, session=db.session)
        db.session.commit()
        live = (
            db.session.execute(select(UserSession).where(UserSession.user_id == user.id))
            .scalars()
            .all()
        )
        assert len(live) == 1
    finally:
        clear_context(token_ctx)

    response = client.patch(
        f"/scim/v2/Users/{created['id']}",
        json={
            "schemas": [PATCH_SCHEMA],
            "Operations": [{"op": "replace", "path": "active", "value": False}],
        },
        headers=_auth(scim_token),
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["active"] is False

    db.session.expire_all()
    token_ctx = _rebound(org)
    try:
        assert db.session.get(User, created["id"]).status != UserStatus.ACTIVE
        remaining = (
            db.session.execute(
                select(UserSession).where(
                    UserSession.user_id == created["id"], UserSession.revoked_at.is_(None)
                )
            )
            .scalars()
            .all()
        )
        assert remaining == []
    finally:
        clear_context(token_ctx)


def test_delete_deactivates_rather_than_removing_the_row(client, db, org, scim_token):
    """A user id appears on ledger entries, audit events, and approvals."""
    from app.context import clear_context
    from app.models.iam import User, UserStatus

    created = client.post(
        "/scim/v2/Users", json=_new_user("sam@acme.test"), headers=_auth(scim_token)
    ).get_json()

    assert (
        client.delete(f"/scim/v2/Users/{created['id']}", headers=_auth(scim_token)).status_code
        == 204
    )

    db.session.expire_all()
    token_ctx = _rebound(org)
    try:
        user = db.session.get(User, created["id"])
        assert user is not None
        assert user.status != UserStatus.ACTIVE
    finally:
        clear_context(token_ctx)


def test_a_provider_cannot_mint_an_account_outside_its_domains(client, scim_token):
    """A provider for acme.com must not reach a rival tenant's domain."""
    response = client.post(
        "/scim/v2/Users", json=_new_user("stranger@rival.test"), headers=_auth(scim_token)
    )
    assert response.status_code in (400, 403)


def test_a_missing_user_is_a_scim_shaped_404(client, scim_token):
    response = client.get(
        "/scim/v2/Users/01a00000-0000-7000-8000-000000000000", headers=_auth(scim_token)
    )
    assert response.status_code == 404
    assert response.get_json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]


def test_one_tenants_token_cannot_see_another_tenants_users(
    client, db, org, other_org, scope, scim_token
):
    """The tenant comes from the token, and from nothing in the request."""
    from app.context import RequestContext, bind_context, clear_context, new_correlation_id
    from app.services.iam.provisioning import create_user

    token_ctx = bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=other_org.id,
            actor_type="system",
            source="test",
        )
    )
    try:
        stranger = create_user(
            db.session,
            org_id=other_org.id,
            email="theirs@rival.test",
            full_name="Their Person",
            password="a-very-long-demo-password-2026",
            role_codes=["auditor"],
        )
        db.session.commit()
        stranger_id = stranger.id
    finally:
        clear_context(token_ctx)

    assert client.get(f"/scim/v2/Users/{stranger_id}", headers=_auth(scim_token)).status_code == 404

    listed = client.get("/scim/v2/Users", headers=_auth(scim_token)).get_json()
    assert stranger_id not in [row["id"] for row in listed["Resources"]]


def test_a_body_that_is_not_an_object_is_refused_in_scims_envelope(client, scim_token):
    response = client.post(
        "/scim/v2/Users",
        data="not json",
        content_type="application/json",
        headers=_auth(scim_token),
    )
    assert response.status_code == 400
    assert response.get_json()["scimType"] == "invalidSyntax"


# ---------------------------------------------------------------------------
# The console
# ---------------------------------------------------------------------------


def test_the_console_shows_the_token_exactly_once(client, db, org, provider, make_user, sign_in):
    make_user("org_admin", email="sso-admin@test.local")
    sign_in("sso-admin@test.local")

    response = client.post(
        f"/admin/identity-providers/{provider.id}/scim-token",
        data={"action": "issue"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"scim_" in response.data
    assert b"Shown once" in response.data

    # Reloading must not show it again: it is not stored anywhere recoverable.
    again = client.get("/admin/identity-providers")
    assert b"Your SCIM token" not in again.data


def test_an_auditor_cannot_issue_a_token(client, db, org, provider, make_user, sign_in):
    make_user("auditor", email="sso-readonly@test.local")
    sign_in("sso-readonly@test.local")

    response = client.post(
        f"/admin/identity-providers/{provider.id}/scim-token", data={"action": "issue"}
    )
    assert response.status_code == 403


def test_another_tenants_provider_is_not_found(client, db, org, other_org, make_user, sign_in):
    from app.context import RequestContext, bind_context, clear_context, new_correlation_id
    from app.models.sso import IdentityProvider, SsoProtocol

    token_ctx = bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=other_org.id,
            actor_type="system",
            source="test",
        )
    )
    try:
        theirs = IdentityProvider(
            org_id=other_org.id,
            code="rival-idp",
            name="Rival IdP",
            protocol=SsoProtocol.OIDC,
            scim_enabled=True,
        )
        db.session.add(theirs)
        db.session.commit()
        theirs_id = theirs.id
    finally:
        clear_context(token_ctx)

    make_user("org_admin", email="sso-admin2@test.local")
    sign_in("sso-admin2@test.local")

    response = client.post(
        f"/admin/identity-providers/{theirs_id}/scim-token", data={"action": "issue"}
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# SSO login
# ---------------------------------------------------------------------------


def test_an_unknown_provider_code_is_404_not_a_hint(client):
    """Which codes exist is not a fact a stranger needs."""
    assert client.get("/auth/sso/not-a-provider").status_code == 404


def test_a_disabled_provider_is_also_just_not_found(client, db, org, scope, provider):
    provider.is_active = False
    db.session.commit()

    assert client.get(f"/auth/sso/{provider.code}").status_code == 404


def test_beginning_a_login_redirects_to_the_provider(client, db, org, scope, provider):
    provider.authorization_endpoint = "https://acme.okta.test/oauth2/v1/authorize"
    provider.client_id = "atlas-demo"
    provider.token_endpoint = "https://acme.okta.test/oauth2/v1/token"
    db.session.commit()

    response = client.get(f"/auth/sso/{provider.code}")
    assert response.status_code == 302
    assert response.headers["Location"].startswith(provider.authorization_endpoint)
    # PKCE, not a bare code flow.
    assert "code_challenge=" in response.headers["Location"]
    assert "code_challenge_method=S256" in response.headers["Location"]


def test_an_unconfigured_provider_says_so_rather_than_500ing(client, db, org, scope, provider):
    response = client.get(f"/auth/sso/{provider.code}", follow_redirects=True)
    assert response.status_code == 200
    assert b"not configured" in response.data


def test_a_callback_without_a_code_is_refused(client, db, org, scope, provider):
    response = client.get(f"/auth/sso/{provider.code}/callback", follow_redirects=True)
    assert response.status_code == 200
    assert b"incomplete" in response.data


def test_a_callback_the_provider_refused_reports_its_words(client, db, org, scope, provider):
    response = client.get(
        f"/auth/sso/{provider.code}/callback?error=access_denied", follow_redirects=True
    )
    assert b"access_denied" in response.data


def test_a_replayed_state_is_refused(client, db, org, scope, provider):
    """The state row is consumed, so a refreshed tab fails rather than
    minting a second session."""
    import datetime as dt
    import secrets

    from app.errors import AtlasError
    from app.models.sso import SsoLoginState
    from app.models.types import utcnow
    from app.services.iam.oidc import consume_state

    state = SsoLoginState(
        org_id=org.id,
        provider_id=provider.id,
        state=secrets.token_urlsafe(16),
        nonce="n",
        code_verifier="v",
        expires_at=utcnow() + dt.timedelta(minutes=5),
    )
    db.session.add(state)
    db.session.commit()
    value = state.state

    assert consume_state(db.session, state_value=value) is not None
    db.session.commit()

    with pytest.raises(AtlasError):
        consume_state(db.session, state_value=value)

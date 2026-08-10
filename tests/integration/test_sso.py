"""Single sign-on: OIDC, SAML, and directory provisioning.

The tests that matter are the refusals. A federated login system that accepts
a forged token, a replayed assertion, or an address it has no business
speaking for is worse than no federation at all, because it looks like security.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import base64
import datetime as dt
import json

import pytest

from app.errors import (
    AuthenticationRequired,
    Conflict,
    PermissionDenied,
    ValidationFailed,
)
from app.models.iam import User, UserStatus
from app.models.sso import IdentityProvider, SsoLoginState, SsoProtocol, SsoReplayGuard
from app.models.types import utcnow
from app.services.iam import oidc, saml, scim

pytestmark = pytest.mark.integration

REDIRECT = "https://atlas.example.com/auth/sso/callback"
AUDIENCE = "https://atlas.example.com/saml/metadata"


# ---------------------------------------------------------------------------
# A throwaway RSA key, generated per session, standing in for the IdP's.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def idp_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def idp_jwks(idp_key):
    import jwt

    public_numbers = idp_key.public_key().public_numbers()

    def b64(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    del jwt
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": "test-key-1",
                "use": "sig",
                "alg": "RS256",
                "n": b64(public_numbers.n),
                "e": b64(public_numbers.e),
            }
        ]
    }


@pytest.fixture()
def provider(db, org, scope, idp_jwks):
    record = IdentityProvider(
        org_id=org.id,
        code="acme-oidc",
        name="Acme SSO",
        protocol=SsoProtocol.OIDC,
        is_active=True,
        issuer="https://idp.acme.test",
        client_id="atlas-client",
        client_secret="s3cret",
        authorization_endpoint="https://idp.acme.test/authorize",
        token_endpoint="https://idp.acme.test/token",
        jwks_uri="https://idp.acme.test/jwks",
        jwks_cache=idp_jwks,
        jwks_fetched_at=utcnow(),
        allowed_email_domains=["acme.test"],
        jit_provisioning=True,
        groups_claim="groups",
    )
    db.session.add(record)
    db.session.commit()
    return record


def _id_token(idp_key, *, nonce=None, kid="test-key-1", alg="RS256", **overrides):
    import jwt

    now = utcnow()
    claims = {
        "iss": "https://idp.acme.test",
        "aud": "atlas-client",
        "sub": "idp-subject-1",
        "email": "dana@acme.test",
        "name": "Dana Okafor",
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(minutes=5)).timestamp()),
    }
    if nonce:
        claims["nonce"] = nonce
    claims.update(overrides)
    return jwt.encode(claims, idp_key, algorithm=alg, headers={"kid": kid})


# ---------------------------------------------------------------------- OIDC


def test_beginning_a_login_creates_a_single_use_state(db, org, scope, provider):
    url, state = oidc.begin_login(db.session, provider=provider, redirect_uri=REDIRECT)
    db.session.commit()

    assert url.startswith("https://idp.acme.test/authorize?")
    assert "code_challenge_method=S256" in url
    assert f"state={state.state}" in url
    assert state.is_usable is True
    # PKCE: the verifier never leaves this server, only its hash does.
    assert state.code_verifier
    assert state.code_verifier not in url


def test_a_state_can_only_be_consumed_once(db, org, scope, provider):
    """A replayed callback must not establish a second session."""
    _, state = oidc.begin_login(db.session, provider=provider, redirect_uri=REDIRECT)
    db.session.commit()

    oidc.consume_state(db.session, state_value=state.state)
    db.session.commit()
    with pytest.raises(AuthenticationRequired):
        oidc.consume_state(db.session, state_value=state.state)


def test_an_expired_state_is_refused(db, org, scope, provider):
    _, state = oidc.begin_login(db.session, provider=provider, redirect_uri=REDIRECT)
    state.expires_at = utcnow() - dt.timedelta(seconds=1)
    db.session.commit()
    with pytest.raises(AuthenticationRequired):
        oidc.consume_state(db.session, state_value=state.state)


def test_an_unknown_state_is_refused(db, org, scope, provider):
    with pytest.raises(AuthenticationRequired):
        oidc.consume_state(db.session, state_value="never-issued")


def test_an_offsite_redirect_target_is_dropped(db, org, scope, provider):
    """An open redirect on a login callback is a phishing primitive."""
    for hostile in ("https://evil.test/steal", "//evil.test", "/\\evil.test"):
        _, state = oidc.begin_login(
            db.session, provider=provider, redirect_uri=REDIRECT, redirect_to=hostile
        )
        assert state.redirect_to is None

    _, state = oidc.begin_login(
        db.session, provider=provider, redirect_uri=REDIRECT, redirect_to="/console/leases"
    )
    assert state.redirect_to == "/console/leases"


def test_a_valid_id_token_verifies(db, org, scope, provider, idp_key):
    claims = oidc.verify_id_token(
        db.session, provider=provider, id_token=_id_token(idp_key, nonce="n1"), nonce="n1"
    )
    assert claims["email"] == "dana@acme.test"


def test_an_unsigned_token_is_refused(db, org, scope, provider):
    """`alg: none` is the oldest JWT attack there is."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=")
    body = base64.urlsafe_b64encode(
        json.dumps({"iss": "https://idp.acme.test", "sub": "x", "aud": "atlas-client"}).encode()
    ).rstrip(b"=")
    token = f"{header.decode()}.{body.decode()}."

    with pytest.raises(AuthenticationRequired) as exc:
        oidc.verify_id_token(db.session, provider=provider, id_token=token, nonce=None)
    assert "asymmetric" in str(exc.value)


def test_a_symmetric_algorithm_is_refused(db, org, scope, provider):
    """HS256 signed with a value we publish is a login as anybody."""
    import jwt

    token = jwt.encode({"sub": "x"}, "atlas-client", algorithm="HS256")
    with pytest.raises(AuthenticationRequired):
        oidc.verify_id_token(db.session, provider=provider, id_token=token, nonce=None)


def test_a_token_signed_by_the_wrong_key_is_refused(db, org, scope, provider):
    from cryptography.hazmat.primitives.asymmetric import rsa

    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthenticationRequired):
        oidc.verify_id_token(
            db.session, provider=provider, id_token=_id_token(attacker), nonce=None
        )


def test_a_token_for_another_audience_is_refused(db, org, scope, provider, idp_key):
    with pytest.raises(AuthenticationRequired):
        oidc.verify_id_token(
            db.session,
            provider=provider,
            id_token=_id_token(idp_key, aud="some-other-app"),
            nonce=None,
        )


def test_a_token_from_another_issuer_is_refused(db, org, scope, provider, idp_key):
    with pytest.raises(AuthenticationRequired):
        oidc.verify_id_token(
            db.session,
            provider=provider,
            id_token=_id_token(idp_key, iss="https://evil.test"),
            nonce=None,
        )


def test_an_expired_token_is_refused(db, org, scope, provider, idp_key):
    stale = int((utcnow() - dt.timedelta(hours=2)).timestamp())
    with pytest.raises(AuthenticationRequired):
        oidc.verify_id_token(
            db.session, provider=provider, id_token=_id_token(idp_key, exp=stale), nonce=None
        )


def test_a_mismatched_nonce_is_refused(db, org, scope, provider, idp_key):
    """Binds the token to this sign-in request, not merely to this client."""
    with pytest.raises(AuthenticationRequired):
        oidc.verify_id_token(
            db.session,
            provider=provider,
            id_token=_id_token(idp_key, nonce="theirs"),
            nonce="ours",
        )


def test_a_token_signed_with_an_unknown_key_is_refused(db, org, scope, provider, idp_key):
    with pytest.raises(AuthenticationRequired) as exc:
        oidc.verify_id_token(
            db.session, provider=provider, id_token=_id_token(idp_key, kid="rotated"), nonce=None
        )
    assert "unknown key" in str(exc.value)


# ------------------------------------------------------------- provisioning


def _identity(email="dana@acme.test", groups=None):
    return oidc.FederatedIdentity(
        subject="idp-subject-1",
        email=email,
        full_name="Dana Okafor",
        groups=groups or [],
    )


def test_a_first_sign_in_provisions_an_account(db, org, scope, provider):
    user = oidc.complete_login(db.session, provider=provider, identity=_identity())
    db.session.commit()

    assert user.email == "dana@acme.test"
    assert user.external_id == "idp-subject-1"
    assert user.identity_provider_id == provider.id
    assert user.status == UserStatus.ACTIVE


def test_a_provider_cannot_speak_for_a_domain_it_does_not_own(db, org, scope, provider):
    """Otherwise a tenant's own IdP mints accounts belonging to other people."""
    with pytest.raises(AuthenticationRequired) as exc:
        oidc.complete_login(db.session, provider=provider, identity=_identity("someone@rival.test"))
    assert "not permitted" in str(exc.value)


def test_that_refusal_is_audited_as_critical(db, org, scope, provider):
    from app.models.audit import AuditEvent, AuditSeverity

    with pytest.raises(AuthenticationRequired):
        oidc.complete_login(db.session, provider=provider, identity=_identity("someone@rival.test"))
    db.session.commit()

    events = [
        event
        for event in db.session.query(AuditEvent).all()
        if event.severity == AuditSeverity.CRITICAL
    ]
    assert events


def test_without_jit_an_unknown_address_cannot_sign_in(db, org, scope, provider):
    provider.jit_provisioning = False
    db.session.commit()
    with pytest.raises(AuthenticationRequired) as exc:
        oidc.complete_login(db.session, provider=provider, identity=_identity())
    assert "not permitted to create" in str(exc.value)


def test_jit_without_a_domain_restriction_is_refused(db, org, scope, provider):
    """Belt and braces on the failure the domain list exists to prevent."""
    provider.allowed_email_domains = []
    db.session.commit()
    with pytest.raises(ValidationFailed):
        oidc.complete_login(db.session, provider=provider, identity=_identity())


def test_a_second_sign_in_reuses_the_account(db, org, scope, provider):
    first = oidc.complete_login(db.session, provider=provider, identity=_identity())
    db.session.commit()
    second = oidc.complete_login(db.session, provider=provider, identity=_identity())
    db.session.commit()

    assert first.id == second.id
    assert db.session.query(User).filter_by(email="dana@acme.test").count() == 1


def test_a_deactivated_account_cannot_sign_in(db, org, scope, provider):
    user = oidc.complete_login(db.session, provider=provider, identity=_identity())
    user.status = UserStatus.DEACTIVATED
    db.session.commit()

    with pytest.raises(AuthenticationRequired) as exc:
        oidc.complete_login(db.session, provider=provider, identity=_identity())
    assert "not active" in str(exc.value)


def test_a_mapped_group_grants_a_role(db, org, scope, provider):
    provider.group_role_map = {"Atlas-Admins": "org_admin"}
    db.session.commit()

    user = oidc.complete_login(
        db.session, provider=provider, identity=_identity(groups=["Atlas-Admins"])
    )
    db.session.commit()

    from app.models.iam import Role, RoleAssignment

    granted = (
        db.session.query(Role.code)
        .join(RoleAssignment, RoleAssignment.role_id == Role.id)
        .filter(RoleAssignment.user_id == user.id)
        .all()
    )
    assert ("org_admin",) in granted


def test_an_unmapped_group_grants_nothing(db, org, scope, provider):
    provider.group_role_map = {"Atlas-Admins": "org_admin"}
    db.session.commit()
    user = oidc.complete_login(
        db.session, provider=provider, identity=_identity(groups=["Some-Other-Group"])
    )
    db.session.commit()
    assert user is not None


# ---------------------------------------------------------------------- SAML


@pytest.fixture(scope="module")
def saml_material(idp_key):
    """A self-signed certificate and a signer for building test assertions."""
    import datetime as _dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509.oid import NameOID

    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp.acme.test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(idp_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=1))
        .not_valid_after(_dt.datetime.now(_dt.UTC) + _dt.timedelta(days=365))
        .sign(idp_key, hashes.SHA256())
    )
    pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    key_pem = idp_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return {"cert": pem, "key": key_pem}


@pytest.fixture()
def saml_provider(db, org, scope, saml_material):
    record = IdentityProvider(
        org_id=org.id,
        code="acme-saml",
        name="Acme SAML",
        protocol=SsoProtocol.SAML,
        is_active=True,
        entity_id="https://idp.acme.test/saml",
        sso_url="https://idp.acme.test/saml/sso",
        signing_certificate=saml_material["cert"],
        allowed_email_domains=["acme.test"],
        jit_provisioning=True,
        email_claim="email",
        name_claim="displayName",
    )
    db.session.add(record)
    db.session.commit()
    return record


def _assertion_xml(
    assertion_id="_assertion-1",
    *,
    email="dana@acme.test",
    audience=AUDIENCE,
    not_after=None,
):
    expiry = (not_after or utcnow() + dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"""<?xml version="1.0"?>
<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
                ID="{assertion_id}" Version="2.0" IssueInstant="2026-08-10T12:00:00Z">
  <saml:Issuer>https://idp.acme.test/saml</saml:Issuer>
  <saml:Subject>
    <saml:NameID>{email}</saml:NameID>
  </saml:Subject>
  <saml:Conditions NotOnOrAfter="{expiry}">
    <saml:AudienceRestriction>
      <saml:Audience>{audience}</saml:Audience>
    </saml:AudienceRestriction>
  </saml:Conditions>
  <saml:AttributeStatement>
    <saml:Attribute Name="email">
      <saml:AttributeValue>{email}</saml:AttributeValue>
    </saml:Attribute>
    <saml:Attribute Name="displayName">
      <saml:AttributeValue>Dana Okafor</saml:AttributeValue>
    </saml:Attribute>
  </saml:AttributeStatement>
</saml:Assertion>"""


def _signed_response(saml_material, **kwargs) -> str:
    from lxml import etree
    from signxml import SignatureMethod, XMLSigner

    document = etree.fromstring(_assertion_xml(**kwargs).encode("utf-8"))
    signed = XMLSigner(
        signature_algorithm=SignatureMethod.RSA_SHA256,
        digest_algorithm="sha256",
    ).sign(document, key=saml_material["key"], cert=saml_material["cert"])
    return base64.b64encode(etree.tostring(signed)).decode("ascii")


def test_a_signed_assertion_signs_a_person_in(db, org, scope, saml_provider, saml_material):
    user = saml.consume_response(
        db.session,
        provider=saml_provider,
        saml_response=_signed_response(saml_material),
        expected_audience=AUDIENCE,
    )
    db.session.commit()

    assert user.email == "dana@acme.test"
    assert user.full_name == "Dana Okafor"


def test_the_same_assertion_cannot_be_presented_twice(db, org, scope, saml_provider, saml_material):
    """A SAML response is a bearer token in an envelope."""
    response = _signed_response(saml_material)
    saml.consume_response(
        db.session,
        provider=saml_provider,
        saml_response=response,
        expected_audience=AUDIENCE,
    )
    db.session.commit()

    with pytest.raises(AuthenticationRequired) as exc:
        saml.consume_response(
            db.session,
            provider=saml_provider,
            saml_response=response,
            expected_audience=AUDIENCE,
        )
    assert "already been used" in str(exc.value)
    assert db.session.query(SsoReplayGuard).count() == 1


def test_a_tampered_assertion_is_refused(db, org, scope, saml_provider, saml_material):
    """Changing one character after signing must break verification."""
    signed = base64.b64decode(_signed_response(saml_material))
    tampered = signed.replace(b"dana@acme.test", b"root@acme.test")
    assert tampered != signed

    with pytest.raises(AuthenticationRequired):
        saml.consume_response(
            db.session,
            provider=saml_provider,
            saml_response=base64.b64encode(tampered).decode("ascii"),
            expected_audience=AUDIENCE,
        )


def test_an_unsigned_assertion_is_refused(db, org, scope, saml_provider):
    raw = base64.b64encode(_assertion_xml().encode("utf-8")).decode("ascii")
    with pytest.raises(AuthenticationRequired):
        saml.consume_response(
            db.session,
            provider=saml_provider,
            saml_response=raw,
            expected_audience=AUDIENCE,
        )


def test_an_assertion_signed_by_another_key_is_refused(db, org, scope, saml_provider):
    """A certificate embedded in the response proves only that someone can sign."""
    import datetime as _dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "evil.test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(attacker.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=1))
        .not_valid_after(_dt.datetime.now(_dt.UTC) + _dt.timedelta(days=365))
        .sign(attacker, hashes.SHA256())
    )
    forged = _signed_response(
        {
            "cert": certificate.public_bytes(serialization.Encoding.PEM).decode("ascii"),
            "key": attacker.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode("ascii"),
        }
    )

    with pytest.raises(AuthenticationRequired):
        saml.consume_response(
            db.session,
            provider=saml_provider,
            saml_response=forged,
            expected_audience=AUDIENCE,
        )


def test_an_assertion_for_another_service_provider_is_refused(
    db, org, scope, saml_provider, saml_material
):
    with pytest.raises(AuthenticationRequired) as exc:
        saml.consume_response(
            db.session,
            provider=saml_provider,
            saml_response=_signed_response(
                saml_material, audience="https://someone-else.example.com"
            ),
            expected_audience=AUDIENCE,
        )
    assert "different service provider" in str(exc.value)


def test_an_expired_assertion_is_refused(db, org, scope, saml_provider, saml_material):
    with pytest.raises(AuthenticationRequired) as exc:
        saml.consume_response(
            db.session,
            provider=saml_provider,
            saml_response=_signed_response(
                saml_material, not_after=utcnow() - dt.timedelta(hours=1)
            ),
            expected_audience=AUDIENCE,
        )
    assert "expired" in str(exc.value)


def test_an_assertion_for_a_foreign_domain_is_refused(db, org, scope, saml_provider, saml_material):
    with pytest.raises(AuthenticationRequired):
        saml.consume_response(
            db.session,
            provider=saml_provider,
            saml_response=_signed_response(saml_material, email="someone@rival.test"),
            expected_audience=AUDIENCE,
        )


def test_garbage_is_refused_without_a_stack_trace(db, org, scope, saml_provider):
    with pytest.raises(AuthenticationRequired):
        saml.consume_response(
            db.session,
            provider=saml_provider,
            saml_response="not base64 at all!!!",
            expected_audience=AUDIENCE,
        )


def test_service_provider_metadata_is_well_formed():
    from lxml import etree

    xml = saml.service_provider_metadata(entity_id=AUDIENCE, acs_url=REDIRECT)
    root = etree.fromstring(xml.encode("utf-8"))
    assert root.get("entityID") == AUDIENCE
    assert b'WantAssertionsSigned="true"' in etree.tostring(root)


# ---------------------------------------------------------------------- SCIM


@pytest.fixture()
def scim_provider(db, org, scope, provider):
    provider.scim_enabled = True
    db.session.commit()
    return provider


def _scim_payload(email="rafi@acme.test", active=True):
    return {
        "schemas": [scim.SCIM_USER_SCHEMA],
        "userName": email,
        "externalId": "dir-1001",
        "name": {"givenName": "Rafi", "familyName": "Nasser"},
        "emails": [{"value": email, "primary": True}],
        "active": active,
    }


def test_scim_creates_a_directory_managed_user(db, org, scope, scim_provider):
    result = scim.create_user_resource(
        db.session, org_id=org.id, provider=scim_provider, payload=_scim_payload()
    )
    db.session.commit()

    assert result.created is True
    assert result.user.email == "rafi@acme.test"
    assert result.user.full_name == "Rafi Nasser"
    assert result.user.external_id == "dir-1001"
    assert result.user.is_directory_managed is True


def test_scim_refuses_a_domain_the_provider_does_not_own(db, org, scope, scim_provider):
    with pytest.raises(PermissionDenied):
        scim.create_user_resource(
            db.session,
            org_id=org.id,
            provider=scim_provider,
            payload=_scim_payload("someone@rival.test"),
        )


def test_scim_refuses_to_adopt_an_existing_account(db, org, scope, scim_provider):
    """Silently taking over a hand-made account is not a create."""
    scim.create_user_resource(
        db.session, org_id=org.id, provider=scim_provider, payload=_scim_payload()
    )
    db.session.commit()
    with pytest.raises(Conflict):
        scim.create_user_resource(
            db.session, org_id=org.id, provider=scim_provider, payload=_scim_payload()
        )


def test_deactivation_revokes_sessions(db, org, scope, scim_provider):
    """The whole point of SCIM. Inactive with a live session is not offboarded."""
    from app.services.iam.session_service import create_user_session

    result = scim.create_user_resource(
        db.session, org_id=org.id, provider=scim_provider, payload=_scim_payload()
    )
    db.session.commit()
    create_user_session(result.user, session=db.session)
    db.session.commit()

    patched = scim.apply_patch(
        db.session,
        org_id=org.id,
        provider=scim_provider,
        user=result.user,
        payload={
            "schemas": [scim.SCIM_PATCH_SCHEMA],
            "Operations": [{"op": "replace", "path": "active", "value": False}],
        },
    )
    db.session.commit()

    assert patched.user.status == UserStatus.DEACTIVATED
    assert patched.changes["sessions_revoked"] == 1

    from app.models.iam import UserSession

    live = [
        s
        for s in db.session.query(UserSession).filter_by(user_id=result.user.id).all()
        if s.revoked_at is None
    ]
    assert live == []


def test_a_pathless_patch_is_understood(db, org, scope, scim_provider):
    """Some directories send the partial resource rather than a path."""
    result = scim.create_user_resource(
        db.session, org_id=org.id, provider=scim_provider, payload=_scim_payload()
    )
    db.session.commit()

    scim.apply_patch(
        db.session,
        org_id=org.id,
        provider=scim_provider,
        user=result.user,
        payload={"Operations": [{"op": "replace", "value": {"active": False}}]},
    )
    db.session.commit()
    assert result.user.status == UserStatus.DEACTIVATED


def test_delete_deactivates_rather_than_removing(db, org, scope, scim_provider):
    """A user id appears on ledger entries. The row stays."""
    result = scim.create_user_resource(
        db.session, org_id=org.id, provider=scim_provider, payload=_scim_payload()
    )
    db.session.commit()

    scim.deactivate_resource(db.session, org_id=org.id, provider=scim_provider, user=result.user)
    db.session.commit()

    assert result.user.status == UserStatus.DEACTIVATED
    assert db.session.get(User, result.user.id) is not None


def test_a_user_managed_by_another_provider_is_refused(db, org, scope, scim_provider):
    result = scim.create_user_resource(
        db.session, org_id=org.id, provider=scim_provider, payload=_scim_payload()
    )
    db.session.commit()

    other = IdentityProvider(
        org_id=org.id,
        code="other",
        name="Other",
        protocol=SsoProtocol.OIDC,
        allowed_email_domains=["acme.test"],
    )
    db.session.add(other)
    db.session.commit()

    with pytest.raises(PermissionDenied):
        scim.deactivate_resource(db.session, org_id=org.id, provider=other, user=result.user)


def test_the_username_filter_finds_one_user(db, org, scope, scim_provider):
    scim.create_user_resource(
        db.session, org_id=org.id, provider=scim_provider, payload=_scim_payload()
    )
    db.session.commit()

    listing = scim.list_users(
        db.session, org_id=org.id, filter_expression='userName eq "rafi@acme.test"'
    )
    assert listing["totalResults"] == 1
    assert listing["Resources"][0]["userName"] == "rafi@acme.test"


def test_an_unsupported_filter_is_refused_not_ignored(db, org, scope, scim_provider):
    """Returning everything to a query meant for one person deactivates a company."""
    with pytest.raises(ValidationFailed):
        scim.list_users(db.session, org_id=org.id, filter_expression='emails.value co "acme"')


def test_listing_is_paged(db, org, scope, scim_provider):
    for index in range(5):
        scim.create_user_resource(
            db.session,
            org_id=org.id,
            provider=scim_provider,
            payload=_scim_payload(f"person{index}@acme.test"),
        )
    db.session.commit()

    page = scim.list_users(db.session, org_id=org.id, start_index=1, count=2)
    assert page["itemsPerPage"] == 2
    assert page["totalResults"] >= 5


def test_a_scim_user_renders_in_scim_shape(db, org, scope, scim_provider):
    result = scim.create_user_resource(
        db.session, org_id=org.id, provider=scim_provider, payload=_scim_payload()
    )
    db.session.commit()

    rendered = scim.to_scim_user(result.user)
    assert rendered["schemas"] == [scim.SCIM_USER_SCHEMA]
    assert rendered["active"] is True
    assert rendered["emails"][0]["primary"] is True


# ----------------------------------------------------------------- isolation


def test_providers_do_not_cross_organizations(db, org, other_org, scope, provider):
    from app.errors import NotFound

    with pytest.raises(NotFound):
        oidc.provider_by_code(db.session, org_id=other_org.id, code="acme-oidc")


def test_expired_states_are_purged(db, org, scope, provider):
    _, state = oidc.begin_login(db.session, provider=provider, redirect_uri=REDIRECT)
    state.expires_at = utcnow() - dt.timedelta(days=3)
    db.session.commit()

    assert oidc.purge_expired_states(db.session, org_id=org.id) == 1
    db.session.commit()
    assert db.session.query(SsoLoginState).count() == 0

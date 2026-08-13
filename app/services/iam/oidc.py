"""OpenID Connect: authorization code flow with PKCE.

The flow is short; the validation is where the security lives, and each check
below is here because omitting it is a known, published attack rather than a
theoretical one.

* **State** is a single-use database row, not a cookie value compared to
  itself. It stops CSRF on the callback, and consuming it stops a replayed
  callback establishing a second session.
* **PKCE** means an authorization code intercepted in a redirect - a browser
  extension, a proxy, a logged URL - cannot be exchanged without the verifier
  that never left this server.
* **The ID token is verified**, not decoded. Signature against the provider's
  published keys, then issuer, audience, expiry, and nonce. ``jwt.decode`` with
  ``verify_signature=False`` is a login as anybody, and it is one keyword
  argument away, so the verification is written out explicitly here rather than
  hidden behind a helper.
* **The email domain is checked against the provider.** A tenant configuring
  their own IdP must not be able to mint an account belonging to somebody at
  another organization.

Discovery and JWKS fetches go through the same SSRF guard as webhooks: a
provider URL is customer-supplied, and "fetch this URL" against an internal
address is a metadata-service read.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import secrets
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import (
    AuthenticationRequired,
    IntegrationFailure,
    NotFound,
    ValidationFailed,
)
from app.logging import get_logger
from app.models.audit import AuditAction, AuditOutcome, AuditSeverity
from app.models.iam import User, UserStatus
from app.models.sso import IdentityProvider, SsoLoginState, SsoProtocol
from app.models.types import utcnow
from app.services.audit.recorder import record_audit_event

__all__ = [
    "STATE_TTL",
    "FederatedIdentity",
    "authorization_url",
    "begin_login",
    "complete_login",
    "discover",
    "verify_id_token",
]

log = get_logger("services.iam.oidc")

#: A sign-in that takes longer than this was abandoned.
STATE_TTL = dt.timedelta(minutes=10)

#: Signing keys are cached this long. Long enough that a login is not a network
#: round trip, short enough that a rotated key is picked up the same day.
JWKS_TTL = dt.timedelta(hours=6)

HTTP_TIMEOUT = 10
MAX_RESPONSE_BYTES = 512 * 1024


@dataclass
class FederatedIdentity:
    """What the provider asserted about the person signing in."""

    subject: str
    email: str
    full_name: str | None = None
    groups: list[str] = field(default_factory=list)
    claims: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Provider metadata
# ---------------------------------------------------------------------------


def _fetch_json(url: str, *, data: bytes | None = None, headers: dict | None = None) -> dict:
    """GET or POST JSON, refusing anything that is not a safe public URL."""
    from app.services.integration.webhooks import assert_safe_url

    # The scheme and the resolved address are both checked here. Everything
    # below is safe against a customer-supplied URL only because of this call.
    assert_safe_url(url)
    request = urllib.request.Request(  # noqa: S310
        url, data=data, headers=headers or {}, method="POST" if data else "GET"
    )
    try:
        # The scheme and resolved address are checked by assert_safe_url above,
        # which is what makes this call safe against a customer-supplied URL.
        with urllib.request.urlopen(  # noqa: S310  # nosec B310
            request, timeout=HTTP_TIMEOUT
        ) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except Exception as exc:  # noqa: BLE001 - any transport failure is the same to us
        raise IntegrationFailure(f"The identity provider did not respond: {exc}") from exc

    if len(body) > MAX_RESPONSE_BYTES:
        raise IntegrationFailure("The identity provider returned an implausibly large response.")
    try:
        parsed = json.loads(body)
    except ValueError as exc:
        raise IntegrationFailure("The identity provider returned malformed JSON.") from exc
    if not isinstance(parsed, dict):
        raise IntegrationFailure("The identity provider returned an unexpected document.")
    return parsed


def discover(session: Session, *, provider: IdentityProvider) -> IdentityProvider:
    """Populate endpoints from the provider's discovery document."""
    if not provider.discovery_url:
        raise ValidationFailed("This provider has no discovery URL configured.")

    document = _fetch_json(provider.discovery_url)
    issuer = document.get("issuer")
    if not issuer:
        raise IntegrationFailure("The discovery document has no issuer.")

    provider.issuer = issuer
    provider.authorization_endpoint = document.get("authorization_endpoint")
    provider.token_endpoint = document.get("token_endpoint")
    provider.jwks_uri = document.get("jwks_uri")
    provider.userinfo_endpoint = document.get("userinfo_endpoint")
    session.flush()

    if not provider.authorization_endpoint or not provider.token_endpoint:
        raise IntegrationFailure("The discovery document is missing required endpoints.")
    return provider


def _signing_keys(session: Session, provider: IdentityProvider) -> dict:
    """The provider's public keys, cached."""
    fresh = (
        provider.jwks_cache
        and provider.jwks_fetched_at is not None
        and provider.jwks_fetched_at > utcnow() - JWKS_TTL
    )
    if fresh:
        return provider.jwks_cache

    if not provider.jwks_uri:
        raise ValidationFailed("This provider has no JWKS URI configured.")
    keys = _fetch_json(provider.jwks_uri)
    provider.jwks_cache = keys
    provider.jwks_fetched_at = utcnow()
    session.flush()
    return keys


# ---------------------------------------------------------------------------
# Starting a login
# ---------------------------------------------------------------------------


def _safe_redirect(target: str | None) -> str | None:
    """Only a local path survives.

    An open redirect on a login callback is a phishing primitive: the attacker
    gets to send a genuine, correctly-branded sign-in link that lands the user
    on their page.
    """
    if not target:
        return None
    if target.startswith("//") or "\\" in target:
        return None
    parsed = urllib.parse.urlparse(target)
    if parsed.scheme or parsed.netloc:
        return None
    return target if target.startswith("/") else None


def begin_login(
    session: Session,
    *,
    provider: IdentityProvider,
    redirect_uri: str,
    redirect_to: str | None = None,
) -> tuple[str, SsoLoginState]:
    """Create the state row and return the URL to send the browser to."""
    if provider.protocol != SsoProtocol.OIDC:
        raise ValidationFailed("That provider does not speak OpenID Connect.")
    if not provider.is_active:
        raise ValidationFailed("That identity provider is not active.")
    if not provider.authorization_endpoint or not provider.client_id:
        raise ValidationFailed("This provider is not configured. Run discovery first.")

    verifier = secrets.token_urlsafe(64)[:128]
    state = SsoLoginState(
        org_id=provider.org_id,
        provider_id=provider.id,
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(24),
        code_verifier=verifier,
        redirect_to=_safe_redirect(redirect_to),
        expires_at=utcnow() + STATE_TTL,
    )
    session.add(state)
    session.flush()

    return authorization_url(provider, state=state, redirect_uri=redirect_uri), state


def authorization_url(
    provider: IdentityProvider, *, state: SsoLoginState, redirect_uri: str
) -> str:
    challenge = (
        base64.urlsafe_b64encode(
            hashlib.sha256((state.code_verifier or "").encode("ascii")).digest()
        )
        .decode("ascii")
        .rstrip("=")
    )
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": provider.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(provider.scopes or ["openid", "email"]),
            "state": state.state,
            "nonce": state.nonce or "",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    separator = "&" if "?" in (provider.authorization_endpoint or "") else "?"
    return f"{provider.authorization_endpoint}{separator}{query}"


# ---------------------------------------------------------------------------
# Completing a login
# ---------------------------------------------------------------------------


def consume_state(session: Session, *, state_value: str) -> SsoLoginState:
    """Take the state row, once.

    The consumption is the point. A callback presented twice - a refreshed tab,
    a replayed URL from a proxy log - must not produce a second session.
    """
    row = session.execute(
        select(SsoLoginState).where(SsoLoginState.state == state_value)
    ).scalar_one_or_none()
    if row is None:
        raise AuthenticationRequired("That sign-in request is not recognised.")
    if not row.is_usable:
        raise AuthenticationRequired(
            "That sign-in request has expired or already been used. Please sign in again."
        )
    row.consumed_at = utcnow()
    session.flush()
    return row


def verify_id_token(
    session: Session,
    *,
    provider: IdentityProvider,
    id_token: str,
    nonce: str | None,
) -> dict[str, Any]:
    """Verify signature, issuer, audience, expiry, and nonce.

    Written out rather than wrapped, because the difference between this and a
    total authentication bypass is one keyword argument.
    """
    import jwt
    from jwt import PyJWKClient  # noqa: F401 - imported to assert the dependency shape

    keys = _signing_keys(session, provider)
    try:
        header = jwt.get_unverified_header(id_token)
    except Exception as exc:  # noqa: BLE001
        raise AuthenticationRequired("That identity token is malformed.") from exc

    algorithm = header.get("alg")
    if not isinstance(algorithm, str) or algorithm in ("none", "HS256", "HS384", "HS512"):
        # A symmetric algorithm here means the token is signed with something we
        # would have to share; "none" means it is not signed at all. Both are
        # the classic JWT confusion attacks.
        raise AuthenticationRequired(
            f"Identity tokens must be signed with an asymmetric algorithm, not {algorithm!r}."
        )

    key = _matching_key(keys, header.get("kid"))
    if key is None:
        raise AuthenticationRequired("The identity token was signed with an unknown key.")

    try:
        claims = jwt.decode(
            id_token,
            key=jwt.PyJWK(key).key,
            algorithms=[algorithm],
            audience=provider.client_id,
            issuer=provider.issuer,
            options={
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": True,
                "verify_iss": True,
                "require": ["exp", "iat", "iss", "aud", "sub"],
            },
            leeway=60,
        )
    except Exception as exc:  # noqa: BLE001 - every failure is the same refusal
        log.warning(
            "identity token rejected",
            extra={"event": "sso.token_rejected", "provider": provider.code},
        )
        raise AuthenticationRequired(f"That identity token was not accepted: {exc}") from exc

    if nonce and claims.get("nonce") != nonce:
        raise AuthenticationRequired("The identity token does not match this sign-in request.")

    return claims


def _matching_key(keys: dict, kid: str | None) -> dict | None:
    candidates = [k for k in keys.get("keys", []) if isinstance(k, dict)]
    if kid:
        for key in candidates:
            if key.get("kid") == kid:
                return key
        return None
    # No kid: only unambiguous when the provider publishes exactly one key.
    return candidates[0] if len(candidates) == 1 else None


def exchange_code(
    session: Session,
    *,
    provider: IdentityProvider,
    code: str,
    redirect_uri: str,
    code_verifier: str | None,
) -> dict[str, Any]:
    """Trade the authorization code for tokens."""
    if not provider.token_endpoint:
        raise ValidationFailed("This provider has no token endpoint configured.")

    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": provider.client_id or "",
    }
    if code_verifier:
        body["code_verifier"] = code_verifier
    if provider.client_secret:
        body["client_secret"] = provider.client_secret

    return _fetch_json(
        provider.token_endpoint,
        data=urllib.parse.urlencode(body).encode("ascii"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )


def identity_from_claims(provider: IdentityProvider, claims: dict[str, Any]) -> FederatedIdentity:
    """Read the configured claims off a verified token."""
    email = str(claims.get(provider.email_claim) or "").strip().lower()
    if not email or "@" not in email:
        raise AuthenticationRequired(
            f"The identity provider did not supply a usable '{provider.email_claim}' claim."
        )

    raw_groups = claims.get(provider.groups_claim) if provider.groups_claim else None
    if isinstance(raw_groups, str):
        groups = [raw_groups]
    elif isinstance(raw_groups, list):
        groups = [str(g) for g in raw_groups]
    else:
        groups = []

    return FederatedIdentity(
        subject=str(claims.get("sub") or email),
        email=email,
        full_name=str(claims.get(provider.name_claim) or "").strip() or None,
        groups=groups,
        claims=claims,
    )


def complete_login(
    session: Session,
    *,
    provider: IdentityProvider,
    identity: FederatedIdentity,
) -> User:
    """Resolve a verified assertion to a local account.

    Refuses an address the provider is not entitled to speak for, and refuses
    to create an account unless the provider is explicitly allowed to.
    """
    if not provider.allows_email(identity.email):
        record_audit_event(
            action=AuditAction.AUTH_LOGIN_FAILED,
            resource_type="IdentityProvider",
            resource_id=provider.id,
            resource_label=provider.code,
            outcome=AuditOutcome.DENIED,
            severity=AuditSeverity.CRITICAL,
            payload={"email_domain": identity.email.rsplit("@", 1)[-1]},
            reason="The identity provider asserted an address outside its allowed domains.",
            org_id=provider.org_id,
            session=session,
        )
        raise AuthenticationRequired(
            "That identity provider is not permitted to sign in addresses at that domain."
        )

    user = session.execute(
        select(User).where(
            User.org_id == provider.org_id,
            User.email == identity.email,
            User.deleted_at.is_(None),
        )
    ).scalar_one_or_none()

    if user is None:
        if not provider.jit_provisioning:
            raise AuthenticationRequired(
                "There is no account for that address, and this provider is not "
                "permitted to create one."
            )
        user = _provision(session, provider=provider, identity=identity)
    else:
        user.external_id = user.external_id or identity.subject
        user.identity_provider_id = provider.id
        if identity.full_name:
            user.full_name = identity.full_name

    if user.status != UserStatus.ACTIVE:
        raise AuthenticationRequired("That account is not active.")

    _apply_group_roles(session, provider=provider, user=user, groups=identity.groups)

    provider.last_login_at = utcnow()
    provider.login_count += 1
    session.flush()

    record_audit_event(
        action=AuditAction.AUTH_LOGIN_SUCCEEDED,
        resource_type="User",
        resource_id=user.id,
        resource_label=user.email,
        severity=AuditSeverity.NOTICE,
        payload={"provider": provider.code, "protocol": str(provider.protocol)},
        reason="Signed in through an identity provider.",
        org_id=provider.org_id,
        actor_id=user.id,
        session=session,
    )
    return user


def _provision(
    session: Session, *, provider: IdentityProvider, identity: FederatedIdentity
) -> User:
    from app.services.iam.provisioning import create_user

    if not provider.allowed_email_domains:
        # Belt and braces: creating accounts for arbitrary addresses is exactly
        # the failure the domain list exists to prevent.
        raise ValidationFailed(
            "Just-in-time provisioning requires at least one allowed email domain."
        )

    user = create_user(
        session,
        org_id=provider.org_id,
        email=identity.email,
        full_name=identity.full_name or identity.email.split("@")[0],
        password=None,
        role_codes=[provider.default_role_code] if provider.default_role_code else [],
        status=UserStatus.ACTIVE,
    )
    user.external_id = identity.subject
    user.identity_provider_id = provider.id
    user.is_directory_managed = provider.scim_enabled
    session.flush()
    log.info(
        "account provisioned from an identity provider",
        extra={"event": "sso.provisioned", "provider": provider.code, "user_id": user.id},
    )
    return user


def _apply_group_roles(
    session: Session,
    *,
    provider: IdentityProvider,
    user: User,
    groups: list[str],
) -> None:
    """Grant roles the IdP's groups map to.

    Additive only. Revocation on group removal belongs to directory sync, where
    the full picture is available - inferring "they left the group" from an
    assertion that simply omits it would revoke access on a misconfiguration.
    """
    mapping = provider.group_role_map or {}
    if not mapping or not groups:
        return

    from app.services.iam.provisioning import assign_role

    for group in groups:
        role_code = mapping.get(group)
        if not role_code:
            continue
        try:
            assign_role(session, user=user, role_code=role_code, granted_by_id=None)
        except Exception:  # noqa: BLE001 - a bad mapping must not block sign-in
            log.warning(
                "could not apply a mapped role",
                extra={"event": "sso.role_map_failed", "group": group, "role": role_code},
            )


def provider_by_code(session: Session, *, org_id: str, code: str) -> IdentityProvider:
    provider = session.execute(
        select(IdentityProvider).where(
            IdentityProvider.org_id == org_id,
            IdentityProvider.code == code,
            IdentityProvider.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if provider is None:
        raise NotFound(f"No identity provider with code {code!r}.")
    return provider


def purge_expired_states(session: Session, *, org_id: str) -> int:
    """Housekeeping. An abandoned sign-in should not accumulate."""
    stale = (
        session.execute(
            select(SsoLoginState).where(
                SsoLoginState.org_id == org_id,
                SsoLoginState.expires_at < utcnow() - dt.timedelta(days=1),
            )
        )
        .scalars()
        .all()
    )
    for row in stale:
        session.delete(row)
    session.flush()
    return len(stale)

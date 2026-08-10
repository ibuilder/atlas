"""API token issuance and authentication.

Tokens are opaque, prefixed, and stored only as SHA-256 digests. The prefix
(``atlas_api_``) makes leaked credentials findable by secret scanners, and the
displayed short prefix lets an operator identify a token in a list without the
system ever retaining the secret.

A token can never exceed the authority of the identity that minted it: its
scopes are intersected with the owner's live permissions at every request, so
revoking someone's role immediately narrows their tokens too.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
import ipaddress

from flask import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging import get_logger
from app.models.base import unscoped
from app.models.iam import ApiToken, User
from app.models.types import utcnow
from app.security.crypto import hash_token, new_token

__all__ = ["authenticate_request_token", "issue_api_token", "revoke_api_token"]

log = get_logger("services.iam.token")

TOKEN_PREFIX = "atlas_api"
PREFIX_DISPLAY_LENGTH = 12


def issue_api_token(
    *,
    user: User,
    name: str,
    scopes: list[str] | None = None,
    allowed_ips: list[str] | None = None,
    ttl_days: int = 90,
    session: Session | None = None,
) -> tuple[ApiToken, str]:
    """Mint a token. The plaintext is returned once and never stored."""
    if session is None:
        from app.extensions import db

        session = db.session

    for cidr in allowed_ips or []:
        # Validate now: a malformed CIDR that silently never matches would lock
        # the customer out of their own integration with no useful error.
        ipaddress.ip_network(cidr, strict=False)

    plaintext = new_token(TOKEN_PREFIX)
    token = ApiToken(
        org_id=user.org_id,
        user_id=user.id,
        name=name,
        prefix=plaintext[:PREFIX_DISPLAY_LENGTH],
        token_hash=hash_token(plaintext),
        scopes=scopes or [],
        allowed_ips=allowed_ips or [],
        expires_at=utcnow() + dt.timedelta(days=ttl_days),
    )
    session.add(token)
    session.flush()
    return token, plaintext


def revoke_api_token(token: ApiToken, session: Session | None = None) -> None:
    if session is None:
        from app.extensions import db

        session = db.session
    token.revoked_at = utcnow()
    session.flush()


def authenticate_request_token(req: Request) -> User | None:
    """Flask-Login request loader for ``Authorization: Bearer`` credentials.

    Returns ``None`` for anything that is not a valid live token, so the request
    falls through to session authentication or to the unauthenticated handler.
    """
    header = req.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    if not value.startswith(f"{TOKEN_PREFIX}_"):
        return None

    from app.extensions import db

    with unscoped(db.session):
        token = db.session.execute(
            select(ApiToken).where(ApiToken.token_hash == hash_token(value))
        ).scalar_one_or_none()

        if token is None or not token.is_live:
            log.info(
                "api token rejected",
                extra={"event": "auth.token_rejected", "reason": "unknown_or_expired"},
            )
            return None

        if not _ip_allowed(token, req.remote_addr):
            log.warning(
                "api token rejected by ip allowlist",
                extra={
                    "event": "security.token_ip_denied",
                    "token_id": token.id,
                    "ip": req.remote_addr,
                },
            )
            return None

        user = db.session.get(User, token.user_id) if token.user_id else None
        if user is None or not user.is_active:
            return None

        token.last_used_at = utcnow()
        token.last_used_ip = req.remote_addr

    # Consumed by the rate limiter to bucket per token rather than per IP.
    req.atlas_token_id = token.id  # type: ignore[attr-defined]
    user.current_session_id = None  # type: ignore[attr-defined]
    user._atlas_token = token  # type: ignore[attr-defined]
    return user


def _ip_allowed(token: ApiToken, remote_addr: str | None) -> bool:
    """Empty allowlist means unrestricted; a populated one is strict."""
    allowed = token.allowed_ips or []
    if not allowed:
        return True
    if not remote_addr:
        return False
    try:
        address = ipaddress.ip_address(remote_addr)
    except ValueError:
        return False
    for cidr in allowed:
        try:
            if address in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False

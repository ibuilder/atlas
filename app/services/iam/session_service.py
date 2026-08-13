"""Server-side session lifecycle.

The cookie carries an opaque token; authority lives in the ``user_sessions``
row. Every request re-validates that row, which is what makes revocation,
idle timeout, and forced logout on credential change actually take effect
rather than waiting for a cookie to expire.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt

from flask import current_app, request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging import get_logger
from app.models.base import unscoped
from app.models.iam import User, UserSession
from app.models.types import utcnow
from app.security.crypto import hash_token
from app.security.session import clear_session_token, issue_session_token, read_session_token

__all__ = [
    "create_user_session",
    "load_user_for_session",
    "revoke_all_sessions",
    "revoke_session",
    "touch_session",
]

log = get_logger("services.iam.session")

#: Only refresh ``last_seen_at`` when it is this stale, so a burst of requests
#: does not turn every page load into a write.
_TOUCH_INTERVAL_SECONDS = 60


def _session() -> Session:
    from app.extensions import current_session

    return current_session()


def load_user_for_session(composite_id: str) -> User | None:
    """Flask-Login user loader.

    ``composite_id`` is ``"<user id>:<credential version>"``. A mismatch on the
    version means credentials changed after this cookie was issued, so the
    cookie is rejected without needing to enumerate and delete remember-me
    tokens.
    """
    if not composite_id:
        return None

    user_id, _, version = composite_id.partition(":")
    db_session = _session()

    # Runs before organization resolution, so there is no tenant scope yet.
    with unscoped(db_session):
        user = db_session.get(User, user_id)
        if user is None or user.is_deleted:
            return None
        if version and str(user.credential_version) != version:
            log.info(
                "session rejected: stale credential version",
                extra={"event": "auth.session_stale", "actor_id": user.id},
            )
            return None
        if not user.is_active:
            return None

        token = read_session_token()
        if not token:
            # No server-side session: an API-token request, or a cookie from
            # before sessions were tracked. Either way, not a browser session.
            return None

        user_session = db_session.execute(
            select(UserSession).where(UserSession.token_hash == hash_token(token))
        ).scalar_one_or_none()

        if user_session is None or user_session.user_id != user.id:
            clear_session_token()
            return None

        settings = current_app.config["SETTINGS"]
        if not user_session.is_valid(settings.session_idle_timeout_minutes):
            clear_session_token()
            log.info(
                "session rejected: expired or revoked",
                extra={"event": "auth.session_invalid", "actor_id": user.id},
            )
            return None

        touch_session(user_session)
        # Carried so the authorization context can read MFA freshness without a
        # second lookup.
        user._atlas_session = user_session  # type: ignore[attr-defined]
        user.current_session_id = user_session.id  # type: ignore[attr-defined]
        return user


def create_user_session(
    user: User,
    *,
    mfa_verified: bool = False,
    device_label: str | None = None,
    session: Session | None = None,
) -> tuple[UserSession, str]:
    """Create a server-side session and return it with its plaintext token."""
    db_session = session or _session()
    settings = current_app.config["SETTINGS"]

    token = issue_session_token(settings.session_lifetime_minutes)
    now = utcnow()

    user_session = UserSession(
        org_id=user.org_id,
        user_id=user.id,
        token_hash=token.digest,
        device_label=device_label or _describe_device(),
        ip_address=request.remote_addr if request else None,
        user_agent=(request.user_agent.string[:512] if request else None),
        issued_at=now,
        last_seen_at=now,
        expires_at=token.expires_at,
        mfa_verified_at=now if mfa_verified else None,
    )
    db_session.add(user_session)
    db_session.flush()
    return user_session, token.plaintext


def touch_session(user_session: UserSession) -> None:
    """Advance the idle clock, throttled to avoid a write per request."""
    now = utcnow()
    if (now - user_session.last_seen_at).total_seconds() < _TOUCH_INTERVAL_SECONDS:
        return
    user_session.last_seen_at = now


def mark_mfa_verified(user_session: UserSession) -> None:
    user_session.mfa_verified_at = utcnow()


def revoke_session(
    user_session: UserSession, reason: str = "user_request", session: Session | None = None
) -> None:
    db_session = session or _session()
    user_session.revoke(reason)
    db_session.flush()
    log.info(
        "session revoked",
        extra={
            "event": "auth.session_revoked",
            "actor_id": user_session.user_id,
            "reason": reason,
        },
    )


def revoke_all_sessions(
    user: User,
    *,
    except_session_id: str | None = None,
    reason: str = "revoke_all",
    session: Session | None = None,
) -> int:
    """Sign out everywhere. Returns how many sessions were ended."""
    db_session = session or _session()
    now = utcnow()

    with unscoped(db_session):
        sessions = db_session.execute(
            select(UserSession).where(
                UserSession.user_id == user.id, UserSession.revoked_at.is_(None)
            )
        ).scalars()

        count = 0
        for user_session in sessions:
            if except_session_id and user_session.id == except_session_id:
                continue
            user_session.revoked_at = now
            user_session.revoked_reason = reason
            count += 1

    db_session.flush()
    return count


def purge_expired_sessions(session: Session | None = None, older_than_days: int = 30) -> int:
    """Delete sessions that expired long ago.

    Revoked and expired rows are kept for a while on purpose - "which device was
    that, and when did it last connect" is a question that comes up during
    incident response.
    """
    db_session = session or _session()
    cutoff = utcnow() - dt.timedelta(days=older_than_days)

    with unscoped(db_session):
        rows = db_session.execute(
            select(UserSession).where(UserSession.expires_at < cutoff)
        ).scalars()
        count = 0
        for row in rows:
            db_session.delete(row)
            count += 1
    db_session.flush()
    return count


def _describe_device() -> str:
    """A short, human-recognisable label for the session list."""
    if not request:
        return "Unknown device"
    agent = request.user_agent
    platform = (agent.platform or "device").title()
    browser = (agent.browser or "browser").title()
    return f"{browser} on {platform}"[:120]

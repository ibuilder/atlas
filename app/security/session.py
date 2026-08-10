"""Server-side session tokens.

Flask's signed cookie proves a session was issued by us; it cannot prove the
session is still meant to exist. Atlas therefore keeps a row per session and
stores only an opaque token in the cookie, so "sign out on that other device",
forced logout on password change, and idle timeout are all real operations
rather than client-side suggestions.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from flask import session as flask_session

from app.models.types import utcnow
from app.security.crypto import hash_token, new_token

__all__ = [
    "SESSION_KEY",
    "SessionToken",
    "clear_session_token",
    "issue_session_token",
    "read_session_token",
    "store_session_token",
]

#: Key under which the opaque session token lives inside the signed cookie.
SESSION_KEY = "atlas_sid"
MFA_PENDING_KEY = "atlas_mfa_pending"
TOKEN_PREFIX = "atlas_sess"


@dataclass(frozen=True)
class SessionToken:
    """A freshly minted session token and its storage digest."""

    plaintext: str
    digest: str
    expires_at: dt.datetime


def issue_session_token(lifetime_minutes: int) -> SessionToken:
    token = new_token(TOKEN_PREFIX)
    return SessionToken(
        plaintext=token,
        digest=hash_token(token),
        expires_at=utcnow() + dt.timedelta(minutes=lifetime_minutes),
    )


def store_session_token(token: str, *, permanent: bool = True) -> None:
    flask_session[SESSION_KEY] = token
    flask_session.permanent = permanent


def read_session_token() -> str | None:
    value = flask_session.get(SESSION_KEY)
    return value if isinstance(value, str) else None


def clear_session_token() -> None:
    flask_session.pop(SESSION_KEY, None)
    flask_session.pop(MFA_PENDING_KEY, None)


def set_mfa_pending(user_id: str) -> None:
    """Mark a half-authenticated session: password accepted, MFA outstanding.

    Deliberately not a logged-in session. Until the second factor is presented,
    the only thing this state permits is completing or abandoning the challenge.
    """
    flask_session[MFA_PENDING_KEY] = user_id


def get_mfa_pending() -> str | None:
    value = flask_session.get(MFA_PENDING_KEY)
    return value if isinstance(value, str) else None


def clear_mfa_pending() -> None:
    flask_session.pop(MFA_PENDING_KEY, None)


def rotate_session() -> None:
    """Discard and re-issue the underlying cookie session identity.

    Called on privilege transitions - sign-in, MFA completion, password change -
    so a session identifier captured before the transition cannot be replayed
    after it.
    """
    preserved = {
        key: value for key, value in flask_session.items() if key in (SESSION_KEY, MFA_PENDING_KEY)
    }
    flask_session.clear()
    flask_session.update(preserved)

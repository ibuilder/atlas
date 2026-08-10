"""Authentication endpoints.

Sign-in is rate limited hard and answers identically for an unknown account and
a wrong password. The MFA step is a separate request against a half-authenticated
session, so a password alone never yields a usable session for an MFA-enrolled
account.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from flask import Response, current_app, jsonify
from flask_login import current_user, login_required, login_user, logout_user
from pydantic import Field

from app.api.helpers import parse_body, respond
from app.api.v1 import api_v1_bp
from app.errors import AuthenticationRequired, ValidationFailed
from app.extensions import db, limiter
from app.models.audit import AuditAction
from app.models.base import unscoped
from app.models.iam import User
from app.schemas.common import AtlasRequest, AtlasResponse, Email
from app.security import session as session_helpers
from app.services.audit.recorder import record_audit_event
from app.services.iam import auth_service
from app.services.iam.session_service import revoke_session

__all__ = []


class LoginRequest(AtlasRequest):
    email: Email
    password: str = Field(min_length=1, max_length=1024)


class MfaRequest(AtlasRequest):
    code: str = Field(min_length=4, max_length=32)


class PasswordResetRequest(AtlasRequest):
    email: Email


class PasswordResetComplete(AtlasRequest):
    token: str = Field(min_length=16, max_length=256)
    new_password: str = Field(min_length=1, max_length=1024)


class SessionOut(AtlasResponse):
    status: str
    user_id: str | None = None
    email: str | None = None
    full_name: str | None = None
    org_id: str | None = None
    user_type: str | None = None
    must_change_password: bool = False


def _rate_limit() -> str:
    return current_app.config["SETTINGS"].ratelimit_auth


@api_v1_bp.post("/auth/login", endpoint="auth_login")
@limiter.limit(_rate_limit)
def login() -> Response:
    payload = parse_body(LoginRequest)
    result = auth_service.authenticate(payload.email, payload.password)

    if result.status == auth_service.AuthStatus.INVALID:
        raise AuthenticationRequired("The credentials provided are not valid.")
    if result.status == auth_service.AuthStatus.DISABLED:
        raise AuthenticationRequired("This account is not active.")

    if result.status == auth_service.AuthStatus.MFA_REQUIRED:
        if result.user is None:  # pragma: no cover - defensive
            raise AuthenticationRequired()
        # Half-authenticated: the only thing this state permits is completing
        # or abandoning the MFA challenge.
        session_helpers.set_mfa_pending(result.user.id)
        return respond(SessionOut(status="mfa_required"), status=200)

    return _complete_login(result)


@api_v1_bp.post("/auth/mfa", endpoint="auth_mfa")
@limiter.limit(_rate_limit)
def verify_mfa() -> Response:
    payload = parse_body(MfaRequest)
    pending_user_id = session_helpers.get_mfa_pending()
    if not pending_user_id:
        raise AuthenticationRequired("No multi-factor challenge is in progress.")

    with unscoped(db.session):
        user = db.session.get(User, pending_user_id)
    if user is None or not user.is_active:
        session_helpers.clear_mfa_pending()
        raise AuthenticationRequired("No multi-factor challenge is in progress.")

    result = auth_service.complete_mfa_challenge(user, payload.code)
    session_helpers.clear_mfa_pending()
    return _complete_login(result)


def _complete_login(result: auth_service.AuthResult) -> Response:
    # An explicit check rather than `assert`: assertions are removed under
    # `python -O`, and this one guards the transition into an authenticated
    # session - exactly the check that must not silently disappear.
    if result.user is None or result.session_token is None:  # pragma: no cover - defensive
        raise AuthenticationRequired()

    # Rotate before establishing the authenticated identity so a session
    # identifier observed pre-login cannot be replayed after it.
    session_helpers.rotate_session()
    login_user(result.user, remember=False)
    session_helpers.store_session_token(result.session_token)

    return respond(
        SessionOut(
            status="authenticated",
            user_id=result.user.id,
            email=result.user.email,
            full_name=result.user.full_name,
            org_id=result.user.org_id,
            user_type=str(result.user.user_type),
            must_change_password=result.user.must_change_password,
        )
    )


@api_v1_bp.post("/auth/logout", endpoint="auth_logout")
@login_required
def logout() -> Response:
    user_session = getattr(current_user, "_atlas_session", None)
    if user_session is not None:
        revoke_session(user_session, reason="user_logout")
    record_audit_event(
        action=AuditAction.AUTH_LOGOUT,
        resource_type="User",
        resource_id=current_user.get_id().split(":")[0],
        org_id=current_user.org_id,
    )
    db.session.commit()

    logout_user()
    session_helpers.clear_session_token()
    return jsonify({"status": "signed_out"})


@api_v1_bp.get("/auth/me", endpoint="auth_me")
@login_required
def me() -> Response:
    from app.services.iam.authorization import get_authorization_context

    context = get_authorization_context()
    return respond(
        {
            "id": current_user.id,
            "email": current_user.email,
            "full_name": current_user.full_name,
            "user_type": str(current_user.user_type),
            "org_id": context.org_id if context else current_user.org_id,
            "mfa_enabled": current_user.mfa_enabled,
            "must_change_password": current_user.must_change_password,
            "permissions": sorted(context.permissions()) if context else [],
        }
    )


@api_v1_bp.post("/auth/password-reset", endpoint="auth_password_reset_request")
@limiter.limit(_rate_limit)
def request_password_reset() -> Response:
    payload = parse_body(PasswordResetRequest)
    token = auth_service.request_password_reset(payload.email)

    if token:
        from app.services.notifications.mailer import send_password_reset

        send_password_reset(payload.email, token)

    # Identical response either way: this endpoint must not reveal which
    # addresses have accounts.
    return jsonify({"status": "sent"})


@api_v1_bp.post("/auth/password-reset/complete", endpoint="auth_password_reset_complete")
@limiter.limit(_rate_limit)
def complete_password_reset() -> Response:
    payload = parse_body(PasswordResetComplete)
    auth_service.complete_password_reset(payload.token, payload.new_password)
    return jsonify({"status": "password_changed"})


class ChangePasswordRequest(AtlasRequest):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


@api_v1_bp.post("/auth/password", endpoint="auth_change_password")
@login_required
def change_password() -> Response:
    payload = parse_body(ChangePasswordRequest)
    if payload.current_password == payload.new_password:
        raise ValidationFailed("The new password must differ from the current one.")

    auth_service.change_password(
        current_user,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    db.session.commit()
    # Every session was just revoked, including this one.
    logout_user()
    session_helpers.clear_session_token()
    return jsonify({"status": "password_changed", "sessions_revoked": True})

"""Authentication: sign-in, MFA, password reset, credential changes.

Every path here is written on the assumption that an attacker is watching the
responses. Unknown accounts and wrong passwords produce the same error, the same
audit shape, and - via a dummy Argon2 verification - roughly the same latency,
because a login form that answers "does this person have an account here" is a
resident-privacy problem before it is a security one.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from flask import current_app, request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import (
    AccountLocked,
    InvalidCredentials,
    MFAInvalid,
    ValidationFailed,
)
from app.logging import get_logger
from app.models.audit import AuditAction, AuditOutcome, AuditSeverity
from app.models.base import unscoped
from app.models.iam import (
    LoginAttempt,
    LoginOutcome,
    MfaRecoveryCode,
    PasswordHistory,
    PasswordResetToken,
    User,
    UserSession,
    UserStatus,
)
from app.models.types import utcnow
from app.observability import AUTH_ATTEMPTS
from app.security import mfa as mfa_lib
from app.security.crypto import hash_token, new_token, sha256_hex
from app.security.passwords import (
    dummy_verify,
    hash_password,
    needs_rehash,
    validate_password,
    verify_password,
)
from app.services.audit.recorder import record_audit_event
from app.services.iam.session_service import create_user_session, revoke_all_sessions

__all__ = [
    "AuthResult",
    "AuthStatus",
    "authenticate",
    "change_password",
    "complete_mfa_challenge",
    "complete_password_reset",
    "enroll_mfa",
    "request_password_reset",
]

log = get_logger("services.iam.auth")

RESET_TOKEN_PREFIX = "atlas_pwreset"


class AuthStatus(StrEnum):
    SUCCESS = "success"
    MFA_REQUIRED = "mfa_required"
    INVALID = "invalid"
    LOCKED = "locked"
    DISABLED = "disabled"
    PASSWORD_CHANGE_REQUIRED = "password_change_required"


@dataclass
class AuthResult:
    status: AuthStatus
    user: User | None = None
    session: UserSession | None = None
    session_token: str | None = None


def _db() -> Session:
    from app.extensions import db

    return db.session


def _settings():  # noqa: ANN202
    return current_app.config["SETTINGS"]


def authenticate(email: str, password: str) -> AuthResult:
    """Verify a password and decide what happens next.

    Never raises for a wrong password - callers get a status. Raises only for
    genuine refusals to proceed (a locked account), so the caller does not have
    to distinguish "failed" from "failed in an interesting way".
    """
    settings = _settings()
    session = _db()
    normalized = (email or "").strip().lower()
    email_digest = sha256_hex(normalized)

    with unscoped(session):
        user = session.execute(select(User).where(User.email == normalized)).scalar_one_or_none()

    if user is None:
        # Spend comparable CPU so response time does not reveal that the account
        # is unknown.
        dummy_verify(
            password,
            time_cost=settings.argon2_time_cost,
            memory_cost_kib=settings.argon2_memory_cost_kib,
            parallelism=settings.argon2_parallelism,
        )
        _record_attempt(session, email_digest, None, LoginOutcome.UNKNOWN_USER)
        AUTH_ATTEMPTS.labels("invalid_credentials").inc()
        return AuthResult(AuthStatus.INVALID)

    if user.is_locked:
        _record_attempt(session, email_digest, user, LoginOutcome.LOCKED)
        AUTH_ATTEMPTS.labels("locked").inc()
        _audit(user, AuditAction.AUTH_LOGIN_FAILED, AuditOutcome.DENIED, {"reason": "locked"})
        session.commit()
        raise AccountLocked()

    if user.status in (UserStatus.SUSPENDED, UserStatus.DEACTIVATED) or user.is_deleted:
        _record_attempt(session, email_digest, user, LoginOutcome.DISABLED)
        AUTH_ATTEMPTS.labels("invalid_credentials").inc()
        session.commit()
        return AuthResult(AuthStatus.DISABLED)

    if not verify_password(user.password_hash, password):
        locked = user.record_failed_login(
            settings.login_max_attempts, settings.login_lockout_minutes
        )
        _record_attempt(session, email_digest, user, LoginOutcome.INVALID_CREDENTIALS)
        AUTH_ATTEMPTS.labels("invalid_credentials").inc()
        _audit(
            user,
            AuditAction.AUTH_LOCKED if locked else AuditAction.AUTH_LOGIN_FAILED,
            AuditOutcome.DENIED,
            {"failed_attempts": user.failed_login_count},
            severity=AuditSeverity.WARNING if locked else AuditSeverity.NOTICE,
        )
        session.commit()
        if locked:
            raise AccountLocked()
        return AuthResult(AuthStatus.INVALID)

    # Password is correct from here on.
    user.clear_lockout()

    if needs_rehash(
        user.password_hash or "",
        time_cost=settings.argon2_time_cost,
        memory_cost_kib=settings.argon2_memory_cost_kib,
        parallelism=settings.argon2_parallelism,
    ):
        # Transparent upgrade: we have the plaintext exactly once, here.
        user.password_hash = hash_password(
            password,
            time_cost=settings.argon2_time_cost,
            memory_cost_kib=settings.argon2_memory_cost_kib,
            parallelism=settings.argon2_parallelism,
        )

    if user.mfa_enabled and user.mfa_confirmed_at is not None:
        _record_attempt(session, email_digest, user, LoginOutcome.MFA_REQUIRED)
        AUTH_ATTEMPTS.labels("mfa_required").inc()
        session.commit()
        return AuthResult(AuthStatus.MFA_REQUIRED, user=user)

    result = _establish_session(session, user, mfa_verified=False)
    _record_attempt(session, email_digest, user, LoginOutcome.SUCCESS)
    AUTH_ATTEMPTS.labels("success").inc()
    session.commit()
    return result


def complete_mfa_challenge(user: User, code: str) -> AuthResult:
    """Second factor. Accepts a TOTP code or a one-time recovery code."""
    settings = _settings()
    session = _db()
    email_digest = sha256_hex(user.email)

    accepted, counter = mfa_lib.verify_totp(
        user.mfa_secret,
        code,
        window=settings.mfa_totp_window,
        last_counter=user.mfa_last_counter,
    )

    if accepted:
        # Recording the accepted step is what prevents the same code being
        # replayed inside its own 30-second window.
        user.mfa_last_counter = counter
    elif not _consume_recovery_code(session, user, code):
        _record_attempt(session, email_digest, user, LoginOutcome.MFA_FAILED)
        AUTH_ATTEMPTS.labels("mfa_failed").inc()
        _audit(user, AuditAction.AUTH_MFA_FAILED, AuditOutcome.DENIED, {})
        user.record_failed_login(settings.login_max_attempts, settings.login_lockout_minutes)
        session.commit()
        raise MFAInvalid()

    user.clear_lockout()
    result = _establish_session(session, user, mfa_verified=True)
    _record_attempt(session, email_digest, user, LoginOutcome.SUCCESS)
    AUTH_ATTEMPTS.labels("success").inc()
    _audit(
        user,
        AuditAction.AUTH_MFA_VERIFIED,
        AuditOutcome.SUCCESS,
        {"method": "totp" if accepted else "recovery_code"},
    )
    session.commit()
    return result


def _establish_session(session: Session, user: User, *, mfa_verified: bool) -> AuthResult:
    user_session, token = create_user_session(user, mfa_verified=mfa_verified, session=session)
    user.last_login_at = utcnow()
    user.last_login_ip = request.remote_addr if request else None
    _audit(user, AuditAction.AUTH_LOGIN_SUCCEEDED, AuditOutcome.SUCCESS, {"mfa": mfa_verified})

    status = (
        AuthStatus.PASSWORD_CHANGE_REQUIRED if user.must_change_password else AuthStatus.SUCCESS
    )
    return AuthResult(status, user=user, session=user_session, session_token=token)


def _consume_recovery_code(session: Session, user: User, code: str) -> bool:
    """Match and burn a recovery code. Single use, enforced atomically."""
    digest = mfa_lib.hash_recovery_code(code)
    with unscoped(session):
        recovery = session.execute(
            select(MfaRecoveryCode).where(
                MfaRecoveryCode.user_id == user.id,
                MfaRecoveryCode.code_hash == digest,
                MfaRecoveryCode.used_at.is_(None),
            )
        ).scalar_one_or_none()

    if recovery is None:
        return False

    recovery.used_at = utcnow()
    recovery.used_ip = request.remote_addr if request else None
    session.flush()
    log.warning(
        "mfa recovery code used",
        extra={"event": "auth.recovery_code_used", "actor_id": user.id},
    )
    return True


def enroll_mfa(user: User, *, session: Session | None = None) -> mfa_lib.MfaEnrollment:
    """Begin TOTP enrolment.

    The secret is stored immediately but ``mfa_enabled`` stays false until a
    code is confirmed - otherwise a user who scans the QR and loses their phone
    before confirming is locked out by their own half-finished enrolment.
    """
    db_session = session or _db()
    settings = _settings()

    secret = mfa_lib.generate_totp_secret()
    codes = mfa_lib.generate_recovery_codes(settings.mfa_recovery_code_count)

    user.mfa_secret = secret
    user.mfa_confirmed_at = None
    user.mfa_last_counter = None

    with unscoped(db_session):
        existing = db_session.execute(
            select(MfaRecoveryCode).where(MfaRecoveryCode.user_id == user.id)
        ).scalars()
        for row in existing:
            db_session.delete(row)

    for code in codes:
        db_session.add(
            MfaRecoveryCode(
                org_id=user.org_id, user_id=user.id, code_hash=mfa_lib.hash_recovery_code(code)
            )
        )
    db_session.flush()

    return mfa_lib.MfaEnrollment(
        secret=secret,
        provisioning_uri=mfa_lib.provisioning_uri(secret, user.email, settings.mfa_issuer),
        recovery_codes=codes,
    )


def confirm_mfa(user: User, code: str, *, session: Session | None = None) -> None:
    """Finish enrolment by proving the authenticator works."""
    db_session = session or _db()
    settings = _settings()

    accepted, counter = mfa_lib.verify_totp(user.mfa_secret, code, window=settings.mfa_totp_window)
    if not accepted:
        raise MFAInvalid()

    user.mfa_enabled = True
    user.mfa_confirmed_at = utcnow()
    user.mfa_last_counter = counter
    _audit(user, AuditAction.AUTH_MFA_ENROLLED, AuditOutcome.SUCCESS, {})
    db_session.flush()


def change_password(
    user: User,
    *,
    current_password: str | None,
    new_password: str,
    require_current: bool = True,
    session: Session | None = None,
) -> None:
    """Change a password, enforcing policy, history, and session invalidation."""
    db_session = session or _db()
    settings = _settings()

    if require_current and not verify_password(user.password_hash, current_password or ""):
        raise InvalidCredentials("Your current password is not correct.")

    strength = validate_password(
        new_password,
        min_length=settings.password_min_length,
        max_length=settings.password_max_length,
        user_terms=[user.email.split("@")[0], user.full_name, "atlas"],
    )
    if not strength.acceptable:
        raise ValidationFailed(
            "The new password does not meet policy.",
            details=[{"field": "new_password", "message": reason} for reason in strength.reasons],
        )

    if _matches_recent_password(db_session, user, new_password, settings.password_history_depth):
        raise ValidationFailed(
            "The new password matches one you have used recently.",
            details=[
                {"field": "new_password", "message": "Choose a password you have not used before."}
            ],
        )

    if user.password_hash:
        db_session.add(
            PasswordHistory(org_id=user.org_id, user_id=user.id, password_hash=user.password_hash)
        )

    user.password_hash = hash_password(
        new_password,
        time_cost=settings.argon2_time_cost,
        memory_cost_kib=settings.argon2_memory_cost_kib,
        parallelism=settings.argon2_parallelism,
    )
    user.password_changed_at = utcnow()
    user.must_change_password = False
    # Invalidates every remember-me cookie issued before this moment.
    user.credential_version += 1

    revoke_all_sessions(user, reason="password_changed", session=db_session)
    _audit(user, AuditAction.AUTH_PASSWORD_CHANGED, AuditOutcome.SUCCESS, {})
    db_session.flush()


def _matches_recent_password(session: Session, user: User, candidate: str, depth: int) -> bool:
    if depth <= 0:
        return False
    if verify_password(user.password_hash, candidate):
        return True

    with unscoped(session):
        history = (
            session.execute(
                select(PasswordHistory)
                .where(PasswordHistory.user_id == user.id)
                .order_by(PasswordHistory.created_at.desc())
                .limit(depth)
            )
            .scalars()
            .all()
        )
    return any(verify_password(entry.password_hash, candidate) for entry in history)


def request_password_reset(email: str) -> str | None:
    """Issue a reset token.

    Always returns without error, whether or not the address is known - the
    caller shows the same "check your email" message either way. Returns the
    plaintext token for the mail service, or ``None`` if there was no account.
    """
    settings = _settings()
    session = _db()
    normalized = (email or "").strip().lower()

    with unscoped(session):
        user = session.execute(select(User).where(User.email == normalized)).scalar_one_or_none()

    if user is None or not user.is_active:
        log.info(
            "password reset requested for unknown or inactive account",
            extra={"event": "auth.reset_noop"},
        )
        return None

    now = utcnow()
    with unscoped(session):
        outstanding = session.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.superseded_at.is_(None),
            )
        ).scalars()
        # A new request invalidates older ones, so a forwarded email cannot be
        # used after the real owner has requested their own reset.
        for token_row in outstanding:
            token_row.superseded_at = now

    plaintext = new_token(RESET_TOKEN_PREFIX)
    session.add(
        PasswordResetToken(
            org_id=user.org_id,
            user_id=user.id,
            token_hash=hash_token(plaintext),
            expires_at=now + dt.timedelta(minutes=settings.password_reset_ttl_minutes),
            requested_ip=request.remote_addr if request else None,
        )
    )
    _audit(user, AuditAction.AUTH_PASSWORD_RESET_REQUESTED, AuditOutcome.SUCCESS, {})
    session.commit()
    return plaintext


def complete_password_reset(token: str, new_password: str) -> User:
    """Consume a reset token and set a new password."""
    session = _db()

    with unscoped(session):
        reset = session.execute(
            select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_token(token))
        ).scalar_one_or_none()

        if reset is None or not reset.is_usable:
            raise ValidationFailed("This password reset link is invalid or has expired.")

        user = session.get(User, reset.user_id)

    if user is None or not user.is_active:
        raise ValidationFailed("This password reset link is invalid or has expired.")

    change_password(
        user,
        current_password=None,
        new_password=new_password,
        require_current=False,
        session=session,
    )
    reset.used_at = utcnow()
    _audit(user, AuditAction.AUTH_PASSWORD_RESET_COMPLETED, AuditOutcome.SUCCESS, {})
    session.commit()
    return user


def _record_attempt(
    session: Session, email_digest: str, user: User | None, outcome: LoginOutcome
) -> None:
    from app.context import current_context

    ctx = current_context()
    session.add(
        LoginAttempt(
            email_hash=email_digest,
            user_id=user.id if user else None,
            org_id=user.org_id if user else None,
            outcome=outcome,
            ip_address=request.remote_addr if request else None,
            user_agent=(request.user_agent.string[:512] if request else None),
            correlation_id=ctx.correlation_id if ctx else None,
        )
    )
    session.flush()


def _audit(
    user: User,
    action: str,
    outcome: AuditOutcome,
    payload: dict,
    severity: AuditSeverity = AuditSeverity.INFO,
) -> None:
    record_audit_event(
        action=action,
        resource_type="User",
        resource_id=user.id,
        resource_label=user.label,
        outcome=outcome,
        severity=severity,
        payload=payload,
        org_id=user.org_id,
        actor_id=user.id,
        actor_label=user.label,
    )


__all__ += ["confirm_mfa"]

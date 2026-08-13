"""Sign-in, multi-factor challenge, and sign-out for the browser surfaces.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from urllib.parse import urlparse

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.wrappers import Response

from app.errors import AccountLocked, AuthenticationRequired, MFAInvalid
from app.extensions import current_session, db, limiter
from app.models.audit import AuditAction
from app.models.base import unscoped
from app.models.iam import User
from app.security import session as session_helpers
from app.services.audit.recorder import record_audit_event
from app.services.iam import auth_service
from app.services.iam.session_service import revoke_session

auth_bp = Blueprint("auth", __name__)

__all__ = ["auth_bp"]


def _rate_limit() -> str:
    return current_app.config["SETTINGS"].ratelimit_auth


def _safe_next(target: str | None) -> str:
    """Only ever redirect to a path on this host.

    An open redirect on a login page is a phishing primitive: the victim sees a
    legitimate domain, signs in, and lands wherever the attacker chose.
    """
    if not target:
        return url_for("public.index")
    if not target.startswith("/"):
        return url_for("public.index")
    if target.startswith("//") or "://" in target:
        return url_for("public.index")
    # A backslash is the bypass: browsers normalise "/\evil.test" to
    # "//evil.test", which is protocol-relative and therefore offsite. The
    # three checks above all pass it.
    if "\\" in target:
        return url_for("public.index")
    # Belt and braces against anything else that parses as absolute.
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return url_for("public.index")
    return target


def _echoable_next(target: str | None) -> str:
    """What is safe to place back into the form.

    Empty rather than the index path: the field means "where the user was
    going", and a value nobody asked for is worse than none.
    """
    if not target:
        return ""
    return target if _safe_next(target) == target else ""


@auth_bp.get("/login")
def login_form() -> ResponseReturnValue:
    if getattr(current_user, "is_authenticated", False):
        return redirect(url_for("public.index"))
    # Sanitised on the way *in* as well as on the way out. The template escapes
    # it, so echoing a hostile value back is inert rather than XSS - but a page
    # that carries "javascript:..." in a field is one refactor away from
    # putting it in an href, and there is no reason to hold it at all.
    return render_template("auth/login.html", next=_echoable_next(request.args.get("next")))


@auth_bp.post("/login")
@limiter.limit(_rate_limit)
def login() -> ResponseReturnValue:
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    next_target = _echoable_next(request.form.get("next"))

    try:
        result = auth_service.authenticate(email, password)
    except AccountLocked:
        flash("This account is temporarily locked after repeated failed attempts.", "error")
        return render_template("auth/login.html", next=next_target), 423

    if result.status in (auth_service.AuthStatus.INVALID, auth_service.AuthStatus.DISABLED):
        # One message for both cases: the form must not reveal which addresses
        # have accounts.
        flash("Those credentials are not valid.", "error")
        return render_template("auth/login.html", next=next_target), 401

    if result.status == auth_service.AuthStatus.MFA_REQUIRED:
        if result.user is None:  # pragma: no cover - defensive
            flash("Those credentials are not valid.", "error")
            return render_template("auth/login.html", next=next_target), 401
        session_helpers.set_mfa_pending(result.user.id)
        return redirect(url_for("auth.mfa_form", next=next_target))

    _establish(result)
    return redirect(_safe_next(next_target))


@auth_bp.get("/mfa")
def mfa_form() -> ResponseReturnValue:
    if not session_helpers.get_mfa_pending():
        return redirect(url_for("auth.login_form"))
    return render_template("auth/mfa.html", next=request.args.get("next", ""))


@auth_bp.post("/mfa")
@limiter.limit(_rate_limit)
def mfa() -> ResponseReturnValue:
    pending_id = session_helpers.get_mfa_pending()
    if not pending_id:
        return redirect(url_for("auth.login_form"))

    with unscoped(current_session()):
        user = db.session.get(User, pending_id)
    if user is None or not user.is_active:
        session_helpers.clear_mfa_pending()
        return redirect(url_for("auth.login_form"))

    next_target = _echoable_next(request.form.get("next"))
    try:
        result = auth_service.complete_mfa_challenge(user, request.form.get("code") or "")
    except (MFAInvalid, AccountLocked):
        flash("That verification code is not valid.", "error")
        return render_template("auth/mfa.html", next=next_target), 401

    session_helpers.clear_mfa_pending()
    _establish(result)
    return redirect(_safe_next(next_target))


def _establish(result: auth_service.AuthResult) -> None:
    # Explicit, not `assert`: assertions are stripped under `python -O`, and
    # this guards the transition into an authenticated session.
    if result.user is None or result.session_token is None:  # pragma: no cover - defensive
        raise AuthenticationRequired()

    session_helpers.rotate_session()
    login_user(result.user, remember=False)
    session_helpers.store_session_token(result.session_token)
    if result.status == auth_service.AuthStatus.PASSWORD_CHANGE_REQUIRED:
        flash("Please choose a new password before continuing.", "warning")


@auth_bp.post("/logout")
@login_required
def logout() -> Response:
    user_session = getattr(current_user, "_atlas_session", None)
    if user_session is not None:
        revoke_session(user_session, reason="user_logout")
    record_audit_event(
        action=AuditAction.AUTH_LOGOUT,
        resource_type="User",
        resource_id=current_user.id,
        org_id=current_user.org_id,
    )
    db.session.commit()

    logout_user()
    session_helpers.clear_session_token()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login_form"))


@auth_bp.get("/reset")
def reset_form() -> str:
    return render_template("auth/reset.html", token=request.args.get("token", ""))

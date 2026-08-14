"""API v1.

Cross-cutting concerns are applied once, at the blueprint, rather than being
repeated in every handler:

* **Authentication** is required for everything except the explicitly public
  endpoints. Opt-out rather than opt-in - a new route is protected by default,
  which is the only safe direction for that default to point.
* **CSRF** applies to cookie-authenticated unsafe requests only. Bearer-token
  callers are not cookie-authenticated, so CSRF does not apply to them, and
  demanding a token they cannot obtain would just push integrators toward
  disabling it.
* **Idempotency** is honoured on every unsafe request carrying the header.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from flask import Blueprint, Response, request
from flask_login import current_user

from app.errors import AuthenticationRequired
from app.logging import get_logger

api_v1_bp = Blueprint("api_v1", __name__)
log = get_logger("api.v1")

__all__ = ["api_v1_bp"]

#: Endpoint names reachable without authentication.
PUBLIC_ENDPOINTS: frozenset[str] = frozenset(
    {
        "api_v1.auth_login",
        "api_v1.auth_mfa",
        "api_v1.auth_password_reset_request",
        "api_v1.auth_password_reset_complete",
        "api_v1.meta_index",
        # Authenticated by a signed, expiring token rather than by session, so a
        # retrieval link can be emailed. The token carries the organization it
        # was minted for, and a quarantined document is refused regardless.
        "api_v1.documents_download",
    }
)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@api_v1_bp.before_request
def _require_authentication() -> None:
    if request.method == "OPTIONS":
        return
    if request.endpoint in PUBLIC_ENDPOINTS:
        return
    if not getattr(current_user, "is_authenticated", False):
        raise AuthenticationRequired()


@api_v1_bp.before_request
def _enforce_csrf_for_cookie_sessions() -> None:
    """CSRF applies only when the browser's cookie is what authenticated us."""
    if request.method in _SAFE_METHODS or request.endpoint in PUBLIC_ENDPOINTS:
        return
    if getattr(request, "atlas_token_id", None):
        return  # Bearer token: not cookie-authenticated, so not CSRF-able.
    if not getattr(current_user, "is_authenticated", False):
        return

    from flask_wtf.csrf import CSRFError, validate_csrf

    from app.errors import PermissionDenied

    settings_obj = request.environ.get("atlas.settings")
    del settings_obj  # settings are read from the app config below

    from flask import current_app

    if not current_app.config.get("WTF_CSRF_ENABLED", True):
        return

    token = request.headers.get("X-CSRFToken") or request.headers.get("X-CSRF-Token")
    try:
        validate_csrf(token)
    except CSRFError as exc:
        log.warning(
            "csrf validation failed",
            extra={"event": "security.csrf_failed", "path": request.path},
        )
        raise PermissionDenied("A valid CSRF token is required for this request.") from exc


@api_v1_bp.before_request
def _begin_idempotency() -> Response | None:
    if request.method in _SAFE_METHODS or request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if not request.headers.get("Idempotency-Key"):
        return None

    from app.api.idempotency import begin_idempotent_request
    from app.context import current_org_id

    org_id = current_org_id()
    if not org_id:
        return None
    return begin_idempotent_request(org_id)


@api_v1_bp.after_request
def _finish_idempotency(response: Response) -> Response:
    from app.api.idempotency import finish_idempotent_request

    return finish_idempotent_request(response)


def _register_routes() -> None:
    """Import route modules for their side effects."""
    from app.api.v1.routes import (  # noqa: F401
        accounting,
        auth,
        documents,
        imports,
        leasing,
        maintenance,
        meta,
        portfolio,
    )


_register_routes()

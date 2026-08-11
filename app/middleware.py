"""Request lifecycle middleware.

Runs, in order, on the way in:

1. **Proxy trust** - normalise ``X-Forwarded-*`` only for the number of proxies
   actually in front of us. Trusting an unbounded chain lets a client forge its
   own source IP, which silently defeats rate limiting and IP allowlisting.
2. **Correlation** - adopt an inbound correlation ID or mint one, and bind the
   ambient context that logging, auditing, and the tenancy guard read.
3. **Organization resolution** - decide which tenant this request operates on,
   and verify the caller may act there *before* any handler runs.

And on the way out: security headers, cache directives, and correlation echo.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from flask import Flask, Response, g, request
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import BaseConverter

from app.context import (
    RequestContext,
    bind_context,
    clear_context,
    current_context,
    get_correlation_id,
    new_correlation_id,
)

__all__ = ["get_correlation_id", "init_middleware", "require_org_scope"]

CORRELATION_HEADER = "X-Correlation-ID"
REQUEST_ID_HEADER = "X-Request-ID"
ORG_HEADER = "X-Atlas-Organization"

#: Inbound correlation IDs are echoed into logs, so they are constrained to a
#: conservative character set and length - a log-injection payload must not be
#: able to arrive this way.
_SAFE_CORRELATION = re.compile(r"^[A-Za-z0-9._\-]{8,128}$")

#: Paths that must never be cached or logged with a body.
_SENSITIVE_PATH_PREFIXES = ("/api/", "/auth/", "/admin/", "/resident/", "/owner/", "/vendor/")


def init_middleware(app: Flask) -> None:
    settings = app.config["SETTINGS"]

    if settings.trusted_proxy_count > 0:
        count = settings.trusted_proxy_count
        app.wsgi_app = ProxyFix(  # type: ignore[method-assign]
            app.wsgi_app, x_for=count, x_proto=count, x_host=count, x_port=count, x_prefix=count
        )

    @app.before_request
    def _bind_request_context() -> None:
        correlation_id = _inbound_correlation_id()
        ctx = RequestContext(
            correlation_id=correlation_id,
            request_id=uuid.uuid4().hex,
            ip_address=request.remote_addr,
            user_agent=(request.user_agent.string or "")[:512],
            source="http",
        )
        bind_context(ctx)
        g.atlas_context = ctx

    @app.before_request
    def _resolve_organization() -> None:
        from flask_login import current_user

        ctx = current_context()
        if ctx is None:  # pragma: no cover - _bind_request_context always runs first
            return

        user = current_user if getattr(current_user, "is_authenticated", False) else None
        if user is None:
            return

        # The bare primary key, not Flask-Login's ``get_id()`` - that returns a
        # composite "<id>:<credential version>" for remember-cookie
        # invalidation, and it lands in UUID actor columns.
        ctx.actor_id = getattr(user, "id", None)
        ctx.actor_type = getattr(user, "actor_type", "user")
        ctx.session_id = getattr(user, "current_session_id", None)
        ctx.org_id = _resolve_org_scope(user)

    @app.after_request
    def _finalize(response: Response) -> Response:
        correlation_id = get_correlation_id()
        if correlation_id:
            response.headers[CORRELATION_HEADER] = correlation_id
        ctx = current_context()
        if ctx and ctx.request_id:
            response.headers[REQUEST_ID_HEADER] = ctx.request_id

        _apply_security_headers(response, settings)
        _apply_cache_policy(response)
        return response

    @app.teardown_request
    def _clear(exc: BaseException | None) -> None:  # noqa: ARG001
        clear_context()


def _inbound_correlation_id() -> str:
    """Adopt a caller-supplied correlation ID when it is well-formed."""
    supplied = request.headers.get(CORRELATION_HEADER, "").strip()
    if supplied and _SAFE_CORRELATION.match(supplied):
        return supplied
    return new_correlation_id()


def _resolve_org_scope(user: Any) -> str | None:
    """Determine which organization this request acts on.

    Defaults to the caller's home organization. A caller may request a different
    one - platform operators and users with multi-organization assignments do
    this routinely - but the request is only honoured after the user object
    confirms access. An unauthorised switch is reported as "not found" so the
    header cannot be used to probe which organization IDs exist.
    """
    requested = (request.headers.get(ORG_HEADER) or request.args.get("org_id") or "").strip()
    home_org = getattr(user, "org_id", None)

    if not requested:
        return home_org

    try:
        uuid.UUID(requested)
    except ValueError:
        from app.errors import ValidationFailed

        raise ValidationFailed("Organization identifier is malformed.") from None

    if requested == home_org:
        return home_org

    can_access = getattr(user, "can_access_org", None)
    if callable(can_access) and can_access(requested):
        return requested

    from app.errors import TenantIsolationViolation

    raise TenantIsolationViolation(
        f"Actor {user.get_id()} attempted to act on organization {requested}."
    )


def require_org_scope() -> str:
    """Return the current organization scope or refuse to continue.

    Handlers that operate on tenant data call this instead of reading the
    context directly, so "no tenant resolved" fails loudly at the top of the
    handler rather than as an empty result set further down.
    """
    ctx = current_context()
    if ctx is None or not ctx.org_id:
        from app.errors import PermissionDenied

        raise PermissionDenied("No organization scope is active for this request.")
    return ctx.org_id


def _apply_security_headers(response: Response, settings: Any) -> None:
    """Headers Talisman does not set, or sets less strictly than we want."""
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    headers.setdefault(
        "Permissions-Policy",
        "geolocation=(self), camera=(self), microphone=(), payment=(), usb=(), interest-cohort=()",
    )
    if settings.force_https:
        headers.setdefault(
            "Strict-Transport-Security",
            f"max-age={settings.hsts_max_age}; includeSubDomains; preload",
        )
    # Werkzeug advertises its version by default; that is free reconnaissance.
    headers["Server"] = "Atlas"


def _apply_cache_policy(response: Response) -> None:
    """Never let an intermediary cache tenant data."""
    if "Cache-Control" in response.headers:
        return
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return
    if any(request.path.startswith(prefix) for prefix in _SENSITIVE_PATH_PREFIXES):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"


class IdentifierConverter(BaseConverter):
    """A URL segment that must be a UUID to name anything at all.

    Registered as the default converter for ``*_id`` parameters, so a malformed
    identifier is a 404 from the router rather than a ValueError from the
    database driver surfacing as a 500. The GUID type deliberately validates at
    the bind boundary - that is right, and it is why the failure has to be
    caught before the query rather than after it.

    404 rather than 400: a non-identifier cannot name a record, and answering
    differently for "malformed" and "not yours" hands an attacker an oracle.
    """

    #: Matches the canonical hyphenated form, case-insensitively. Anything else
    #: - a path traversal, an injection payload, a bare integer - does not
    #: match the rule and never reaches a view.
    regex = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

    def to_python(self, value: str) -> str:
        return value.lower()

    def to_url(self, value: object) -> str:
        return str(value)


def register_url_converters(app: Flask) -> None:
    """Install the identifier converter under a name routes can use."""
    app.url_map.converters["id"] = IdentifierConverter

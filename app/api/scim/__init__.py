"""SCIM 2.0, for the customer's directory to own the user list.

This blueprint is deliberately separate from ``/api/v1``. Everything there is
authenticated as a *person* — a session cookie or a user's bearer token — and
everything here is authenticated as an *integration*: the identity provider
presents a token issued to it, and the token is the claim about which tenant it
speaks for. Bolting that onto the v1 authentication hook would have meant an
exception in the one place that must not have exceptions.

Three consequences follow and each is enforced here rather than hoped for:

The tenant comes from the token, never from the request. A directory that could
name its own tenant could deactivate a different company's staff.

CSRF does not apply, because nothing here is cookie-authenticated. Demanding a
token an identity provider cannot obtain would simply push integrators into
turning the integration off.

Errors are returned in SCIM's own envelope, not Atlas's. A directory parses
``{"schemas": [...], "status": "404"}`` and will treat anything else as a
transport failure and retry it — which is how a deactivation gets attempted
four times and reported as an outage.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, g, jsonify, request

from app.errors import AtlasError, Conflict, NotFound, PermissionDenied, ValidationFailed
from app.logging import get_logger

scim_bp = Blueprint("scim", __name__)
log = get_logger("api.scim")

__all__ = ["scim_bp"]

SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"

#: SCIM's own content type. Directories send it; some also insist on it back.
SCIM_CONTENT_TYPE = "application/scim+json"


def scim_error(status: int, detail: str, *, scim_type: str | None = None) -> Response:
    """An error a directory will understand rather than retry blindly."""
    body: dict[str, Any] = {"schemas": [SCIM_ERROR_SCHEMA], "status": str(status), "detail": detail}
    if scim_type:
        body["scimType"] = scim_type
    response = jsonify(body)
    response.status_code = status
    response.mimetype = SCIM_CONTENT_TYPE
    return response


def scim_response(body: Any, status: int = 200, *, location: str | None = None) -> Response:
    response = jsonify(body)
    response.status_code = status
    response.mimetype = SCIM_CONTENT_TYPE
    if location:
        response.headers["Location"] = location
    return response


@scim_bp.before_request
def _authenticate_the_directory() -> Response | None:
    """Establish which provider — and therefore which tenant — is calling.

    Binds the tenant context from the token. Nothing downstream reads an
    organization id off the request, because a directory that could name its
    own tenant could deactivate a different company's staff.
    """
    from app.context import RequestContext, bind_context, new_correlation_id
    from app.extensions import current_session
    from app.services.iam.scim import provider_for_token

    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return scim_error(401, "A SCIM request must present a bearer token.")

    try:
        provider = provider_for_token(current_session(), token.strip())
    except PermissionDenied as exc:
        # Deliberately the same shape for an unknown token and a disabled
        # integration: telling a caller which one it was is a probe.
        log.warning(
            "scim authentication refused",
            extra={"event": "scim.auth_refused", "reason": str(exc)},
        )
        return scim_error(401, "That SCIM token is not recognised.")

    g.scim_provider = provider
    g.scim_context_token = bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=provider.org_id,
            actor_type="integration",
            source=f"scim:{provider.code}",
        )
    )
    return None


@scim_bp.teardown_request
def _release_context(exc: BaseException | None) -> None:
    from app.context import clear_context

    token = g.pop("scim_context_token", None)
    if token is not None:
        clear_context(token)


@scim_bp.errorhandler(AtlasError)
def _atlas_error(exc: AtlasError) -> Response:
    """Translate Atlas's refusals into SCIM's envelope."""
    status = {
        ValidationFailed: 400,
        PermissionDenied: 403,
        NotFound: 404,
        Conflict: 409,
    }.get(type(exc), 400)
    scim_type = {400: "invalidValue", 409: "uniqueness"}.get(status)
    return scim_error(status, str(exc), scim_type=scim_type)


from app.api.scim import routes  # noqa: E402,F401  - registers the endpoints

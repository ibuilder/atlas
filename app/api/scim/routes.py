"""The SCIM endpoints themselves.

Every handler takes the organization from ``g.scim_provider`` rather than from
anything in the request. The token is the claim about which tenant is being
provisioned, and accepting a second claim alongside it would mean deciding
which one wins.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from typing import Any

from flask import Response, g, request, url_for

from app.api.scim import scim_bp, scim_error, scim_response
from app.extensions import current_session, db
from app.services.iam import scim

__all__ = []


def _payload() -> dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError("A SCIM request body must be a JSON object.")
    return body


def _location(user_id: str) -> str:
    return url_for("scim.get_user", user_id=user_id, _external=False)


@scim_bp.get("/v2/ServiceProviderConfig")
def service_provider_config() -> Response:
    """What this implementation actually supports.

    Directories read this before deciding what to send. Claiming a capability
    here that the endpoints do not have is how a sync fails halfway through
    rather than refusing at the start.
    """
    return scim_response(
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "documentationUri": "https://github.com/ibuilder/atlas",
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            # Only `userName eq "..."`, which is what directories send to check
            # whether a user exists before creating one.
            "filter": {"supported": True, "maxResults": scim.MAX_PAGE_SIZE},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "OAuth Bearer Token",
                    "description": "A token issued to this identity provider by an "
                    "Atlas administrator.",
                }
            ],
        }
    )


@scim_bp.get("/v2/Users")
def list_users() -> Response:
    """List users, honouring the one filter directories actually send.

    A filter this implementation does not understand is refused rather than
    ignored: silently returning everybody to a query meant to match one person
    is how a sync decides to deactivate the entire company.
    """
    provider = g.scim_provider
    body = scim.list_users(
        current_session(),
        org_id=provider.org_id,
        filter_expression=request.args.get("filter"),
        start_index=int(request.args.get("startIndex", 1) or 1),
        count=int(request.args.get("count", 100) or 100),
    )
    return scim_response(body)


@scim_bp.get("/v2/Users/<id:user_id>")
def get_user(user_id: str) -> Response:
    provider = g.scim_provider
    user = scim.user_by_id(current_session(), org_id=provider.org_id, user_id=user_id)
    return scim_response(scim.to_scim_user(user, location=_location(user.id)))


@scim_bp.post("/v2/Users")
def create_user() -> Response:
    """Provision a user the directory has announced."""
    provider = g.scim_provider
    try:
        payload = _payload()
    except ValueError as exc:
        return scim_error(400, str(exc), scim_type="invalidSyntax")

    result = scim.create_user_resource(
        current_session(), org_id=provider.org_id, provider=provider, payload=payload
    )
    db.session.commit()
    return scim_response(
        scim.to_scim_user(result.user, location=_location(result.user.id)),
        status=201 if result.created else 200,
        location=_location(result.user.id),
    )


@scim_bp.put("/v2/Users/<id:user_id>")
def replace_user(user_id: str) -> Response:
    provider = g.scim_provider
    try:
        payload = _payload()
    except ValueError as exc:
        return scim_error(400, str(exc), scim_type="invalidSyntax")

    result = scim.replace_user_resource(
        current_session(),
        org_id=provider.org_id,
        provider=provider,
        user=scim.user_by_id(current_session(), org_id=provider.org_id, user_id=user_id),
        payload=payload,
    )
    db.session.commit()
    return scim_response(scim.to_scim_user(result.user, location=_location(result.user.id)))


@scim_bp.patch("/v2/Users/<id:user_id>")
def patch_user(user_id: str) -> Response:
    """The operation offboarding actually runs.

    ``active: false`` disables the account *and revokes its sessions* in one
    operation. Marking a user inactive while leaving a live session token is
    offboarding that does not offboard.
    """
    provider = g.scim_provider
    try:
        payload = _payload()
    except ValueError as exc:
        return scim_error(400, str(exc), scim_type="invalidSyntax")

    result = scim.apply_patch(
        current_session(),
        org_id=provider.org_id,
        provider=provider,
        user=scim.user_by_id(current_session(), org_id=provider.org_id, user_id=user_id),
        payload=payload,
    )
    db.session.commit()
    return scim_response(scim.to_scim_user(result.user, location=_location(result.user.id)))


@scim_bp.delete("/v2/Users/<id:user_id>")
def delete_user(user_id: str) -> Response:
    """Deactivate. Never a row removal.

    A user id appears on ledger entries, audit events, and approvals. Deleting
    the row would either cascade into financial history or leave dangling
    references, so DELETE deactivates and returns 204 as the directory expects.
    """
    provider = g.scim_provider
    scim.deactivate_resource(
        current_session(),
        org_id=provider.org_id,
        provider=provider,
        user=scim.user_by_id(current_session(), org_id=provider.org_id, user_id=user_id),
    )
    db.session.commit()
    return Response(status=204)

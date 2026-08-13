"""OpenAPI 3.1 generation.

The spec is derived from the live URL map and the pydantic models, so it cannot
drift from the code the way a hand-maintained document does. Handler docstrings
become endpoint descriptions, which keeps the incentive right: documenting a
route improves both the code and the spec.

The documentation page is rendered server-side from the spec rather than loading
a viewer from a CDN, because the application's CSP forbids external scripts -
and a docs page that requires relaxing the CSP is not worth the relaxation.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import re
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, render_template

from app import __version__

openapi_bp = Blueprint("openapi", __name__)

__all__ = ["build_spec", "openapi_bp"]

_PATH_PARAM = re.compile(r"<(?:[^:<>]+:)?([^<>]+)>")

#: Human-readable groupings, in the order they appear in the docs.
TAG_ORDER: tuple[tuple[str, str], ...] = (
    ("Authentication", "Sign-in, multi-factor, and credential management."),
    ("Portfolio", "Properties, units, and owners."),
    ("Leasing", "Leads, residents, and leases."),
    ("Maintenance", "Requests and work orders."),
    ("Accounting", "Invoices, payments, and the general ledger."),
    ("Platform", "Service metadata, audit trail, and dashboards."),
)

_TAG_BY_PREFIX: tuple[tuple[str, str], ...] = (
    ("/auth", "Authentication"),
    ("/properties", "Portfolio"),
    ("/units", "Portfolio"),
    ("/owners", "Portfolio"),
    ("/leads", "Leasing"),
    ("/residents", "Leasing"),
    ("/leases", "Leasing"),
    ("/requests", "Maintenance"),
    ("/work-orders", "Maintenance"),
    ("/invoices", "Accounting"),
    ("/payments", "Accounting"),
    ("/ledger", "Accounting"),
)


def _tag_for(path: str) -> str:
    for prefix, tag in _TAG_BY_PREFIX:
        if path.startswith(prefix):
            return tag
    return "Platform"


def build_spec() -> dict[str, Any]:
    """Assemble the OpenAPI document from the running application."""
    settings = current_app.config["SETTINGS"]
    paths: dict[str, dict[str, Any]] = {}

    for rule in sorted(current_app.url_map.iter_rules(), key=lambda r: str(r.rule)):
        if not str(rule.rule).startswith("/api/v1"):
            continue
        if rule.endpoint.endswith("static"):
            continue

        relative = str(rule.rule)[len("/api/v1") :] or "/"
        spec_path = _PATH_PARAM.sub(r"{\1}", str(rule.rule))
        view = current_app.view_functions.get(rule.endpoint)
        summary, description = _describe(view)

        parameters = [
            {
                "name": name,
                "in": "path",
                "required": True,
                "schema": {"type": "string", "format": "uuid"},
            }
            for name in sorted(rule.arguments)
        ]

        for method in sorted(rule.methods or set()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            operation: dict[str, Any] = {
                "operationId": f"{rule.endpoint.replace('.', '_')}_{method.lower()}",
                "summary": summary,
                "description": description,
                "tags": [_tag_for(relative)],
                "parameters": list(parameters),
                "responses": _responses(method),
            }
            if method in {"POST", "PUT", "PATCH"}:
                operation["requestBody"] = {
                    "required": True,
                    "content": {"application/json": {"schema": {"type": "object"}}},
                }
                operation["parameters"].append(
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": False,
                        "description": (
                            "Retry-safe key. Replaying the same key with the same body "
                            "returns the original response; a different body is rejected."
                        ),
                        "schema": {"type": "string", "maxLength": 255},
                    }
                )
            if method == "GET" and not rule.arguments:
                operation["parameters"].extend(_collection_parameters())

            paths.setdefault(spec_path, {})[method.lower()] = operation

    return {
        "openapi": "3.1.0",
        "info": {
            "title": f"{settings.app_name} API",
            "version": __version__,
            "summary": "Property operations, unified behind one canonical model.",
            "description": (
                "All responses share one error envelope with a stable machine-readable "
                "`code`. Collections are cursor-paginated. Unsafe requests accept an "
                "`Idempotency-Key` header."
            ),
            "license": {"name": "MIT", "identifier": "MIT"},
        },
        "servers": [{"url": settings.app_url.rstrip("/"), "description": settings.env}],
        "tags": [{"name": name, "description": text} for name, text in TAG_ORDER],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "An Atlas API token (`atlas_api_...`).",
                },
                "sessionCookie": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": settings.session_cookie_name,
                    "description": "Browser session. Unsafe requests also require `X-CSRFToken`.",
                },
            },
            "schemas": _component_schemas(),
        },
        "security": [{"bearerAuth": []}, {"sessionCookie": []}],
    }


def _describe(view: Any) -> tuple[str, str]:
    doc = (view.__doc__ or "").strip() if view else ""
    if not doc:
        return "", ""
    lines = [line.strip() for line in doc.splitlines()]
    summary = lines[0]
    body = "\n".join(lines[1:]).strip()
    return summary, body


def _collection_parameters() -> list[dict[str, Any]]:
    return [
        {
            "name": "cursor",
            "in": "query",
            "required": False,
            "description": "Opaque keyset cursor from a previous `page_info.next_cursor`.",
            "schema": {"type": "string"},
        },
        {
            "name": "limit",
            "in": "query",
            "required": False,
            "schema": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
        },
        {
            "name": "q",
            "in": "query",
            "required": False,
            "description": "Free-text search across the resource's primary labels.",
            "schema": {"type": "string", "maxLength": 200},
        },
    ]


def _responses(method: str) -> dict[str, Any]:
    error = {"$ref": "#/components/schemas/ErrorEnvelope"}
    success_code = "201" if method == "POST" else "200"
    responses: dict[str, Any] = {
        success_code: {
            "description": "Success.",
            "content": {"application/json": {"schema": {"type": "object"}}},
        },
        "401": {
            "description": "Authentication required.",
            "content": {"application/json": {"schema": error}},
        },
        "403": {
            "description": "Permission denied.",
            "content": {"application/json": {"schema": error}},
        },
        "404": {"description": "Not found.", "content": {"application/json": {"schema": error}}},
        "422": {
            "description": "Validation failed.",
            "content": {"application/json": {"schema": error}},
        },
        "429": {"description": "Rate limited.", "content": {"application/json": {"schema": error}}},
    }
    if method in {"POST", "PUT", "PATCH", "DELETE"}:
        responses["409"] = {
            "description": "Conflict, including idempotency-key reuse with a different body.",
            "content": {"application/json": {"schema": error}},
        }
    return responses


def _component_schemas() -> dict[str, Any]:
    """JSON Schema for the shared models, generated from pydantic."""
    from pydantic import BaseModel

    from app.schemas.common import ErrorEnvelope, PageInfo
    from app.schemas.operations import (
        InvoiceOut,
        LeaseOut,
        MaintenanceRequestOut,
        PaymentOut,
        WorkOrderOut,
    )
    from app.schemas.portfolio import PropertyOut, UnitOut

    models: dict[str, type[BaseModel]] = {
        "ErrorEnvelope": ErrorEnvelope,
        "PageInfo": PageInfo,
        "Property": PropertyOut,
        "Unit": UnitOut,
        "Lease": LeaseOut,
        "MaintenanceRequest": MaintenanceRequestOut,
        "WorkOrder": WorkOrderOut,
        "Invoice": InvoiceOut,
        "Payment": PaymentOut,
    }

    schemas: dict[str, Any] = {}
    for name, model in models.items():
        generated = model.model_json_schema(ref_template="#/components/schemas/{model}")
        # Pydantic emits nested models under $defs; OpenAPI wants them hoisted
        # into components alongside their parent.
        for def_name, definition in generated.pop("$defs", {}).items():
            schemas.setdefault(def_name, definition)
        schemas[name] = generated
    return schemas


@openapi_bp.get("/openapi.json", endpoint="spec")
def spec() -> Response:
    return jsonify(build_spec())


@openapi_bp.get("/docs", endpoint="docs")
def docs() -> str:
    document = build_spec()
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name, _ in TAG_ORDER}

    for path, operations in sorted(document["paths"].items()):
        for method, operation in operations.items():
            tag = (operation.get("tags") or ["Platform"])[0]
            grouped.setdefault(tag, []).append(
                {
                    "method": method.upper(),
                    "path": path,
                    "summary": operation.get("summary") or "",
                    "description": operation.get("description") or "",
                }
            )

    return render_template(
        "docs/openapi.html",
        document=document,
        grouped=grouped,
        tag_order=TAG_ORDER,
    )

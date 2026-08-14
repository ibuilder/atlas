"""Bulk import: plan, then apply.

The plan is the point of this module. A CSV of four hundred units is not
something anybody can check by reading, and an import that goes straight to
writing leaves the operator finding out afterwards. The plan step reads only:
it says what would be created, what would be updated and how, and every problem
in the file rather than the first.

The plan is deliberately not stored between the two calls. Storing it would be
worse, not better — applying a decision taken against a database that has moved
since is how an "update" quietly becomes a "create". Apply re-plans the same
bytes and checks the counts the caller was shown against the fresh plan; a
mismatch is refused rather than reconciled, because only a person knows whether
what changed underneath matters.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from flask import Response

from app.api.helpers import parse_body, respond
from app.api.v1 import api_v1_bp
from app.errors import Conflict
from app.extensions import current_session
from app.middleware import require_org_scope
from app.schemas.operations import (
    ImportApplyRequest,
    ImportPlanOut,
    ImportPlanRequest,
    RowErrorOut,
    RowPlanOut,
)
from app.security.permissions import Perm
from app.security.policies import require
from app.services.common.unit_of_work import transaction
from app.services.imports import bulk

__all__ = []


def _as_out(plan: bulk.ImportPlan) -> ImportPlanOut:
    return ImportPlanOut(
        resource=plan.resource,
        is_valid=plan.is_valid,
        creates=plan.creates,
        updates=plan.updates,
        unchanged=plan.unchanged,
        errors=[
            RowErrorOut(row=error.row, column=error.column, message=error.message)
            for error in plan.errors
        ],
        rows=[
            RowPlanOut(row=row.row, key=row.key, action=row.action, changes=row.changes)
            for row in plan.rows
        ],
    )


@api_v1_bp.get("/imports", endpoint="imports_index")
def list_importers() -> Response:
    """What can be imported, and the columns each one needs."""
    require(Perm.IMPORT_RUN)
    return respond(
        {
            "data": [
                {
                    "resource": definition.resource,
                    "name": definition.name,
                    "required_columns": list(definition.required_columns),
                    "optional_columns": list(definition.optional_columns),
                    "max_rows": bulk.MAX_ROWS,
                }
                for definition in (bulk.importer(name) for name in bulk.known_importers())
            ]
        }
    )


@api_v1_bp.get("/imports/<resource>/template", endpoint="imports_template")
def import_template(resource: str) -> Response:
    """A header row for this resource, so nobody has to guess the columns."""
    require(Perm.IMPORT_RUN)
    body = bulk.template_for(resource)
    response = Response(body, mimetype="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="{resource}-template.csv"'
    return response


@api_v1_bp.post("/imports/plan", endpoint="imports_plan")
def plan_import() -> Response:
    """What this file would do. Writes nothing."""
    require(Perm.IMPORT_RUN)
    payload = parse_body(ImportPlanRequest)
    org_id = require_org_scope()

    plan = bulk.plan_import(
        current_session(), org_id=org_id, resource=payload.resource, text=payload.csv
    )
    return respond(_as_out(plan))


@api_v1_bp.post("/imports/apply", endpoint="imports_apply")
def apply_import() -> Response:
    """Write the file, if it still does what the caller was shown.

    Refuses a plan with any error at all. A partial import is worse than a
    failed one: the operator cannot tell which half landed, and re-uploading
    duplicates the half that did.
    """
    require(Perm.IMPORT_RUN)
    payload = parse_body(ImportApplyRequest)
    org_id = require_org_scope()

    with transaction() as session:
        plan = bulk.plan_import(session, org_id=org_id, resource=payload.resource, text=payload.csv)
        expected = (payload.expect_creates, payload.expect_updates, payload.expect_unchanged)
        actual = (plan.creates, plan.updates, plan.unchanged)
        if plan.is_valid and expected != actual:
            raise Conflict(
                "This file no longer does what you were shown: it now plans "
                f"{plan.creates} creates, {plan.updates} updates and {plan.unchanged} "
                f"unchanged, against the {payload.expect_creates}/"
                f"{payload.expect_updates}/{payload.expect_unchanged} you confirmed. "
                "Something changed underneath. Plan it again."
            )
        applied = bulk.apply_plan(session, org_id=org_id, plan=plan, actor_id=None)

    return respond(_as_out(applied))

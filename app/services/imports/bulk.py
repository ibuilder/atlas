"""Bulk import: bringing a portfolio in from a spreadsheet.

Every implementation of this gets used the same way. Somebody exports from
their old system, uploads it, half the rows fail on a date format, they fix the
spreadsheet, and upload the whole thing again. The design follows from that.

**Validate everything before writing anything.** A partial import is worse than
a failed one: the operator cannot tell which half landed, and re-uploading
duplicates the half that did. So the whole file is checked first, and a file
with errors is rejected in full with a per-row report.

**Re-uploading is safe.** Every row is keyed by a natural business identifier -
a property code, a unit number within a property - and an existing record is
*updated* rather than duplicated. That is what makes "fix the spreadsheet and
try again" the obvious move rather than a gamble.

**A dry run is the default posture.** ``plan()`` reports exactly what would be
created and changed, reading only. Nothing about a spreadsheet from an unknown
system deserves to be trusted on the first pass.

Errors are reported with the *spreadsheet's* row number, because that is the
number the person is looking at.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import NotFound, ValidationFailed
from app.logging import get_logger
from app.models.audit import AuditAction, AuditSeverity
from app.services.audit.recorder import record_audit_event

__all__ = [
    "IMPORTERS",
    "ImportPlan",
    "ImporterDefinition",
    "RowError",
    "RowPlan",
    "apply_plan",
    "known_importers",
    "plan_import",
]

log = get_logger("services.imports.bulk")

#: A spreadsheet larger than this is a migration project, not an upload.
MAX_ROWS = 5_000


@dataclass(frozen=True)
class RowError:
    """One problem, located where the person can see it."""

    row: int
    column: str | None
    message: str

    def __str__(self) -> str:
        where = f"row {self.row}" + (f", column '{self.column}'" if self.column else "")
        return f"{where}: {self.message}"


@dataclass
class RowPlan:
    row: int
    key: str
    action: str  # "create" | "update" | "unchanged"
    values: dict[str, Any] = field(default_factory=dict)
    changes: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImportPlan:
    """What a file would do. Produced by reading only."""

    resource: str
    rows: list[RowPlan] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def creates(self) -> int:
        return sum(1 for row in self.rows if row.action == "create")

    @property
    def updates(self) -> int:
        return sum(1 for row in self.rows if row.action == "update")

    @property
    def unchanged(self) -> int:
        return sum(1 for row in self.rows if row.action == "unchanged")


@dataclass(frozen=True)
class ImporterDefinition:
    resource: str
    name: str
    #: Columns that must be present in the header.
    required_columns: tuple[str, ...]
    optional_columns: tuple[str, ...]
    #: (session, org_id, row, row_number) -> (key, values) or raises ValidationFailed.
    parse: Callable[..., tuple[str, dict[str, Any]]]
    #: (session, org_id, key) -> existing record or None.
    find: Callable[..., Any]
    #: (session, org_id, key, values) -> record.
    create: Callable[..., Any]
    #: (session, record, values) -> dict of changed fields.
    update: Callable[..., dict[str, Any]]


IMPORTERS: dict[str, ImporterDefinition] = {}


def register(definition: ImporterDefinition) -> ImporterDefinition:
    if definition.resource in IMPORTERS:  # pragma: no cover - registration is static
        raise RuntimeError(f"Importer {definition.resource!r} is already registered.")
    IMPORTERS[definition.resource] = definition
    return definition


def known_importers() -> list[str]:
    return sorted(IMPORTERS)


def importer(resource: str) -> ImporterDefinition:
    definition = IMPORTERS.get(resource)
    if definition is None:
        raise NotFound(f"No importer for {resource!r}. Available: {', '.join(known_importers())}.")
    return definition


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


def _text(value: Any, *, limit: int = 255) -> str:
    return str(value or "").strip()[:limit]


def _decimal(value: Any, *, column: str) -> Decimal:
    raw = str(value or "").replace(",", "").replace("$", "").strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = "-" + raw[1:-1]
    if not raw:
        return Decimal("0")
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValidationFailed(f"'{value}' in column '{column}' is not a number.") from exc


def _date(value: Any, *, column: str) -> dt.date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValidationFailed(
        f"'{value}' in column '{column}' is not a date Atlas recognises " "(try YYYY-MM-DD)."
    )


def _int(value: Any, *, column: str) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError as exc:
        raise ValidationFailed(f"'{value}' in column '{column}' is not a whole number.") from exc


def _enum(value: Any, enum_cls: Any, *, column: str) -> Any:
    raw = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    valid = {member.value: member for member in enum_cls}
    if raw in valid:
        return valid[raw]
    raise ValidationFailed(
        f"'{value}' in column '{column}' is not one of: {', '.join(sorted(valid))}."
    )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def parse_csv(text: str) -> tuple[list[str], list[dict[str, str]]]:
    """Read a CSV into normalised header names and rows."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        raise ValidationFailed("That file has no header row.")

    headers = [(name or "").strip().lower() for name in reader.fieldnames]
    rows: list[dict[str, str]] = []
    for row in reader:
        rows.append(
            {(key or "").strip().lower(): (value or "").strip() for key, value in row.items()}
        )
        if len(rows) > MAX_ROWS:
            raise ValidationFailed(
                f"That file has more than {MAX_ROWS:,} rows. Split it, or ask about a "
                "one-off migration."
            )
    return headers, rows


def plan_import(
    session: Session,
    *,
    org_id: str,
    resource: str,
    text: str,
) -> ImportPlan:
    """Work out what a file would do. Reads only; writes nothing."""
    definition = importer(resource)
    headers, rows = parse_csv(text)
    plan = ImportPlan(resource=resource)

    missing = [column for column in definition.required_columns if column not in headers]
    if missing:
        plan.errors.append(
            RowError(row=1, column=None, message=f"Missing column(s): {', '.join(missing)}.")
        )
        return plan

    seen: dict[str, int] = {}
    for index, row in enumerate(rows, start=2):  # row 1 is the header
        if not any(row.values()):
            continue
        try:
            key, values = definition.parse(session, org_id=org_id, row=row, row_number=index)
        except ValidationFailed as exc:
            plan.errors.append(RowError(row=index, column=None, message=str(exc)))
            continue

        if key in seen:
            # Two rows claiming the same record is a mistake in the file, not a
            # decision for us to make about which one wins.
            plan.errors.append(
                RowError(
                    row=index,
                    column=None,
                    message=f"'{key}' also appears on row {seen[key]}.",
                )
            )
            continue
        seen[key] = index

        existing = definition.find(session, org_id=org_id, key=key)
        if existing is None:
            plan.rows.append(RowPlan(row=index, key=key, action="create", values=values))
        else:
            changes = {
                field_name: {"from": getattr(existing, field_name, None), "to": new}
                for field_name, new in values.items()
                if getattr(existing, field_name, None) != new
            }
            plan.rows.append(
                RowPlan(
                    row=index,
                    key=key,
                    action="update" if changes else "unchanged",
                    values=values,
                    changes=changes,
                )
            )

    return plan


def apply_plan(
    session: Session,
    *,
    org_id: str,
    plan: ImportPlan,
    actor_id: str | None = None,
) -> ImportPlan:
    """Write a validated plan.

    Refuses a plan with any error. A partial import is worse than a failed one:
    the operator cannot tell which half landed, and re-uploading duplicates the
    half that did.
    """
    if not plan.is_valid:
        raise ValidationFailed(
            f"This file has {len(plan.errors)} problem(s) and was not imported. "
            f"First: {plan.errors[0]}"
        )

    definition = importer(plan.resource)
    created = updated = 0

    for row in plan.rows:
        if row.action == "create":
            definition.create(session, org_id=org_id, key=row.key, values=row.values)
            created += 1
        elif row.action == "update":
            existing = definition.find(session, org_id=org_id, key=row.key)
            if existing is None:  # pragma: no cover - only under concurrent deletion
                definition.create(session, org_id=org_id, key=row.key, values=row.values)
                created += 1
                continue
            definition.update(session, record=existing, values=row.values)
            updated += 1

    session.flush()
    record_audit_event(
        action=AuditAction.DATA_EXPORTED,
        resource_type="BulkImport",
        resource_label=plan.resource,
        severity=AuditSeverity.NOTICE,
        payload={
            "resource": plan.resource,
            "created": created,
            "updated": updated,
            "unchanged": plan.unchanged,
        },
        reason="Bulk import applied.",
        org_id=org_id,
        actor_id=actor_id,
        session=session,
    )
    log.info(
        "bulk import applied",
        extra={
            "event": "import.applied",
            "resource": plan.resource,
            "created": created,
            "updated": updated,
        },
    )
    return plan


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


def _parse_property(session: Session, *, org_id: str, row: dict, row_number: int):  # noqa: ANN202
    from app.models.org import PropertyType

    code = _text(row.get("code"), limit=30)
    if not code:
        raise ValidationFailed("A property needs a code.")
    if not _text(row.get("name")):
        raise ValidationFailed("A property needs a name.")

    return code, {
        "name": _text(row.get("name"), limit=150),
        "property_type": _enum(
            row.get("property_type") or "residential_multi",
            PropertyType,
            column="property_type",
        ),
        "address_line1": _text(row.get("address_line1")),
        "city": _text(row.get("city"), limit=100),
        "region": _text(row.get("region"), limit=100),
        "postal_code": _text(row.get("postal_code"), limit=20),
        "year_built": _int(row.get("year_built"), column="year_built"),
    }


def _find_property(session: Session, *, org_id: str, key: str):  # noqa: ANN202
    from app.models.org import Property

    return session.execute(
        select(Property).where(
            Property.org_id == org_id, Property.code == key, Property.deleted_at.is_(None)
        )
    ).scalar_one_or_none()


def _create_property(session: Session, *, org_id: str, key: str, values: dict):  # noqa: ANN202
    from app.models.org import Property

    record = Property(org_id=org_id, code=key, **values)
    session.add(record)
    session.flush()
    return record


def _update_record(session: Session, *, record: Any, values: dict) -> dict[str, Any]:
    changed: dict[str, Any] = {}
    for field_name, new in values.items():
        if getattr(record, field_name, None) != new:
            changed[field_name] = {"from": getattr(record, field_name, None), "to": new}
            setattr(record, field_name, new)
    session.flush()
    return changed


register(
    ImporterDefinition(
        resource="properties",
        name="Properties",
        required_columns=("code", "name", "address_line1", "city", "postal_code"),
        optional_columns=("property_type", "region", "year_built"),
        parse=_parse_property,
        find=_find_property,
        create=_create_property,
        update=_update_record,
    )
)


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def _parse_unit(session: Session, *, org_id: str, row: dict, row_number: int):  # noqa: ANN202
    from app.models.org import UnitStatus

    property_code = _text(row.get("property_code"), limit=30)
    unit_number = _text(row.get("unit_number"), limit=30)
    if not property_code or not unit_number:
        raise ValidationFailed("A unit needs a property code and a unit number.")

    prop = _find_property(session, org_id=org_id, key=property_code)
    if prop is None:
        raise ValidationFailed(f"No property with code '{property_code}'.")

    return f"{property_code}/{unit_number}", {
        "property_id": prop.id,
        "unit_number": unit_number,
        "bedrooms": _int(row.get("bedrooms"), column="bedrooms"),
        "bathrooms": _decimal(row.get("bathrooms") or "0", column="bathrooms"),
        "square_feet": _int(row.get("square_feet"), column="square_feet"),
        "market_rent": _decimal(row.get("market_rent") or "0", column="market_rent"),
        "status": _enum(row.get("status") or "vacant_ready", UnitStatus, column="status"),
    }


def _find_unit(session: Session, *, org_id: str, key: str):  # noqa: ANN202
    from app.models.org import Property, Unit

    property_code, _, unit_number = key.partition("/")
    return session.execute(
        select(Unit)
        .join(Property, Property.id == Unit.property_id)
        .where(
            Unit.org_id == org_id,
            Property.code == property_code,
            Unit.unit_number == unit_number,
            Unit.deleted_at.is_(None),
        )
    ).scalar_one_or_none()


def _create_unit(session: Session, *, org_id: str, key: str, values: dict):  # noqa: ANN202
    from app.models.org import Unit

    record = Unit(org_id=org_id, **values)
    session.add(record)
    session.flush()
    return record


register(
    ImporterDefinition(
        resource="units",
        name="Units",
        required_columns=("property_code", "unit_number"),
        optional_columns=(
            "bedrooms",
            "bathrooms",
            "square_feet",
            "market_rent",
            "status",
        ),
        parse=_parse_unit,
        find=_find_unit,
        create=_create_unit,
        update=_update_record,
    )
)


# ---------------------------------------------------------------------------
# Vendors
# ---------------------------------------------------------------------------


def _parse_vendor(session: Session, *, org_id: str, row: dict, row_number: int):  # noqa: ANN202
    code = _text(row.get("code"), limit=30)
    name = _text(row.get("name"), limit=150)
    if not code or not name:
        raise ValidationFailed("A vendor needs a code and a name.")

    email = _text(row.get("email"), limit=320).lower()
    if email and "@" not in email:
        raise ValidationFailed(f"'{email}' is not an email address.")

    return code, {
        "name": name,
        "legal_name": _text(row.get("legal_name"), limit=200) or None,
        "email": email or None,
        "phone": _text(row.get("phone"), limit=40) or None,
        "compliance_expires_at": _date(
            row.get("compliance_expires_at"), column="compliance_expires_at"
        ),
    }


def _find_vendor(session: Session, *, org_id: str, key: str):  # noqa: ANN202
    from app.models.vendor import Vendor

    return session.execute(
        select(Vendor).where(
            Vendor.org_id == org_id, Vendor.code == key, Vendor.deleted_at.is_(None)
        )
    ).scalar_one_or_none()


def _create_vendor(session: Session, *, org_id: str, key: str, values: dict):  # noqa: ANN202
    from app.models.vendor import Vendor

    record = Vendor(org_id=org_id, code=key, **values)
    session.add(record)
    session.flush()
    return record


register(
    ImporterDefinition(
        resource="vendors",
        name="Vendors",
        required_columns=("code", "name"),
        optional_columns=("legal_name", "email", "phone", "compliance_expires_at"),
        parse=_parse_vendor,
        find=_find_vendor,
        create=_create_vendor,
        update=_update_record,
    )
)


def template_for(resource: str) -> str:
    """A header-only CSV, so nobody has to guess the column names."""
    definition = importer(resource)
    columns = list(definition.required_columns) + list(definition.optional_columns)
    return ",".join(columns) + "\r\n"

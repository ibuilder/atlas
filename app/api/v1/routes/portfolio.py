"""Properties, units, and owners.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from flask import Response
from sqlalchemy import select

from app.api.helpers import (
    add_etag,
    conditional_get,
    paginate,
    parse_body,
    parse_query,
    require_if_match,
    respond,
    respond_collection,
    respond_created,
)
from app.api.v1 import api_v1_bp
from app.errors import NotFound
from app.extensions import db
from app.middleware import require_org_scope
from app.models.audit import AuditAction
from app.models.base import model_to_dict
from app.models.org import OwnerEntity, Property, Unit
from app.schemas.portfolio import (
    OwnerCreate,
    OwnerOut,
    PropertyCreate,
    PropertyListQuery,
    PropertyOut,
    PropertyUpdate,
    UnitCreate,
    UnitListQuery,
    UnitOut,
    UnitUpdate,
)
from app.security.permissions import Perm
from app.security.policies import require
from app.services.audit.recorder import diff_payload, record_audit_event
from app.services.common.unit_of_work import transaction

__all__ = []


def _get_property(property_id: str) -> Property:
    """Load a property within the current tenant, or 404.

    The tenancy guard has already constrained the query to the caller's
    organization, so a hit from another tenant is simply not returned - the
    absence is the isolation.
    """
    record = db.session.get(Property, property_id)
    if record is None:
        raise NotFound("That property was not found.")
    return record


def _get_unit(unit_id: str) -> Unit:
    record = db.session.get(Unit, unit_id)
    if record is None:
        raise NotFound("That unit was not found.")
    return record


# ------------------------------------------------------------- properties


@api_v1_bp.get("/properties", endpoint="properties_list")
def list_properties() -> Response:
    require(Perm.PROPERTY_READ)
    query = parse_query(PropertyListQuery)
    org_id = require_org_scope()

    stmt = select(Property).where(Property.org_id == org_id)
    if query.status:
        stmt = stmt.where(Property.status == query.status)
    if query.property_type:
        stmt = stmt.where(Property.property_type == query.property_type)
    if query.portfolio_id:
        stmt = stmt.where(Property.portfolio_id == query.portfolio_id)
    if query.q:
        pattern = f"%{query.q}%"
        stmt = stmt.where(Property.name.ilike(pattern) | Property.code.ilike(pattern))

    page = paginate(db.session, stmt, Property, limit=query.limit, cursor=query.cursor)
    return respond_collection(page, PropertyOut)


@api_v1_bp.post("/properties", endpoint="properties_create")
def create_property() -> Response:
    require(Perm.PROPERTY_CREATE)
    payload = parse_body(PropertyCreate)
    org_id = require_org_scope()

    with transaction() as session:
        record = Property(org_id=org_id, **payload.model_dump(exclude_none=True))
        session.add(record)
        session.flush()
        record_audit_event(
            action=AuditAction.PROPERTY_CREATED,
            resource_type="Property",
            resource_id=record.id,
            resource_label=record.name,
            payload={"code": record.code, "type": str(record.property_type)},
            org_id=org_id,
            session=session,
        )

    return respond_created(
        PropertyOut.model_validate(record, from_attributes=True),
        location=f"/api/v1/properties/{record.id}",
    )


@api_v1_bp.get("/properties/<id:property_id>", endpoint="properties_get")
def get_property(property_id: str) -> Response:
    record = _get_property(property_id)
    require(Perm.PROPERTY_READ, record)

    cached = conditional_get(record)
    if cached is not None:
        return cached

    response = respond(PropertyOut.model_validate(record, from_attributes=True))
    return add_etag(response, record)


@api_v1_bp.patch("/properties/<id:property_id>", endpoint="properties_update")
def update_property(property_id: str) -> Response:
    record = _get_property(property_id)
    require(Perm.PROPERTY_UPDATE, record)
    require_if_match(record)

    payload = parse_body(PropertyUpdate)
    before = model_to_dict(record)

    with transaction() as session:
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(record, field, value)
        session.flush()
        changes = diff_payload(before, model_to_dict(record))
        if changes:
            record_audit_event(
                action=AuditAction.PROPERTY_UPDATED,
                resource_type="Property",
                resource_id=record.id,
                resource_label=record.name,
                payload=changes,
                org_id=record.org_id,
                session=session,
            )

    response = respond(PropertyOut.model_validate(record, from_attributes=True))
    return add_etag(response, record)


# ------------------------------------------------------------------ units


@api_v1_bp.get("/units", endpoint="units_list")
def list_units() -> Response:
    require(Perm.UNIT_READ)
    query = parse_query(UnitListQuery)
    org_id = require_org_scope()

    stmt = select(Unit).where(Unit.org_id == org_id)
    if query.property_id:
        stmt = stmt.where(Unit.property_id == query.property_id)
    if query.status:
        stmt = stmt.where(Unit.status == query.status)
    if query.is_listed is not None:
        stmt = stmt.where(Unit.is_listed.is_(query.is_listed))
    if query.bedrooms is not None:
        stmt = stmt.where(Unit.bedrooms == query.bedrooms)
    if query.q:
        stmt = stmt.where(Unit.unit_number.ilike(f"%{query.q}%"))

    page = paginate(db.session, stmt, Unit, limit=query.limit, cursor=query.cursor)
    return respond_collection(page, UnitOut)


@api_v1_bp.post("/units", endpoint="units_create")
def create_unit() -> Response:
    payload = parse_body(UnitCreate)
    org_id = require_org_scope()
    parent = _get_property(payload.property_id)
    require(Perm.UNIT_MANAGE, parent)

    with transaction() as session:
        record = Unit(org_id=org_id, **payload.model_dump(exclude_none=True))
        session.add(record)
        session.flush()
        # Denormalised count on the property, kept in step here rather than
        # recomputed on every dashboard read.
        parent.total_units = (parent.total_units or 0) + 1
        record_audit_event(
            action=AuditAction.UNIT_CREATED,
            resource_type="Unit",
            resource_id=record.id,
            resource_label=f"{parent.code} / {record.unit_number}",
            payload={"property_id": parent.id, "unit_number": record.unit_number},
            org_id=org_id,
            session=session,
        )

    return respond_created(
        UnitOut.model_validate(record, from_attributes=True),
        location=f"/api/v1/units/{record.id}",
    )


@api_v1_bp.get("/units/<id:unit_id>", endpoint="units_get")
def get_unit(unit_id: str) -> Response:
    record = _get_unit(unit_id)
    require(Perm.UNIT_READ, record)
    cached = conditional_get(record)
    if cached is not None:
        return cached
    return add_etag(respond(UnitOut.model_validate(record, from_attributes=True)), record)


@api_v1_bp.patch("/units/<id:unit_id>", endpoint="units_update")
def update_unit(unit_id: str) -> Response:
    record = _get_unit(unit_id)
    require(Perm.UNIT_MANAGE, record)
    require_if_match(record)

    payload = parse_body(UnitUpdate)
    before = model_to_dict(record)

    with transaction() as session:
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(record, field, value)
        session.flush()
        changes = diff_payload(before, model_to_dict(record))
        if changes:
            record_audit_event(
                action=AuditAction.UNIT_UPDATED,
                resource_type="Unit",
                resource_id=record.id,
                resource_label=record.unit_number,
                payload=changes,
                org_id=record.org_id,
                session=session,
            )

    return add_etag(respond(UnitOut.model_validate(record, from_attributes=True)), record)


# ----------------------------------------------------------------- owners


@api_v1_bp.get("/owners", endpoint="owners_list")
def list_owners() -> Response:
    require(Perm.OWNER_READ)
    query = parse_query(UnitListQuery)
    org_id = require_org_scope()

    stmt = select(OwnerEntity).where(OwnerEntity.org_id == org_id)
    if query.q:
        stmt = stmt.where(OwnerEntity.name.ilike(f"%{query.q}%"))

    page = paginate(db.session, stmt, OwnerEntity, limit=query.limit, cursor=query.cursor)
    return respond_collection(page, OwnerOut)


@api_v1_bp.post("/owners", endpoint="owners_create")
def create_owner() -> Response:
    require(Perm.OWNER_MANAGE)
    payload = parse_body(OwnerCreate)
    org_id = require_org_scope()

    with transaction() as session:
        record = OwnerEntity(org_id=org_id, **payload.model_dump(exclude_none=True))
        session.add(record)
        session.flush()
        record_audit_event(
            action=AuditAction.OWNER_CREATED,
            resource_type="OwnerEntity",
            resource_id=record.id,
            resource_label=record.name,
            payload={"code": record.code},
            org_id=org_id,
            session=session,
        )

    return respond_created(
        OwnerOut.model_validate(record, from_attributes=True),
        location=f"/api/v1/owners/{record.id}",
    )

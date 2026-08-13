"""Service index, audit access, and dashboard KPIs.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from flask import Response, url_for
from sqlalchemy import func, select

from app.api.helpers import paginate, parse_query, respond, respond_collection
from app.api.v1 import api_v1_bp
from app.extensions import current_session, db
from app.middleware import require_org_scope
from app.models.accounting import Invoice, InvoiceStatus
from app.models.audit import AuditEvent
from app.models.leasing import Lease, LeaseStatus
from app.models.maintenance import WORK_ORDER_TERMINAL, WorkOrder
from app.models.org import Property, Unit, UnitStatus
from app.models.types import quantize_money, utcnow
from app.schemas.common import AtlasResponse, ListQuery
from app.security.permissions import Perm
from app.security.policies import require

__all__ = []


class AuditEventOut(AtlasResponse):
    id: str
    sequence: int
    occurred_at: dt.datetime
    action: str
    outcome: str
    severity: str
    resource_type: str | None = None
    resource_id: str | None = None
    resource_label: str | None = None
    actor_id: str | None = None
    actor_label: str | None = None
    correlation_id: str | None = None
    payload: dict = {}
    entry_hash: str


class AuditQuery(ListQuery):
    action: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    actor_id: str | None = None


@api_v1_bp.get("/", endpoint="meta_index")
def index() -> Response:
    """Discovery document. Public: it lists shapes, never data."""
    return respond(
        {
            "name": "Atlas PMOS API",
            "version": "v1",
            "documentation": url_for("openapi.docs", _external=False),
            "openapi": url_for("openapi.spec", _external=False),
            "resources": [
                "properties",
                "units",
                "owners",
                "leads",
                "residents",
                "leases",
                "requests",
                "work-orders",
                "invoices",
                "payments",
                "ledger/entries",
                "audit/events",
            ],
        }
    )


@api_v1_bp.get("/audit/events", endpoint="audit_events_list")
def list_audit_events() -> Response:
    """The audit trail, filtered but never mutable."""
    require(Perm.AUDIT_READ)
    query = parse_query(AuditQuery)
    org_id = require_org_scope()

    stmt = select(AuditEvent).where(AuditEvent.org_id == org_id)
    if query.action:
        stmt = stmt.where(AuditEvent.action == query.action)
    if query.resource_type:
        stmt = stmt.where(AuditEvent.resource_type == query.resource_type)
    if query.resource_id:
        stmt = stmt.where(AuditEvent.resource_id == query.resource_id)
    if query.actor_id:
        stmt = stmt.where(AuditEvent.actor_id == query.actor_id)

    page = paginate(current_session(), stmt, AuditEvent, limit=query.limit, cursor=query.cursor)
    return respond_collection(page, AuditEventOut)


@api_v1_bp.get("/audit/verify", endpoint="audit_verify")
def verify_audit_chain() -> Response:
    """Re-walk the hash chain and report whether it is intact."""
    require(Perm.AUDIT_EXPORT)
    org_id = require_org_scope()

    from app.services.audit.recorder import verify_chain

    return respond(verify_chain(current_session(), org_id=org_id))


@api_v1_bp.get("/dashboard/kpis", endpoint="dashboard_kpis")
def dashboard_kpis() -> Response:
    """Headline operating metrics for the current organization.

    Computed live against operational tables. At portfolio scale these move to
    the ``kpi_snapshots`` projection; the shape of the response does not change
    when they do.
    """
    require(Perm.REPORT_READ)
    org_id = require_org_scope()
    today = utcnow().date()

    total_units = db.session.execute(
        select(func.count()).select_from(Unit).where(Unit.org_id == org_id)
    ).scalar_one()
    occupied_units = db.session.execute(
        select(func.count())
        .select_from(Unit)
        .where(Unit.org_id == org_id, Unit.status.in_([UnitStatus.OCCUPIED, UnitStatus.NOTICE]))
    ).scalar_one()

    active_leases = db.session.execute(
        select(func.count())
        .select_from(Lease)
        .where(Lease.org_id == org_id, Lease.status == LeaseStatus.ACTIVE)
    ).scalar_one()
    expiring_soon = db.session.execute(
        select(func.count())
        .select_from(Lease)
        .where(
            Lease.org_id == org_id,
            Lease.status.in_([LeaseStatus.ACTIVE, LeaseStatus.HOLDOVER]),
            Lease.end_date <= today + dt.timedelta(days=90),
        )
    ).scalar_one()

    open_receivable = db.session.execute(
        select(func.coalesce(func.sum(Invoice.balance), 0)).where(
            Invoice.org_id == org_id,
            Invoice.status.in_([InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID]),
        )
    ).scalar_one()
    overdue_receivable = db.session.execute(
        select(func.coalesce(func.sum(Invoice.balance), 0)).where(
            Invoice.org_id == org_id,
            Invoice.status.in_([InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID]),
            Invoice.due_date < today,
        )
    ).scalar_one()

    open_work_orders = db.session.execute(
        select(func.count())
        .select_from(WorkOrder)
        .where(WorkOrder.org_id == org_id, WorkOrder.status.notin_(list(WORK_ORDER_TERMINAL)))
    ).scalar_one()
    breached_work_orders = db.session.execute(
        select(func.count())
        .select_from(WorkOrder)
        .where(
            WorkOrder.org_id == org_id,
            WorkOrder.status.notin_(list(WORK_ORDER_TERMINAL)),
            WorkOrder.resolution_due_at < utcnow(),
        )
    ).scalar_one()

    property_count = db.session.execute(
        select(func.count()).select_from(Property).where(Property.org_id == org_id)
    ).scalar_one()

    occupancy = (occupied_units / total_units * 100) if total_units else 0.0
    delinquency = (
        float(
            quantize_money(Decimal(overdue_receivable))
            / quantize_money(Decimal(open_receivable))
            * 100
        )
        if open_receivable
        else 0.0
    )
    sla_compliance = (
        (open_work_orders - breached_work_orders) / open_work_orders * 100
        if open_work_orders
        else 100.0
    )

    return respond(
        {
            "as_of": today.isoformat(),
            "portfolio": {
                "properties": property_count,
                "units": total_units,
                "occupied_units": occupied_units,
                "occupancy_rate": round(occupancy, 1),
            },
            "leasing": {
                "active_leases": active_leases,
                "expiring_within_90_days": expiring_soon,
            },
            "receivables": {
                "open_balance": str(quantize_money(Decimal(open_receivable))),
                "overdue_balance": str(quantize_money(Decimal(overdue_receivable))),
                "delinquency_rate": round(delinquency, 1),
            },
            "maintenance": {
                "open_work_orders": open_work_orders,
                "sla_breached": breached_work_orders,
                "sla_compliance_rate": round(sla_compliance, 1),
            },
        }
    )

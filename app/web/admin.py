"""The operations console.

Every view enforces permission through the policy engine before rendering, and
the templates additionally hide controls the viewer cannot use. The hiding is
courtesy; the enforcement is the check.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from flask import Blueprint, render_template, request
from flask_login import login_required
from sqlalchemy import func, select

from app.extensions import db
from app.middleware import require_org_scope
from app.models.accounting import Invoice, InvoiceStatus
from app.models.audit import AuditEvent
from app.models.leasing import Lease, LeaseStatus
from app.models.maintenance import WORK_ORDER_TERMINAL, MaintenanceRequest, WorkOrder
from app.models.org import Property, Unit, UnitStatus
from app.models.types import utcnow
from app.security.permissions import Perm
from app.security.policies import require

admin_bp = Blueprint("admin", __name__)

__all__ = ["admin_bp"]


@admin_bp.get("/")
@login_required
def dashboard() -> str:
    """Portfolio, leasing, receivables, and maintenance at a glance."""
    require(Perm.REPORT_READ)
    org_id = require_org_scope()
    today = utcnow().date()

    total_units = _count(select(func.count()).select_from(Unit).where(Unit.org_id == org_id))
    occupied = _count(
        select(func.count())
        .select_from(Unit)
        .where(Unit.org_id == org_id, Unit.status.in_([UnitStatus.OCCUPIED, UnitStatus.NOTICE]))
    )
    properties = _count(select(func.count()).select_from(Property).where(Property.org_id == org_id))
    active_leases = _count(
        select(func.count())
        .select_from(Lease)
        .where(Lease.org_id == org_id, Lease.status == LeaseStatus.ACTIVE)
    )
    expiring = _count(
        select(func.count())
        .select_from(Lease)
        .where(
            Lease.org_id == org_id,
            Lease.status.in_([LeaseStatus.ACTIVE, LeaseStatus.HOLDOVER]),
            Lease.end_date <= today + dt.timedelta(days=90),
        )
    )
    open_balance = _sum(
        select(func.coalesce(func.sum(Invoice.balance), 0)).where(
            Invoice.org_id == org_id,
            Invoice.status.in_([InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID]),
        )
    )
    overdue_balance = _sum(
        select(func.coalesce(func.sum(Invoice.balance), 0)).where(
            Invoice.org_id == org_id,
            Invoice.status.in_([InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID]),
            Invoice.due_date < today,
        )
    )
    open_work_orders = _count(
        select(func.count())
        .select_from(WorkOrder)
        .where(WorkOrder.org_id == org_id, WorkOrder.status.notin_(list(WORK_ORDER_TERMINAL)))
    )
    breached = _count(
        select(func.count())
        .select_from(WorkOrder)
        .where(
            WorkOrder.org_id == org_id,
            WorkOrder.status.notin_(list(WORK_ORDER_TERMINAL)),
            WorkOrder.resolution_due_at < utcnow(),
        )
    )

    recent_requests = list(
        db.session.execute(
            select(MaintenanceRequest)
            .where(MaintenanceRequest.org_id == org_id)
            .order_by(MaintenanceRequest.created_at.desc())
            .limit(6)
        ).scalars()
    )
    urgent_work = list(
        db.session.execute(
            select(WorkOrder)
            .where(
                WorkOrder.org_id == org_id,
                WorkOrder.status.notin_(list(WORK_ORDER_TERMINAL)),
            )
            .order_by(WorkOrder.resolution_due_at.asc())
            .limit(6)
        ).scalars()
    )

    return render_template(
        "admin/dashboard.html",
        metrics={
            "properties": properties,
            "units": total_units,
            "occupied": occupied,
            "occupancy": (occupied / total_units * 100) if total_units else 0.0,
            "active_leases": active_leases,
            "expiring": expiring,
            "open_balance": open_balance,
            "overdue_balance": overdue_balance,
            "delinquency": (float(overdue_balance / open_balance * 100) if open_balance else 0.0),
            "open_work_orders": open_work_orders,
            "breached": breached,
            "sla_compliance": (
                (open_work_orders - breached) / open_work_orders * 100
                if open_work_orders
                else 100.0
            ),
        },
        recent_requests=recent_requests,
        urgent_work=urgent_work,
    )


@admin_bp.get("/properties")
@login_required
def properties() -> str:
    """Every property in the portfolio."""
    require(Perm.PROPERTY_READ)
    org_id = require_org_scope()

    stmt = select(Property).where(Property.org_id == org_id).order_by(Property.name)
    search = (request.args.get("q") or "").strip()
    if search:
        pattern = f"%{search}%"
        stmt = stmt.where(Property.name.ilike(pattern) | Property.code.ilike(pattern))

    records = list(db.session.execute(stmt.limit(200)).scalars())
    return render_template("admin/properties.html", properties=records, search=search)


@admin_bp.get("/work-orders")
@login_required
def work_orders() -> str:
    """The maintenance queue, most urgent first."""
    require(Perm.WORK_ORDER_READ)
    org_id = require_org_scope()

    stmt = select(WorkOrder).where(WorkOrder.org_id == org_id)
    status = request.args.get("status")
    if status == "open":
        stmt = stmt.where(WorkOrder.status.notin_(list(WORK_ORDER_TERMINAL)))
    elif status:
        stmt = stmt.where(WorkOrder.status == status)

    records = list(
        db.session.execute(
            stmt.order_by(WorkOrder.resolution_due_at.asc().nulls_last()).limit(200)
        ).scalars()
    )
    return render_template("admin/work_orders.html", work_orders=records, status=status)


@admin_bp.get("/ledger")
@login_required
def ledger() -> str:
    """Trial balance. Debits and credits must agree."""
    require(Perm.LEDGER_READ)
    org_id = require_org_scope()

    from app.services.accounting.ledger import trial_balance

    rows = trial_balance(db.session, org_id=org_id)
    return render_template(
        "admin/ledger.html",
        rows=rows,
        total_debit=sum((row["debit"] for row in rows), Decimal("0")),
        total_credit=sum((row["credit"] for row in rows), Decimal("0")),
    )


@admin_bp.get("/audit")
@login_required
def audit() -> str:
    """The tamper-evident audit trail."""
    require(Perm.AUDIT_READ)
    org_id = require_org_scope()

    events = list(
        db.session.execute(
            select(AuditEvent)
            .where(AuditEvent.org_id == org_id)
            .order_by(AuditEvent.sequence.desc())
            .limit(100)
        ).scalars()
    )

    integrity = None
    from app.security.policies import can

    if can(Perm.AUDIT_EXPORT):
        from app.services.audit.recorder import verify_chain

        integrity = verify_chain(db.session, org_id=org_id)

    return render_template("admin/audit.html", events=events, integrity=integrity)


def _count(stmt) -> int:  # noqa: ANN001
    return int(db.session.execute(stmt).scalar_one() or 0)


def _sum(stmt) -> Decimal:  # noqa: ANN001
    return Decimal(str(db.session.execute(stmt).scalar_one() or 0))

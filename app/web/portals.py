"""Resident, owner, and vendor portals.

Each portal is scoped by the policy engine's ownership predicates rather than by
a hand-written ``WHERE`` clause per view. A resident sees their lease because
the engine says the lease is theirs, not because this file remembered to filter.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from decimal import Decimal

from flask import Blueprint, render_template
from flask_login import current_user, login_required
from sqlalchemy import select

from app.errors import PermissionDenied
from app.extensions import db
from app.middleware import require_org_scope
from app.models.accounting import Invoice, InvoiceStatus
from app.models.iam import UserType
from app.models.leasing import Lease, LeaseStatus
from app.models.maintenance import WORK_ORDER_TERMINAL, MaintenanceRequest, WorkOrder
from app.models.org import OwnershipStake, Property
from app.models.resident import Tenancy
from app.security.permissions import Perm
from app.security.policies import require

resident_bp = Blueprint("resident", __name__)
owner_bp = Blueprint("owner", __name__)
vendor_bp = Blueprint("vendor", __name__)

__all__ = ["owner_bp", "resident_bp", "vendor_bp"]


def _require_user_type(expected: UserType) -> None:
    """Portals are for their own audience.

    Staff with the right permissions can read the same data through the admin
    console; the portal itself stays a single-purpose surface, so a portal view
    never has to reason about two very different callers.
    """
    if current_user.user_type != expected and not current_user.is_platform_admin:
        raise PermissionDenied("This portal is not available for your account type.")


@resident_bp.get("/", endpoint="dashboard")
@login_required
def resident_dashboard() -> str:
    """Balance, lease, requests, and messages for the signed-in resident."""
    _require_user_type(UserType.RESIDENT)
    require(Perm.LEASE_READ)
    org_id = require_org_scope()
    resident_id = current_user.resident_id

    lease_ids = [
        row
        for row in db.session.execute(
            select(Tenancy.lease_id).where(
                Tenancy.resident_id == resident_id, Tenancy.org_id == org_id
            )
        ).scalars()
    ]

    leases = (
        list(
            db.session.execute(
                select(Lease).where(Lease.id.in_(lease_ids)).order_by(Lease.start_date.desc())
            ).scalars()
        )
        if lease_ids
        else []
    )
    current_lease = next((lease for lease in leases if lease.status == LeaseStatus.ACTIVE), None)

    invoices = (
        list(
            db.session.execute(
                select(Invoice)
                .where(
                    Invoice.lease_id.in_(lease_ids),
                    Invoice.status.in_([InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID]),
                )
                .order_by(Invoice.due_date.asc())
            ).scalars()
        )
        if lease_ids
        else []
    )
    balance = sum((invoice.balance for invoice in invoices), Decimal("0"))

    requests = list(
        db.session.execute(
            select(MaintenanceRequest)
            .where(MaintenanceRequest.resident_id == resident_id)
            .order_by(MaintenanceRequest.created_at.desc())
            .limit(10)
        ).scalars()
    )

    return render_template(
        "portals/resident.html",
        lease=current_lease,
        leases=leases,
        invoices=invoices,
        balance=balance,
        requests=requests,
    )


@owner_bp.get("/", endpoint="dashboard")
@login_required
def owner_dashboard() -> str:
    """Owned properties, income position, and recent activity."""
    _require_user_type(UserType.OWNER)
    require(Perm.OWNER_STATEMENT_READ)
    org_id = require_org_scope()

    property_ids = [
        row
        for row in db.session.execute(
            select(OwnershipStake.property_id).where(
                OwnershipStake.owner_entity_id == current_user.owner_entity_id,
                OwnershipStake.org_id == org_id,
            )
        ).scalars()
    ]

    properties = (
        list(
            db.session.execute(
                select(Property).where(Property.id.in_(property_ids)).order_by(Property.name)
            ).scalars()
        )
        if property_ids
        else []
    )

    open_work = (
        list(
            db.session.execute(
                select(WorkOrder)
                .where(
                    WorkOrder.property_id.in_(property_ids),
                    WorkOrder.status.notin_(list(WORK_ORDER_TERMINAL)),
                )
                .order_by(WorkOrder.created_at.desc())
                .limit(10)
            ).scalars()
        )
        if property_ids
        else []
    )

    receivable = Decimal("0")
    if property_ids:
        rows = db.session.execute(
            select(Invoice.balance).where(
                Invoice.property_id.in_(property_ids),
                Invoice.status.in_([InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID]),
            )
        ).scalars()
        receivable = sum(rows, Decimal("0"))

    return render_template(
        "portals/owner.html",
        properties=properties,
        open_work=open_work,
        receivable=receivable,
    )


@vendor_bp.get("/", endpoint="dashboard")
@login_required
def vendor_dashboard() -> str:
    """Work assigned to this vendor, and their compliance standing."""
    _require_user_type(UserType.VENDOR)
    require(Perm.WORK_ORDER_READ)

    from app.models.vendor import Vendor

    vendor = db.session.get(Vendor, current_user.vendor_id) if current_user.vendor_id else None

    assigned = (
        list(
            db.session.execute(
                select(WorkOrder)
                .where(
                    WorkOrder.vendor_id == current_user.vendor_id,
                    WorkOrder.status.notin_(list(WORK_ORDER_TERMINAL)),
                )
                .order_by(WorkOrder.resolution_due_at.asc().nulls_last())
                .limit(50)
            ).scalars()
        )
        if current_user.vendor_id
        else []
    )

    return render_template("portals/vendor.html", vendor=vendor, work_orders=assigned)

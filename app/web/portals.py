"""Resident, owner, and vendor portals.

Each portal is scoped by the policy engine's ownership predicates rather than by
a hand-written ``WHERE`` clause per view. A resident sees their lease because
the engine says the lease is theirs, not because this file remembered to filter.

The write surfaces follow one rule, and it is the rule that matters when the
caller is a resident rather than a member of staff: **the portal proves the
subject belongs to the caller before it acts on it.** Every POST re-derives the
set of leases, properties, or work orders the signed-in user owns and refuses
anything outside it - as a *404*, so the portal cannot be used to discover
which identifiers exist. A permission check alone is not enough here: every
resident holds ``payment.create``, so the question is never "may they pay?" but
"is this their invoice?".

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from decimal import Decimal

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import select
from werkzeug.wrappers import Response

from app.errors import AtlasError, PermissionDenied
from app.extensions import current_session, db
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

    lease_ids = _resident_lease_ids(org_id)

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

    property_ids = _owned_property_ids(org_id)

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


# ---------------------------------------------------------------------------
# Ownership: the check that actually protects these routes
# ---------------------------------------------------------------------------


def _resident_lease_ids(org_id: str) -> list[str]:
    return [
        row
        for row in db.session.execute(
            select(Tenancy.lease_id).where(
                Tenancy.resident_id == current_user.resident_id, Tenancy.org_id == org_id
            )
        ).scalars()
    ]


def _owned_property_ids(org_id: str) -> list[str]:
    return [
        row
        for row in db.session.execute(
            select(OwnershipStake.property_id).where(
                OwnershipStake.owner_entity_id == current_user.owner_entity_id,
                OwnershipStake.org_id == org_id,
            )
        ).scalars()
    ]


def _resident_invoice(invoice_id: str, org_id: str) -> Invoice:
    """An open invoice on one of the caller's own leases, or a 404.

    404 rather than 403: telling a resident that an invoice exists but is not
    theirs turns the portal into a way to enumerate the building's invoices.
    """
    lease_ids = _resident_lease_ids(org_id)
    if not lease_ids:
        abort(404)

    invoice = db.session.execute(
        select(Invoice).where(
            Invoice.id == invoice_id,
            Invoice.org_id == org_id,
            Invoice.lease_id.in_(lease_ids),
        )
    ).scalar_one_or_none()
    if invoice is None:
        abort(404)
    return invoice


# ---------------------------------------------------------------------------
# Resident: paying, and raising a request
# ---------------------------------------------------------------------------


@resident_bp.post("/invoices/<id:invoice_id>/pay", endpoint="pay_invoice")
@login_required
def resident_pay_invoice(invoice_id: str) -> Response:
    """Pay an invoice from the portal rather than only through the API."""
    _require_user_type(UserType.RESIDENT)
    require(Perm.PAYMENT_RECORD)
    org_id = require_org_scope()

    from app.models.accounting import PaymentMethod
    from app.models.types import quantize_money, utcnow
    from app.services.accounting.receivables import record_payment

    invoice = _resident_invoice(invoice_id, org_id)
    if invoice.status not in (InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID):
        flash("That invoice is not open for payment.", "error")
        return redirect(url_for("resident.dashboard"))

    raw = (request.form.get("amount") or "").strip()
    try:
        amount = quantize_money(Decimal(raw)) if raw else invoice.balance
        # NaN survives quantization, and every ordered comparison against it
        # raises rather than returning False - so without this the guards below
        # would not reject the value, they would 500 on it.
        if not amount.is_finite():
            raise ArithmeticError("not a finite amount")
    except (ArithmeticError, ValueError):
        flash("That is not an amount.", "error")
        return redirect(url_for("resident.dashboard"))

    if amount <= Decimal("0"):
        flash("A payment must be greater than zero.", "error")
        return redirect(url_for("resident.dashboard"))
    if amount > invoice.balance:
        # Overpayment through a portal is nearly always a typo, and the credit
        # it would create is somebody else's afternoon.
        flash(f"That is more than the {invoice.balance} outstanding on this invoice.", "error")
        return redirect(url_for("resident.dashboard"))

    try:
        record_payment(
            current_session(),
            org_id=org_id,
            amount=amount,
            method=PaymentMethod.ACH,
            received_date=utcnow().date(),
            lease_id=invoice.lease_id,
            property_id=invoice.property_id,
            reference=f"PORTAL-{invoice.invoice_number}",
            # Explicit, because the default retires the lease's open invoices
            # oldest-first. A resident who picked this invoice would otherwise
            # have paid a different one and been told they had not.
            allocations=[(invoice.id, amount)],
            actor_id=current_user.id,
        )
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("resident.dashboard"))

    flash(f"Payment of {amount} recorded against {invoice.invoice_number}.", "success")
    return redirect(url_for("resident.dashboard"))


@resident_bp.post("/requests", endpoint="raise_request")
@login_required
def resident_raise_request() -> Response:
    """Report something broken without telephoning anybody."""
    _require_user_type(UserType.RESIDENT)
    require(Perm.REQUEST_CREATE)
    org_id = require_org_scope()

    from app.services.maintenance.service import create_request

    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip()
    if not title or not description:
        flash("A request needs a summary and a description.", "error")
        return redirect(url_for("resident.dashboard"))

    lease_ids = _resident_lease_ids(org_id)
    lease = (
        db.session.execute(
            select(Lease)
            .where(Lease.id.in_(lease_ids), Lease.status == LeaseStatus.ACTIVE)
            .limit(1)
        ).scalar_one_or_none()
        if lease_ids
        else None
    )
    if lease is None:
        flash("There is no active lease on your account to raise a request against.", "error")
        return redirect(url_for("resident.dashboard"))

    try:
        created = create_request(
            current_session(),
            org_id=org_id,
            property_id=lease.property_id,
            title=title[:200],
            description=description[:4000],
            unit_id=lease.unit_id,
            resident_id=current_user.resident_id,
            permission_to_enter=bool(request.form.get("permission_to_enter")),
            actor_id=current_user.id,
        )
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("resident.dashboard"))

    flash(f"Request {created.request_number} raised. We will be in touch.", "success")
    return redirect(url_for("resident.dashboard"))


# ---------------------------------------------------------------------------
# Owner: the statements the system can now generate
# ---------------------------------------------------------------------------


@owner_bp.get("/statements", endpoint="statements")
@login_required
def owner_statements() -> str:
    """Every statement issued to this owner, newest first."""
    _require_user_type(UserType.OWNER)
    require(Perm.OWNER_STATEMENT_READ)
    org_id = require_org_scope()

    from app.models.accounting import OwnerStatement

    statements = list(
        db.session.execute(
            select(OwnerStatement)
            .where(
                OwnerStatement.org_id == org_id,
                OwnerStatement.owner_entity_id == current_user.owner_entity_id,
            )
            .order_by(OwnerStatement.period_end.desc())
            .limit(48)
        ).scalars()
    )

    # Guard the empty case by not running the query. Substituting a sentinel
    # id binds it through the GUID type, which validates identifiers and
    # rejects anything that is not one - so an owner with no statements yet
    # would get a 500 rather than an empty page.
    property_ids = {statement.property_id for statement in statements if statement.property_id}
    properties = (
        {
            record.id: record
            for record in db.session.execute(
                select(Property).where(Property.id.in_(list(property_ids)))
            ).scalars()
        }
        if property_ids
        else {}
    )

    return render_template(
        "portals/owner_statements.html", statements=statements, properties=properties
    )


@owner_bp.get("/statements/<id:statement_id>", endpoint="statement_detail")
@login_required
def owner_statement_detail(statement_id: str) -> str:
    """One statement, with the arithmetic that produced it.

    Ownership is checked on the *statement's* owner rather than by walking the
    property, because a statement belongs to whoever it was issued to even if
    the property has since changed hands - which is exactly the case temporal
    apportionment exists for.
    """
    _require_user_type(UserType.OWNER)
    require(Perm.OWNER_STATEMENT_READ)
    org_id = require_org_scope()

    from app.models.accounting import OwnerDistribution, OwnerStatement

    statement = db.session.execute(
        select(OwnerStatement).where(
            OwnerStatement.id == statement_id,
            OwnerStatement.org_id == org_id,
            OwnerStatement.owner_entity_id == current_user.owner_entity_id,
        )
    ).scalar_one_or_none()
    if statement is None:
        abort(404)

    distributions = list(
        db.session.execute(
            select(OwnerDistribution)
            .where(
                OwnerDistribution.org_id == org_id,
                OwnerDistribution.owner_entity_id == statement.owner_entity_id,
                OwnerDistribution.distribution_date >= statement.period_start,
                OwnerDistribution.distribution_date <= statement.period_end,
            )
            .order_by(OwnerDistribution.distribution_date)
        ).scalars()
    )

    return render_template(
        "portals/owner_statement.html",
        statement=statement,
        property=db.session.get(Property, statement.property_id),
        distributions=distributions,
    )


# ---------------------------------------------------------------------------
# Vendor: updating work from the field
# ---------------------------------------------------------------------------

#: What a vendor may do to their own job. Deliberately not the full state
#: machine: cancelling work, reassigning it, and verifying it are the
#: management company's decisions, not the contractor's.
VENDOR_TRANSITIONS = ("accept", "start", "hold", "complete")


@vendor_bp.post("/work-orders/<id:work_order_id>", endpoint="update_work_order")
@login_required
def vendor_update_work_order(work_order_id: str) -> Response:
    """Accept, start, hold, or complete a job from a phone in a basement."""
    _require_user_type(UserType.VENDOR)
    require(Perm.WORK_ORDER_UPDATE)
    org_id = require_org_scope()

    from app.models.maintenance import WorkOrderStatus
    from app.models.types import quantize_money
    from app.services.maintenance.service import transition_work_order

    if not current_user.vendor_id:
        abort(404)

    work_order = db.session.execute(
        select(WorkOrder).where(
            WorkOrder.id == work_order_id,
            WorkOrder.org_id == org_id,
            # The check that matters: their own job, or nothing.
            WorkOrder.vendor_id == current_user.vendor_id,
        )
    ).scalar_one_or_none()
    if work_order is None:
        abort(404)

    action = (request.form.get("action") or "").strip().lower()
    if action not in VENDOR_TRANSITIONS:
        flash("That is not something you can do to this job.", "error")
        return redirect(url_for("vendor.dashboard"))

    target = {
        "accept": WorkOrderStatus.ASSIGNED,
        "start": WorkOrderStatus.IN_PROGRESS,
        "hold": WorkOrderStatus.ON_HOLD,
        "complete": WorkOrderStatus.COMPLETED,
    }[action]

    note = (request.form.get("note") or "").strip()[:2000] or None
    labor_cost = material_cost = None
    if action == "complete":
        # Completing without saying what it cost is how a job gets invoiced
        # twice, from memory, three weeks later.
        try:
            labor_cost = quantize_money(Decimal(request.form.get("labor_cost") or "0"))
            material_cost = quantize_money(Decimal(request.form.get("material_cost") or "0"))
            # See the pay route: NaN quantizes cleanly and then raises on the
            # comparison below rather than failing it.
            if not (labor_cost.is_finite() and material_cost.is_finite()):
                raise ArithmeticError("not a finite amount")
        except (ArithmeticError, ValueError):
            flash("Labour and materials must be amounts.", "error")
            return redirect(url_for("vendor.dashboard"))
        if labor_cost < 0 or material_cost < 0:
            flash("Costs cannot be negative.", "error")
            return redirect(url_for("vendor.dashboard"))

    try:
        transition_work_order(
            current_session(),
            work_order=work_order,
            target=target,
            actor_id=current_user.id,
            actor_label=current_user.full_name,
            note=note,
            labor_cost=labor_cost,
            material_cost=material_cost,
            resolution_notes=note if action == "complete" else None,
            # Residents see that work started and finished, not the costs.
            resident_visible=action in ("start", "complete"),
        )
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("vendor.dashboard"))

    flash(f"{work_order.work_order_number} is now {work_order.status.value}.", "success")
    return redirect(url_for("vendor.dashboard"))

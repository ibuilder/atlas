"""The operations console.

Every view enforces permission through the policy engine before rendering, and
the templates additionally hide controls the viewer cannot use. The hiding is
courtesy; the enforcement is the check.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func, select
from werkzeug.wrappers import Response

from app.errors import AtlasError, ValidationFailed
from app.extensions import current_session, db
from app.middleware import require_org_scope
from app.models.accounting import BankAccount, Invoice, InvoiceStatus
from app.models.audit import AuditEvent
from app.models.iam import (
    Permission,
    Role,
    RoleAssignment,
    RolePermission,
    User,
)
from app.models.leasing import Application, Lease, LeaseStatus
from app.models.maintenance import WORK_ORDER_TERMINAL, MaintenanceRequest, WorkOrder
from app.models.org import Property, Unit, UnitStatus
from app.models.types import utcnow
from app.security.permissions import Perm
from app.security.policies import require

admin_bp = Blueprint("admin", __name__)

__all__ = ["admin_bp"]


@dataclass(frozen=True)
class _Holding:
    """What one lease is owed out of one trust account."""

    lease_id: str
    lease: Lease | None
    held: Decimal


@dataclass(frozen=True)
class _TrustAccountView:
    """One trust account and every beneficiary of it."""

    account: BankAccount
    holdings: list[_Holding]

    @property
    def total(self) -> Decimal:
        return sum((holding.held for holding in self.holdings), Decimal("0"))


@dataclass(frozen=True)
class _Holder:
    """One owner's share of one property on the date being viewed."""

    owner_entity_id: str
    owner: object | None
    percentage: Decimal
    since: dt.date


@dataclass(frozen=True)
class _OwnershipRow:
    """A property and everyone holding a share of it.

    The property field is named ``record`` rather than ``property``: a field of
    that name shadows the builtin decorator for the rest of the class body.
    """

    record: Property
    holders: list[_Holder]
    total: Decimal

    @property
    def is_fully_allocated(self) -> bool:
        """Zero is fine - a managed property with no equity record on file.

        Anything between is a share nobody holds, which never reaches a
        statement.
        """
        return self.total in (Decimal("0"), Decimal("100.0000"))


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

    rows = trial_balance(current_session(), org_id=org_id)
    return render_template(
        "admin/ledger.html",
        rows=rows,
        total_debit=sum((row["debit"] for row in rows), Decimal("0")),
        total_credit=sum((row["credit"] for row in rows), Decimal("0")),
    )


@admin_bp.get("/deposits")
@login_required
def deposits() -> str:
    """What is held in trust, and for whom.

    Presented per trust account rather than as one list, because that is the
    unit a reconciliation is performed against - and an operator with an account
    per jurisdiction has to be able to see them apart.
    """
    require(Perm.DEPOSIT_READ)
    org_id = require_org_scope()

    from app.models.accounting import DepositMovement
    from app.services.accounting.deposits import deposit_balances

    as_of_raw = (request.args.get("as_of") or "").strip()
    try:
        as_of = dt.date.fromisoformat(as_of_raw) if as_of_raw else utcnow().date()
    except ValueError:
        as_of = utcnow().date()

    trust_accounts = list(
        db.session.execute(
            select(BankAccount)
            .where(
                BankAccount.org_id == org_id,
                BankAccount.is_trust.is_(True),
                BankAccount.deleted_at.is_(None),
            )
            .order_by(BankAccount.name)
        ).scalars()
    )

    leases = {
        lease.id: lease
        for lease in db.session.execute(select(Lease).where(Lease.org_id == org_id)).scalars()
    }

    accounts: list[_TrustAccountView] = []
    for account in trust_accounts:
        balances = deposit_balances(
            current_session(), org_id=org_id, bank_account_id=account.id, as_of=as_of
        )
        holdings = sorted(
            (
                _Holding(lease_id=lease_id, lease=leases.get(lease_id), held=held)
                for lease_id, held in balances.items()
                if held != Decimal("0")
            ),
            key=lambda holding: holding.lease.lease_number if holding.lease else holding.lease_id,
        )
        accounts.append(_TrustAccountView(account=account, holdings=holdings))

    recent = list(
        db.session.execute(
            select(DepositMovement)
            .where(DepositMovement.org_id == org_id)
            .order_by(DepositMovement.effective_date.desc(), DepositMovement.created_at.desc())
            .limit(50)
        ).scalars()
    )

    return render_template(
        "admin/deposits.html",
        accounts=accounts,
        recent=recent,
        leases=leases,
        as_of=as_of,
    )


@admin_bp.get("/ownership")
@login_required
def ownership() -> str:
    """Who owns what, as at a date, with the history behind it.

    Ownership is the input to every owner statement, so the figure that matters
    is the one for a *period* rather than for today. The date control is the
    point of the page, not a convenience on it.
    """
    require(Perm.OWNER_READ)
    org_id = require_org_scope()

    from app.models.org import OwnerEntity
    from app.services.portfolio.ownership import ownership_on, total_allocated

    as_of_raw = (request.args.get("as_of") or "").strip()
    try:
        as_of = dt.date.fromisoformat(as_of_raw) if as_of_raw else utcnow().date()
    except ValueError:
        as_of = utcnow().date()

    owners = {
        owner.id: owner
        for owner in db.session.execute(
            select(OwnerEntity).where(OwnerEntity.org_id == org_id)
        ).scalars()
    }
    records = list(
        db.session.execute(
            select(Property).where(Property.org_id == org_id).order_by(Property.name)
        ).scalars()
    )

    rows = []
    for record in records:
        stakes = ownership_on(
            current_session(), org_id=org_id, property_id=record.id, on_date=as_of
        )
        total = total_allocated(
            current_session(), org_id=org_id, property_id=record.id, on_date=as_of
        )
        rows.append(
            _OwnershipRow(
                record=record,
                holders=[
                    _Holder(
                        owner=owners.get(stake.owner_entity_id),
                        owner_entity_id=stake.owner_entity_id,
                        percentage=Decimal(stake.percentage),
                        since=stake.effective_from,
                    )
                    for stake in stakes
                ],
                total=total,
            )
        )

    return render_template(
        "admin/ownership.html",
        rows=rows,
        as_of=as_of,
        owners=sorted(owners.values(), key=lambda owner: owner.name),
    )


@admin_bp.get("/properties/<id:property_id>/spaces")
@login_required
def property_spaces(property_id: str) -> str:
    """The space hierarchy of one property, and what is installed in it.

    Shown as a tree rather than a list because the nesting *is* the
    information: a riser that serves four floors is a different thing from a
    riser that happens to be on one.
    """
    require(Perm.PROPERTY_READ)
    org_id = require_org_scope()

    from app.models.asset_graph import SpaceKind
    from app.services.assets.spaces import assets_in, rolled_up_area, space_tree

    record = db.session.get(Property, property_id)
    if record is None or record.org_id != org_id:
        abort(404)

    tree = space_tree(current_session(), org_id=org_id, property_id=property_id)
    flattened = []
    for root in tree:
        for node in root.walk():
            flattened.append(
                {
                    "space": node.space,
                    "depth": len([parent for parent in _space_ancestors(node.space)]),
                    # Own area plus everything under it. A floor's area is the
                    # rooms on it, and reporting the floor's own number as the
                    # total is how square footage quietly halves.
                    "area": rolled_up_area(current_session(), space=node.space),
                    "assets": assets_in(
                        current_session(), space=node.space, include_descendants=False
                    ),
                }
            )

    return render_template(
        "admin/spaces.html",
        property=record,
        rows=flattened,
        kinds=[member.value for member in SpaceKind],
        units=list(
            db.session.execute(
                select(Unit).where(Unit.org_id == org_id, Unit.property_id == property_id)
            ).scalars()
        ),
    )


@admin_bp.post("/properties/<id:property_id>/spaces")
@login_required
def property_space_create(property_id: str) -> Response:
    """Add a space, optionally under another."""
    require(Perm.PROPERTY_UPDATE)
    org_id = require_org_scope()

    from app.models.asset_graph import Space, SpaceKind
    from app.services.assets.spaces import create_space

    record = db.session.get(Property, property_id)
    if record is None or record.org_id != org_id:
        abort(404)
    back = redirect(url_for("admin.property_spaces", property_id=property_id))
    form = request.form

    try:
        parent_id = (form.get("parent_space_id") or "").strip()
        parent = db.session.get(Space, parent_id) if parent_id else None
        if parent is not None and parent.org_id != org_id:
            abort(404)
        create_space(
            current_session(),
            org_id=org_id,
            property_id=property_id,
            code=(form.get("code") or "").strip(),
            name=(form.get("name") or "").strip(),
            kind=SpaceKind(form.get("kind") or SpaceKind.ROOM.value),
            parent=parent,
            unit_id=(form.get("unit_id") or "").strip() or None,
            area_sqft=_decimal_or_none(form.get("area_sqft")),
        )
        db.session.commit()
    except (AtlasError, ValueError) as exc:
        db.session.rollback()
        flash(_said(exc, "That is not a valid space."), "error")
        return back

    flash("Space added.", "success")
    return back


@admin_bp.post("/spaces/<id:space_id>/move")
@login_required
def space_move(space_id: str) -> Response:
    """Re-parent a space.

    The service refuses a move into a space's own subtree. A cycle here is not
    a validation nicety: every roll-up walks this tree, and one loop hangs the
    page that reports it.
    """
    require(Perm.PROPERTY_UPDATE)
    org_id = require_org_scope()

    from app.models.asset_graph import Space
    from app.services.assets.spaces import move_space

    space = db.session.get(Space, space_id)
    if space is None or space.org_id != org_id:
        abort(404)
    back = redirect(url_for("admin.property_spaces", property_id=space.property_id))

    try:
        target_id = (request.form.get("new_parent_id") or "").strip()
        target = db.session.get(Space, target_id) if target_id else None
        if target is not None and target.org_id != org_id:
            abort(404)
        move_space(current_session(), space=space, new_parent=target)
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash("Space moved.", "success")
    return back


@admin_bp.get("/assets")
@login_required
def assets() -> str:
    """Every asset, worst condition first."""
    require(Perm.ASSET_READ)
    org_id = require_org_scope()

    from app.models.asset_graph import Asset, AssetStatus

    stmt = select(Asset).where(Asset.org_id == org_id)
    status = (request.args.get("status") or "").strip()
    if status in {member.value for member in AssetStatus}:
        stmt = stmt.where(Asset.status == status)
    elif not status:
        stmt = stmt.where(Asset.status != AssetStatus.RETIRED)

    records = list(
        db.session.execute(
            stmt.order_by(Asset.condition_score.asc().nulls_last()).limit(200)
        ).scalars()
    )
    return render_template(
        "admin/assets.html",
        assets=records,
        properties=_properties_by_id(org_id),
        status=status,
        statuses=[member.value for member in AssetStatus],
    )


@admin_bp.get("/assets/<id:asset_id>")
@login_required
def asset_detail(asset_id: str) -> str:
    """One asset: its warranty, its history, and the repair-or-replace call.

    The recommendation is worth little on its own; the three numbers behind it
    are worth a lot, because they turn "it keeps breaking" into something a
    budget meeting can act on.
    """
    require(Perm.ASSET_READ)
    org_id = require_org_scope()

    from app.models.asset_graph import AssetServiceEvent, ServiceEventType
    from app.services.assets.lifecycle import check_warranty, repair_or_replace

    asset = _asset_or_404(asset_id, org_id)
    return render_template(
        "admin/asset.html",
        asset=asset,
        warranty=check_warranty(current_session(), asset=asset),
        advice=repair_or_replace(current_session(), asset=asset),
        history=list(
            db.session.execute(
                select(AssetServiceEvent)
                .where(
                    AssetServiceEvent.org_id == org_id,
                    AssetServiceEvent.asset_id == asset_id,
                )
                .order_by(AssetServiceEvent.performed_on.desc())
            ).scalars()
        ),
        event_types=[member.value for member in ServiceEventType],
        today=utcnow().date(),
    )


@admin_bp.post("/assets/<id:asset_id>/service")
@login_required
def asset_record_service(asset_id: str) -> Response:
    """Record what happened to it.

    The asset's aggregates - service count, lifetime cost, condition - are
    derived here from the event rather than maintained separately, so they
    cannot disagree with the history they summarise.
    """
    require(Perm.ASSET_MANAGE)
    org_id = require_org_scope()

    from app.models.asset_graph import ServiceEventType
    from app.services.assets.lifecycle import record_service

    asset = _asset_or_404(asset_id, org_id)
    back = redirect(url_for("admin.asset_detail", asset_id=asset_id))
    form = request.form

    try:
        condition = (form.get("condition_after") or "").strip()
        record_service(
            current_session(),
            asset=asset,
            event_type=ServiceEventType(form.get("event_type") or ServiceEventType.REPAIR.value),
            performed_on=dt.date.fromisoformat(form.get("performed_on") or ""),
            cost=_decimal_or_none(form.get("cost")) or Decimal("0"),
            condition_after=int(condition) if condition else None,
            notes=(form.get("notes") or "").strip() or None,
            performed_by_id=current_user.id,
        )
        db.session.commit()
    except (AtlasError, ValueError) as exc:
        db.session.rollback()
        flash(_said(exc, "That is not a date, an amount, or a condition."), "error")
        return back

    flash("Service recorded.", "success")
    return back


@admin_bp.post("/asset-events/<id:event_id>/warranty")
@login_required
def asset_event_warranty(event_id: str) -> Response:
    """Mark a repair already paid for as recoverable.

    Separate from recording it, because the discovery usually happens later —
    somebody notices the unit was still covered. The audit event says plainly
    that money was spent on covered work, which is the part worth noticing.
    """
    require(Perm.ASSET_MANAGE)
    org_id = require_org_scope()

    from app.models.asset_graph import AssetServiceEvent
    from app.services.assets.lifecycle import recover_under_warranty

    event = db.session.get(AssetServiceEvent, event_id)
    if event is None or event.org_id != org_id:
        abort(404)
    back = redirect(url_for("admin.asset_detail", asset_id=event.asset_id))

    try:
        recover_under_warranty(current_session(), event=event, actor_id=current_user.id)
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash("Marked as recoverable under warranty.", "success")
    return back


@admin_bp.post("/assets/<id:asset_id>/retire")
@login_required
def asset_retire(asset_id: str) -> Response:
    """Take it out of service.

    Retired rather than deleted: its service history is the evidence behind
    this replacement decision, and behind the next one for the same model.
    """
    require(Perm.ASSET_MANAGE)
    org_id = require_org_scope()

    from app.services.assets.lifecycle import retire_asset

    asset = _asset_or_404(asset_id, org_id)
    back = redirect(url_for("admin.asset_detail", asset_id=asset_id))

    try:
        retired_on = (request.form.get("retired_on") or "").strip()
        retire_asset(
            current_session(),
            asset=asset,
            retired_on=dt.date.fromisoformat(retired_on) if retired_on else None,
            reason=request.form.get("reason") or "",
            actor_id=current_user.id,
        )
        db.session.commit()
    except (AtlasError, ValueError) as exc:
        db.session.rollback()
        flash(_said(exc, "That is not a date."), "error")
        return back

    flash("Retired.", "success")
    return back


def _asset_or_404(asset_id: str, org_id: str):  # noqa: ANN202 - Asset
    from app.models.asset_graph import Asset

    record = db.session.get(Asset, asset_id)
    if record is None or record.org_id != org_id:
        abort(404)
    return record


def _space_ancestors(space):  # noqa: ANN001, ANN202 - list[Space]
    from app.services.assets.spaces import ancestors

    return ancestors(current_session(), space=space)


@admin_bp.get("/extractions")
@login_required
def extractions() -> str:
    """Documents with readings a person has not decided on yet.

    Lowest confidence first, because that is the row worth reaching. A queue
    ordered by arrival puts the machine's most confident guesses in front of a
    reviewer and buries the ones it got wrong.
    """
    require(Perm.DOCUMENT_READ)
    org_id = require_org_scope()

    from app.models.documents import Document
    from app.services.documents.extraction import is_reviewable

    candidates = list(
        db.session.execute(
            select(Document)
            .where(
                Document.org_id == org_id,
                Document.deleted_at.is_(None),
                Document.ocr_text.is_not(None),
            )
            .order_by(Document.extraction_confidence.asc().nulls_first())
            .limit(200)
        ).scalars()
    )
    reviewable = [document for document in candidates if is_reviewable(document)]
    return render_template(
        "admin/extractions.html",
        documents=reviewable,
        # Undecided, so the count means "waiting on you" rather than "seen".
        pending=[
            document for document in reviewable if (document.extraction_confidence or 0) < 100
        ],
    )


@admin_bp.get("/extractions/<id:document_id>")
@login_required
def extraction_detail(document_id: str) -> str:
    """What this document appears to say, and the text each reading came from.

    The evidence is shown beside every value so the reviewer checks rather than
    trusts. A confidence score with nothing behind it just moves the guess from
    the machine to the person.
    """
    require(Perm.DOCUMENT_READ)
    org_id = require_org_scope()

    from app.services.documents.extraction import REVIEW_THRESHOLD, extraction_for

    document = _document_or_404(document_id, org_id)
    try:
        extraction = extraction_for(document)
    except AtlasError as exc:
        flash(str(exc), "error")
        return render_template(
            "admin/extraction.html",
            document=document,
            extraction=None,
            threshold=REVIEW_THRESHOLD,
        )

    return render_template(
        "admin/extraction.html",
        document=document,
        extraction=extraction,
        threshold=REVIEW_THRESHOLD,
    )


@admin_bp.post("/extractions/<id:document_id>")
@login_required
def extraction_decide(document_id: str) -> Response:
    """Accept a reading, correct it, or throw it out.

    Accepting is the only path from extracted text to a value the system will
    act on, and it is attributed — so "why does it say this?" has an answer
    that is a name and a sentence rather than a shrug.

    A correction is not a separate action. The common case is that the
    extractor found the right field and misread a digit, and making the
    reviewer reject-then-retype loses the evidence link that made the reading
    checkable in the first place.
    """
    require(Perm.DOCUMENT_EXTRACTION_REVIEW)
    org_id = require_org_scope()

    from app.services.documents.extraction import (
        accept_suggestion,
        extraction_for,
        record_decisions,
        reject_suggestion,
    )

    document = _document_or_404(document_id, org_id)
    back = redirect(url_for("admin.extraction_detail", document_id=document_id))
    field_name = (request.form.get("field") or "").strip()
    action = (request.form.get("action") or "").strip().lower()

    try:
        extraction = extraction_for(document)
        if action == "accept":
            correction = (request.form.get("value") or "").strip()
            accept_suggestion(
                current_session(),
                extraction=extraction,
                field_name=field_name,
                accepted_by_id=current_user.id,
                org_id=org_id,
                value=correction or None,
            )
            message = f"{field_name} accepted."
        elif action == "reject":
            reject_suggestion(extraction, field_name=field_name, rejected_by_id=current_user.id)
            message = f"{field_name} thrown out."
        else:
            flash("That is not something you can do to a reading.", "error")
            return back
        record_decisions(document, extraction)
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash(message, "success")
    return back


def _document_or_404(document_id: str, org_id: str):  # noqa: ANN202 - Document
    from app.models.documents import Document

    record = db.session.get(Document, document_id)
    if record is None or record.org_id != org_id or record.deleted_at is not None:
        abort(404)
    return record


@admin_bp.get("/imports")
@login_required
def imports() -> str:
    """Upload a file and see what it would do before it does it."""
    require(Perm.IMPORT_RUN)
    require_org_scope()

    from app.services.imports.bulk import MAX_ROWS, importer, known_importers

    return render_template(
        "admin/imports.html",
        importers=[importer(name) for name in known_importers()],
        max_rows=MAX_ROWS,
        plan=None,
        resource=None,
        csv_text=None,
    )


@admin_bp.get("/imports/<resource>/template")
@login_required
def import_template(resource: str) -> Response:
    """A header row, so nobody has to guess the columns."""
    require(Perm.IMPORT_RUN)
    require_org_scope()

    from app.errors import NotFound as AtlasNotFound
    from app.services.imports.bulk import template_for

    try:
        body = template_for(resource)
    except (AtlasError, AtlasNotFound):
        abort(404)

    response = Response(body, mimetype="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="{resource}-template.csv"'
    return response


@admin_bp.post("/imports/plan")
@login_required
def import_plan() -> str | Response:
    """What this file would do. Writes nothing at all.

    The whole point of the module: a CSV of four hundred units is not something
    anybody can check by reading, and an import that goes straight to writing
    leaves the operator finding out afterwards.
    """
    require(Perm.IMPORT_RUN)
    org_id = require_org_scope()

    from app.services.imports.bulk import MAX_ROWS, importer, known_importers, plan_import

    resource = (request.form.get("resource") or "").strip()
    text = _uploaded_text("file") or (request.form.get("csv") or "")
    if not text.strip():
        flash("There was nothing to read.", "error")
        return redirect(url_for("admin.imports"))

    try:
        plan = plan_import(current_session(), org_id=org_id, resource=resource, text=text)
    except AtlasError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.imports"))

    return render_template(
        "admin/imports.html",
        importers=[importer(name) for name in known_importers()],
        max_rows=MAX_ROWS,
        plan=plan,
        resource=resource,
        # Carried through the form rather than stored: the apply step re-plans
        # these same bytes, so nothing decided here has to survive a round trip.
        csv_text=text,
    )


@admin_bp.post("/imports/apply")
@login_required
def import_apply() -> Response:
    """Write the file, if it still does what was shown.

    The plan is re-derived here rather than carried over. Applying a decision
    taken against a database that has moved since is how an "update" quietly
    becomes a "create"; if the fresh plan disagrees with what the operator
    confirmed, that is a person's call and not this route's.
    """
    require(Perm.IMPORT_RUN)
    org_id = require_org_scope()

    from app.services.imports.bulk import apply_plan, plan_import

    resource = (request.form.get("resource") or "").strip()
    text = request.form.get("csv") or ""
    back = redirect(url_for("admin.imports"))

    try:
        expected = (
            int(request.form.get("expect_creates") or 0),
            int(request.form.get("expect_updates") or 0),
            int(request.form.get("expect_unchanged") or 0),
        )
        plan = plan_import(current_session(), org_id=org_id, resource=resource, text=text)
        if plan.is_valid and (plan.creates, plan.updates, plan.unchanged) != expected:
            flash(
                f"This file no longer does what you were shown: {plan.creates} creates, "
                f"{plan.updates} updates, {plan.unchanged} unchanged now, against the "
                f"{expected[0]}/{expected[1]}/{expected[2]} you confirmed. Plan it again.",
                "error",
            )
            return back
        apply_plan(current_session(), org_id=org_id, plan=plan, actor_id=current_user.id)
        db.session.commit()
    except (AtlasError, ValueError) as exc:
        db.session.rollback()
        flash(_said(exc, "That file could not be applied."), "error")
        return back

    flash(
        f"{plan.creates} created, {plan.updates} updated, {plan.unchanged} already matched.",
        "success",
    )
    return back


def _uploaded_text(field: str) -> str:
    """Read an uploaded file as text, tolerating the BOM Excel writes."""
    upload = request.files.get(field)
    if upload is None or not upload.filename:
        return ""
    return upload.read().decode("utf-8-sig", errors="replace")


@admin_bp.get("/identity-providers")
@login_required
def identity_providers() -> str:
    """Single sign-on and directory provisioning.

    The SCIM token is shown once, at the moment it is issued, and never again.
    Storing it recoverably would mean a leaked database hands somebody the
    ability to deactivate every account in the tenant.
    """
    require(Perm.INTEGRATION_READ)
    org_id = require_org_scope()

    from app.models.sso import IdentityProvider

    return render_template(
        "admin/identity_providers.html",
        providers=list(
            db.session.execute(
                select(IdentityProvider)
                .where(IdentityProvider.org_id == org_id)
                .order_by(IdentityProvider.code)
            ).scalars()
        ),
        # Handed over exactly once, on the redirect that issued it.
        issued_token=session.pop("scim_token_once", None),
    )


@admin_bp.post("/identity-providers/<id:provider_id>/scim-token")
@login_required
def identity_provider_scim_token(provider_id: str) -> Response:
    """Issue or revoke the bearer token the directory presents."""
    require(Perm.INTEGRATION_MANAGE)
    org_id = require_org_scope()

    from app.models.sso import IdentityProvider
    from app.services.iam.scim import issue_scim_token, revoke_scim_token

    provider = db.session.get(IdentityProvider, provider_id)
    if provider is None or provider.org_id != org_id:
        abort(404)
    back = redirect(url_for("admin.identity_providers"))
    action = (request.form.get("action") or "").strip().lower()

    try:
        if action == "issue":
            token = issue_scim_token(current_session(), provider=provider, actor_id=current_user.id)
            # Through the session rather than the flash body: a token in a
            # flash message ends up in whatever renders flashes, and this one
            # should be on exactly one page for exactly one reader.
            session["scim_token_once"] = token
            message = "Token issued. It is shown once and cannot be recovered."
        elif action == "revoke":
            revoke_scim_token(current_session(), provider=provider, actor_id=current_user.id)
            message = "Token revoked. The directory can no longer call."
        else:
            flash("That is not something you can do to a token.", "error")
            return back
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash(message, "success")
    return back


@admin_bp.get("/embed-forms")
@login_required
def embed_forms() -> str:
    """The enquiry forms this organization publishes, and the snippet for each.

    The key is shown in full and repeatedly, unlike the SCIM token beside it.
    That is not an inconsistency: this one is pasted into a public page and is
    therefore readable by anyone who views source. Treating it as a secret
    would teach operators the wrong lesson about the one next to it that is.
    """
    require(Perm.INTEGRATION_READ)
    org_id = require_org_scope()

    from app.models.leasing import EmbedForm
    from app.services.leasing.embeds import snippet_for

    forms = list(
        db.session.execute(
            select(EmbedForm)
            .where(EmbedForm.org_id == org_id)
            .order_by(EmbedForm.revoked_at.is_(None).desc(), EmbedForm.created_at.desc())
        ).scalars()
    )

    return render_template(
        "admin/embed_forms.html",
        rows=[{"form": form, "snippet": snippet_for(form, base_url=request.host_url)} for form in forms],
        properties=list(
            db.session.execute(
                select(Property).where(Property.org_id == org_id).order_by(Property.name)
            ).scalars()
        ),
    )


@admin_bp.post("/embed-forms")
@login_required
def embed_form_create() -> Response:
    """Issue a key for one page of the operator's site."""
    require(Perm.INTEGRATION_MANAGE)
    org_id = require_org_scope()

    from app.services.leasing.embeds import create_embed_form

    back = redirect(url_for("admin.embed_forms"))
    form = request.form

    property_id = (form.get("property_id") or "").strip() or None
    if property_id is not None:
        record = db.session.get(Property, property_id)
        # 404 rather than a validation message: an id from another tenant must
        # not be distinguishable from one that was never real.
        if record is None or record.org_id != org_id:
            abort(404)

    try:
        create_embed_form(
            current_session(),
            org_id=org_id,
            label=(form.get("label") or "").strip(),
            # One per line is what an operator actually pastes.
            allowed_origins=[
                line.strip()
                for line in (form.get("allowed_origins") or "").splitlines()
                if line.strip()
            ],
            property_id=property_id,
            actor_id=current_user.id,
        )
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash("Form created. Copy the snippet into your site.", "success")
    return back


@admin_bp.post("/embed-forms/<id:form_id>")
@login_required
def embed_form_update(form_id: str) -> Response:
    """Pause, resume, or permanently revoke a key."""
    require(Perm.INTEGRATION_MANAGE)
    org_id = require_org_scope()

    from app.models.leasing import EmbedForm
    from app.services.leasing.embeds import revoke_embed_form, update_embed_form

    record = db.session.get(EmbedForm, form_id)
    if record is None or record.org_id != org_id:
        abort(404)

    back = redirect(url_for("admin.embed_forms"))
    action = (request.form.get("action") or "").strip().lower()

    try:
        if action == "revoke":
            revoke_embed_form(current_session(), form=record, actor_id=current_user.id)
            message = "Form revoked. The snippet no longer renders anywhere."
        elif action in {"pause", "resume"}:
            update_embed_form(
                current_session(),
                form=record,
                enabled=(action == "resume"),
                actor_id=current_user.id,
            )
            message = "Form paused." if action == "pause" else "Form live again."
        else:
            flash("That is not something you can do to a form.", "error")
            return back
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash(message, "success")
    return back


@admin_bp.get("/reconciliations")
@login_required
def reconciliations() -> str:
    """Every tie-out, newest statement first."""
    require(Perm.RECONCILIATION_READ)
    org_id = require_org_scope()

    from app.models.accounting import Reconciliation, ReconciliationStatus

    stmt = select(Reconciliation).where(Reconciliation.org_id == org_id)
    status = (request.args.get("status") or "").strip()
    if status in {member.value for member in ReconciliationStatus}:
        stmt = stmt.where(Reconciliation.status == status)

    records = list(
        db.session.execute(stmt.order_by(Reconciliation.statement_end.desc()).limit(200)).scalars()
    )
    return render_template(
        "admin/reconciliations.html",
        reconciliations=records,
        accounts=_bank_accounts_by_id(org_id),
        status=status,
        statuses=[member.value for member in ReconciliationStatus],
    )


@admin_bp.get("/reconciliations/<id:reconciliation_id>")
@login_required
def reconciliation_detail(reconciliation_id: str) -> str:
    """The workspace: what is matched, what is not, and what is out.

    The difference is shown from the stored figures rather than recomputed for
    display, because the number somebody signs off has to be the number the
    record holds.
    """
    require(Perm.RECONCILIATION_READ)
    org_id = require_org_scope()

    from app.models.accounting import BankTransaction
    from app.services.accounting.reconciliation import (
        MatchStatus,
        refresh_totals,
        suggest_matches,
        unresolved_exceptions,
    )

    record = _reconciliation_or_404(reconciliation_id, org_id)
    refresh_totals(current_session(), reconciliation=record)
    db.session.commit()

    transactions = list(
        db.session.execute(
            select(BankTransaction)
            .where(
                BankTransaction.org_id == org_id,
                BankTransaction.reconciliation_id == reconciliation_id,
            )
            .order_by(BankTransaction.posted_date)
        ).scalars()
    )
    outstanding = [
        record_
        for record_ in transactions
        if record_.match_status not in (MatchStatus.MATCHED, MatchStatus.IGNORED)
    ]
    return render_template(
        "admin/reconciliation.html",
        reconciliation=record,
        account=db.session.get(BankAccount, record.bank_account_id),
        transactions=transactions,
        outstanding=outstanding,
        # Suggestions for the first few unmatched lines only. Scoring every
        # line against the whole ledger on a page load is how a workspace
        # becomes something nobody opens.
        suggestions={
            record_.id: suggest_matches(current_session(), transaction=record_)
            for record_ in outstanding[:10]
        },
        unresolved=unresolved_exceptions(current_session(), record),
    )


@admin_bp.post("/reconciliations")
@login_required
def reconciliation_open() -> Response:
    """Start a reconciliation over a statement window."""
    require(Perm.RECONCILIATION_MANAGE)
    org_id = require_org_scope()

    from app.services.accounting.reconciliation import open_reconciliation

    form = request.form
    back = redirect(url_for("admin.reconciliations"))

    try:
        opening = _decimal_or_none(form.get("opening_balance"))
        closing = _decimal_or_none(form.get("closing_balance"))
        if opening is None or closing is None:
            raise ValidationFailed("Both balances are needed to tie out against.")
        record = open_reconciliation(
            current_session(),
            org_id=org_id,
            bank_account_id=form.get("bank_account_id") or "",
            statement_start=dt.date.fromisoformat(form.get("statement_start") or ""),
            statement_end=dt.date.fromisoformat(form.get("statement_end") or ""),
            opening_balance=opening,
            closing_balance=closing,
            actor_id=current_user.id,
        )
        db.session.commit()
    except (AtlasError, ValueError) as exc:
        db.session.rollback()
        flash(_said(exc, "That is not a date or an amount."), "error")
        return back

    flash("Reconciliation opened.", "success")
    return redirect(url_for("admin.reconciliation_detail", reconciliation_id=record.id))


@admin_bp.post("/reconciliations/<id:reconciliation_id>/statement")
@login_required
def reconciliation_import(reconciliation_id: str) -> Response:
    """Load a bank CSV export.

    Re-importing the same file inserts nothing: each line carries a stable
    fingerprint including an occurrence index, so two genuinely identical
    transactions on one day both survive while a repeat of either does not.
    """
    require(Perm.RECONCILIATION_MANAGE)
    org_id = require_org_scope()

    from app.services.accounting.reconciliation import import_statement, parse_statement_csv

    record = _reconciliation_or_404(reconciliation_id, org_id)
    back = redirect(url_for("admin.reconciliation_detail", reconciliation_id=reconciliation_id))

    upload = request.files.get("statement")
    text = (
        upload.read().decode("utf-8-sig", errors="replace")
        if upload is not None and upload.filename
        else (request.form.get("csv") or "")
    )
    if not text.strip():
        flash("There was nothing to import.", "error")
        return back

    try:
        lines = parse_statement_csv(
            text,
            date_column=(request.form.get("date_column") or "date").strip(),
            amount_column=(request.form.get("amount_column") or "amount").strip(),
            description_column=(request.form.get("description_column") or "description").strip(),
        )
        result = import_statement(
            current_session(),
            org_id=org_id,
            bank_account_id=record.bank_account_id,
            lines=lines,
            actor_id=current_user.id,
        )
        db.session.commit()
    except (AtlasError, ValueError) as exc:
        db.session.rollback()
        flash(_said(exc, "That file could not be read as a bank statement."), "error")
        return back

    flash(
        f"{result.count} imported, {result.duplicates} already present, "
        f"{result.rejected} rejected.",
        "success",
    )
    return back


@admin_bp.post("/reconciliations/<id:reconciliation_id>/auto-match")
@login_required
def reconciliation_auto_match(reconciliation_id: str) -> Response:
    """Match what is confident and unambiguous, and leave the rest.

    A near-tie stays unmatched on purpose. Guessing between two payments of the
    same amount on the same day is how a reconciliation quietly stops meaning
    anything.
    """
    require(Perm.RECONCILIATION_MANAGE)
    org_id = require_org_scope()

    from app.services.accounting.reconciliation import auto_match

    record = _reconciliation_or_404(reconciliation_id, org_id)
    back = redirect(url_for("admin.reconciliation_detail", reconciliation_id=reconciliation_id))

    try:
        matched = auto_match(
            current_session(),
            org_id=org_id,
            bank_account_id=record.bank_account_id,
            actor_id=current_user.id,
        )
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash(
        f"{len(matched)} matched. Anything left needs a person, which is the point.",
        "success",
    )
    return back


@admin_bp.post("/bank-transactions/<id:transaction_id>")
@login_required
def bank_transaction_action(transaction_id: str) -> Response:
    """Match a bank line to a ledger line, or release one."""
    require(Perm.RECONCILIATION_MANAGE)
    org_id = require_org_scope()

    from app.models.accounting import BankTransaction, JournalLine
    from app.services.accounting.reconciliation import match_transaction, unmatch_transaction

    record = db.session.get(BankTransaction, transaction_id)
    if record is None or record.org_id != org_id:
        abort(404)
    back = redirect(
        url_for("admin.reconciliation_detail", reconciliation_id=record.reconciliation_id)
        if record.reconciliation_id
        else url_for("admin.reconciliations")
    )
    action = (request.form.get("action") or "").strip().lower()

    try:
        if action == "match":
            line = db.session.get(JournalLine, request.form.get("journal_line_id") or "")
            if line is None or line.org_id != org_id:
                abort(404)
            match_transaction(
                current_session(),
                transaction=record,
                journal_line=line,
                actor_id=current_user.id,
            )
            message = "Matched."
        elif action == "unmatch":
            unmatch_transaction(current_session(), transaction=record)
            message = "Released."
        else:
            flash("That is not something you can do to a bank line.", "error")
            return back
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash(message, "success")
    return back


@admin_bp.post("/reconciliations/<id:reconciliation_id>/exceptions")
@login_required
def reconciliation_exception(reconciliation_id: str) -> Response:
    """Record something that does not agree and needs a person."""
    require(Perm.RECONCILIATION_MANAGE)
    org_id = require_org_scope()

    from app.services.accounting.reconciliation import raise_exception

    record = _reconciliation_or_404(reconciliation_id, org_id)
    back = redirect(url_for("admin.reconciliation_detail", reconciliation_id=reconciliation_id))

    try:
        raise_exception(
            current_session(),
            reconciliation=record,
            kind=(request.form.get("kind") or "unexplained").strip(),
            description=(request.form.get("description") or "").strip(),
            amount=_decimal_or_none(request.form.get("amount")),
            bank_transaction_id=(request.form.get("bank_transaction_id") or "").strip() or None,
        )
        db.session.commit()
    except (AtlasError, ValueError) as exc:
        db.session.rollback()
        flash(_said(exc, "That is not an amount."), "error")
        return back

    flash("Exception recorded.", "success")
    return back


@admin_bp.post("/reconciliation-exceptions/<id:exception_id>")
@login_required
def reconciliation_exception_resolve(exception_id: str) -> Response:
    """Close an exception, with the note that makes it auditable."""
    require(Perm.RECONCILIATION_MANAGE)
    org_id = require_org_scope()

    from app.models.accounting import ReconciliationException
    from app.services.accounting.reconciliation import resolve_exception

    record = db.session.get(ReconciliationException, exception_id)
    if record is None or record.org_id != org_id:
        abort(404)
    back = redirect(
        url_for("admin.reconciliation_detail", reconciliation_id=record.reconciliation_id)
    )

    try:
        resolve_exception(
            current_session(),
            exception=record,
            resolved_by_id=current_user.id,
            note=request.form.get("note") or "",
        )
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash("Exception resolved.", "success")
    return back


@admin_bp.post("/reconciliations/<id:reconciliation_id>/complete")
@login_required
def reconciliation_complete(reconciliation_id: str) -> Response:
    """Sign off. Refuses anything that does not actually agree.

    A reconciliation that can be signed while it is out is not a
    reconciliation, so the refusals here are the whole value of the screen.
    """
    require(Perm.RECONCILIATION_MANAGE)
    org_id = require_org_scope()

    from app.services.accounting.reconciliation import complete_reconciliation

    record = _reconciliation_or_404(reconciliation_id, org_id)
    back = redirect(url_for("admin.reconciliation_detail", reconciliation_id=reconciliation_id))

    try:
        complete_reconciliation(
            current_session(),
            reconciliation=record,
            completed_by_id=current_user.id,
            notes=(request.form.get("notes") or "").strip() or None,
        )
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash("Reconciled and signed off.", "success")
    return back


def _reconciliation_or_404(reconciliation_id: str, org_id: str):  # noqa: ANN202 - Reconciliation
    from app.models.accounting import Reconciliation

    record = db.session.get(Reconciliation, reconciliation_id)
    if record is None or record.org_id != org_id:
        abort(404)
    return record


def _bank_accounts_by_id(org_id: str) -> dict[str, BankAccount]:
    return {
        account.id: account
        for account in db.session.execute(
            select(BankAccount).where(BankAccount.org_id == org_id)
        ).scalars()
    }


@admin_bp.get("/inspections")
@login_required
def inspections() -> str:
    """What is booked, what is running, and what has been signed off."""
    require(Perm.INSPECTION_READ)
    org_id = require_org_scope()

    from app.models.maintenance import Inspection, InspectionKind

    stmt = select(Inspection).where(Inspection.org_id == org_id)
    status = (request.args.get("status") or "").strip()
    if status:
        stmt = stmt.where(Inspection.status == status)

    records = list(
        db.session.execute(
            stmt.order_by(Inspection.scheduled_for.desc().nulls_last()).limit(200)
        ).scalars()
    )
    return render_template(
        "admin/inspections.html",
        inspections=records,
        properties=_properties_by_id(org_id),
        units=_units_by_id(org_id),
        templates=_inspection_templates(org_id),
        kinds=[member.value for member in InspectionKind],
        status=status,
        statuses=["scheduled", "in_progress", "completed", "cancelled"],
    )


@admin_bp.get("/inspections/<id:inspection_id>")
@login_required
def inspection_detail(inspection_id: str) -> str:
    """One inspection and its checklist as performed.

    The checklist was copied onto this record at the template version used, so
    what is shown here is what was actually asked - editing the template since
    has not rewritten history.
    """
    require(Perm.INSPECTION_READ)
    org_id = require_org_scope()

    from app.models.maintenance import ItemResult
    from app.services.maintenance.inspections import items_missing_evidence

    record = _inspection_or_404(inspection_id, org_id)
    items = sorted(record.items, key=lambda item: item.sort_order)
    return render_template(
        "admin/inspection.html",
        inspection=record,
        items=items,
        property=db.session.get(Property, record.property_id),
        unit=db.session.get(Unit, record.unit_id) if record.unit_id else None,
        unanswered=[item for item in items if item.result is None],
        # Asked of the service rather than recomputed here: a surface that
        # reimplements this rule is a surface that will drift from it, and the
        # drift shows up as a sign-off the page offered and the service refused.
        missing_photos=items_missing_evidence(current_session(), record),
        results=[member.value for member in ItemResult],
    )


@admin_bp.post("/inspections")
@login_required
def inspection_schedule() -> Response:
    """Book an inspection, freezing its checklist at the template version used."""
    require(Perm.INSPECTION_MANAGE)
    org_id = require_org_scope()

    from app.models.maintenance import InspectionKind
    from app.services.maintenance.inspections import current_template, schedule_inspection

    form = request.form
    back = redirect(url_for("admin.inspections"))

    try:
        code = (form.get("template_code") or "").strip()
        scheduled = (form.get("scheduled_for") or "").strip()
        record = schedule_inspection(
            current_session(),
            org_id=org_id,
            kind=InspectionKind(form.get("kind") or InspectionKind.ROUTINE.value),
            property_id=form.get("property_id") or "",
            template=(
                current_template(current_session(), org_id=org_id, code=code) if code else None
            ),
            unit_id=(form.get("unit_id") or "").strip() or None,
            scheduled_for=dt.datetime.fromisoformat(scheduled) if scheduled else None,
            inspector_user_id=current_user.id,
            actor_id=current_user.id,
        )
        db.session.commit()
    except (AtlasError, ValueError) as exc:
        db.session.rollback()
        flash(_said(exc, "That is not a date and time."), "error")
        return back

    flash(f"Inspection {record.inspection_number} booked.", "success")
    return redirect(url_for("admin.inspection_detail", inspection_id=record.id))


@admin_bp.post("/inspections/<id:inspection_id>/findings")
@login_required
def inspection_record(inspection_id: str) -> Response:
    """Record one observation. Re-recording an item overwrites it."""
    require(Perm.INSPECTION_PERFORM)
    org_id = require_org_scope()

    from app.models.maintenance import ItemResult
    from app.services.maintenance.inspections import (
        ItemFinding,
        record_finding,
        start_inspection,
    )

    inspection = _inspection_or_404(inspection_id, org_id)
    back = redirect(url_for("admin.inspection_detail", inspection_id=inspection_id))
    form = request.form

    try:
        # Recording the first finding is what "started" means; asking somebody
        # to press a separate button first is a step nobody would remember.
        if inspection.status == "scheduled":
            start_inspection(current_session(), inspection=inspection)
        record_finding(
            current_session(),
            inspection=inspection,
            finding=ItemFinding(
                item_id=form.get("item_id") or "",
                result=ItemResult(form.get("result") or ItemResult.PASS.value),
                condition=(form.get("condition") or "").strip() or None,
                notes=(form.get("notes") or "").strip() or None,
                remedy_cost=_decimal_or_none(form.get("remedy_cost")),
                is_resident_responsible=bool(form.get("is_resident_responsible")),
            ),
        )
        db.session.commit()
    except (AtlasError, ValueError) as exc:
        db.session.rollback()
        flash(_said(exc, "That is not a valid finding."), "error")
        return back

    return back


@admin_bp.post("/inspections/<id:inspection_id>/complete")
@login_required
def inspection_complete(inspection_id: str) -> Response:
    """Sign off, and raise work from what failed.

    Refused while any item has no finding, and refused while a failed item that
    demands a photo has none. Nobody can take that photo retrospectively, which
    is exactly why the refusal has to land here rather than at a deposit
    disposition three weeks later.
    """
    require(Perm.INSPECTION_PERFORM)
    org_id = require_org_scope()

    from app.services.maintenance.inspections import (
        complete_inspection,
        raise_work_orders_from_findings,
    )

    inspection = _inspection_or_404(inspection_id, org_id)
    back = redirect(url_for("admin.inspection_detail", inspection_id=inspection_id))

    try:
        complete_inspection(
            current_session(),
            inspection=inspection,
            notes=(request.form.get("notes") or "").strip() or None,
            inspector_signed=bool(request.form.get("inspector_signed")),
            resident_signed=bool(request.form.get("resident_signed")),
            actor_id=current_user.id,
        )
        raised = raise_work_orders_from_findings(
            current_session(), inspection=inspection, actor_id=current_user.id
        )
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash(
        f"Signed off as {inspection.result}."
        + (f" {len(raised)} work order(s) raised." if raised else ""),
        "success",
    )
    return back


def _inspection_or_404(inspection_id: str, org_id: str):  # noqa: ANN202 - Inspection
    from app.models.maintenance import Inspection

    record = db.session.get(Inspection, inspection_id)
    if record is None or record.org_id != org_id:
        abort(404)
    return record


def _inspection_templates(org_id: str) -> list:  # noqa: ANN201 - InspectionTemplate
    from app.models.maintenance import InspectionTemplate

    return list(
        db.session.execute(
            select(InspectionTemplate)
            .where(InspectionTemplate.org_id == org_id, InspectionTemplate.is_active.is_(True))
            .order_by(InspectionTemplate.code)
        ).scalars()
    )


def _properties_by_id(org_id: str) -> dict[str, Property]:
    return {
        record.id: record
        for record in db.session.execute(
            select(Property).where(Property.org_id == org_id)
        ).scalars()
    }


@admin_bp.get("/bills")
@login_required
def bills() -> str:
    """The payables queue: what is waiting on an approver, and what is due."""
    require(Perm.BILL_READ)
    org_id = require_org_scope()

    from app.models.accounting import Bill, BillStatus
    from app.models.vendor import Vendor

    today = utcnow().date()
    stmt = select(Bill).where(Bill.org_id == org_id)
    status = (request.args.get("status") or "").strip()
    if status in {member.value for member in BillStatus}:
        stmt = stmt.where(Bill.status == status)

    records = list(db.session.execute(stmt.order_by(Bill.due_date.asc()).limit(200)).scalars())
    return render_template(
        "admin/bills.html",
        bills=records,
        vendors={
            vendor.id: vendor
            for vendor in db.session.execute(
                select(Vendor).where(Vendor.org_id == org_id)
            ).scalars()
        },
        awaiting=[b for b in records if b.status == BillStatus.PENDING_APPROVAL],
        due=[
            b
            for b in records
            if b.status in (BillStatus.APPROVED, BillStatus.PARTIALLY_PAID)
            and b.balance > Decimal("0")
            and b.due_date <= today
        ],
        status=status,
        statuses=[member.value for member in BillStatus],
        today=today,
    )


@admin_bp.get("/bills/<id:bill_id>")
@login_required
def bill_detail(bill_id: str) -> str:
    """One bill, its coded lines, and what has been disbursed against it."""
    require(Perm.BILL_READ)
    org_id = require_org_scope()

    from app.models.accounting import BankAccount, BillPayment
    from app.models.vendor import Vendor
    from app.services.accounting.payables import requires_approval

    record = _bill_or_404(bill_id, org_id)
    return render_template(
        "admin/bill.html",
        bill=record,
        vendor=db.session.get(Vendor, record.vendor_id),
        payments=list(
            db.session.execute(
                select(BillPayment)
                .where(BillPayment.org_id == org_id, BillPayment.bill_id == bill_id)
                .order_by(BillPayment.paid_date)
            ).scalars()
        ),
        bank_accounts=list(
            db.session.execute(
                select(BankAccount).where(
                    BankAccount.org_id == org_id, BankAccount.is_trust.is_(False)
                )
            ).scalars()
        ),
        # An unset threshold means everything needs a second person. A control
        # that disappears when configuration is missing is not a control.
        needs_approval=requires_approval(current_session(), org_id=org_id, total=record.total),
        # Whoever recorded it cannot approve it, and the page should not offer
        # a button the service is going to refuse.
        is_own_bill=record.created_by_id == current_user.id,
        today=utcnow().date(),
    )


@admin_bp.post("/bills/<id:bill_id>/approve")
@login_required
def bill_approve(bill_id: str) -> Response:
    """Authorise a bill for payment.

    The service refuses an approver who is the bill's author. That is by
    identity, not by role: fake-vendor fraud needs one person able to do both
    halves, and no amount of seniority makes one person into two.
    """
    require(Perm.BILL_APPROVE)
    org_id = require_org_scope()

    from app.services.accounting.payables import approve_bill

    bill = _bill_or_404(bill_id, org_id)
    back = redirect(url_for("admin.bill_detail", bill_id=bill_id))

    try:
        approve_bill(
            current_session(),
            bill=bill,
            approver_id=current_user.id,
            note=(request.form.get("note") or "").strip() or None,
        )
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash(f"Approved {bill.total} for payment.", "success")
    return back


@admin_bp.post("/bills/<id:bill_id>/payments")
@login_required
def bill_pay(bill_id: str) -> Response:
    """Disburse against an approved bill."""
    require(Perm.BILL_PAY)
    org_id = require_org_scope()

    from app.models.accounting import PaymentMethod
    from app.services.accounting.payables import pay_bill

    bill = _bill_or_404(bill_id, org_id)
    back = redirect(url_for("admin.bill_detail", bill_id=bill_id))
    form = request.form

    try:
        amount = _decimal_or_none(form.get("amount"))
        if amount is None:
            raise ValidationFailed("A payment needs an amount.")
        pay_bill(
            current_session(),
            bill=bill,
            bank_account_id=form.get("bank_account_id") or "",
            amount=amount,
            paid_date=dt.date.fromisoformat(form.get("paid_date") or ""),
            method=PaymentMethod(form.get("method") or PaymentMethod.CHECK.value),
            check_number=(form.get("check_number") or "").strip() or None,
            actor_id=current_user.id,
        )
        db.session.commit()
    except (AtlasError, ValueError) as exc:
        db.session.rollback()
        flash(_said(exc, "That is not a date or an amount."), "error")
        return back

    flash(f"Paid. {bill.balance} still owing.", "success")
    return back


def _bill_or_404(bill_id: str, org_id: str):  # noqa: ANN202 - Bill
    from app.models.accounting import Bill

    record = db.session.get(Bill, bill_id)
    if record is None or record.org_id != org_id:
        abort(404)
    return record


@admin_bp.get("/leases")
@login_required
def leases() -> str:
    """Active tenancies, expiring soonest first."""
    require(Perm.LEASE_READ)
    org_id = require_org_scope()

    stmt = select(Lease).where(Lease.org_id == org_id)
    status = (request.args.get("status") or "").strip()
    if status in {member.value for member in LeaseStatus}:
        stmt = stmt.where(Lease.status == status)
    elif not status:
        stmt = stmt.where(Lease.status.in_([LeaseStatus.ACTIVE, LeaseStatus.HOLDOVER]))

    records = list(db.session.execute(stmt.order_by(Lease.end_date.asc()).limit(200)).scalars())
    return render_template(
        "admin/leases.html",
        leases=records,
        units=_units_by_id(org_id),
        status=status,
        statuses=[member.value for member in LeaseStatus],
        today=utcnow().date(),
        # Jinja has no timedelta, and a 90-day horizon computed in the
        # template would be computed once per row.
        cutoff=utcnow().date() + dt.timedelta(days=90),
    )


@admin_bp.get("/leases/<id:lease_id>")
@login_required
def lease_detail(lease_id: str) -> str:
    """One lease, its renewal history, and its move-out if it has one."""
    require(Perm.LEASE_READ)
    org_id = require_org_scope()

    from app.models.leasing import LeaseRenewal, MoveOut

    lease = _lease_or_404(lease_id, org_id)
    return render_template(
        "admin/lease.html",
        lease=lease,
        unit=db.session.get(Unit, lease.unit_id) if lease.unit_id else None,
        renewals=list(
            db.session.execute(
                select(LeaseRenewal)
                .where(LeaseRenewal.org_id == org_id, LeaseRenewal.lease_id == lease_id)
                .order_by(LeaseRenewal.created_at.desc())
            ).scalars()
        ),
        move_out=db.session.execute(
            select(MoveOut).where(MoveOut.org_id == org_id, MoveOut.lease_id == lease_id)
        ).scalar_one_or_none(),
        # What was actually collected. The lease's own figure is what was
        # agreed, which is a different number whenever a deposit was waived,
        # part-paid, or replaced by a rider.
        held=_deposit_held(org_id, lease_id),
        today=utcnow().date(),
    )


@admin_bp.post("/leases/<id:lease_id>/renewals")
@login_required
def lease_offer_renewal(lease_id: str) -> Response:
    """Offer terms for the next term. They are fixed at this moment."""
    require(Perm.LEASE_RENEW)
    org_id = require_org_scope()

    from app.services.leasing.tenancy import offer_renewal

    lease = _lease_or_404(lease_id, org_id)
    back = redirect(url_for("admin.lease_detail", lease_id=lease_id))
    form = request.form

    try:
        offer_renewal(
            current_session(),
            lease=lease,
            offered_rent=_decimal_or_none(form.get("offered_rent")) or Decimal("0"),
            proposed_start=dt.date.fromisoformat(form.get("proposed_start") or ""),
            proposed_end=dt.date.fromisoformat(form.get("proposed_end") or ""),
            term_months=int(form.get("term_months") or 12),
            expires_in_days=int(form.get("expires_in_days") or 30),
            actor_id=current_user.id,
        )
        db.session.commit()
    except (AtlasError, ValueError) as exc:
        db.session.rollback()
        flash(_said(exc, "That is not a date."), "error")
        return back

    flash("Renewal offered.", "success")
    return back


@admin_bp.post("/renewals/<id:renewal_id>")
@login_required
def renewal_action(renewal_id: str) -> Response:
    """Accept an offer, or decline it.

    Accepting a lapsed offer is refused by the service. Honouring an expired
    price is a decision somebody should take deliberately, by making a new
    offer at the price they mean.
    """
    require(Perm.LEASE_RENEW)
    org_id = require_org_scope()

    from app.models.leasing import LeaseRenewal
    from app.services.leasing.tenancy import accept_renewal, decline_renewal

    renewal = db.session.get(LeaseRenewal, renewal_id)
    if renewal is None or renewal.org_id != org_id:
        abort(404)
    back = redirect(url_for("admin.lease_detail", lease_id=renewal.lease_id))
    action = (request.form.get("action") or "").strip().lower()

    try:
        if action == "accept":
            new_lease = accept_renewal(current_session(), renewal=renewal, actor_id=current_user.id)
            message = f"Renewed as {new_lease.lease_number}."
        elif action == "decline":
            decline_renewal(
                current_session(), renewal=renewal, reason=request.form.get("reason") or None
            )
            message = "Offer declined."
        else:
            flash("That is not something you can do to an offer.", "error")
            return back
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash(message, "success")
    return back


@admin_bp.post("/leases/<id:lease_id>/notice")
@login_required
def lease_give_notice(lease_id: str) -> Response:
    """Record that a resident is leaving."""
    require(Perm.LEASE_TERMINATE)
    org_id = require_org_scope()

    from app.services.leasing.tenancy import give_notice

    lease = _lease_or_404(lease_id, org_id)
    back = redirect(url_for("admin.lease_detail", lease_id=lease_id))
    form = request.form

    try:
        give_notice(
            current_session(),
            lease=lease,
            notice_date=dt.date.fromisoformat(form.get("notice_date") or ""),
            scheduled_date=dt.date.fromisoformat(form.get("scheduled_date") or ""),
            reason=(form.get("reason") or "").strip() or None,
            is_early_termination=bool(form.get("is_early_termination")),
        )
        db.session.commit()
    except (AtlasError, ValueError) as exc:
        db.session.rollback()
        flash(_said(exc, "That is not a date."), "error")
        return back

    flash("Notice recorded.", "success")
    return back


@admin_bp.get("/move-outs")
@login_required
def move_outs() -> str:
    """The disposition board.

    Overdue leads, because past that date the deductions are usually forfeit
    entirely and often with a penalty on top. It is the one number on this
    page that costs money to ignore.
    """
    require(Perm.LEASE_READ)
    org_id = require_org_scope()

    from app.models.leasing import MoveOut
    from app.services.leasing.tenancy import overdue_dispositions

    records = list(
        db.session.execute(
            select(MoveOut)
            .where(MoveOut.org_id == org_id)
            .order_by(MoveOut.scheduled_date.desc().nulls_last())
            .limit(200)
        ).scalars()
    )
    return render_template(
        "admin/move_outs.html",
        move_outs=records,
        overdue=overdue_dispositions(current_session(), org_id=org_id),
        leases={lease.id: lease for lease in _leases_by_id(org_id)},
        today=utcnow().date(),
    )


@admin_bp.get("/move-outs/<id:move_out_id>")
@login_required
def move_out_detail(move_out_id: str) -> str:
    """One move-out, its clock, and its disposition."""
    require(Perm.LEASE_READ)
    org_id = require_org_scope()

    record = _move_out_or_404(move_out_id, org_id)
    lease = db.session.get(Lease, record.lease_id)
    return render_template(
        "admin/move_out.html",
        move_out=record,
        lease=lease,
        unit=db.session.get(Unit, lease.unit_id) if lease and lease.unit_id else None,
        today=utcnow().date(),
    )


@admin_bp.post("/move-outs/<id:move_out_id>/record")
@login_required
def move_out_record(move_out_id: str) -> Response:
    """They actually left. This starts the statutory clock."""
    require(Perm.LEASE_TERMINATE)
    org_id = require_org_scope()

    from app.services.leasing.tenancy import record_move_out

    move_out = _move_out_or_404(move_out_id, org_id)
    back = redirect(url_for("admin.move_out_detail", move_out_id=move_out_id))
    form = request.form

    try:
        record_move_out(
            current_session(),
            move_out=move_out,
            actual_date=dt.date.fromisoformat(form.get("actual_date") or ""),
            forwarding_address=(
                {"raw": form["forwarding_address"].strip()}
                if (form.get("forwarding_address") or "").strip()
                else None
            ),
            disposition_days=int(form.get("disposition_days") or 21),
            inspection_id=(form.get("inspection_id") or "").strip() or None,
            actor_id=current_user.id,
        )
        db.session.commit()
    except (AtlasError, ValueError) as exc:
        db.session.rollback()
        flash(_said(exc, "That is not a date."), "error")
        return back

    flash(
        f"Move-out recorded. The disposition is due by "
        f"{move_out.disposition_due_by.isoformat() if move_out.disposition_due_by else 'unknown'}.",
        "success",
    )
    return back


@admin_bp.post("/move-outs/<id:move_out_id>/disposition")
@login_required
def move_out_disposition(move_out_id: str) -> Response:
    """Settle the deposit: what is withheld, what is returned, and why.

    Deductions come from a completed inspection where there is one. A finding
    photographed on a checklist the resident could see is the defensible kind;
    a figure typed at settlement is what a magistrate disallows.
    """
    require(Perm.DEPOSIT_RELEASE)
    org_id = require_org_scope()

    from app.models.maintenance import Inspection
    from app.services.leasing.tenancy import deductions_from_inspection, settle_deposit

    move_out = _move_out_or_404(move_out_id, org_id)
    back = redirect(url_for("admin.move_out_detail", move_out_id=move_out_id))
    form = request.form

    try:
        inspection_id = (form.get("from_inspection_id") or "").strip()
        if inspection_id:
            inspection = db.session.get(Inspection, inspection_id)
            if inspection is None or inspection.org_id != org_id:
                abort(404)
            deductions = deductions_from_inspection(current_session(), inspection_id=inspection_id)
        else:
            deductions = _parsed_deductions(form.get("deductions"))

        settle_deposit(
            current_session(),
            move_out=move_out,
            deductions=deductions,
            settled_by_id=current_user.id,
        )
        db.session.commit()
    except (AtlasError, ValueError) as exc:
        db.session.rollback()
        flash(_said(exc, "That is not an amount."), "error")
        return back

    flash(
        f"Disposition settled: {move_out.deposit_deductions} withheld, "
        f"{move_out.deposit_refunded} returned.",
        "success",
    )
    return back


def _parsed_deductions(raw: str | None) -> list:  # noqa: ANN201 - list[Deduction]
    """One deduction per line, as ``description | amount``.

    A textarea rather than a repeating form: the number of deductions is not
    known in advance, and an operator writing them out reads back what they
    withheld in one place.
    """
    from app.services.leasing.tenancy import Deduction

    parsed = []
    for line in (raw or "").splitlines():
        if not line.strip():
            continue
        description, separator, amount = line.rpartition("|")
        if not separator:
            raise ValidationFailed(
                f"{line.strip()!r} is not a deduction. Write each one as 'description | amount'."
            )
        value = _decimal_or_none(amount)
        if value is None:
            raise ValidationFailed(f"{amount.strip()!r} is not an amount.")
        parsed.append(Deduction(description=description.strip(), amount=value))
    return parsed


def _said(exc: Exception, fallback: str) -> str:
    """A service refusal reads as itself; a stdlib ValueError does not."""
    return str(exc) if isinstance(exc, AtlasError) else fallback


def _lease_or_404(lease_id: str, org_id: str) -> Lease:
    record = db.session.get(Lease, lease_id)
    if record is None or record.org_id != org_id:
        abort(404)
    return record


def _move_out_or_404(move_out_id: str, org_id: str):  # noqa: ANN202 - MoveOut
    from app.models.leasing import MoveOut

    record = db.session.get(MoveOut, move_out_id)
    if record is None or record.org_id != org_id:
        abort(404)
    return record


def _deposit_held(org_id: str, lease_id: str) -> Decimal:
    from app.services.accounting.deposits import deposit_balance

    return deposit_balance(current_session(), org_id=org_id, lease_id=lease_id)


def _units_by_id(org_id: str) -> dict[str, Unit]:
    return {
        unit.id: unit
        for unit in db.session.execute(select(Unit).where(Unit.org_id == org_id)).scalars()
    }


def _leases_by_id(org_id: str) -> list[Lease]:
    return list(db.session.execute(select(Lease).where(Lease.org_id == org_id)).scalars())


@admin_bp.get("/applications")
@login_required
def applications() -> str:
    """The leasing funnel: what has come in and what is waiting on a decision."""
    require(Perm.APPLICATION_READ)
    org_id = require_org_scope()

    from app.models.leasing import ApplicationStatus

    status = (request.args.get("status") or "").strip()
    stmt = select(Application).where(Application.org_id == org_id)
    if status in {member.value for member in ApplicationStatus}:
        stmt = stmt.where(Application.status == status)

    records = list(
        db.session.execute(stmt.order_by(Application.created_at.desc()).limit(200)).scalars()
    )
    properties = {
        record.id: record
        for record in db.session.execute(
            select(Property).where(Property.org_id == org_id)
        ).scalars()
    }
    return render_template(
        "admin/applications.html",
        applications=records,
        properties=properties,
        units=list(
            db.session.execute(
                select(Unit).where(Unit.org_id == org_id).order_by(Unit.unit_number).limit(500)
            ).scalars()
        ),
        status=status,
        statuses=[member.value for member in ApplicationStatus],
    )


@admin_bp.get("/applications/<id:application_id>")
@login_required
def application_detail(application_id: str) -> str:
    """One application, its applicants and screenings, and the assessment.

    The assessment recommends; a person decides. It is shown with its reasons
    and with what is still missing, because an application short of a document
    is a different conversation from one that fails on its merits.
    """
    require(Perm.APPLICATION_READ)
    org_id = require_org_scope()

    from app.services.leasing.applications import assess_application

    record = _application_or_404(application_id, org_id)
    return render_template(
        "admin/application.html",
        application=record,
        assessment=assess_application(current_session(), application=record),
        property=db.session.get(Property, record.property_id),
    )


@admin_bp.get("/turns")
@login_required
def turns() -> str:
    """The turn board: what is vacant, what is late, and the days it is costing.

    Days vacant is the number this page exists to move. It is shown for
    completed turns rather than estimated for open ones, because an average
    that includes turns still running flatters itself as they get worse.
    """
    require(Perm.UNIT_READ)
    org_id = require_org_scope()

    from app.services.leasing.turns import turn_board

    property_id = (request.args.get("property_id") or "").strip() or None
    board = turn_board(current_session(), org_id=org_id, property_id=property_id)

    units = {
        unit.id: unit
        for unit in db.session.execute(select(Unit).where(Unit.org_id == org_id)).scalars()
    }
    properties = list(
        db.session.execute(
            select(Property).where(Property.org_id == org_id).order_by(Property.name)
        ).scalars()
    )

    return render_template(
        "admin/turns.html",
        board=board,
        units=units,
        properties=properties,
        property_id=property_id,
        today=utcnow().date(),
    )


@admin_bp.get("/turns/<id:turn_id>")
@login_required
def turn_detail(turn_id: str) -> str:
    """One turn and its steps, in the order they are meant to happen."""
    require(Perm.UNIT_READ)
    org_id = require_org_scope()

    from app.models.leasing import Turn
    from app.services.leasing.turns import outstanding_steps

    turn = db.session.get(Turn, turn_id)
    if turn is None or turn.org_id != org_id or turn.deleted_at is not None:
        abort(404)

    return render_template(
        "admin/turn.html",
        turn=turn,
        unit=db.session.get(Unit, turn.unit_id),
        outstanding=outstanding_steps(turn),
        today=utcnow().date(),
    )


@admin_bp.get("/messages")
@login_required
def messages() -> str:
    """The office side of every conversation, internal ones included."""
    require(Perm.MESSAGE_READ)
    org_id = require_org_scope()

    from app.models.resident import MessageThread

    status = (request.args.get("status") or "").strip()
    stmt = select(MessageThread).where(
        MessageThread.org_id == org_id, MessageThread.deleted_at.is_(None)
    )
    if status in ("open", "pending", "resolved"):
        stmt = stmt.where(MessageThread.status == status)

    threads = list(
        db.session.execute(
            stmt.order_by(MessageThread.last_message_at.desc().nulls_last()).limit(200)
        ).scalars()
    )
    return render_template("admin/messages.html", threads=threads, status=status)


@admin_bp.get("/messages/<id:thread_id>")
@login_required
def message_thread(thread_id: str) -> str:
    """One conversation, with the office's reply box."""
    require(Perm.MESSAGE_READ)
    org_id = require_org_scope()

    from app.models.resident import MessageThread
    from app.services.notifications.messaging import mark_read

    thread = db.session.get(MessageThread, thread_id)
    if thread is None or thread.org_id != org_id or thread.deleted_at is not None:
        abort(404)

    mark_read(current_session(), thread=thread, reader_is_staff=True)
    db.session.commit()
    return render_template("admin/message_thread.html", thread=thread)


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

        integrity = verify_chain(current_session(), org_id=org_id)

    return render_template("admin/audit.html", events=events, integrity=integrity)


# ---------------------------------------------------------------------------
# Role administration
# ---------------------------------------------------------------------------


@admin_bp.get("/roles")
@login_required
def roles() -> str:
    """Roles, their permissions, and who holds them.

    Read-only by design. Granting a role from a screen is one click away from
    granting the wrong one to the wrong person, so the *change* goes through
    the service layer's audited path; this view exists so an administrator can
    see the current picture before deciding, and answer "who can do this?"
    without reading the database.
    """
    require(Perm.ROLE_READ)
    org_id = require_org_scope()

    role_rows = list(
        db.session.execute(
            select(Role).where(Role.org_id == org_id, Role.deleted_at.is_(None)).order_by(Role.name)
        ).scalars()
    )

    # Permissions per role, and holders per role, in two queries rather than
    # two per role: this page is small now and would degrade quietly otherwise.
    grants: dict[str, list[str]] = {}
    for role_id, code in db.session.execute(
        select(RolePermission.role_id, RolePermission.permission_code).where(
            RolePermission.org_id == org_id
        )
    ).all():
        grants.setdefault(role_id, []).append(code)

    holders: dict[str, list[tuple[str, str]]] = {}
    for role_id, name, email in db.session.execute(
        select(RoleAssignment.role_id, User.full_name, User.email)
        .join(User, User.id == RoleAssignment.user_id)
        .where(
            RoleAssignment.org_id == org_id,
            RoleAssignment.revoked_at.is_(None),
            User.deleted_at.is_(None),
        )
        .order_by(User.full_name)
    ).all():
        holders.setdefault(role_id, []).append((name, email))

    catalogue = list(
        db.session.execute(
            select(Permission).order_by(Permission.category, Permission.code)
        ).scalars()
    )

    return render_template(
        "admin/roles.html",
        roles=role_rows,
        grants={key: sorted(value) for key, value in grants.items()},
        holders=holders,
        catalogue=catalogue,
        # Every permission nobody holds, which is the question an auditor asks
        # and the one a permission matrix is bad at answering.
        unassigned=sorted(
            {permission.code for permission in catalogue}
            - {code for codes in grants.values() for code in codes}
        ),
    )


@admin_bp.get("/roles/<id:role_id>")
@login_required
def role_detail(role_id: str) -> str:
    """One role: what it grants, and who holds it."""
    require(Perm.ROLE_READ)
    org_id = require_org_scope()

    role = db.session.execute(
        select(Role).where(Role.org_id == org_id, Role.id == role_id, Role.deleted_at.is_(None))
    ).scalar_one_or_none()
    if role is None:
        abort(404)

    permissions = list(
        db.session.execute(
            select(Permission)
            .join(RolePermission, RolePermission.permission_code == Permission.code)
            .where(RolePermission.org_id == org_id, RolePermission.role_id == role.id)
            .order_by(Permission.category, Permission.code)
        ).scalars()
    )
    assignments = list(
        db.session.execute(
            select(RoleAssignment, User)
            .join(User, User.id == RoleAssignment.user_id)
            .where(
                RoleAssignment.org_id == org_id,
                RoleAssignment.role_id == role.id,
                User.deleted_at.is_(None),
            )
            .order_by(User.full_name)
        ).all()
    )

    return render_template(
        "admin/role_detail.html",
        role=role,
        permissions=permissions,
        assignments=assignments,
        # Passed rather than made a template global: one view needs it, and a
        # global "now" is the sort of thing that quietly acquires callers.
        now=utcnow(),
    )


@admin_bp.get("/users")
@login_required
def users() -> str:
    """Who has access, and through which roles.

    The reverse of the role view, and the one that actually gets used: the
    question is almost always "what can this person do?" rather than "who is in
    this role?".
    """
    require(Perm.USER_READ)
    org_id = require_org_scope()

    search = (request.args.get("q") or "").strip()
    query = select(User).where(User.org_id == org_id, User.deleted_at.is_(None))
    if search:
        pattern = f"%{search.lower()}%"
        query = query.where(
            func.lower(User.full_name).like(pattern) | func.lower(User.email).like(pattern)
        )

    people = list(db.session.execute(query.order_by(User.full_name).limit(200)).scalars())

    roles_by_user: dict[str, list[str]] = {}
    for user_id, name in db.session.execute(
        select(RoleAssignment.user_id, Role.name)
        .join(Role, Role.id == RoleAssignment.role_id)
        .where(RoleAssignment.org_id == org_id, RoleAssignment.revoked_at.is_(None))
    ).all():
        roles_by_user.setdefault(user_id, []).append(name)

    return render_template(
        "admin/users.html",
        people=people,
        roles_by_user={key: sorted(value) for key, value in roles_by_user.items()},
        search=search,
    )


def _count(stmt) -> int:  # noqa: ANN001
    return int(db.session.execute(stmt).scalar_one() or 0)


def _sum(stmt) -> Decimal:  # noqa: ANN001
    return Decimal(str(db.session.execute(stmt).scalar_one() or 0))


# ---------------------------------------------------------------------------
# Write actions
#
# Each delegates to the service and surfaces its refusal verbatim. The rules
# live there - a turn that cannot be ready, a transfer that would not total
# 100% - and the console does not restate them, because two copies of a rule is
# one copy that goes stale.
# ---------------------------------------------------------------------------


def _turn_or_404(turn_id: str, org_id: str):  # noqa: ANN202
    from app.models.leasing import Turn

    turn = db.session.get(Turn, turn_id)
    if turn is None or turn.org_id != org_id or turn.deleted_at is not None:
        abort(404)
    return turn


@admin_bp.post("/turns/<id:turn_id>/steps/<id:step_id>")
@login_required
def turn_step_action(turn_id: str, step_id: str) -> Response:
    """Complete a step, skip it with a reason, or attach the job doing it."""
    require(Perm.UNIT_MANAGE)
    org_id = require_org_scope()

    from app.services.leasing.turns import complete_step, link_work_order, skip_step

    turn = _turn_or_404(turn_id, org_id)
    step = next((candidate for candidate in turn.steps if candidate.id == step_id), None)
    if step is None:
        abort(404)

    action = (request.form.get("action") or "").strip().lower()
    back = redirect(url_for("admin.turn_detail", turn_id=turn_id))

    try:
        if action == "complete":
            complete_step(current_session(), step=step, actor_id=current_user.id)
        elif action == "skip":
            # The reason is mandatory in the service. Passing the raw field lets
            # it say so, rather than the console inventing its own message.
            skip_step(
                current_session(),
                step=step,
                reason=request.form.get("reason") or "",
                actor_id=current_user.id,
            )
        elif action == "link":
            work_order_id = (request.form.get("work_order_id") or "").strip()
            if not work_order_id:
                flash("Linking a step needs a work order.", "error")
                return back
            link_work_order(current_session(), step=step, work_order_id=work_order_id)
        else:
            flash("That is not something you can do to a step.", "error")
            return back
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash(f"{step.name} updated.", "success")
    return back


@admin_bp.post("/turns/<id:turn_id>")
@login_required
def turn_action(turn_id: str) -> Response:
    """Mark a turn ready, or cancel it."""
    require(Perm.UNIT_MANAGE)
    org_id = require_org_scope()

    from app.services.leasing.turns import cancel_turn, mark_ready

    turn = _turn_or_404(turn_id, org_id)
    action = (request.form.get("action") or "").strip().lower()
    back = redirect(url_for("admin.turn_detail", turn_id=turn_id))

    try:
        if action == "ready":
            # Deliberately no date field: a unit is ready when somebody says it
            # is, and letting the form back-date readiness is how days-vacant
            # gets flattered.
            mark_ready(current_session(), turn=turn, actor_id=current_user.id)
            message = f"Unit ready after {turn.days_vacant} days."
        elif action == "cancel":
            cancel_turn(
                current_session(),
                turn=turn,
                reason=request.form.get("reason") or "",
                actor_id=current_user.id,
            )
            message = "Turn cancelled."
        else:
            flash("That is not something you can do to a turn.", "error")
            return back
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash(message, "success")
    return back


def _application_or_404(application_id: str, org_id: str) -> Application:
    record = db.session.get(Application, application_id)
    if record is None or record.org_id != org_id:
        abort(404)
    return record


def _decimal_or_none(raw: str | None) -> Decimal | None:
    """A blank box means "not stated", which is not the same as zero.

    ``Decimal("NaN")`` and ``Decimal("Infinity")`` both parse happily, and then
    every ordered comparison downstream *raises* rather than returning False.
    Finiteness is checked here so the refusal lands on the form, not on a 500.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValidationFailed(f"{text!r} is not an amount.") from exc
    if not value.is_finite():
        raise ValidationFailed(f"{text!r} is not an amount.")
    return value


@admin_bp.post("/applications")
@login_required
def application_create() -> Response:
    """Open a draft application, the way a leasing agent takes one by phone."""
    require(Perm.APPLICATION_MANAGE)
    org_id = require_org_scope()

    from app.services.leasing.applications import create_application

    form = request.form
    try:
        move_in = (form.get("desired_move_in") or "").strip()
        record = create_application(
            current_session(),
            org_id=org_id,
            property_id=form.get("property_id") or "",
            unit_id=(form.get("unit_id") or "").strip() or None,
            desired_move_in=dt.date.fromisoformat(move_in) if move_in else None,
            lease_term_months=int(form.get("lease_term_months") or 12),
            quoted_rent=_decimal_or_none(form.get("quoted_rent")),
            application_fee=_decimal_or_none(form.get("application_fee")),
            actor_id=current_user.id,
        )
        db.session.commit()
    except (AtlasError, ValueError) as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return redirect(url_for("admin.applications"))

    flash(f"Application {record.application_number} opened.", "success")
    return redirect(url_for("admin.application_detail", application_id=record.id))


@admin_bp.post("/applications/<id:application_id>/applicants")
@login_required
def application_add_applicant(application_id: str) -> Response:
    """Add a person to the application."""
    require(Perm.APPLICATION_MANAGE)
    org_id = require_org_scope()

    from app.models.leasing import ApplicantRole
    from app.services.leasing.applications import add_applicant

    record = _application_or_404(application_id, org_id)
    back = redirect(url_for("admin.application_detail", application_id=application_id))
    form = request.form

    try:
        add_applicant(
            current_session(),
            application=record,
            first_name=(form.get("first_name") or "").strip(),
            last_name=(form.get("last_name") or "").strip(),
            role=ApplicantRole(form.get("role") or ApplicantRole.PRIMARY.value),
            email=(form.get("email") or "").strip() or None,
            phone=(form.get("phone") or "").strip() or None,
            monthly_income=_decimal_or_none(form.get("monthly_income")),
            employer_name=(form.get("employer_name") or "").strip() or None,
        )
        db.session.commit()
    except (AtlasError, ValueError) as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash("Applicant added.", "success")
    return back


@admin_bp.post("/applicants/<id:applicant_id>/consent")
@login_required
def applicant_consent(applicant_id: str) -> Response:
    """Record that this applicant consented to being screened.

    The address comes from the connection, never from the form. Consent
    evidence the submitter can dictate is not evidence, and this is the only
    moment the real one exists.
    """
    require(Perm.APPLICATION_MANAGE)
    org_id = require_org_scope()

    from app.models.leasing import Applicant
    from app.services.leasing.applications import record_consent

    applicant = db.session.get(Applicant, applicant_id)
    if applicant is None or applicant.org_id != org_id:
        abort(404)
    back = redirect(url_for("admin.application_detail", application_id=applicant.application_id))

    try:
        record_consent(
            current_session(),
            applicant=applicant,
            ip_address=request.remote_addr or "unknown",
        )
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash("Consent recorded.", "success")
    return back


@admin_bp.post("/applications/<id:application_id>/submit")
@login_required
def application_submit(application_id: str) -> Response:
    """Move a draft into the pipeline."""
    require(Perm.APPLICATION_MANAGE)
    org_id = require_org_scope()

    from app.services.leasing.applications import submit_application

    record = _application_or_404(application_id, org_id)
    back = redirect(url_for("admin.application_detail", application_id=application_id))

    try:
        submit_application(current_session(), application=record, actor_id=current_user.id)
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash("Application submitted.", "success")
    return back


@admin_bp.post("/applications/<id:application_id>/decision")
@login_required
def application_decision(application_id: str) -> Response:
    """Approve, approve with conditions, or deny - always with a reason.

    The reason is mandatory on every outcome, not only denials. Those words
    become the adverse-action notice, and an approval that needs no explanation
    is what makes the denials beside it look arbitrary.
    """
    require(Perm.APPLICATION_DECIDE)
    org_id = require_org_scope()

    from app.services.leasing.applications import approve_application, deny_application

    record = _application_or_404(application_id, org_id)
    back = redirect(url_for("admin.application_detail", application_id=application_id))
    action = (request.form.get("action") or "").strip().lower()
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("A decision needs a reason. That reason is the notice.", "error")
        return back

    try:
        if action == "deny":
            deny_application(
                current_session(),
                application=record,
                decided_by_id=current_user.id,
                reasons=[reason],
            )
            message = "Application denied."
        elif action in {"approve", "approve_with_conditions"}:
            # One condition per line. An empty box is refused rather than
            # quietly downgraded to a plain approval: "approved subject to a
            # co-signer" and "approved" are different tenancies.
            conditions = [
                line.strip()
                for line in (request.form.get("conditions") or "").splitlines()
                if line.strip()
            ]
            if action == "approve_with_conditions" and not conditions:
                flash("A conditional approval has to say what the conditions are.", "error")
                return back
            approve_application(
                current_session(),
                application=record,
                decided_by_id=current_user.id,
                conditions={"conditions": conditions} if conditions else None,
                reason=reason,
            )
            message = (
                "Application approved with conditions." if conditions else "Application approved."
            )
        else:
            flash("That is not a decision an application can take.", "error")
            return back
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash(message, "success")
    return back


@admin_bp.post("/ownership/<id:property_id>")
@login_required
def ownership_action(property_id: str) -> Response:
    """Record a stake, or transfer one."""
    require(Perm.OWNER_MANAGE)
    org_id = require_org_scope()

    from app.services.portfolio.ownership import record_initial_stake, transfer_ownership

    action = (request.form.get("action") or "").strip().lower()
    back = redirect(url_for("admin.ownership"))

    raw_date = (request.form.get("effective_from") or "").strip()
    try:
        effective_from = dt.date.fromisoformat(raw_date) if raw_date else utcnow().date()
    except ValueError:
        flash("That is not a date.", "error")
        return back

    raw_percentage = (request.form.get("percentage") or "").strip()
    percentage: Decimal | None = None
    if raw_percentage:
        try:
            percentage = Decimal(raw_percentage)
            # NaN survives construction and then raises on every comparison
            # downstream rather than failing one, so it is rejected here.
            if not percentage.is_finite():
                raise ArithmeticError("not a finite percentage")
        except (ArithmeticError, ValueError):
            flash("That is not a percentage.", "error")
            return back

    try:
        if action == "record":
            if percentage is None:
                flash("A stake needs a percentage.", "error")
                return back
            record_initial_stake(
                current_session(),
                org_id=org_id,
                property_id=property_id,
                owner_entity_id=(request.form.get("owner_entity_id") or "").strip(),
                percentage=percentage,
                effective_from=effective_from,
                is_primary_contact=bool(request.form.get("is_primary_contact")),
                actor_id=current_user.id,
            )
            message = "Stake recorded."
        elif action == "transfer":
            transfer = transfer_ownership(
                current_session(),
                org_id=org_id,
                property_id=property_id,
                from_owner_entity_id=(request.form.get("from_owner_entity_id") or "").strip(),
                to_owner_entity_id=(request.form.get("to_owner_entity_id") or "").strip(),
                effective_from=effective_from,
                # Omitted moves the seller's whole holding.
                percentage=percentage,
                reason=request.form.get("reason") or None,
                actor_id=current_user.id,
            )
            message = f"{transfer.percentage}% transferred with effect from {effective_from}."
        else:
            flash("That is not something you can do to ownership.", "error")
            return back
        db.session.commit()
    except AtlasError as exc:
        db.session.rollback()
        flash(str(exc), "error")
        return back

    flash(message, "success")
    return back

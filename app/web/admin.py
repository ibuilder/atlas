"""The operations console.

Every view enforces permission through the policy engine before rendering, and
the templates additionally hide controls the viewer cannot use. The hiding is
courtesy; the enforcement is the check.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, select
from werkzeug.wrappers import Response

from app.errors import AtlasError
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
from app.models.leasing import Lease, LeaseStatus
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
            key=lambda holding: (holding.lease.lease_number if holding.lease else holding.lease_id),
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

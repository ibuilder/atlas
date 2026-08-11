"""The report catalogue.

A report is a code, a column list, and a function that returns rows. Keeping
them in a registry rather than scattered across blueprints means one place
answers "what can this system produce?", and a scheduled report can name a code
without the scheduler knowing anything about rent.

Builders take the session and validated parameters and return plain
dictionaries. They read; they never write. A report that mutates while it runs
cannot be re-run to check a figure, and checking a figure is what reports are
for.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import NotFound, ValidationFailed

__all__ = [
    "REPORTS",
    "ReportDefinition",
    "known_reports",
    "report_definition",
]


@dataclass(frozen=True)
class ReportDefinition:
    code: str
    name: str
    description: str
    columns: list[str]
    build: Callable[..., list[dict[str, Any]]]
    #: Parameters the report understands, with defaults.
    parameters: dict[str, Any] = field(default_factory=dict)


REPORTS: dict[str, ReportDefinition] = {}


def register(definition: ReportDefinition) -> ReportDefinition:
    if definition.code in REPORTS:  # pragma: no cover - registration is static
        raise RuntimeError(f"Report {definition.code!r} is already registered.")
    REPORTS[definition.code] = definition
    return definition


def known_reports() -> list[str]:
    return sorted(REPORTS)


def report_definition(code: str) -> ReportDefinition:
    definition = REPORTS.get(code)
    if definition is None:
        raise NotFound(f"No report with code {code!r}. Available: {', '.join(known_reports())}.")
    return definition


def _as_of(parameters: dict[str, Any]) -> dt.date:
    raw = parameters.get("as_of")
    if raw is None:
        from app.models.types import utcnow

        return utcnow().date()
    if isinstance(raw, dt.date):
        return raw
    try:
        return dt.date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ValidationFailed(f"'as_of' is not a date: {raw!r}.") from exc


# ---------------------------------------------------------------------------
# Rent roll
# ---------------------------------------------------------------------------


def _rent_roll(session: Session, *, org_id: str, parameters: dict[str, Any]) -> list[dict]:
    from app.models.leasing import Lease, LeaseStatus
    from app.models.org import Property, Unit

    as_of = _as_of(parameters)
    rows: list[dict[str, Any]] = []

    leases = (
        session.execute(
            select(Lease)
            .where(
                Lease.org_id == org_id,
                Lease.status.in_([LeaseStatus.ACTIVE, LeaseStatus.HOLDOVER]),
                Lease.start_date <= as_of,
                Lease.deleted_at.is_(None),
            )
            .order_by(Lease.start_date)
        )
        .scalars()
        .all()
    )

    for lease in leases:
        if lease.end_date is not None and lease.end_date < as_of:
            continue
        unit = session.get(Unit, lease.unit_id) if lease.unit_id else None
        prop = session.get(Property, lease.property_id) if lease.property_id else None
        rows.append(
            {
                "property": prop.name if prop else "",
                "unit": unit.unit_number if unit else "",
                "lease_number": lease.lease_number,
                "status": str(lease.status),
                "start_date": lease.start_date.isoformat() if lease.start_date else "",
                "end_date": lease.end_date.isoformat() if lease.end_date else "open",
                "rent": lease.rent_amount,
                "deposit": lease.security_deposit,
            }
        )
    return rows


register(
    ReportDefinition(
        code="rent_roll",
        name="Rent roll",
        description="Every occupied unit with its lease terms as at a date.",
        columns=[
            "property",
            "unit",
            "lease_number",
            "status",
            "start_date",
            "end_date",
            "rent",
            "deposit",
        ],
        build=_rent_roll,
        parameters={"as_of": None},
    )
)


# ---------------------------------------------------------------------------
# Trial balance
# ---------------------------------------------------------------------------


def _trial_balance(session: Session, *, org_id: str, parameters: dict[str, Any]) -> list[dict]:
    from app.models.accounting import Account, JournalEntry, JournalLine

    as_of = _as_of(parameters)
    totals = session.execute(
        select(
            JournalLine.account_id,
            func.sum(JournalLine.debit),
            func.sum(JournalLine.credit),
        )
        .join(JournalEntry, JournalEntry.id == JournalLine.journal_entry_id)
        .where(
            JournalLine.org_id == org_id,
            JournalEntry.entry_date <= as_of,
        )
        .group_by(JournalLine.account_id)
    ).all()

    rows: list[dict[str, Any]] = []
    for account_id, debit, credit in totals:
        account = session.get(Account, account_id)
        if account is None:
            continue
        rows.append(
            {
                "code": account.code,
                "account": account.name,
                "type": str(account.account_type),
                "debit": Decimal(debit or 0),
                "credit": Decimal(credit or 0),
            }
        )
    rows.sort(key=lambda row: row["code"])
    return rows


register(
    ReportDefinition(
        code="trial_balance",
        name="Trial balance",
        description="Debits and credits by account. The two totals must agree.",
        columns=["code", "account", "type", "debit", "credit"],
        build=_trial_balance,
        parameters={"as_of": None},
    )
)


# ---------------------------------------------------------------------------
# Delinquency
# ---------------------------------------------------------------------------


def _delinquency(session: Session, *, org_id: str, parameters: dict[str, Any]) -> list[dict]:
    from app.models.accounting import ZERO, Invoice, InvoiceStatus
    from app.models.leasing import Lease

    as_of = _as_of(parameters)
    invoices = (
        session.execute(
            select(Invoice)
            .where(
                Invoice.org_id == org_id,
                Invoice.status.in_([InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID]),
                Invoice.balance > ZERO,
                Invoice.due_date < as_of,
            )
            .order_by(Invoice.due_date)
        )
        .scalars()
        .all()
    )

    rows: list[dict[str, Any]] = []
    for invoice in invoices:
        lease = session.get(Lease, invoice.lease_id) if invoice.lease_id else None
        days = (as_of - invoice.due_date).days
        rows.append(
            {
                "invoice": invoice.invoice_number,
                "lease": lease.lease_number if lease else "",
                "due_date": invoice.due_date.isoformat(),
                "days_overdue": days,
                "bucket": _ageing_bucket(days),
                "balance": invoice.balance,
                "stage": invoice.delinquency_stage,
            }
        )
    return rows


def _ageing_bucket(days: int) -> str:
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


register(
    ReportDefinition(
        code="delinquency",
        name="Delinquency and ageing",
        description="Overdue balances by ageing bucket and escalation stage.",
        columns=["invoice", "lease", "due_date", "days_overdue", "bucket", "balance", "stage"],
        build=_delinquency,
        parameters={"as_of": None},
    )
)


# ---------------------------------------------------------------------------
# Maintenance SLA
# ---------------------------------------------------------------------------


def _work_order_sla(session: Session, *, org_id: str, parameters: dict[str, Any]) -> list[dict]:
    from app.models.maintenance import WorkOrder, WorkOrderStatus

    orders = (
        session.execute(
            select(WorkOrder)
            .where(
                WorkOrder.org_id == org_id,
                WorkOrder.status.not_in([WorkOrderStatus.CANCELLED]),
                WorkOrder.deleted_at.is_(None),
            )
            .order_by(WorkOrder.resolution_due_at)
        )
        .scalars()
        .all()
    )
    return [
        {
            "work_order": order.work_order_number,
            "title": order.title,
            "priority": str(order.priority),
            "status": str(order.status),
            "due": order.resolution_due_at.isoformat() if order.resolution_due_at else "",
            "breached": order.sla_breached_at is not None,
            "vendor_id": order.vendor_id or "",
            "total_cost": order.total_cost,
        }
        for order in orders
    ]


register(
    ReportDefinition(
        code="work_order_sla",
        name="Work-order SLA",
        description="Open and completed work with its resolution target and breach state.",
        columns=[
            "work_order",
            "title",
            "priority",
            "status",
            "due",
            "breached",
            "vendor_id",
            "total_cost",
        ],
        build=_work_order_sla,
    )
)


# ---------------------------------------------------------------------------
# Vendor compliance
# ---------------------------------------------------------------------------


def _vendor_compliance(session: Session, *, org_id: str, parameters: dict[str, Any]) -> list[dict]:
    from app.models.vendor import Vendor

    as_of = _as_of(parameters)
    vendors = (
        session.execute(
            select(Vendor)
            .where(Vendor.org_id == org_id, Vendor.deleted_at.is_(None))
            .order_by(Vendor.name)
        )
        .scalars()
        .all()
    )
    return [
        {
            "vendor": vendor.name,
            "status": str(vendor.status),
            "compliance": str(vendor.compliance_status),
            "compliance_expires": (
                vendor.compliance_expires_at.isoformat() if vendor.compliance_expires_at else ""
            ),
            "dispatchable": vendor.is_dispatchable,
            "expired": bool(vendor.compliance_expires_at and vendor.compliance_expires_at < as_of),
            "is_1099": vendor.is_1099_reportable,
        }
        for vendor in vendors
    ]


register(
    ReportDefinition(
        code="vendor_compliance",
        name="Vendor compliance",
        description="Every vendor with insurance standing as at a date.",
        columns=[
            "vendor",
            "status",
            "compliance",
            "compliance_expires",
            "dispatchable",
            "expired",
            "is_1099",
        ],
        build=_vendor_compliance,
        parameters={"as_of": None},
    )
)


# ---------------------------------------------------------------------------
# Capital plan
# ---------------------------------------------------------------------------


def _capital_plan(session: Session, *, org_id: str, parameters: dict[str, Any]) -> list[dict]:
    from decimal import Decimal as _Decimal

    from app.services.assets.capital import DEFAULT_INFLATION, plan_as_rows, plan_capital

    horizon = int(parameters.get("horizon_years") or 5)
    raw_rate = parameters.get("inflation")
    inflation = _Decimal(str(raw_rate)) if raw_rate is not None else DEFAULT_INFLATION

    plan = plan_capital(
        session,
        org_id=org_id,
        property_id=parameters.get("property_id"),
        horizon_years=horizon,
        inflation=inflation,
        as_of=_as_of(parameters) if parameters.get("as_of") else None,
    )
    return plan_as_rows(plan)


register(
    ReportDefinition(
        code="capital_plan",
        name="Capital plan",
        description=(
            "Predicted asset replacements by year, inflated forward. The 'why' column "
            "states what drove each date, and 'confidence' says where the forecast is "
            "measured rather than merely estimated."
        ),
        columns=[
            "year",
            "asset",
            "name",
            "category",
            "criticality",
            "replace_on",
            "base_cost",
            "forecast_cost",
            "confidence",
            "why",
        ],
        build=_capital_plan,
        parameters={"horizon_years": 5, "inflation": None, "property_id": None, "as_of": None},
    )
)


# ---------------------------------------------------------------------------
# Year-end and trust
# ---------------------------------------------------------------------------


def _tax_1099(session: Session, *, org_id: str, parameters: dict[str, Any]) -> list[dict]:
    from app.services.accounting.tax import generate_1099_report, tax_report_rows

    year = int(parameters.get("year") or (_as_of(parameters).year - 1))
    return tax_report_rows(generate_1099_report(session, org_id=org_id, year=year))


register(
    ReportDefinition(
        code="tax_1099",
        name="1099 year-end totals",
        description=(
            "What each vendor was paid in a calendar year, on a cash basis. The status "
            "column separates what can be filed from what is over the threshold and "
            "blocked - a run that silently omits a vendor is the expensive kind."
        ),
        columns=[
            "vendor",
            "legal_name",
            "tin_last4",
            "payments",
            "total_paid",
            "status",
            "backup_withholding",
            "blockers",
        ],
        build=_tax_1099,
        parameters={"year": None},
    )
)


def _trust_position(session: Session, *, org_id: str, parameters: dict[str, Any]) -> list[dict]:
    from app.models.accounting import BankAccount
    from app.services.accounting.trust import reconcile_trust, trust_position_rows

    rows: list[dict] = []
    accounts = session.execute(
        select(BankAccount).where(
            BankAccount.org_id == org_id,
            BankAccount.is_trust.is_(True),
            BankAccount.deleted_at.is_(None),
        )
    ).scalars()

    for account in accounts:
        position = reconcile_trust(
            session,
            org_id=org_id,
            bank_account_id=account.id,
            as_of=_as_of(parameters),
        )
        for row in trust_position_rows(position):
            rows.append({"account": account.name, **row})
        for exception in position.exceptions:
            rows.append({"account": account.name, "lease": "", "held": None, "status": exception})
    return rows


register(
    ReportDefinition(
        code="trust_position",
        name="Trust three-way position",
        description=(
            "Bank against book against what every beneficiary is owed. The third leg "
            "is the one that catches a shortfall while the first two agree."
        ),
        columns=["account", "lease", "held", "status"],
        build=_trust_position,
        parameters={"as_of": None},
    )
)

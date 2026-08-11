"""Year-end tax reporting: 1099-NEC and 1099-MISC.

Filing these wrong is expensive in a specific way — the penalty is per form and
it compounds with lateness, so a run that quietly omits three vendors costs
more than one that loudly refuses. This module is therefore built to *report
what it cannot file* as prominently as what it can.

Three things decide whether a vendor belongs on a return, and all three are
checked rather than assumed:

* **Payments in the calendar year, on a cash basis.** Not what was billed —
  what was actually paid, and dated by when it left the bank. A bill accrued in
  December and paid in January belongs to January's year.
* **The threshold.** $600 for both forms as of the 2020 reform that split
  non-employee compensation out of MISC into NEC. Below it, no return is due.
* **A taxpayer identification number.** A vendor over the threshold with no TIN
  cannot be filed, and the correct response is backup withholding and a W-9
  request — not silently dropping them from the run.

The output is deliberately a dataset rather than an IRS file. FIRE-format
transmission is a filing-agent concern with its own certification, and
generating a fixed-width file nobody validated is how a return gets rejected in
February.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ValidationFailed
from app.logging import get_logger
from app.models.accounting import Bill, BillPayment
from app.models.audit import AuditAction, AuditSeverity
from app.models.types import quantize_money, utcnow
from app.models.vendor import Vendor
from app.services.audit.recorder import record_audit_event

__all__ = [
    "NEC_THRESHOLD",
    "TaxYearReport",
    "VendorTaxTotal",
    "generate_1099_report",
    "tax_report_rows",
]

log = get_logger("services.accounting.tax")

ZERO = Decimal("0")

#: The reporting threshold for both 1099-NEC and 1099-MISC. A constant with a
#: date attached, because it has moved before and will move again.
NEC_THRESHOLD = Decimal("600.00")

#: Backup withholding rate where a payee has not furnished a TIN.
BACKUP_WITHHOLDING_RATE = Decimal("0.24")


@dataclass
class VendorTaxTotal:
    """One vendor's year, and whether it can actually be filed."""

    vendor: Vendor
    total_paid: Decimal
    payment_count: int
    #: Reportable is a decision about the *vendor*; filable is about the data.
    is_reportable: bool
    blockers: list[str] = field(default_factory=list)

    @property
    def is_filable(self) -> bool:
        return self.is_reportable and not self.blockers

    @property
    def backup_withholding_due(self) -> Decimal:
        """What should have been withheld where no TIN was furnished."""
        if not self.is_reportable or self.vendor.tax_id:
            return ZERO
        return quantize_money(self.total_paid * BACKUP_WITHHOLDING_RATE)


@dataclass
class TaxYearReport:
    year: int
    totals: list[VendorTaxTotal] = field(default_factory=list)

    @property
    def filable(self) -> list[VendorTaxTotal]:
        return [total for total in self.totals if total.is_filable]

    @property
    def blocked(self) -> list[VendorTaxTotal]:
        """Over the threshold and cannot be filed. The list that matters."""
        return [total for total in self.totals if total.is_reportable and total.blockers]

    @property
    def below_threshold(self) -> list[VendorTaxTotal]:
        return [total for total in self.totals if not total.is_reportable]

    @property
    def total_reportable(self) -> Decimal:
        return quantize_money(sum((t.total_paid for t in self.filable), ZERO))

    @property
    def is_clean(self) -> bool:
        return not self.blocked


def generate_1099_report(
    session: Session,
    *,
    org_id: str,
    year: int,
    threshold: Decimal = NEC_THRESHOLD,
    actor_id: str | None = None,
) -> TaxYearReport:
    """Total what each vendor was *paid* in a calendar year.

    Cash basis, deliberately: the IRS wants what left the bank in the year, and
    an accrual total would report a December bill paid in January against the
    wrong return.
    """
    if year < 1990 or year > utcnow().year + 1:
        raise ValidationFailed(f"{year} is not a plausible tax year.")
    if threshold < ZERO:
        raise ValidationFailed("A reporting threshold cannot be negative.")

    start = dt.date(year, 1, 1)
    end = dt.date(year, 12, 31)

    rows = session.execute(
        select(BillPayment, Bill)
        .join(Bill, Bill.id == BillPayment.bill_id)
        .where(
            BillPayment.org_id == org_id,
            BillPayment.paid_date >= start,
            BillPayment.paid_date <= end,
        )
    ).all()

    paid: dict[str, list[Decimal]] = {}
    for payment, bill in rows:
        paid.setdefault(bill.vendor_id, []).append(payment.amount)

    vendors = {
        vendor.id: vendor
        for vendor in session.execute(
            select(Vendor).where(Vendor.org_id == org_id, Vendor.deleted_at.is_(None))
        ).scalars()
    }

    report = TaxYearReport(year=year)
    for vendor_id, amounts in paid.items():
        vendor = vendors.get(vendor_id)
        if vendor is None:  # pragma: no cover - a payment to a deleted vendor
            continue

        total = quantize_money(sum(amounts, ZERO))
        reportable = vendor.is_1099_reportable and total >= threshold

        blockers: list[str] = []
        if reportable:
            # Each of these stops a return being filed, and each is fixable -
            # which is why they are reported rather than silently dropped.
            if not vendor.tax_id:
                blockers.append(
                    "No taxpayer identification number on file. Request a W-9; "
                    f"backup withholding of {BACKUP_WITHHOLDING_RATE:.0%} applies "
                    "until one is furnished."
                )
            if not vendor.legal_name:
                blockers.append("No legal name on file. A trading name cannot be filed.")
            if not _has_address(vendor):
                blockers.append("No mailing address on file. The payee copy cannot be sent.")

        report.totals.append(
            VendorTaxTotal(
                vendor=vendor,
                total_paid=total,
                payment_count=len(amounts),
                is_reportable=reportable,
                blockers=blockers,
            )
        )

    report.totals.sort(key=lambda total: -total.total_paid)

    if report.blocked:
        log.warning(
            "1099 run has vendors over the threshold that cannot be filed",
            extra={
                "event": "tax.1099_blocked",
                "year": year,
                "blocked": len(report.blocked),
                "filable": len(report.filable),
            },
        )

    record_audit_event(
        action=AuditAction.DATA_EXPORTED,
        resource_type="TaxReport",
        resource_label=f"1099-{year}",
        severity=AuditSeverity.NOTICE if report.is_clean else AuditSeverity.WARNING,
        payload={
            "year": year,
            "threshold": str(threshold),
            "filable": len(report.filable),
            "blocked": len(report.blocked),
            "below_threshold": len(report.below_threshold),
            "total_reportable": str(report.total_reportable),
        },
        reason=(
            f"1099 totals generated for {year}."
            if report.is_clean
            else f"1099 totals generated for {year} with {len(report.blocked)} "
            "vendor(s) that cannot be filed."
        ),
        org_id=org_id,
        actor_id=actor_id,
        session=session,
    )
    return report


def _has_address(vendor: Vendor) -> bool:
    return bool(
        getattr(vendor, "address_line1", None)
        and getattr(vendor, "city", None)
        and getattr(vendor, "postal_code", None)
    )


def tax_report_rows(report: TaxYearReport) -> list[dict]:
    """Flatten for a report or an export to a filing agent.

    The TIN is *not* included: a spreadsheet of taxpayer identification numbers
    is a breach waiting for somebody to email it. The last four are enough to
    reconcile against, and a filing agent takes the full number over a channel
    built for it.
    """
    return [
        {
            "vendor": total.vendor.name,
            "legal_name": total.vendor.legal_name or "",
            "tin_last4": total.vendor.tax_id_last4 or "",
            "payments": total.payment_count,
            "total_paid": total.total_paid,
            "status": (
                "filable"
                if total.is_filable
                else ("blocked" if total.is_reportable else "below threshold")
            ),
            "backup_withholding": total.backup_withholding_due,
            "blockers": "; ".join(total.blockers),
        }
        for total in report.totals
    ]

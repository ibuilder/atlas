"""Accounting: chart of accounts, double-entry ledger, AR, AP, banking, close.

Accounting is treated as a first-class system, not a reporting side-effect of
operations. Three rules shape everything here:

**Double entry is enforced, not assumed.** Every journal entry stores its own
debit and credit totals, and a database ``CHECK`` refuses to let a *posted* entry
exist with those totals unequal. A flush-time listener independently recomputes
both totals from the lines, so the denormalised figures cannot drift from the
rows they summarise. Two mechanisms, because the ledger being wrong is not a bug
you find in a test - it is one you find in an audit.

**Posted entries are immutable.** Corrections are reversals plus a new entry.
Mutating history is how a reconciled month silently stops reconciling.

**Trust money is structurally separate.** Trust bank accounts carry their own
flag, their own GL control account, and a constraint that keeps operating
disbursements out of them. In most jurisdictions commingling is not an
accounting error, it is a licensing matter.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.models.base import SoftDeleteMixin, TenantModel
from app.models.types import GUID, EncryptedText, JSONType, Money, UTCDateTime, enum_column, utcnow

if TYPE_CHECKING:
    from app.models.vendor import Vendor

__all__ = [
    "Account",
    "AccountType",
    "BankAccount",
    "BankAccountType",
    "BankTransaction",
    "Bill",
    "BillLine",
    "BillPayment",
    "BillStatus",
    "ChargeCode",
    "FiscalPeriod",
    "Invoice",
    "InvoiceLine",
    "InvoiceStatus",
    "JournalEntry",
    "JournalLine",
    "JournalStatus",
    "LedgerImbalanceError",
    "NormalBalance",
    "OwnerDistribution",
    "OwnerStatement",
    "Payment",
    "PaymentApplication",
    "PaymentMethod",
    "PaymentStatus",
    "PeriodStatus",
    "Reconciliation",
    "ReconciliationException",
    "ReconciliationStatus",
    "ZERO",
]

ZERO = Decimal("0")


class LedgerImbalanceError(RuntimeError):
    """Raised when a posted journal entry does not balance."""


class AccountType(StrEnum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class NormalBalance(StrEnum):
    DEBIT = "debit"
    CREDIT = "credit"


#: Which side increases each account type. Used to derive presentation sign and
#: to validate that a hand-built chart of accounts is internally consistent.
NORMAL_BALANCE_BY_TYPE: dict[AccountType, NormalBalance] = {
    AccountType.ASSET: NormalBalance.DEBIT,
    AccountType.EXPENSE: NormalBalance.DEBIT,
    AccountType.LIABILITY: NormalBalance.CREDIT,
    AccountType.EQUITY: NormalBalance.CREDIT,
    AccountType.REVENUE: NormalBalance.CREDIT,
}


class JournalStatus(StrEnum):
    DRAFT = "draft"
    POSTED = "posted"
    REVERSED = "reversed"
    VOID = "void"


class PeriodStatus(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    #: Permanently sealed - reopening requires a platform-level override.
    LOCKED = "locked"


class InvoiceStatus(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    VOID = "void"
    WRITTEN_OFF = "written_off"


class PaymentMethod(StrEnum):
    ACH = "ach"
    CARD = "card"
    CHECK = "check"
    CASH = "cash"
    MONEY_ORDER = "money_order"
    WIRE = "wire"
    CREDIT = "credit"
    OTHER = "other"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SETTLED = "settled"
    FAILED = "failed"
    RETURNED = "returned"  # NSF / chargeback
    REFUNDED = "refunded"
    VOID = "void"


class BillStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    VOID = "void"


class BankAccountType(StrEnum):
    OPERATING = "operating"
    TRUST = "trust"
    SECURITY_DEPOSIT = "security_deposit"
    RESERVE = "reserve"
    PAYROLL = "payroll"


class ReconciliationStatus(StrEnum):
    DRAFT = "draft"
    IN_REVIEW = "in_review"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class DistributionStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    ISSUED = "issued"
    CLEARED = "cleared"
    VOID = "void"


# ---------------------------------------------------------------------------
# Chart of accounts
# ---------------------------------------------------------------------------


class Account(TenantModel, SoftDeleteMixin):
    """A general ledger account."""

    __tablename__ = "accounts"
    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_accounts_org_code"),
        Index("ix_accounts_org_type", "org_id", "account_type"),
        Index("ix_accounts_org_created", "org_id", "created_at"),
    )

    code: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    account_type: Mapped[AccountType] = mapped_column(enum_column(AccountType), nullable=False)
    normal_balance: Mapped[NormalBalance] = mapped_column(
        enum_column(NormalBalance), nullable=False
    )
    subtype: Mapped[str | None] = mapped_column(String(60))
    parent_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("accounts.id", ondelete="RESTRICT"), index=True
    )

    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: System accounts are referenced by code from posting rules and cannot be
    #: renumbered or deleted without breaking those rules.
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Control accounts are posted to only by the system (AR, AP, trust
    #: liability); manual journal entries against them are refused.
    is_control: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_bank: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_trust: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cash_flow_category: Mapped[str | None] = mapped_column(String(40))
    tax_line: Mapped[str | None] = mapped_column(String(60))

    parent: Mapped[Account | None] = relationship(remote_side="Account.id")

    @property
    def is_debit_normal(self) -> bool:
        return self.normal_balance == NormalBalance.DEBIT

    def signed_amount(self, debit: Decimal, credit: Decimal) -> Decimal:
        """Amount in the account's natural direction."""
        return (debit - credit) if self.is_debit_normal else (credit - debit)


class ChargeCode(TenantModel, SoftDeleteMixin):
    """A billable item template - rent, parking, pet fee, late fee, utility."""

    __tablename__ = "charge_codes"
    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_charge_codes_org_code"),
        Index("ix_charge_codes_org_created", "org_id", "created_at"),
    )

    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    gl_account_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    default_amount: Mapped[Decimal | None] = mapped_column(Money)
    is_recurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_taxable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Late fees and deposits get special handling in collections and at move-out.
    category: Mapped[str] = mapped_column(String(40), nullable=False, default="rent")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    gl_account: Mapped[Account] = relationship()


class FiscalPeriod(TenantModel):
    """An accounting period and its close state."""

    __tablename__ = "fiscal_periods"
    __table_args__ = (
        UniqueConstraint("org_id", "fiscal_year", "period_number", name="uq_fiscal_periods_org_ym"),
        CheckConstraint("period_number BETWEEN 1 AND 13", name="period_number_range"),
        CheckConstraint("end_date >= start_date", name="period_date_order"),
        Index("ix_fiscal_periods_org_dates", "org_id", "start_date", "end_date"),
        Index("ix_fiscal_periods_org_created", "org_id", "created_at"),
    )

    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 13 allows for an adjustment period after the final month.
    period_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(40), nullable=False)
    start_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    end_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    status: Mapped[PeriodStatus] = mapped_column(
        enum_column(PeriodStatus), nullable=False, default=PeriodStatus.OPEN, index=True
    )

    closed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    closed_by_id: Mapped[str | None] = mapped_column(GUID)
    reopened_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    reopened_by_id: Mapped[str | None] = mapped_column(GUID)
    reopen_reason: Mapped[str | None] = mapped_column(Text)
    #: Immutable record of the checklist state at the moment of close.
    close_checklist: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    @property
    def accepts_postings(self) -> bool:
        return self.status in (PeriodStatus.OPEN, PeriodStatus.CLOSING)

    def contains(self, on_date: dt.date) -> bool:
        return self.start_date <= on_date <= self.end_date


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class JournalEntry(TenantModel):
    """A balanced set of debits and credits, posted as one atomic fact."""

    __tablename__ = "journal_entries"
    __table_args__ = (
        UniqueConstraint("org_id", "entry_number", name="uq_journal_entries_org_number"),
        # The invariant, at the storage layer: a posted entry that does not
        # balance cannot exist as a row.
        CheckConstraint(
            "status <> 'posted' OR total_debit = total_credit",
            name="posted_entries_balance",
        ),
        CheckConstraint("total_debit >= 0 AND total_credit >= 0", name="totals_non_negative"),
        Index("ix_journal_entries_org_date", "org_id", "entry_date"),
        Index("ix_journal_entries_source", "org_id", "source_type", "source_id"),
        Index("ix_journal_entries_org_status", "org_id", "status"),
        Index("ix_journal_entries_org_created", "org_id", "created_at"),
    )

    entry_number: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    entry_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    period_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("fiscal_periods.id", ondelete="RESTRICT"), index=True
    )
    property_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="RESTRICT"), index=True
    )

    description: Mapped[str] = mapped_column(String(255), nullable=False)
    memo: Mapped[str | None] = mapped_column(Text)
    status: Mapped[JournalStatus] = mapped_column(
        enum_column(JournalStatus), nullable=False, default=JournalStatus.DRAFT, index=True
    )

    #: What produced this entry: ``invoice``, ``payment``, ``bill``, ``manual``.
    #: The link back from ledger impact to the operational event is what makes
    #: an audit tractable.
    source_type: Mapped[str | None] = mapped_column(String(40), index=True)
    source_id: Mapped[str | None] = mapped_column(GUID, index=True)

    total_debit: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    total_credit: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)

    posted_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    posted_by_id: Mapped[str | None] = mapped_column(GUID)

    is_adjusting: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Populated on the *correcting* entry, pointing at what it reverses.
    reverses_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("journal_entries.id", ondelete="RESTRICT"), index=True
    )
    #: Populated on the *original* entry once a reversal exists.
    reversed_by_id: Mapped[str | None] = mapped_column(GUID, index=True)
    reversal_reason: Mapped[str | None] = mapped_column(Text)

    lines: Mapped[list[JournalLine]] = relationship(
        back_populates="entry",
        cascade="all, delete-orphan",
        order_by="JournalLine.line_number",
        passive_deletes=True,
    )
    reverses: Mapped[JournalEntry | None] = relationship(remote_side="JournalEntry.id")

    @property
    def is_posted(self) -> bool:
        return self.status in (JournalStatus.POSTED, JournalStatus.REVERSED)

    @property
    def is_balanced(self) -> bool:
        debit, credit = self.computed_totals()
        return debit == credit

    def computed_totals(self) -> tuple[Decimal, Decimal]:
        debit = sum((line.debit for line in self.lines), ZERO)
        credit = sum((line.credit for line in self.lines), ZERO)
        return Decimal(debit), Decimal(credit)

    def refresh_totals(self) -> None:
        self.total_debit, self.total_credit = self.computed_totals()


class JournalLine(TenantModel):
    """One side of one account movement.

    A line carries exactly one of debit or credit. Allowing both on the same
    line makes ``SUM(debit) - SUM(credit)`` reporting correct but every other
    query ambiguous, and it hides sign errors that a single-sided line surfaces
    immediately.
    """

    __tablename__ = "journal_lines"
    __table_args__ = (
        CheckConstraint("debit >= 0 AND credit >= 0", name="amounts_non_negative"),
        CheckConstraint("NOT (debit > 0 AND credit > 0)", name="single_sided"),
        CheckConstraint("debit > 0 OR credit > 0", name="non_zero"),
        UniqueConstraint("journal_entry_id", "line_number", name="uq_journal_lines_entry_line"),
        Index("ix_journal_lines_account_date", "account_id", "created_at"),
        Index("ix_journal_lines_property", "org_id", "property_id"),
        Index("ix_journal_lines_org_created", "org_id", "created_at"),
    )

    journal_entry_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("journal_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    debit: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    credit: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    memo: Mapped[str | None] = mapped_column(String(255))

    # Segments. Every reporting dimension the business slices by lives on the
    # line, so a report never has to guess which property an amount belonged to.
    property_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="RESTRICT"), index=True
    )
    unit_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("units.id", ondelete="RESTRICT"), index=True
    )
    lease_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("leases.id", ondelete="RESTRICT"), index=True
    )
    owner_entity_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("owner_entities.id", ondelete="RESTRICT"), index=True
    )
    vendor_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="RESTRICT"), index=True
    )
    bank_account_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("bank_accounts.id", ondelete="RESTRICT"), index=True
    )

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship()

    @property
    def amount(self) -> Decimal:
        return self.debit if self.debit > ZERO else self.credit

    @property
    def side(self) -> NormalBalance:
        return NormalBalance.DEBIT if self.debit > ZERO else NormalBalance.CREDIT


# ---------------------------------------------------------------------------
# Banking
# ---------------------------------------------------------------------------


class BankAccount(TenantModel, SoftDeleteMixin):
    """A real-world bank account, mirrored by a GL account."""

    __tablename__ = "bank_accounts"
    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_bank_accounts_org_code"),
        CheckConstraint(
            "account_type <> 'trust' OR is_trust = true",
            name="trust_type_implies_trust_flag",
        ),
        Index("ix_bank_accounts_org_created", "org_id", "created_at"),
    )

    code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    account_type: Mapped[BankAccountType] = mapped_column(
        enum_column(BankAccountType), nullable=False, default=BankAccountType.OPERATING
    )
    #: Trust accounts hold money that belongs to residents or owners. Structural
    #: separation is a licensing requirement in most jurisdictions, not a
    #: preference.
    is_trust: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    gl_account_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    property_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="RESTRICT"), index=True
    )

    institution_name: Mapped[str | None] = mapped_column(String(150))
    #: Encrypted; the last four are stored separately for display and matching.
    account_number: Mapped[str | None] = mapped_column(EncryptedText)
    account_number_last4: Mapped[str | None] = mapped_column(String(4))
    routing_number: Mapped[str | None] = mapped_column(EncryptedText)

    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    opening_balance: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    opening_date: Mapped[dt.date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Changing bank details is a classic fraud vector; every change requires a
    #: second approver and is audited as sensitive.
    last_verified_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    gl_account: Mapped[Account] = relationship()


class BankTransaction(TenantModel):
    """A line imported from a bank statement or feed."""

    __tablename__ = "bank_transactions"
    __table_args__ = (
        UniqueConstraint(
            "bank_account_id", "external_id", name="uq_bank_transactions_account_external"
        ),
        Index("ix_bank_transactions_account_date", "bank_account_id", "posted_date"),
        Index("ix_bank_transactions_status", "org_id", "match_status"),
        Index("ix_bank_transactions_org_created", "org_id", "created_at"),
    )

    bank_account_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("bank_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    posted_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    #: Signed: positive is money in, negative is money out. One signed column
    #: beats a separate direction flag that can disagree with the amount.
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(80))
    #: Bank-supplied identifier; the unique constraint above makes re-importing
    #: an overlapping statement window idempotent instead of duplicating.
    external_id: Mapped[str | None] = mapped_column(String(120))

    match_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unmatched")
    matched_journal_line_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("journal_lines.id", ondelete="SET NULL"), index=True
    )
    reconciliation_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("reconciliations.id", ondelete="SET NULL"), index=True
    )
    match_confidence: Mapped[int | None] = mapped_column(Integer)
    import_batch_id: Mapped[str | None] = mapped_column(GUID, index=True)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)


class Reconciliation(TenantModel):
    """A bank reconciliation session."""

    __tablename__ = "reconciliations"
    __table_args__ = (
        UniqueConstraint(
            "bank_account_id", "statement_end", name="uq_reconciliations_account_period"
        ),
        Index("ix_reconciliations_org_status", "org_id", "status"),
        Index("ix_reconciliations_org_created", "org_id", "created_at"),
    )

    bank_account_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("bank_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    statement_start: Mapped[dt.date] = mapped_column(Date, nullable=False)
    statement_end: Mapped[dt.date] = mapped_column(Date, nullable=False)
    statement_opening_balance: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    statement_closing_balance: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)

    #: Ledger balance computed at completion, retained so a historical
    #: reconciliation can be re-examined without recomputing from a ledger that
    #: has moved on.
    ledger_balance: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    difference: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)

    status: Mapped[ReconciliationStatus] = mapped_column(
        enum_column(ReconciliationStatus), nullable=False, default=ReconciliationStatus.DRAFT
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    completed_by_id: Mapped[str | None] = mapped_column(GUID)
    notes: Mapped[str | None] = mapped_column(Text)

    exceptions: Mapped[list[ReconciliationException]] = relationship(
        back_populates="reconciliation", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_balanced(self) -> bool:
        return self.difference == ZERO


class ReconciliationException(TenantModel):
    """An item that prevented a clean reconciliation."""

    __tablename__ = "reconciliation_exceptions"
    __table_args__ = (Index("ix_reconciliation_exceptions_org_created", "org_id", "created_at"),)

    reconciliation_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("reconciliations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Money)
    bank_transaction_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("bank_transactions.id", ondelete="SET NULL"), index=True
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    resolved_by_id: Mapped[str | None] = mapped_column(GUID)
    resolution_note: Mapped[str | None] = mapped_column(Text)

    reconciliation: Mapped[Reconciliation] = relationship(back_populates="exceptions")


# ---------------------------------------------------------------------------
# Accounts receivable
# ---------------------------------------------------------------------------


class Invoice(TenantModel):
    """A receivable raised against a lease or an owner."""

    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("org_id", "invoice_number", name="uq_invoices_org_number"),
        CheckConstraint("total >= 0", name="total_non_negative"),
        CheckConstraint("balance >= 0", name="balance_non_negative"),
        CheckConstraint("balance <= total", name="balance_within_total"),
        CheckConstraint("due_date >= issue_date", name="due_after_issue"),
        Index("ix_invoices_org_status_due", "org_id", "status", "due_date"),
        Index("ix_invoices_lease", "org_id", "lease_id"),
        Index("ix_invoices_org_created", "org_id", "created_at"),
    )

    invoice_number: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    lease_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("leases.id", ondelete="RESTRICT"), index=True
    )
    resident_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("residents.id", ondelete="RESTRICT"), index=True
    )
    owner_entity_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("owner_entities.id", ondelete="RESTRICT"), index=True
    )
    property_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="RESTRICT"), index=True
    )
    unit_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("units.id", ondelete="RESTRICT"), index=True
    )

    issue_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    due_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    period_start: Mapped[dt.date | None] = mapped_column(Date)
    period_end: Mapped[dt.date | None] = mapped_column(Date)

    status: Mapped[InvoiceStatus] = mapped_column(
        enum_column(InvoiceStatus), nullable=False, default=InvoiceStatus.DRAFT, index=True
    )
    subtotal: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    tax_total: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    total: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    #: Maintained by payment application, never computed on read: a resident's
    #: balance is queried far more often than it changes.
    balance: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")

    memo: Mapped[str | None] = mapped_column(Text)
    journal_entry_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("journal_entries.id", ondelete="RESTRICT"), index=True
    )
    voided_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    void_reason: Mapped[str | None] = mapped_column(String(255))
    #: Delinquency stage reached, used by the collections workflow.
    delinquency_stage: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    lines: Mapped[list[InvoiceLine]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        order_by="InvoiceLine.line_number",
        passive_deletes=True,
    )
    applications: Mapped[list[PaymentApplication]] = relationship(
        back_populates="invoice", passive_deletes=True
    )

    @property
    def amount_paid(self) -> Decimal:
        return self.total - self.balance

    @property
    def is_open(self) -> bool:
        return self.status in (InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID)

    def days_overdue(self, as_of: dt.date | None = None) -> int:
        if not self.is_open:
            return 0
        reference = as_of or dt.date.today()
        return max(0, (reference - self.due_date).days)


class InvoiceLine(TenantModel):
    __tablename__ = "invoice_lines"
    __table_args__ = (
        UniqueConstraint("invoice_id", "line_number", name="uq_invoice_lines_invoice_line"),
        Index("ix_invoice_lines_org_created", "org_id", "created_at"),
    )

    invoice_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    charge_code_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("charge_codes.id", ondelete="RESTRICT"), index=True
    )
    account_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("1"))
    unit_amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    tax_amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    service_period_start: Mapped[dt.date | None] = mapped_column(Date)
    service_period_end: Mapped[dt.date | None] = mapped_column(Date)

    invoice: Mapped[Invoice] = relationship(back_populates="lines")


class Payment(TenantModel):
    """Money received."""

    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("org_id", "payment_number", name="uq_payments_org_number"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("unapplied_amount >= 0", name="unapplied_non_negative"),
        CheckConstraint("unapplied_amount <= amount", name="unapplied_within_amount"),
        Index("ix_payments_org_status_date", "org_id", "status", "received_date"),
        Index("ix_payments_org_created", "org_id", "created_at"),
    )

    payment_number: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    received_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    #: Decremented as the payment is applied to invoices. A resident who
    #: overpays keeps a credit here rather than an orphaned transaction.
    unapplied_amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    fee_amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)

    method: Mapped[PaymentMethod] = mapped_column(enum_column(PaymentMethod), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        enum_column(PaymentStatus), nullable=False, default=PaymentStatus.PENDING, index=True
    )

    lease_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("leases.id", ondelete="RESTRICT"), index=True
    )
    resident_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("residents.id", ondelete="RESTRICT"), index=True
    )
    owner_entity_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("owner_entities.id", ondelete="RESTRICT"), index=True
    )
    property_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="RESTRICT"), index=True
    )
    bank_account_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("bank_accounts.id", ondelete="RESTRICT"), index=True
    )

    reference: Mapped[str | None] = mapped_column(String(80))
    #: Processor identifier. Unique per organization so a webhook replay cannot
    #: create a second payment.
    external_id: Mapped[str | None] = mapped_column(String(120), index=True)
    journal_entry_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("journal_entries.id", ondelete="RESTRICT"), index=True
    )
    settled_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    returned_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    return_reason: Mapped[str | None] = mapped_column(String(120))
    memo: Mapped[str | None] = mapped_column(Text)

    applications: Mapped[list[PaymentApplication]] = relationship(
        back_populates="payment", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def applied_amount(self) -> Decimal:
        return self.amount - self.unapplied_amount


class PaymentApplication(TenantModel):
    """Allocation of part of a payment to one invoice."""

    __tablename__ = "payment_applications"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        UniqueConstraint("payment_id", "invoice_id", name="uq_payment_applications_pair"),
        Index("ix_payment_applications_org_created", "org_id", "created_at"),
    )

    payment_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("payments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("invoices.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    applied_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    applied_by_id: Mapped[str | None] = mapped_column(GUID)
    reversed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    payment: Mapped[Payment] = relationship(back_populates="applications")
    invoice: Mapped[Invoice] = relationship(back_populates="applications")


# ---------------------------------------------------------------------------
# Accounts payable
# ---------------------------------------------------------------------------


class Bill(TenantModel):
    """A payable owed to a vendor."""

    __tablename__ = "bills"
    __table_args__ = (
        UniqueConstraint("org_id", "bill_number", name="uq_bills_org_number"),
        UniqueConstraint("vendor_id", "vendor_invoice_number", name="uq_bills_vendor_invoice"),
        CheckConstraint("total >= 0", name="total_non_negative"),
        CheckConstraint("balance >= 0 AND balance <= total", name="balance_within_total"),
        Index("ix_bills_org_status_due", "org_id", "status", "due_date"),
        Index("ix_bills_org_created", "org_id", "created_at"),
    )

    bill_number: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    vendor_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: The vendor's own invoice number. Unique per vendor, which is what stops
    #: the same paper invoice being entered - and paid - twice.
    vendor_invoice_number: Mapped[str | None] = mapped_column(String(80))

    bill_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    due_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    status: Mapped[BillStatus] = mapped_column(
        enum_column(BillStatus), nullable=False, default=BillStatus.DRAFT, index=True
    )

    property_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="RESTRICT"), index=True
    )
    work_order_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("work_orders.id", ondelete="SET NULL"), index=True
    )

    subtotal: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    tax_total: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    total: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    balance: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)

    approved_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    approved_by_id: Mapped[str | None] = mapped_column(GUID)
    journal_entry_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("journal_entries.id", ondelete="RESTRICT"), index=True
    )
    memo: Mapped[str | None] = mapped_column(Text)
    is_1099_reportable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    lines: Mapped[list[BillLine]] = relationship(
        back_populates="bill",
        cascade="all, delete-orphan",
        order_by="BillLine.line_number",
        passive_deletes=True,
    )
    vendor: Mapped[Vendor] = relationship()


class BillLine(TenantModel):
    __tablename__ = "bill_lines"
    __table_args__ = (
        UniqueConstraint("bill_id", "line_number", name="uq_bill_lines_bill_line"),
        Index("ix_bill_lines_org_created", "org_id", "created_at"),
    )

    bill_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("bills.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    account_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    property_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="RESTRICT"), index=True
    )
    unit_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("units.id", ondelete="RESTRICT"), index=True
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("1"))
    unit_amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    #: Whether this cost passes through to the owner's statement.
    is_owner_billable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    bill: Mapped[Bill] = relationship(back_populates="lines")


class BillPayment(TenantModel):
    """A disbursement against one or more bills."""

    __tablename__ = "bill_payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_bill_payments_org_created", "org_id", "created_at"),
    )

    bill_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("bills.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    bank_account_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("bank_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    paid_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    method: Mapped[PaymentMethod] = mapped_column(
        enum_column(PaymentMethod), nullable=False, default=PaymentMethod.CHECK
    )
    check_number: Mapped[str | None] = mapped_column(String(30))
    journal_entry_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("journal_entries.id", ondelete="RESTRICT"), index=True
    )
    voided_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    void_reason: Mapped[str | None] = mapped_column(String(255))


# ---------------------------------------------------------------------------
# Owner reporting
# ---------------------------------------------------------------------------


class OwnerStatement(TenantModel):
    """A period statement for one owner and one property."""

    __tablename__ = "owner_statements"
    __table_args__ = (
        UniqueConstraint(
            "owner_entity_id", "property_id", "period_end", name="uq_owner_statements_period"
        ),
        Index("ix_owner_statements_org_period", "org_id", "period_end"),
        Index("ix_owner_statements_org_created", "org_id", "created_at"),
    )

    statement_number: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    owner_entity_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("owner_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    property_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period_start: Mapped[dt.date] = mapped_column(Date, nullable=False)
    period_end: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)

    opening_balance: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    total_income: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    total_expense: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    management_fee: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    net_income: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    distribution_amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    closing_balance: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)
    #: The owner's share on the statement date, snapshotted because ownership
    #: can change after a statement is issued.
    ownership_percentage: Mapped[Decimal] = mapped_column(Money, nullable=False, default=ZERO)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    generated_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    document_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("documents.id", ondelete="SET NULL"), index=True
    )
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)


class OwnerDistribution(TenantModel):
    """A payment of funds out to an owner."""

    __tablename__ = "owner_distributions"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        Index("ix_owner_distributions_org_date", "org_id", "distribution_date"),
        Index("ix_owner_distributions_org_created", "org_id", "created_at"),
    )

    distribution_number: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    owner_entity_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("owner_entities.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    property_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="RESTRICT"), index=True
    )
    statement_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("owner_statements.id", ondelete="SET NULL"), index=True
    )
    bank_account_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("bank_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    distribution_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    method: Mapped[PaymentMethod] = mapped_column(
        enum_column(PaymentMethod), nullable=False, default=PaymentMethod.ACH
    )
    status: Mapped[DistributionStatus] = mapped_column(
        enum_column(DistributionStatus), nullable=False, default=DistributionStatus.DRAFT
    )
    reference: Mapped[str | None] = mapped_column(String(80))
    journal_entry_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("journal_entries.id", ondelete="RESTRICT"), index=True
    )
    approved_by_id: Mapped[str | None] = mapped_column(GUID)
    approved_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)


# ---------------------------------------------------------------------------
# Ledger invariants
# ---------------------------------------------------------------------------


@event.listens_for(Session, "before_flush")
def _enforce_ledger_invariants(session: Session, flush_context: Any, instances: Any) -> None:
    """Recompute and verify journal totals before anything reaches the database.

    Runs for both new and modified entries. The database ``CHECK`` catches an
    unbalanced *stored total*; this catches stored totals that disagree with the
    lines they claim to summarise - the failure mode a denormalised column
    always eventually has.
    """
    candidates: set[JournalEntry] = set()

    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, JournalEntry):
            candidates.add(obj)
        elif isinstance(obj, JournalLine) and obj.entry is not None:
            candidates.add(obj.entry)

    for entry in candidates:
        if entry.status in (JournalStatus.DRAFT, JournalStatus.VOID):
            entry.refresh_totals()
            continue

        debit, credit = entry.computed_totals()
        if debit != credit:
            raise LedgerImbalanceError(
                f"Journal entry {entry.entry_number or entry.id} does not balance: "
                f"debits {debit} != credits {credit}."
            )
        if debit == ZERO:
            raise LedgerImbalanceError(
                f"Journal entry {entry.entry_number or entry.id} has no lines to post."
            )
        entry.total_debit = debit
        entry.total_credit = credit


@event.listens_for(Session, "before_flush")
def _enforce_posted_entry_immutability(
    session: Session, flush_context: Any, instances: Any
) -> None:
    """Refuse edits to a posted entry beyond the reversal bookkeeping fields.

    Change detection uses the attribute history on the in-session instance, not
    a re-read of the row: inside ``before_flush`` a re-read returns the same
    identity-mapped (already-mutated) object, so it would compare a value to
    itself and never fire.
    """
    from sqlalchemy import inspect as sa_inspect

    mutable_after_posting = {
        "reversed_by_id",
        "reversal_reason",
        "status",
        "updated_at",
        "updated_by_id",
    }

    for obj in session.dirty:
        if not isinstance(obj, JournalEntry) or not session.is_modified(obj):
            continue
        # Status is read from the *loaded* value, so an entry being posted for
        # the first time (draft -> posted) is allowed through.
        status_history = sa_inspect(obj).attrs.status.history
        previous_status = status_history.deleted[0] if status_history.deleted else obj.status
        changed = {
            attr.key
            for attr in sa_inspect(obj).attrs
            if attr.history.has_changes() and attr.key not in mutable_after_posting
        }
        if changed and previous_status in (JournalStatus.POSTED, JournalStatus.REVERSED):
            raise LedgerImbalanceError(
                f"Posted journal entry {obj.entry_number} is immutable; "
                f"attempted to change {sorted(changed)}. Post a reversal instead."
            )

    for obj in session.deleted:
        if isinstance(obj, JournalEntry) and obj.status in (
            JournalStatus.POSTED,
            JournalStatus.REVERSED,
        ):
            raise LedgerImbalanceError(
                f"Posted journal entry {obj.entry_number} cannot be deleted; post a reversal."
            )

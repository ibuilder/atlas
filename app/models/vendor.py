"""Vendors and vendor compliance.

Compliance is modelled as dated *documents with expiry*, not as booleans on the
vendor. "Insurance: yes" is worthless three months after the certificate lapsed,
and an expired certificate of insurance on an active work order is exactly the
exposure this table exists to prevent. The denormalised
:attr:`Vendor.compliance_expires_at` is a cache for dispatch-time filtering; the
compliance rows remain the source of truth.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import SoftDeleteMixin, TenantModel
from app.models.types import GUID, EncryptedText, JSONType, Money, UTCDateTime, enum_column, utcnow

__all__ = [
    "ComplianceKind",
    "ComplianceStatus",
    "Vendor",
    "VendorCompliance",
    "VendorStatus",
    "VendorTrade",
    "VendorType",
]


class VendorType(StrEnum):
    CONTRACTOR = "contractor"
    SUPPLIER = "supplier"
    UTILITY = "utility"
    PROFESSIONAL = "professional"
    MANAGEMENT = "management"
    OTHER = "other"


class VendorStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    INACTIVE = "inactive"
    BLOCKED = "blocked"


class ComplianceKind(StrEnum):
    CERTIFICATE_OF_INSURANCE = "certificate_of_insurance"
    W9 = "w9"
    LICENSE = "license"
    CONTRACT = "contract"
    BACKGROUND_CHECK = "background_check"
    WORKERS_COMP = "workers_comp"
    BOND = "bond"


class ComplianceStatus(StrEnum):
    MISSING = "missing"
    PENDING_REVIEW = "pending_review"
    VALID = "valid"
    EXPIRING = "expiring"
    EXPIRED = "expired"
    REJECTED = "rejected"


class Vendor(TenantModel, SoftDeleteMixin):
    """A third party that performs work or supplies goods."""

    __tablename__ = "vendors"
    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_vendors_org_code"),
        Index("ix_vendors_org_status", "org_id", "status"),
        Index("ix_vendors_compliance_expiry", "org_id", "compliance_expires_at"),
        Index("ix_vendors_org_created", "org_id", "created_at"),
    )

    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    legal_name: Mapped[str | None] = mapped_column(String(255))
    vendor_type: Mapped[VendorType] = mapped_column(
        enum_column(VendorType), nullable=False, default=VendorType.CONTRACTOR
    )
    status: Mapped[VendorStatus] = mapped_column(
        enum_column(VendorStatus), nullable=False, default=VendorStatus.PENDING, index=True
    )

    email: Mapped[str | None] = mapped_column(String(320), index=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    after_hours_phone: Mapped[str | None] = mapped_column(String(40))
    website: Mapped[str | None] = mapped_column(String(255))
    contact_name: Mapped[str | None] = mapped_column(String(150))

    address_line1: Mapped[str | None] = mapped_column(String(255))
    address_line2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    region: Mapped[str | None] = mapped_column(String(120))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="US")

    tax_id: Mapped[str | None] = mapped_column(EncryptedText)
    tax_id_last4: Mapped[str | None] = mapped_column(String(4))
    is_1099_reportable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    payment_terms_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    default_expense_account_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )

    #: Dispatch controls
    is_preferred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Vendors approved for emergency dispatch outside business hours.
    accepts_emergency: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    hourly_rate: Mapped[Decimal | None] = mapped_column(Money)
    #: Work above this value requires an approved purchase order.
    not_to_exceed_amount: Mapped[Decimal | None] = mapped_column(Money)
    service_areas: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    completed_work_orders: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Cached worst-case expiry across compliance documents, so dispatch can
    #: filter without a join. Recomputed whenever compliance changes.
    compliance_status: Mapped[ComplianceStatus] = mapped_column(
        enum_column(ComplianceStatus), nullable=False, default=ComplianceStatus.MISSING, index=True
    )
    compliance_expires_at: Mapped[dt.date | None] = mapped_column(Date)

    notes: Mapped[str | None] = mapped_column(Text)
    portal_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    trades: Mapped[list[VendorTrade]] = relationship(
        back_populates="vendor", cascade="all, delete-orphan", passive_deletes=True
    )
    compliance_records: Mapped[list[VendorCompliance]] = relationship(
        back_populates="vendor", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_dispatchable(self) -> bool:
        """Whether this vendor may be assigned new work right now."""
        return self.status == VendorStatus.ACTIVE and self.compliance_status in (
            ComplianceStatus.VALID,
            ComplianceStatus.EXPIRING,
        )

    @property
    def trade_codes(self) -> set[str]:
        return {trade.trade for trade in self.trades}


class VendorTrade(TenantModel):
    """A trade this vendor performs.

    A table rather than a JSON array because dispatch routing filters on it, and
    "find me an active, insured plumber who covers this postcode" should be an
    index scan.
    """

    __tablename__ = "vendor_trades"
    __table_args__ = (
        UniqueConstraint("vendor_id", "trade", name="uq_vendor_trades_vendor_trade"),
        Index("ix_vendor_trades_org_trade", "org_id", "trade"),
        Index("ix_vendor_trades_org_created", "org_id", "created_at"),
    )

    vendor_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trade: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    vendor: Mapped[Vendor] = relationship(back_populates="trades")


class VendorCompliance(TenantModel):
    """One compliance artefact with a validity window."""

    __tablename__ = "vendor_compliances"
    __table_args__ = (
        Index("ix_vendor_compliances_expiry", "org_id", "expires_at"),
        Index("ix_vendor_compliances_vendor_kind", "vendor_id", "kind"),
        Index("ix_vendor_compliances_org_created", "org_id", "created_at"),
    )

    vendor_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[ComplianceKind] = mapped_column(enum_column(ComplianceKind), nullable=False)
    status: Mapped[ComplianceStatus] = mapped_column(
        enum_column(ComplianceStatus), nullable=False, default=ComplianceStatus.PENDING_REVIEW
    )

    document_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("documents.id", ondelete="SET NULL"), index=True
    )
    issued_on: Mapped[dt.date | None] = mapped_column(Date)
    expires_at: Mapped[dt.date | None] = mapped_column(Date, index=True)

    carrier_name: Mapped[str | None] = mapped_column(String(150))
    policy_number: Mapped[str | None] = mapped_column(String(80))
    coverage_amount: Mapped[Decimal | None] = mapped_column(Money)
    license_number: Mapped[str | None] = mapped_column(String(80))
    jurisdiction: Mapped[str | None] = mapped_column(String(80))

    verified_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    verified_by_id: Mapped[str | None] = mapped_column(GUID)
    rejection_reason: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)

    vendor: Mapped[Vendor] = relationship(back_populates="compliance_records")

    def evaluate_status(self, warn_within_days: int = 30) -> ComplianceStatus:
        """Derive current status from the expiry date."""
        if self.status in (ComplianceStatus.REJECTED, ComplianceStatus.PENDING_REVIEW):
            return self.status
        if self.expires_at is None:
            return ComplianceStatus.VALID
        today = utcnow().date()
        if self.expires_at < today:
            return ComplianceStatus.EXPIRED
        if (self.expires_at - today).days <= warn_within_days:
            return ComplianceStatus.EXPIRING
        return ComplianceStatus.VALID

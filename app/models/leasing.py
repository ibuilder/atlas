"""Leasing: leads, applications, screening, leases, renewals, move-outs.

The pipeline runs lead -> application -> screening -> decision -> lease ->
renewal or move-out. Each stage is a row rather than a status on the previous
one, because each has its own timestamps, its own actors, and its own retention
rules - and because a denied application must remain reviewable long after the
lead that produced it has been archived.

Screening data gets particular care: it is the most regulated information Atlas
touches. Raw provider responses are never stored verbatim; only the decision
factors that a fair-housing audit would require.

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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import SoftDeleteMixin, TenantModel
from app.models.types import GUID, EncryptedText, JSONType, Money, UTCDateTime, enum_column, utcnow

if TYPE_CHECKING:
    from app.models.org import Unit
    from app.models.resident import Tenancy

__all__ = [
    "Applicant",
    "ApplicantRole",
    "Application",
    "ApplicationStatus",
    "ChargeFrequency",
    "Lead",
    "LeadStatus",
    "Lease",
    "LeaseCharge",
    "LeaseRenewal",
    "LeaseStatus",
    "MoveOut",
    "ScreeningRecommendation",
    "ScreeningResult",
    "ScreeningStatus",
]


class LeadStatus(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    TOUR_SCHEDULED = "tour_scheduled"
    TOURED = "toured"
    APPLIED = "applied"
    CONVERTED = "converted"
    LOST = "lost"
    DISQUALIFIED = "disqualified"


class ApplicationStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    SCREENING = "screening"
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    CONDITIONALLY_APPROVED = "conditionally_approved"
    DENIED = "denied"
    WITHDRAWN = "withdrawn"
    EXPIRED = "expired"
    CONVERTED = "converted"


class ApplicantRole(StrEnum):
    PRIMARY = "primary"
    CO_APPLICANT = "co_applicant"
    GUARANTOR = "guarantor"
    OCCUPANT = "occupant"


class ScreeningStatus(StrEnum):
    NOT_STARTED = "not_started"
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class ScreeningRecommendation(StrEnum):
    APPROVE = "approve"
    APPROVE_WITH_CONDITIONS = "approve_with_conditions"
    DECLINE = "decline"
    REVIEW = "review"


class LeaseStatus(StrEnum):
    DRAFT = "draft"
    PENDING_SIGNATURE = "pending_signature"
    EXECUTED = "executed"
    ACTIVE = "active"
    HOLDOVER = "holdover"
    EXPIRED = "expired"
    TERMINATED = "terminated"
    RENEWED = "renewed"
    CANCELLED = "cancelled"


class ChargeFrequency(StrEnum):
    ONE_TIME = "one_time"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


class Lead(TenantModel, SoftDeleteMixin):
    """An inbound prospect."""

    __tablename__ = "leads"
    __table_args__ = (
        Index("ix_leads_org_status", "org_id", "status"),
        Index("ix_leads_org_created", "org_id", "created_at"),
        Index("ix_leads_assigned", "org_id", "assigned_to_id", "status"),
    )

    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    phone: Mapped[str | None] = mapped_column(String(40), index=True)

    source: Mapped[str] = mapped_column(String(60), nullable=False, default="direct")
    source_detail: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[LeadStatus] = mapped_column(
        enum_column(LeadStatus), nullable=False, default=LeadStatus.NEW, index=True
    )

    property_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="SET NULL"), index=True
    )
    unit_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("units.id", ondelete="SET NULL"), index=True
    )

    desired_move_in: Mapped[dt.date | None] = mapped_column(Date)
    desired_bedrooms: Mapped[int | None] = mapped_column(Integer)
    budget_max: Mapped[Decimal | None] = mapped_column(Money)

    assigned_to_id: Mapped[str | None] = mapped_column(GUID, index=True)
    first_contacted_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    last_contacted_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    tour_scheduled_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    converted_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    lost_reason: Mapped[str | None] = mapped_column(String(120))

    notes: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part)

    def hours_to_first_contact(self) -> float | None:
        """Speed-to-lead, the single strongest predictor of conversion."""
        if self.first_contacted_at is None:
            return None
        return (self.first_contacted_at - self.created_at).total_seconds() / 3600


class Application(TenantModel, SoftDeleteMixin):
    """A rental application for a specific unit."""

    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("org_id", "application_number", name="uq_applications_org_number"),
        Index("ix_applications_org_status", "org_id", "status"),
        Index("ix_applications_unit", "org_id", "unit_id", "status"),
        Index("ix_applications_org_created", "org_id", "created_at"),
    )

    application_number: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    lead_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("leads.id", ondelete="SET NULL"), index=True
    )
    property_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    unit_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("units.id", ondelete="SET NULL"), index=True
    )

    status: Mapped[ApplicationStatus] = mapped_column(
        enum_column(ApplicationStatus), nullable=False, default=ApplicationStatus.DRAFT, index=True
    )
    desired_move_in: Mapped[dt.date | None] = mapped_column(Date)
    lease_term_months: Mapped[int | None] = mapped_column(Integer)
    quoted_rent: Mapped[Decimal | None] = mapped_column(Money)

    submitted_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    #: Applications expire so stale screening data is never used for a decision.
    expires_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    decided_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    decided_by_id: Mapped[str | None] = mapped_column(GUID)
    #: Required for every adverse decision. Fair-housing defensibility depends
    #: on a recorded, consistent reason - not on someone's memory.
    decision_reason: Mapped[str | None] = mapped_column(Text)
    decision_conditions: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict
    )

    application_fee: Mapped[Decimal | None] = mapped_column(Money)
    fee_paid_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    holding_deposit: Mapped[Decimal | None] = mapped_column(Money)

    #: Snapshot of the approval matrix in force when the decision was made, so a
    #: later policy change does not rewrite the past.
    approval_policy_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    lease_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("leases.id", ondelete="SET NULL"), index=True
    )

    applicants: Mapped[list[Applicant]] = relationship(
        back_populates="application", cascade="all, delete-orphan", passive_deletes=True
    )
    screenings: Mapped[list[ScreeningResult]] = relationship(
        back_populates="application", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_decided(self) -> bool:
        return self.status in (
            ApplicationStatus.APPROVED,
            ApplicationStatus.CONDITIONALLY_APPROVED,
            ApplicationStatus.DENIED,
        )

    @property
    def total_monthly_income(self) -> Decimal:
        return sum(
            (a.monthly_income or Decimal("0") for a in self.applicants),
            Decimal("0"),
        )

    def income_to_rent_ratio(self) -> Decimal | None:
        if not self.quoted_rent or self.quoted_rent <= 0:
            return None
        return self.total_monthly_income / self.quoted_rent


class Applicant(TenantModel):
    """One person on an application."""

    __tablename__ = "applicants"
    __table_args__ = (Index("ix_applicants_org_created", "org_id", "created_at"),)

    application_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resident_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("residents.id", ondelete="SET NULL"), index=True
    )
    role: Mapped[ApplicantRole] = mapped_column(
        enum_column(ApplicantRole), nullable=False, default=ApplicantRole.PRIMARY
    )

    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(40))
    date_of_birth: Mapped[str | None] = mapped_column(EncryptedText)
    #: Encrypted and never returned by the API. Held only for the duration the
    #: screening provider needs it.
    ssn: Mapped[str | None] = mapped_column(EncryptedText)

    monthly_income: Mapped[Decimal | None] = mapped_column(Money)
    employer_name: Mapped[str | None] = mapped_column(String(150))
    employment_start: Mapped[dt.date | None] = mapped_column(Date)
    current_address: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    #: Explicit, timestamped consent to run a background check.
    screening_consent_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    screening_consent_ip: Mapped[str | None] = mapped_column(String(45))

    application: Mapped[Application] = relationship(back_populates="applicants")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class ScreeningResult(TenantModel):
    """The outcome of a background or credit check.

    Deliberately stores *decision factors*, not the provider's raw report. A
    consumer report retained indefinitely in an operational database is a
    liability with no operational benefit; the factors below are what an
    adverse-action notice and a fair-housing review actually need.
    """

    __tablename__ = "screening_results"
    __table_args__ = (Index("ix_screening_results_org_created", "org_id", "created_at"),)

    application_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    applicant_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("applicants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    provider_reference: Mapped[str | None] = mapped_column(String(120), index=True)
    status: Mapped[ScreeningStatus] = mapped_column(
        enum_column(ScreeningStatus), nullable=False, default=ScreeningStatus.NOT_STARTED
    )
    recommendation: Mapped[ScreeningRecommendation | None] = mapped_column(
        enum_column(ScreeningRecommendation)
    )

    credit_score: Mapped[int | None] = mapped_column(Integer)
    has_eviction_history: Mapped[bool | None] = mapped_column(Boolean)
    has_criminal_record: Mapped[bool | None] = mapped_column(Boolean)
    income_verified: Mapped[bool | None] = mapped_column(Boolean)
    verified_monthly_income: Mapped[Decimal | None] = mapped_column(Money)
    #: Structured, human-readable reasons - the basis of an adverse action notice.
    factors: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)

    requested_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    #: Results go stale; a decision must not rest on a year-old report.
    expires_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    error_message: Mapped[str | None] = mapped_column(String(255))

    application: Mapped[Application] = relationship(back_populates="screenings")

    @property
    def is_usable(self) -> bool:
        if self.status != ScreeningStatus.COMPLETED:
            return False
        return self.expires_at is None or self.expires_at > utcnow()


class Lease(TenantModel, SoftDeleteMixin):
    """A contract to occupy a unit for a term."""

    __tablename__ = "leases"
    __table_args__ = (
        UniqueConstraint("org_id", "lease_number", name="uq_leases_org_number"),
        CheckConstraint("end_date >= start_date", name="lease_date_order"),
        CheckConstraint("rent_amount >= 0", name="rent_non_negative"),
        CheckConstraint("billing_day BETWEEN 1 AND 28", name="billing_day_range"),
        Index("ix_leases_org_status", "org_id", "status"),
        Index("ix_leases_unit_dates", "unit_id", "start_date", "end_date"),
        Index("ix_leases_end_date", "org_id", "end_date"),
        Index("ix_leases_org_created", "org_id", "created_at"),
    )

    lease_number: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    property_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    unit_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("units.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    status: Mapped[LeaseStatus] = mapped_column(
        enum_column(LeaseStatus), nullable=False, default=LeaseStatus.DRAFT, index=True
    )
    start_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    # Covered by the composite (org_id, end_date) index above, which is what the
    # renewal and expiry sweeps actually query.
    end_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    move_in_date: Mapped[dt.date | None] = mapped_column(Date)
    move_out_date: Mapped[dt.date | None] = mapped_column(Date)

    rent_amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    #: Day of month rent is charged. Capped at 28 so every month behaves the same.
    billing_day: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    security_deposit: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    #: Deposit actually collected, which is not always what the lease specifies.
    deposit_held: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))

    #: Grace period before a late fee applies. Jurisdiction-specific, so it is
    #: configured per lease rather than assumed globally.
    late_fee_grace_days: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    late_fee_amount: Mapped[Decimal | None] = mapped_column(Money)
    notice_period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    auto_renew: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    executed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    esign_envelope_id: Mapped[str | None] = mapped_column(String(120), index=True)
    terminated_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    termination_reason: Mapped[str | None] = mapped_column(String(255))

    #: Renewal chain, so a tenancy's full history is walkable in both directions.
    previous_lease_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("leases.id", ondelete="SET NULL"), index=True
    )
    terms: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    unit: Mapped[Unit] = relationship(back_populates="leases")
    tenancies: Mapped[list[Tenancy]] = relationship(
        back_populates="lease", cascade="all, delete-orphan", passive_deletes=True
    )
    charges: Mapped[list[LeaseCharge]] = relationship(
        back_populates="lease", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_active(self) -> bool:
        return self.status in (LeaseStatus.ACTIVE, LeaseStatus.HOLDOVER)

    @property
    def term_months(self) -> int:
        months = (self.end_date.year - self.start_date.year) * 12
        return months + (self.end_date.month - self.start_date.month)

    def days_until_expiry(self, as_of: dt.date | None = None) -> int:
        return (self.end_date - (as_of or dt.date.today())).days

    def is_in_renewal_window(self, window_days: int = 90, as_of: dt.date | None = None) -> bool:
        remaining = self.days_until_expiry(as_of)
        return 0 <= remaining <= window_days


class LeaseCharge(TenantModel):
    """A recurring or one-time charge attached to a lease."""

    __tablename__ = "lease_charges"
    __table_args__ = (
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        Index("ix_lease_charges_lease_active", "lease_id", "is_active"),
        Index("ix_lease_charges_org_created", "org_id", "created_at"),
    )

    lease_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("leases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    charge_code_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("charge_codes.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Money, nullable=False)
    frequency: Mapped[ChargeFrequency] = mapped_column(
        enum_column(ChargeFrequency), nullable=False, default=ChargeFrequency.MONTHLY
    )
    start_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    end_date: Mapped[dt.date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: First and last months are charged pro rata when the term does not align
    #: to the billing cycle.
    prorate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Last cycle already billed, so re-running the billing job is idempotent.
    last_billed_through: Mapped[dt.date | None] = mapped_column(Date)

    lease: Mapped[Lease] = relationship(back_populates="charges")

    def applies_on(self, on_date: dt.date) -> bool:
        if not self.is_active or on_date < self.start_date:
            return False
        return self.end_date is None or on_date <= self.end_date


class LeaseRenewal(TenantModel):
    """A renewal offer and its outcome."""

    __tablename__ = "lease_renewals"
    __table_args__ = (
        Index("ix_lease_renewals_org_status", "org_id", "status"),
        Index("ix_lease_renewals_org_created", "org_id", "created_at"),
    )

    lease_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("leases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft", index=True)

    offered_rent: Mapped[Decimal] = mapped_column(Money, nullable=False)
    offered_term_months: Mapped[int] = mapped_column(Integer, nullable=False, default=12)
    proposed_start: Mapped[dt.date] = mapped_column(Date, nullable=False)
    proposed_end: Mapped[dt.date] = mapped_column(Date, nullable=False)

    offer_sent_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    offer_expires_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    responded_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    response: Mapped[str | None] = mapped_column(String(20))
    declined_reason: Mapped[str | None] = mapped_column(String(255))
    new_lease_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("leases.id", ondelete="SET NULL"), index=True
    )
    notice_document_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("documents.id", ondelete="SET NULL"), index=True
    )

    @property
    def rent_increase(self) -> Decimal | None:
        lease = self.lease if hasattr(self, "lease") else None
        return None if lease is None else self.offered_rent - lease.rent_amount


class MoveOut(TenantModel):
    """Move-out, including deposit disposition.

    Deposit accounting is statutory in most jurisdictions - a deadline, an
    itemised statement, and interest in some states - so each step carries its
    own timestamp rather than a single "done" flag.
    """

    __tablename__ = "move_outs"
    __table_args__ = (
        UniqueConstraint("lease_id", name="uq_move_outs_lease"),
        Index("ix_move_outs_org_status", "org_id", "status"),
        Index("ix_move_outs_org_created", "org_id", "created_at"),
    )

    lease_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("leases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="notice_given")

    notice_given_at: Mapped[dt.date | None] = mapped_column(Date)
    scheduled_date: Mapped[dt.date | None] = mapped_column(Date, index=True)
    actual_date: Mapped[dt.date | None] = mapped_column(Date)
    reason: Mapped[str | None] = mapped_column(String(120))
    is_early_termination: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    inspection_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("inspections.id", ondelete="SET NULL"), index=True
    )
    forwarding_address: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict
    )

    deposit_held: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    deposit_deductions: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    deposit_refunded: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    deduction_detail: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    #: Statutory deadline for returning the deposit. Missing it is expensive.
    disposition_due_by: Mapped[dt.date | None] = mapped_column(Date, index=True)
    disposition_sent_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    statement_document_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("documents.id", ondelete="SET NULL"), index=True
    )

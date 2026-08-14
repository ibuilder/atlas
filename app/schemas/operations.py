"""Leasing, residents, accounting, maintenance, and document contracts.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field, StringConstraints, model_validator

from app.models.accounting import (
    BillStatus,
    DepositMovementKind,
    InvoiceStatus,
    PaymentMethod,
    PaymentStatus,
    ReconciliationStatus,
)
from app.models.documents import (
    DocumentCategory,
    DocumentVisibility,
    EnvelopeStatus,
    SignerStatus,
)
from app.models.leasing import (
    ApplicantRole,
    ApplicationStatus,
    LeadStatus,
    LeaseStatus,
    ScreeningRecommendation,
    ScreeningStatus,
)
from app.models.maintenance import (
    InspectionKind,
    InspectionResult,
    ItemResult,
    Priority,
    RequestStatus,
    WorkOrderStatus,
)
from app.models.resident import ResidentStatus, TenancyRole
from app.schemas.common import (
    AtlasRequest,
    AtlasResponse,
    Email,
    ListQuery,
    ShortText,
    Text,
)

Phone = Annotated[str, StringConstraints(max_length=40)]

__all__ = [
    "ApplicationDecision",
    "ApplicantCreate",
    "ApplicantOut",
    "ApplicationCreate",
    "ApplicationListQuery",
    "ApplicationWithdraw",
    "AssessmentOut",
    "LeaseFromApplication",
    "RenewalOffer",
    "RenewalDecline",
    "RenewalOut",
    "NoticeGiven",
    "MoveOutRecord",
    "DeductionIn",
    "DepositSettlement",
    "MoveOutOut",
    "MoveOutListQuery",
    "BillLineIn",
    "BillCreate",
    "BillApproval",
    "BillPaymentCreate",
    "BillLineOut",
    "BillOut",
    "BillPaymentOut",
    "BillListQuery",
    "InspectionSchedule",
    "FindingIn",
    "InspectionComplete",
    "InspectionItemOut",
    "InspectionOut",
    "InspectionListQuery",
    "ReconciliationOpen",
    "StatementImport",
    "TransactionMatch",
    "AutoMatchRequest",
    "ExceptionRaise",
    "ExceptionResolve",
    "ReconciliationComplete",
    "BankTransactionOut",
    "MatchCandidateOut",
    "ReconciliationExceptionOut",
    "ReconciliationOut",
    "ReconciliationListQuery",
    "ScreeningOut",
    "ScreeningRecord",
    "ScreeningRequest",
    "DepositBalanceOut",
    "DepositCollect",
    "DepositMovementListQuery",
    "DepositMovementOut",
    "DepositRelease",
    "DocumentOut",
    "EnvelopeCreate",
    "EnvelopeListQuery",
    "EnvelopeOut",
    "EnvelopeSignerOut",
    "EnvelopeVoid",
    "SignerIn",
    "InvoiceOut",
    "LeadCreate",
    "LeadOut",
    "LeaseCreate",
    "LeaseOut",
    "MaintenanceRequestCreate",
    "MaintenanceRequestOut",
    "PaymentCreate",
    "PaymentOut",
    "ResidentCreate",
    "ResidentOut",
    "WorkOrderCreate",
    "WorkOrderOut",
    "WorkOrderTransition",
]


# ---------------------------------------------------------------- residents


class ResidentCreate(AtlasRequest):
    first_name: ShortText
    last_name: ShortText
    email: Email | None = None
    phone: Phone | None = None
    preferred_name: ShortText | None = None
    emergency_contact_name: ShortText | None = None
    emergency_contact_phone: Phone | None = None
    notes: Text | None = None


class ResidentOut(AtlasResponse):
    id: str
    first_name: str
    last_name: str
    preferred_name: str | None = None
    email: str | None = None
    phone: str | None = None
    status: ResidentStatus
    first_move_in: dt.date | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class ResidentListQuery(ListQuery):
    status: ResidentStatus | None = None


# ------------------------------------------------------------------ leasing


class LeadCreate(AtlasRequest):
    first_name: ShortText
    last_name: ShortText | None = None
    email: Email | None = None
    phone: Phone | None = None
    source: ShortText = "direct"
    property_id: str | None = None
    unit_id: str | None = None
    desired_move_in: dt.date | None = None
    desired_bedrooms: int | None = Field(default=None, ge=0, le=20)
    budget_max: Decimal | None = Field(default=None, ge=0)
    notes: Text | None = None

    @model_validator(mode="after")
    def _need_a_way_to_reach_them(self) -> LeadCreate:
        # A lead with no contact method cannot be followed up, which makes it
        # not a lead. Better to reject at intake than to discover it in a report.
        if not self.email and not self.phone:
            raise ValueError("provide at least an email address or a phone number")
        return self


class LeadOut(AtlasResponse):
    id: str
    first_name: str
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    source: str
    status: LeadStatus
    property_id: str | None = None
    unit_id: str | None = None
    desired_move_in: dt.date | None = None
    assigned_to_id: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class LeadListQuery(ListQuery):
    status: LeadStatus | None = None
    property_id: str | None = None
    assigned_to_id: str | None = None


class ApplicationDecision(AtlasRequest):
    decision: Annotated[str, StringConstraints(pattern=r"^(approve|approve_with_conditions|deny)$")]
    #: Mandatory on every outcome. A denial without a recorded, consistent
    #: reason is indefensible in a fair-housing review, and an approval without
    #: one makes the denials look arbitrary by comparison.
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=2000)]
    conditions: dict[str, Any] = Field(default_factory=dict)


class ApplicationOut(AtlasResponse):
    id: str
    application_number: str
    property_id: str
    unit_id: str | None = None
    status: ApplicationStatus
    submitted_at: dt.datetime | None = None
    decided_at: dt.datetime | None = None
    quoted_rent: Decimal | None = None
    created_at: dt.datetime


class LeaseCreate(AtlasRequest):
    unit_id: str
    start_date: dt.date
    end_date: dt.date
    rent_amount: Decimal = Field(ge=0)
    security_deposit: Decimal = Field(default=Decimal("0"), ge=0)
    billing_day: int = Field(default=1, ge=1, le=28)
    late_fee_grace_days: int = Field(default=5, ge=0, le=31)
    late_fee_amount: Decimal | None = Field(default=None, ge=0)
    notice_period_days: int = Field(default=30, ge=0, le=365)
    resident_ids: list[str] = Field(default_factory=list, max_length=12)
    application_id: str | None = None

    @model_validator(mode="after")
    def _dates_make_sense(self) -> LeaseCreate:
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        if (self.end_date - self.start_date).days > 365 * 10:
            raise ValueError("lease term must not exceed ten years")
        return self


class LeaseOut(AtlasResponse):
    id: str
    lease_number: str
    property_id: str
    unit_id: str
    status: LeaseStatus
    start_date: dt.date
    end_date: dt.date
    rent_amount: Decimal
    security_deposit: Decimal
    deposit_held: Decimal
    billing_day: int
    created_at: dt.datetime
    updated_at: dt.datetime


class LeaseListQuery(ListQuery):
    status: LeaseStatus | None = None
    property_id: str | None = None
    unit_id: str | None = None
    expiring_within_days: int | None = Field(default=None, ge=0, le=365)


class TenancyOut(AtlasResponse):
    id: str
    lease_id: str
    resident_id: str
    role: TenancyRole
    is_financially_responsible: bool
    started_at: dt.date
    ended_at: dt.date | None = None


# --------------------------------------------------------------- accounting


class InvoiceLineOut(AtlasResponse):
    id: str
    line_number: int
    description: str
    quantity: Decimal
    unit_amount: Decimal
    amount: Decimal


class InvoiceOut(AtlasResponse):
    id: str
    invoice_number: str
    lease_id: str | None = None
    resident_id: str | None = None
    property_id: str | None = None
    status: InvoiceStatus
    issue_date: dt.date
    due_date: dt.date
    subtotal: Decimal
    total: Decimal
    balance: Decimal
    currency: str
    created_at: dt.datetime


class InvoiceListQuery(ListQuery):
    status: InvoiceStatus | None = None
    lease_id: str | None = None
    property_id: str | None = None
    overdue: bool | None = None


class PaymentCreate(AtlasRequest):
    amount: Decimal = Field(gt=0)
    method: PaymentMethod
    received_date: dt.date
    lease_id: str | None = None
    resident_id: str | None = None
    bank_account_id: str | None = None
    reference: Annotated[str, StringConstraints(max_length=80)] | None = None
    memo: Text | None = None
    #: Explicit allocation. Left empty, the payment is applied oldest-first,
    #: which is the convention residents and courts both expect.
    applications: list[dict[str, Any]] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def _need_a_payer(self) -> PaymentCreate:
        if not self.lease_id and not self.resident_id:
            raise ValueError("a payment must reference a lease or a resident")
        return self


class PaymentOut(AtlasResponse):
    id: str
    payment_number: str
    amount: Decimal
    unapplied_amount: Decimal
    method: PaymentMethod
    status: PaymentStatus
    received_date: dt.date
    lease_id: str | None = None
    resident_id: str | None = None
    reference: str | None = None
    created_at: dt.datetime


class DepositMovementListQuery(ListQuery):
    lease_id: str | None = None
    bank_account_id: str | None = None
    #: Balances and movements at a date rather than now. A disposition is
    #: argued about months later, against what was held at the time.
    as_of: dt.date | None = None


class DepositCollect(AtlasRequest):
    lease_id: str
    bank_account_id: str
    amount: Decimal = Field(gt=0)
    effective_date: dt.date | None = None
    reason: Annotated[str, StringConstraints(max_length=255)] | None = None


class DepositRelease(AtlasRequest):
    lease_id: str
    bank_account_id: str
    amount: Decimal = Field(gt=0)
    #: Why it left. ``returned`` goes to the resident, ``applied`` covers
    #: damage or arrears, ``forfeited`` is neither.
    kind: DepositMovementKind = DepositMovementKind.RETURNED
    effective_date: dt.date | None = None
    reason: Annotated[str, StringConstraints(max_length=255)] | None = None

    @model_validator(mode="after")
    def _must_release(self) -> DepositRelease:
        if self.kind in (DepositMovementKind.COLLECTED, DepositMovementKind.ADJUSTMENT):
            raise ValueError(f"{self.kind} does not release money from the trust")
        return self


class DepositMovementOut(AtlasResponse):
    id: str
    lease_id: str
    bank_account_id: str
    #: Signed: positive took money in, negative let it out.
    amount: Decimal
    effective_date: dt.date
    kind: DepositMovementKind
    reason: str | None = None
    journal_entry_id: str | None = None
    created_at: dt.datetime


class DepositBalanceOut(AtlasResponse):
    lease_id: str
    bank_account_id: str | None = None
    as_of: dt.date
    held: Decimal


class JournalLineIn(AtlasRequest):
    account_id: str
    debit: Decimal = Field(default=Decimal("0"), ge=0)
    credit: Decimal = Field(default=Decimal("0"), ge=0)
    memo: Annotated[str, StringConstraints(max_length=255)] | None = None
    property_id: str | None = None
    unit_id: str | None = None
    lease_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one_side(self) -> JournalLineIn:
        if (self.debit > 0) == (self.credit > 0):
            raise ValueError("each line must carry exactly one of debit or credit")
        return self


class JournalEntryCreate(AtlasRequest):
    entry_date: dt.date
    description: ShortText
    memo: Text | None = None
    property_id: str | None = None
    lines: list[JournalLineIn] = Field(min_length=2, max_length=500)
    post: bool = True

    @model_validator(mode="after")
    def _must_balance(self) -> JournalEntryCreate:
        debits = sum(line.debit for line in self.lines)
        credits = sum(line.credit for line in self.lines)
        if debits != credits:
            raise ValueError(f"entry does not balance: debits {debits} != credits {credits}")
        if debits == 0:
            raise ValueError("entry has no value")
        return self


class JournalEntryOut(AtlasResponse):
    id: str
    entry_number: str
    entry_date: dt.date
    description: str
    status: str
    total_debit: Decimal
    total_credit: Decimal
    posted_at: dt.datetime | None = None
    created_at: dt.datetime


# -------------------------------------------------------------- maintenance


class MaintenanceRequestCreate(AtlasRequest):
    property_id: str
    title: ShortText
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)
    ]
    unit_id: str | None = None
    lease_id: str | None = None
    category: ShortText = "general"
    priority: Priority = Priority.NORMAL
    is_habitability: bool = False
    permission_to_enter: bool = False
    entry_notes: Annotated[str, StringConstraints(max_length=255)] | None = None
    has_pets: bool = False
    preferred_times: list[str] = Field(default_factory=list, max_length=10)


class MaintenanceRequestOut(AtlasResponse):
    id: str
    request_number: str
    property_id: str
    unit_id: str | None = None
    title: str
    description: str
    category: str
    priority: Priority
    is_habitability: bool
    status: RequestStatus
    permission_to_enter: bool
    created_at: dt.datetime
    updated_at: dt.datetime


class MaintenanceRequestListQuery(ListQuery):
    status: RequestStatus | None = None
    priority: Priority | None = None
    property_id: str | None = None
    unit_id: str | None = None


class WorkOrderCreate(AtlasRequest):
    property_id: str
    title: ShortText
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)
    ]
    request_id: str | None = None
    unit_id: str | None = None
    asset_id: str | None = None
    trade: Annotated[str, StringConstraints(max_length=40)] | None = None
    priority: Priority = Priority.NORMAL
    estimated_cost: Decimal | None = Field(default=None, ge=0)
    is_owner_billable: bool = True
    is_resident_billable: bool = False


class WorkOrderTransition(AtlasRequest):
    status: WorkOrderStatus
    note: Text | None = None
    assigned_user_id: str | None = None
    vendor_id: str | None = None
    scheduled_start: dt.datetime | None = None
    scheduled_end: dt.datetime | None = None
    labor_hours: Decimal | None = Field(default=None, ge=0)
    labor_cost: Decimal | None = Field(default=None, ge=0)
    material_cost: Decimal | None = Field(default=None, ge=0)
    resolution_notes: Text | None = None
    is_resident_visible: bool = False


class WorkOrderOut(AtlasResponse):
    id: str
    work_order_number: str
    property_id: str
    unit_id: str | None = None
    title: str
    description: str
    trade: str | None = None
    priority: Priority
    status: WorkOrderStatus
    assigned_user_id: str | None = None
    vendor_id: str | None = None
    scheduled_start: dt.datetime | None = None
    completed_at: dt.datetime | None = None
    response_due_at: dt.datetime | None = None
    resolution_due_at: dt.datetime | None = None
    sla_breached_at: dt.datetime | None = None
    total_cost: Decimal
    created_at: dt.datetime
    updated_at: dt.datetime


class WorkOrderListQuery(ListQuery):
    status: WorkOrderStatus | None = None
    priority: Priority | None = None
    property_id: str | None = None
    vendor_id: str | None = None
    assigned_user_id: str | None = None
    breached: bool | None = None


class WorkOrderEventOut(AtlasResponse):
    id: str
    event_type: str
    from_status: WorkOrderStatus | None = None
    to_status: WorkOrderStatus | None = None
    actor_label: str
    note: str | None = None
    occurred_at: dt.datetime


# ---------------------------------------------------------------- documents


class DocumentOut(AtlasResponse):
    id: str
    name: str
    original_filename: str
    content_type: str
    size_bytes: int
    category: DocumentCategory
    visibility: DocumentVisibility
    scan_status: str
    is_quarantined: bool
    version: int
    created_at: dt.datetime


class DocumentLinkCreate(AtlasRequest):
    entity_type: Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)]
    entity_id: str
    relation: Annotated[str, StringConstraints(max_length=40)] = "attachment"
    is_primary: bool = False


class DocumentListQuery(ListQuery):
    category: DocumentCategory | None = None
    entity_type: str | None = None
    entity_id: str | None = None


# ------------------------------------------------------------------- e-sign


class EnvelopeListQuery(ListQuery):
    status: EnvelopeStatus | None = None
    subject_type: str | None = None
    subject_id: str | None = None


class SignerIn(AtlasRequest):
    name: ShortText
    email: Email
    role: Annotated[str, StringConstraints(max_length=60)] | None = None


class EnvelopeCreate(AtlasRequest):
    document_id: str
    title: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    reference: Annotated[str, StringConstraints(min_length=1, max_length=60)]
    signers: list[SignerIn] = Field(min_length=1, max_length=20)
    subject_type: Annotated[str, StringConstraints(max_length=40)] | None = None
    subject_id: str | None = None
    expires_in_days: int = Field(default=30, ge=1, le=365)

    @model_validator(mode="after")
    def _subject_needs_both_halves(self) -> EnvelopeCreate:
        if (self.subject_type is None) != (self.subject_id is None):
            raise ValueError("a subject needs both a type and an id, or neither")
        return self


class EnvelopeVoid(AtlasRequest):
    #: Mandatory. An envelope withdrawn without a stated reason is one nobody
    #: can account for afterwards.
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class EnvelopeSignerOut(AtlasResponse):
    id: str
    sequence: int
    name: str
    email: str
    role: str | None = None
    status: SignerStatus
    signed_at: dt.datetime | None = None
    declined_at: dt.datetime | None = None
    decline_reason: str | None = None
    #: The consent record. Present because producing it is the point of storing
    #: it; a signature nobody can evidence is not worth having.
    typed_name: str | None = None
    signed_ip: str | None = None
    consent_text: str | None = None


class EnvelopeOut(AtlasResponse):
    id: str
    reference: str
    title: str
    document_id: str
    document_sha256: str | None = None
    subject_type: str | None = None
    subject_id: str | None = None
    status: EnvelopeStatus
    provider: str
    sent_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None
    voided_reason: str | None = None
    created_at: dt.datetime


# ------------------------------------------------------- applications


class ApplicationListQuery(ListQuery):
    status: ApplicationStatus | None = None
    property_id: str | None = None
    unit_id: str | None = None


class ApplicationCreate(AtlasRequest):
    property_id: str
    unit_id: str | None = None
    lead_id: str | None = None
    desired_move_in: dt.date | None = None
    lease_term_months: int = Field(default=12, ge=1, le=120)
    quoted_rent: Decimal | None = Field(default=None, ge=0)
    application_fee: Decimal | None = Field(default=None, ge=0)


class ApplicantCreate(AtlasRequest):
    first_name: ShortText
    last_name: ShortText
    role: ApplicantRole = ApplicantRole.PRIMARY
    email: Email | None = None
    phone: Annotated[str, StringConstraints(max_length=40)] | None = None
    monthly_income: Decimal | None = Field(default=None, ge=0)
    employer_name: ShortText | None = None
    resident_id: str | None = None


class ApplicantOut(AtlasResponse):
    id: str
    application_id: str
    first_name: str
    last_name: str
    role: ApplicantRole
    email: str | None = None
    monthly_income: Decimal | None = None
    employer_name: str | None = None
    #: Consent evidence. A screening ordered without it is a statutory
    #: violation, so the record of it is part of the applicant, not a flag.
    screening_consent_at: dt.datetime | None = None
    screening_consent_ip: str | None = None


class ScreeningRequest(AtlasRequest):
    applicant_id: str
    provider: Annotated[str, StringConstraints(max_length=60)] = "manual"


class ScreeningRecord(AtlasRequest):
    recommendation: ScreeningRecommendation
    credit_score: int | None = Field(default=None, ge=300, le=900)
    has_eviction_history: bool | None = None
    has_criminal_record: bool | None = None
    verified_monthly_income: Decimal | None = Field(default=None, ge=0)
    provider_reference: Annotated[str, StringConstraints(max_length=120)] | None = None


class ScreeningOut(AtlasResponse):
    id: str
    application_id: str
    applicant_id: str
    status: ScreeningStatus
    provider: str
    recommendation: ScreeningRecommendation | None = None
    credit_score: int | None = None
    has_eviction_history: bool | None = None
    has_criminal_record: bool | None = None
    verified_monthly_income: Decimal | None = None
    completed_at: dt.datetime | None = None
    expires_on: dt.date | None = None


class AssessmentOut(AtlasResponse):
    """What the criteria say. A recommendation, never a decision."""

    recommendation: ScreeningRecommendation
    income_ratio: Decimal | None = None
    lowest_credit_score: int | None = None
    #: Why it would be declined.
    reasons: list[str] = Field(default_factory=list)
    #: What is not yet known. Distinct from reasons: an application short of a
    #: document is a different conversation from one that fails on its merits.
    missing: list[str] = Field(default_factory=list)


class ApplicationWithdraw(AtlasRequest):
    reason: Annotated[str, StringConstraints(max_length=255)] | None = None


class LeaseFromApplication(AtlasRequest):
    start_date: dt.date
    end_date: dt.date
    rent_amount: Decimal | None = Field(default=None, ge=0)
    #: Omitted falls back to the rent. Zero is honoured as zero - a
    #: deposit-replacement rider in place of a deposit is a real arrangement.
    security_deposit: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _dates_make_sense(self) -> LeaseFromApplication:
        if self.end_date <= self.start_date:
            raise ValueError("a lease must end after it starts")
        return self


# ------------------------------------------------- renewals and move-outs


class RenewalOffer(AtlasRequest):
    #: The terms are fixed when offered. A resident who accepts is accepting
    #: *this* offer, not whatever the asking rent has become since.
    offered_rent: Decimal = Field(ge=0)
    proposed_start: dt.date
    proposed_end: dt.date
    term_months: int = Field(default=12, ge=1, le=120)
    expires_in_days: int = Field(default=30, ge=1, le=365)

    @model_validator(mode="after")
    def _dates_make_sense(self) -> RenewalOffer:
        if self.proposed_end <= self.proposed_start:
            raise ValueError("a renewal must end after it starts")
        return self


class RenewalDecline(AtlasRequest):
    reason: Annotated[str, StringConstraints(max_length=255)] | None = None


class RenewalOut(AtlasResponse):
    id: str
    lease_id: str
    status: str
    offered_rent: Decimal
    offered_term_months: int
    proposed_start: dt.date
    proposed_end: dt.date
    offer_sent_at: dt.datetime | None = None
    #: A lapsed offer cannot be honoured, so when it lapses is part of it.
    offer_expires_at: dt.datetime | None = None
    responded_at: dt.datetime | None = None
    response: str | None = None
    declined_reason: str | None = None
    new_lease_id: str | None = None


class NoticeGiven(AtlasRequest):
    notice_date: dt.date
    scheduled_date: dt.date
    reason: Annotated[str, StringConstraints(max_length=120)] | None = None
    is_early_termination: bool = False

    @model_validator(mode="after")
    def _order_makes_sense(self) -> NoticeGiven:
        if self.scheduled_date < self.notice_date:
            raise ValueError("a move-out cannot be scheduled before notice was given")
        return self


class MoveOutRecord(AtlasRequest):
    actual_date: dt.date
    forwarding_address: dict[str, Any] | None = None
    #: The statutory disposition window. Stored on the move-out rather than
    #: recomputed on read: a recomputed deadline drifts every time somebody
    #: changes the setting, and this is the date the law measures against.
    disposition_days: int = Field(default=21, ge=1, le=365)
    inspection_id: str | None = None
    start_turn_on_vacancy: bool = True


class DeductionIn(AtlasRequest):
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)
    ]
    amount: Decimal = Field(gt=0)
    inspection_item_id: str | None = None


class DepositSettlement(AtlasRequest):
    #: Empty means a full refund, which is a real and common disposition.
    deductions: list[DeductionIn] = Field(default_factory=list)
    #: Take the deductions from a completed inspection instead of the body.
    #: Findings that were photographed on a checklist the resident can see are
    #: the defensible kind; a figure typed at settlement time is not.
    from_inspection_id: str | None = None

    @model_validator(mode="after")
    def _one_source(self) -> DepositSettlement:
        if self.deductions and self.from_inspection_id:
            raise ValueError("give either deductions or an inspection to take them from, not both")
        return self


class MoveOutOut(AtlasResponse):
    id: str
    lease_id: str
    status: str
    notice_given_at: dt.date | None = None
    scheduled_date: dt.date | None = None
    actual_date: dt.date | None = None
    reason: str | None = None
    is_early_termination: bool
    #: What was actually collected, not what the lease specified.
    deposit_held: Decimal
    deposit_deductions: Decimal
    deposit_refunded: Decimal
    deduction_detail: list[Any] = Field(default_factory=list)
    disposition_due_by: dt.date | None = None
    disposition_sent_at: dt.datetime | None = None


class MoveOutListQuery(ListQuery):
    status: Annotated[str, StringConstraints(max_length=20)] | None = None
    #: Only those whose statutory deadline has passed unsettled. Past this
    #: date the deductions are usually forfeit, often with a penalty on top.
    overdue: bool = False


# ----------------------------------------------------------------- payables


class BillLineIn(AtlasRequest):
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ]
    amount: Decimal = Field(gt=0)
    account_id: str
    property_id: str | None = None
    unit_id: str | None = None
    quantity: Decimal = Field(default=Decimal("1"), gt=0)
    #: Whether this line reaches an owner statement. Default true; a line the
    #: operator is absorbing is the exception and has to say so.
    is_owner_billable: bool = True


class BillCreate(AtlasRequest):
    vendor_id: str
    bill_date: dt.date
    due_date: dt.date
    lines: list[BillLineIn] = Field(min_length=1, max_length=200)
    vendor_invoice_number: Annotated[str, StringConstraints(max_length=80)] | None = None
    property_id: str | None = None
    work_order_id: str | None = None
    memo: Annotated[str, StringConstraints(max_length=2000)] | None = None

    @model_validator(mode="after")
    def _dates_make_sense(self) -> BillCreate:
        if self.due_date < self.bill_date:
            raise ValueError("the due date cannot precede the bill date")
        return self


class BillApproval(AtlasRequest):
    note: Annotated[str, StringConstraints(max_length=500)] | None = None


class BillPaymentCreate(AtlasRequest):
    bank_account_id: str
    amount: Decimal = Field(gt=0)
    paid_date: dt.date
    method: PaymentMethod = PaymentMethod.CHECK
    check_number: Annotated[str, StringConstraints(max_length=40)] | None = None


class BillLineOut(AtlasResponse):
    id: str
    line_number: int
    account_id: str
    property_id: str | None = None
    unit_id: str | None = None
    description: str
    quantity: Decimal
    amount: Decimal
    is_owner_billable: bool


class BillOut(AtlasResponse):
    id: str
    bill_number: str
    vendor_id: str
    vendor_invoice_number: str | None = None
    bill_date: dt.date
    due_date: dt.date
    status: BillStatus
    property_id: str | None = None
    work_order_id: str | None = None
    total: Decimal
    balance: Decimal
    approved_at: dt.datetime | None = None
    approved_by_id: str | None = None
    #: What was actually authorised. Approving *this* bill is not the same as
    #: approving whatever it later becomes, so the figure is snapshotted.
    approved_total: Decimal | None = None
    memo: str | None = None
    is_1099_reportable: bool = False


class BillPaymentOut(AtlasResponse):
    id: str
    bill_id: str
    bank_account_id: str
    amount: Decimal
    paid_date: dt.date
    method: PaymentMethod
    check_number: str | None = None
    voided_at: dt.datetime | None = None


class BillListQuery(ListQuery):
    status: BillStatus | None = None
    vendor_id: str | None = None
    property_id: str | None = None
    #: Approved, unpaid, and past due. What the run should be paying today.
    due: bool = False


# -------------------------------------------------------------- inspections


class InspectionSchedule(AtlasRequest):
    kind: InspectionKind
    property_id: str
    #: The checklist is *copied* onto the inspection at this version. Editing
    #: the template afterwards must never change what a completed inspection
    #: appears to have asked.
    template_code: Annotated[str, StringConstraints(max_length=40)] | None = None
    unit_id: str | None = None
    lease_id: str | None = None
    scheduled_for: dt.datetime | None = None
    inspector_user_id: str | None = None
    inspector_vendor_id: str | None = None


class FindingIn(AtlasRequest):
    item_id: str
    result: ItemResult
    condition: Annotated[str, StringConstraints(max_length=40)] | None = None
    severity: Annotated[str, StringConstraints(max_length=40)] | None = None
    notes: Annotated[str, StringConstraints(max_length=2000)] | None = None
    #: What it would cost to put right. This is the figure that becomes a
    #: deposit deduction, so it is recorded at the time it is observed rather
    #: than invented at settlement.
    remedy_cost: Decimal | None = Field(default=None, ge=0)
    is_resident_responsible: bool = False


class InspectionComplete(AtlasRequest):
    notes: Annotated[str, StringConstraints(max_length=4000)] | None = None
    inspector_signed: bool = False
    resident_signed: bool = False
    #: Raise work orders from the failed items in the same call. Idempotent in
    #: the service, so a retry never raises two jobs for one broken window.
    raise_work: bool = True


class InspectionItemOut(AtlasResponse):
    id: str
    section: str
    name: str
    sort_order: int
    result: ItemResult | None = None
    condition: str | None = None
    severity: str | None = None
    notes: str | None = None
    remedy_cost: Decimal | None = None
    is_resident_responsible: bool = False
    requires_photo: bool = False
    work_order_id: str | None = None


class InspectionOut(AtlasResponse):
    id: str
    inspection_number: str
    kind: InspectionKind
    property_id: str
    unit_id: str | None = None
    lease_id: str | None = None
    status: str
    scheduled_for: dt.datetime | None = None
    started_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None
    inspector_user_id: str | None = None
    inspector_vendor_id: str | None = None
    result: InspectionResult | None = None
    notes: str | None = None
    captured_offline: bool = False


class InspectionListQuery(ListQuery):
    status: Annotated[str, StringConstraints(max_length=20)] | None = None
    kind: InspectionKind | None = None
    property_id: str | None = None
    unit_id: str | None = None


# ---------------------------------------------------------- reconciliation


class ReconciliationOpen(AtlasRequest):
    bank_account_id: str
    statement_start: dt.date
    statement_end: dt.date
    opening_balance: Decimal
    closing_balance: Decimal

    @model_validator(mode="after")
    def _period_makes_sense(self) -> ReconciliationOpen:
        if self.statement_end < self.statement_start:
            raise ValueError("a statement period must end on or after it starts")
        return self


class StatementImport(AtlasRequest):
    bank_account_id: str
    #: The export as the bank produced it. Column *names* differ per bank, so
    #: they are configurable; values are not — a row whose amount will not
    #: parse is rejected loudly rather than imported as zero, because a zero in
    #: a reconciliation is a difference somebody spends an afternoon hunting.
    csv: Annotated[str, StringConstraints(min_length=1, max_length=4_000_000)]
    date_column: Annotated[str, StringConstraints(max_length=60)] = "date"
    amount_column: Annotated[str, StringConstraints(max_length=60)] = "amount"
    description_column: Annotated[str, StringConstraints(max_length=60)] = "description"
    reference_column: Annotated[str, StringConstraints(max_length=60)] | None = "reference"
    external_id_column: Annotated[str, StringConstraints(max_length=60)] | None = "id"
    date_format: Annotated[str, StringConstraints(max_length=40)] | None = None


class TransactionMatch(AtlasRequest):
    journal_line_id: str
    confidence: int | None = Field(default=None, ge=0, le=100)


class AutoMatchRequest(AtlasRequest):
    #: Only what is both confident *and* unambiguous is matched automatically.
    #: A second candidate scoring near the first is exactly the case a person
    #: has to look at.
    threshold: int = Field(default=90, ge=50, le=100)


class ExceptionRaise(AtlasRequest):
    kind: Annotated[str, StringConstraints(max_length=40)]
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=3, max_length=255)
    ]
    amount: Decimal | None = None
    bank_transaction_id: str | None = None


class ExceptionResolve(AtlasRequest):
    #: Mandatory. An exception closed without a note is one nobody can audit.
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=1000)]


class ReconciliationComplete(AtlasRequest):
    notes: Annotated[str, StringConstraints(max_length=4000)] | None = None


class BankTransactionOut(AtlasResponse):
    id: str
    bank_account_id: str
    posted_date: dt.date
    amount: Decimal
    description: str
    reference: str | None = None
    match_status: str
    match_confidence: int | None = None
    matched_journal_line_id: str | None = None
    reconciliation_id: str | None = None


class MatchCandidateOut(AtlasResponse):
    journal_line_id: str
    journal_entry_id: str
    confidence: int
    amount: Decimal
    memo: str | None = None
    #: Why this scored what it did. A suggestion nobody can interrogate is a
    #: suggestion nobody should accept.
    reasons: list[str] = Field(default_factory=list)


class ReconciliationExceptionOut(AtlasResponse):
    id: str
    kind: str
    description: str
    amount: Decimal | None = None
    bank_transaction_id: str | None = None
    resolved_at: dt.datetime | None = None
    resolution_note: str | None = None


class ReconciliationOut(AtlasResponse):
    id: str
    bank_account_id: str
    statement_start: dt.date
    statement_end: dt.date
    statement_opening_balance: Decimal
    statement_closing_balance: Decimal
    #: Opening plus everything matched — the statement side of the tie-out.
    cleared_balance: Decimal
    ledger_balance: Decimal
    difference: Decimal
    status: ReconciliationStatus
    completed_at: dt.datetime | None = None
    completed_by_id: str | None = None
    notes: str | None = None


class ReconciliationListQuery(ListQuery):
    bank_account_id: str | None = None
    status: ReconciliationStatus | None = None

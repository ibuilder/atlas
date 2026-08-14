"""Leads, residents, and leases.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt

from flask import Response, request
from flask_login import current_user
from sqlalchemy import select

from app.api.helpers import (
    add_etag,
    paginate,
    parse_body,
    parse_query,
    respond,
    respond_collection,
    respond_created,
)
from app.api.v1 import api_v1_bp
from app.errors import BusinessRuleViolation, NotFound
from app.extensions import current_session, db
from app.middleware import require_org_scope
from app.models.audit import AuditAction
from app.models.leasing import (
    Applicant,
    Application,
    Lead,
    Lease,
    LeaseStatus,
    ScreeningResult,
)
from app.models.org import Unit, UnitStatus
from app.models.resident import Resident, Tenancy, TenancyRole
from app.models.sequences import SequenceKey
from app.schemas.operations import (
    ApplicantCreate,
    ApplicantOut,
    ApplicationCreate,
    ApplicationDecision,
    ApplicationListQuery,
    ApplicationOut,
    ApplicationWithdraw,
    AssessmentOut,
    LeadCreate,
    LeadListQuery,
    LeadOut,
    LeaseCreate,
    LeaseFromApplication,
    LeaseListQuery,
    LeaseOut,
    ResidentCreate,
    ResidentListQuery,
    ResidentOut,
    ScreeningOut,
    ScreeningRecord,
    ScreeningRequest,
)
from app.security.permissions import Perm
from app.security.policies import filter_permitted, require
from app.services.audit.recorder import record_audit_event
from app.services.common.numbering import next_number
from app.services.common.unit_of_work import transaction
from app.services.leasing import applications

__all__ = []


@api_v1_bp.get("/leads", endpoint="leads_list")
def list_leads() -> Response:
    require(Perm.LEAD_READ)
    query = parse_query(LeadListQuery)
    org_id = require_org_scope()

    stmt = select(Lead).where(Lead.org_id == org_id)
    if query.status:
        stmt = stmt.where(Lead.status == query.status)
    if query.property_id:
        stmt = stmt.where(Lead.property_id == query.property_id)
    if query.assigned_to_id:
        stmt = stmt.where(Lead.assigned_to_id == query.assigned_to_id)

    page = paginate(current_session(), stmt, Lead, limit=query.limit, cursor=query.cursor)
    return respond_collection(page, LeadOut)


@api_v1_bp.post("/leads", endpoint="leads_create")
def create_lead() -> Response:
    require(Perm.LEAD_MANAGE)
    payload = parse_body(LeadCreate)
    org_id = require_org_scope()

    with transaction() as session:
        record = Lead(org_id=org_id, **payload.model_dump(exclude_none=True))
        session.add(record)
        session.flush()
        record_audit_event(
            action=AuditAction.LEAD_CREATED,
            resource_type="Lead",
            resource_id=record.id,
            resource_label=record.full_name,
            payload={"source": record.source},
            org_id=org_id,
            session=session,
        )

    return respond_created(
        LeadOut.model_validate(record, from_attributes=True),
        location=f"/api/v1/leads/{record.id}",
    )


@api_v1_bp.get("/residents", endpoint="residents_list")
def list_residents() -> Response:
    require(Perm.RESIDENT_READ)
    query = parse_query(ResidentListQuery)
    org_id = require_org_scope()

    stmt = select(Resident).where(Resident.org_id == org_id)
    if query.status:
        stmt = stmt.where(Resident.status == query.status)
    if query.q:
        pattern = f"%{query.q}%"
        stmt = stmt.where(Resident.last_name.ilike(pattern) | Resident.first_name.ilike(pattern))

    page = paginate(current_session(), stmt, Resident, limit=query.limit, cursor=query.cursor)
    page.items = filter_permitted(Perm.RESIDENT_READ, page.items)
    return respond_collection(page, ResidentOut)


@api_v1_bp.post("/residents", endpoint="residents_create")
def create_resident() -> Response:
    require(Perm.RESIDENT_MANAGE)
    payload = parse_body(ResidentCreate)
    org_id = require_org_scope()

    with transaction() as session:
        record = Resident(org_id=org_id, **payload.model_dump(exclude_none=True))
        session.add(record)
        session.flush()

    return respond_created(
        ResidentOut.model_validate(record, from_attributes=True),
        location=f"/api/v1/residents/{record.id}",
    )


@api_v1_bp.get("/leases", endpoint="leases_list")
def list_leases() -> Response:
    require(Perm.LEASE_READ)
    query = parse_query(LeaseListQuery)
    org_id = require_org_scope()

    stmt = select(Lease).where(Lease.org_id == org_id)
    if query.status:
        stmt = stmt.where(Lease.status == query.status)
    if query.property_id:
        stmt = stmt.where(Lease.property_id == query.property_id)
    if query.unit_id:
        stmt = stmt.where(Lease.unit_id == query.unit_id)
    if query.expiring_within_days is not None:
        horizon = dt.date.today() + dt.timedelta(days=query.expiring_within_days)
        stmt = stmt.where(
            Lease.end_date <= horizon,
            Lease.status.in_([LeaseStatus.ACTIVE, LeaseStatus.HOLDOVER]),
        )

    page = paginate(current_session(), stmt, Lease, limit=query.limit, cursor=query.cursor)
    page.items = filter_permitted(Perm.LEASE_READ, page.items)
    return respond_collection(page, LeaseOut)


@api_v1_bp.post("/leases", endpoint="leases_create")
def create_lease() -> Response:
    require(Perm.LEASE_CREATE)
    payload = parse_body(LeaseCreate)
    org_id = require_org_scope()

    unit = db.session.get(Unit, payload.unit_id)
    if unit is None:
        raise NotFound("That unit was not found.")

    with transaction() as session:
        _assert_no_overlapping_lease(session, unit.id, payload.start_date, payload.end_date)

        lease = Lease(
            org_id=org_id,
            lease_number=next_number(session, SequenceKey.LEASE, org_id=org_id),
            property_id=unit.property_id,
            unit_id=unit.id,
            status=LeaseStatus.DRAFT,
            start_date=payload.start_date,
            end_date=payload.end_date,
            rent_amount=payload.rent_amount,
            security_deposit=payload.security_deposit,
            billing_day=payload.billing_day,
            late_fee_grace_days=payload.late_fee_grace_days,
            late_fee_amount=payload.late_fee_amount,
            notice_period_days=payload.notice_period_days,
        )
        session.add(lease)
        session.flush()

        for index, resident_id in enumerate(payload.resident_ids):
            resident = session.get(Resident, resident_id)
            if resident is None or resident.org_id != org_id:
                raise NotFound(f"Resident {resident_id} was not found.")
            session.add(
                Tenancy(
                    org_id=org_id,
                    lease_id=lease.id,
                    resident_id=resident.id,
                    role=TenancyRole.PRIMARY if index == 0 else TenancyRole.CO_RESIDENT,
                    started_at=payload.start_date,
                )
            )

        record_audit_event(
            action=AuditAction.LEASE_CREATED,
            resource_type="Lease",
            resource_id=lease.id,
            resource_label=lease.lease_number,
            payload={
                "unit_id": unit.id,
                "rent": str(lease.rent_amount),
                "term": f"{lease.start_date} to {lease.end_date}",
                "residents": len(payload.resident_ids),
            },
            org_id=org_id,
            session=session,
        )

    return respond_created(
        LeaseOut.model_validate(lease, from_attributes=True),
        location=f"/api/v1/leases/{lease.id}",
    )


def _assert_no_overlapping_lease(session, unit_id: str, start: dt.date, end: dt.date) -> None:  # noqa: ANN001
    """Refuse to double-let a unit.

    Overlap is inclusive on both ends: a lease ending on the 31st and another
    starting on the 31st is two households in one unit for a day, which is a
    conversation nobody wants to have.
    """
    clash = (
        session.execute(
            select(Lease).where(
                Lease.unit_id == unit_id,
                Lease.status.in_(
                    [
                        LeaseStatus.PENDING_SIGNATURE,
                        LeaseStatus.EXECUTED,
                        LeaseStatus.ACTIVE,
                        LeaseStatus.HOLDOVER,
                    ]
                ),
                Lease.start_date <= end,
                Lease.end_date >= start,
            )
        )
        .scalars()
        .first()
    )

    if clash is not None:
        raise BusinessRuleViolation(
            f"Unit already has lease {clash.lease_number} covering "
            f"{clash.start_date} to {clash.end_date}."
        )


@api_v1_bp.post("/leases/<id:lease_id>/activate", endpoint="leases_activate")
def activate_lease(lease_id: str) -> Response:
    """Move a lease into effect and mark the unit occupied."""
    lease = db.session.get(Lease, lease_id)
    if lease is None:
        raise NotFound("That lease was not found.")
    require(Perm.LEASE_UPDATE, lease)

    if lease.status not in (LeaseStatus.DRAFT, LeaseStatus.PENDING_SIGNATURE, LeaseStatus.EXECUTED):
        raise BusinessRuleViolation(f"A {lease.status} lease cannot be activated.")

    with transaction() as session:
        lease.status = LeaseStatus.ACTIVE
        lease.executed_at = lease.executed_at or dt.datetime.now(dt.UTC)
        lease.move_in_date = lease.move_in_date or lease.start_date

        unit = session.get(Unit, lease.unit_id)
        if unit is not None:
            unit.status = UnitStatus.OCCUPIED
            unit.is_listed = False

        for tenancy in lease.tenancies:
            resident = session.get(Resident, tenancy.resident_id)
            if resident is not None:
                from app.models.resident import ResidentStatus

                resident.status = ResidentStatus.CURRENT
                resident.first_move_in = resident.first_move_in or lease.start_date

        record_audit_event(
            action=AuditAction.LEASE_ACTIVATED,
            resource_type="Lease",
            resource_id=lease.id,
            resource_label=lease.lease_number,
            payload={"unit_id": lease.unit_id, "start_date": lease.start_date.isoformat()},
            org_id=lease.org_id,
            session=session,
        )

    return add_etag(respond(LeaseOut.model_validate(lease, from_attributes=True)), lease)


# ------------------------------------------------------------- applications
#
# The funnel from an enquiry to a tenancy. Two things here are not ordinary
# CRUD and are worth reading before changing:
#
# Consent is recorded from the *request*, never from the body. A screening
# ordered without recorded consent is a statutory violation, and the evidence
# that it was given cannot be reconstructed afterwards - so the address it came
# from is taken from the connection.
#
# The decision endpoint demands a reason on every outcome, including approval.
# Those reasons are the adverse-action notice, and a denial without one is
# indefensible in a fair-housing review.


def _application_or_404(application_id: str, org_id: str) -> Application:
    record = db.session.get(Application, application_id)
    if record is None or record.org_id != org_id:
        raise NotFound("That application was not found.")
    return record


@api_v1_bp.get("/applications", endpoint="applications_list")
def list_applications() -> Response:
    require(Perm.APPLICATION_READ)
    query = parse_query(ApplicationListQuery)
    org_id = require_org_scope()

    stmt = select(Application).where(Application.org_id == org_id)
    if query.status:
        stmt = stmt.where(Application.status == query.status)
    if query.property_id:
        stmt = stmt.where(Application.property_id == query.property_id)
    if query.unit_id:
        stmt = stmt.where(Application.unit_id == query.unit_id)

    page = paginate(current_session(), stmt, Application, limit=query.limit, cursor=query.cursor)
    return respond_collection(page, ApplicationOut)


@api_v1_bp.get("/applications/<id:application_id>", endpoint="applications_get")
def get_application(application_id: str) -> Response:
    """One application with its applicants, screenings, and the assessment."""
    require(Perm.APPLICATION_READ)
    org_id = require_org_scope()

    record = _application_or_404(application_id, org_id)
    assessment = applications.assess_application(current_session(), application=record)

    return respond(
        {
            **ApplicationOut.model_validate(record, from_attributes=True).model_dump(mode="json"),
            "applicants": [
                ApplicantOut.model_validate(a, from_attributes=True).model_dump(mode="json")
                for a in record.applicants
            ],
            "screenings": [
                ScreeningOut.model_validate(s, from_attributes=True).model_dump(mode="json")
                for s in record.screenings
            ],
            "assessment": AssessmentOut.model_validate(assessment, from_attributes=True).model_dump(
                mode="json"
            ),
        }
    )


@api_v1_bp.post("/applications", endpoint="applications_create")
def create_application() -> Response:
    require(Perm.APPLICATION_MANAGE)
    payload = parse_body(ApplicationCreate)
    org_id = require_org_scope()

    with transaction() as session:
        record = applications.create_application(
            session,
            org_id=org_id,
            property_id=payload.property_id,
            unit_id=payload.unit_id,
            lead_id=payload.lead_id,
            desired_move_in=payload.desired_move_in,
            lease_term_months=payload.lease_term_months,
            quoted_rent=payload.quoted_rent,
            application_fee=payload.application_fee,
            actor_id=current_user.id,
        )

    return respond_created(
        ApplicationOut.model_validate(record, from_attributes=True),
        location=f"/api/v1/applications/{record.id}",
    )


@api_v1_bp.post("/applications/<id:application_id>/applicants", endpoint="applicants_create")
def add_applicant(application_id: str) -> Response:
    require(Perm.APPLICATION_MANAGE)
    payload = parse_body(ApplicantCreate)
    org_id = require_org_scope()

    with transaction() as session:
        application = _application_or_404(application_id, org_id)
        record = applications.add_applicant(
            session,
            application=application,
            first_name=payload.first_name,
            last_name=payload.last_name,
            role=payload.role,
            email=payload.email,
            phone=payload.phone,
            monthly_income=payload.monthly_income,
            employer_name=payload.employer_name,
            resident_id=payload.resident_id,
        )

    return respond_created(
        ApplicantOut.model_validate(record, from_attributes=True),
        location=f"/api/v1/applications/{application_id}",
    )


@api_v1_bp.post("/applicants/<id:applicant_id>/consent", endpoint="applicants_consent")
def record_consent(applicant_id: str) -> Response:
    """Record that this applicant consented to being screened.

    The address is taken from the connection rather than the body. Consent
    evidence that the caller can dictate is not evidence, and this is the only
    moment the real one exists.
    """
    require(Perm.APPLICATION_MANAGE)
    org_id = require_org_scope()

    with transaction() as session:
        applicant = session.get(Applicant, applicant_id)
        if applicant is None or applicant.org_id != org_id:
            raise NotFound("That applicant was not found.")
        record = applications.record_consent(
            session, applicant=applicant, ip_address=request.remote_addr or "unknown"
        )

    return respond(ApplicantOut.model_validate(record, from_attributes=True))


@api_v1_bp.post("/applications/<id:application_id>/submit", endpoint="applications_submit")
def submit_application(application_id: str) -> Response:
    require(Perm.APPLICATION_MANAGE)
    org_id = require_org_scope()

    with transaction() as session:
        application = _application_or_404(application_id, org_id)
        record = applications.submit_application(
            session, application=application, actor_id=current_user.id
        )

    return respond(ApplicationOut.model_validate(record, from_attributes=True))


@api_v1_bp.post("/applications/<id:application_id>/screenings", endpoint="screenings_request")
def request_screening(application_id: str) -> Response:
    """Order a screening. Refused without recorded consent."""
    require(Perm.SCREENING_ORDER)
    payload = parse_body(ScreeningRequest)
    org_id = require_org_scope()

    with transaction() as session:
        application = _application_or_404(application_id, org_id)
        applicant = session.get(Applicant, payload.applicant_id)
        if applicant is None or applicant.application_id != application.id:
            raise NotFound("That applicant was not found on this application.")
        record = applications.request_screening(
            session, application=application, applicant=applicant, provider=payload.provider
        )

    return respond_created(
        ScreeningOut.model_validate(record, from_attributes=True),
        location=f"/api/v1/applications/{application_id}",
    )


@api_v1_bp.post("/screenings/<id:screening_id>", endpoint="screenings_record")
def record_screening(screening_id: str) -> Response:
    """Record what the provider came back with."""
    require(Perm.SCREENING_ORDER)
    payload = parse_body(ScreeningRecord)
    org_id = require_org_scope()

    with transaction() as session:
        screening = session.get(ScreeningResult, screening_id)
        if screening is None or screening.org_id != org_id:
            raise NotFound("That screening was not found.")
        record = applications.record_screening(
            session,
            screening=screening,
            recommendation=payload.recommendation,
            credit_score=payload.credit_score,
            has_eviction_history=payload.has_eviction_history,
            has_criminal_record=payload.has_criminal_record,
            verified_monthly_income=payload.verified_monthly_income,
            provider_reference=payload.provider_reference,
        )

    return respond(ScreeningOut.model_validate(record, from_attributes=True))


@api_v1_bp.post("/applications/<id:application_id>/decision", endpoint="applications_decide")
def decide_application(application_id: str) -> Response:
    """Approve, approve with conditions, or deny.

    A reason is mandatory on every outcome. Those reasons *are* the
    adverse-action notice on a denial, and requiring one on approvals too is
    what stops the denials looking arbitrary by comparison.
    """
    require(Perm.APPLICATION_DECIDE)
    payload = parse_body(ApplicationDecision)
    org_id = require_org_scope()

    with transaction() as session:
        application = _application_or_404(application_id, org_id)
        if payload.decision == "deny":
            record = applications.deny_application(
                session,
                application=application,
                decided_by_id=current_user.id,
                reasons=[payload.reason],
            )
        else:
            record = applications.approve_application(
                session,
                application=application,
                decided_by_id=current_user.id,
                conditions=payload.conditions if payload.decision.endswith("conditions") else None,
                reason=payload.reason,
            )

    return respond(ApplicationOut.model_validate(record, from_attributes=True))


@api_v1_bp.post("/applications/<id:application_id>/withdraw", endpoint="applications_withdraw")
def withdraw_application(application_id: str) -> Response:
    require(Perm.APPLICATION_MANAGE)
    payload = parse_body(ApplicationWithdraw)
    org_id = require_org_scope()

    with transaction() as session:
        application = _application_or_404(application_id, org_id)
        record = applications.withdraw_application(
            session, application=application, reason=payload.reason
        )

    return respond(ApplicationOut.model_validate(record, from_attributes=True))


@api_v1_bp.post("/applications/<id:application_id>/lease", endpoint="applications_convert")
def convert_application(application_id: str) -> Response:
    """Turn an approved application into a lease.

    Refuses anything not approved, and refuses an approval that has lapsed: one
    granted four months ago was granted against circumstances that have moved.
    """
    require(Perm.LEASE_CREATE)
    payload = parse_body(LeaseFromApplication)
    org_id = require_org_scope()

    with transaction() as session:
        application = _application_or_404(application_id, org_id)
        lease = applications.convert_to_lease(
            session,
            application=application,
            start_date=payload.start_date,
            end_date=payload.end_date,
            rent_amount=payload.rent_amount,
            security_deposit=payload.security_deposit,
            actor_id=current_user.id,
        )

    return respond_created(
        LeaseOut.model_validate(lease, from_attributes=True),
        location=f"/api/v1/leases/{lease.id}",
    )

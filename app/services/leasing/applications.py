"""Applications and screening.

This is the part of a property system with the most law attached to it, and the
law is mostly about *evidence*. Three rules follow, and each is enforced rather
than documented.

**No screening without recorded consent.** Running a consumer report on somebody
who has not agreed to it is an FCRA violation, and "we always ask" is not a
defence. The consent timestamp and the address it came from are required before
a screening can be requested, per applicant, every time.

**No adverse decision without stated reasons.** A denial with an empty reason
field is indefensible under fair housing, and the reasons are also what an
adverse-action notice has to contain. So a denial without them is refused at
the service, not caught in review.

**The criteria are snapshotted at the decision.** "Why was this application
denied?" has to be answerable against the policy in force *then*, not against
whatever the thresholds have since become. The snapshot goes on the application
and is never rewritten.

One thing deliberately absent: this module does not decide. It computes whether
an application meets the stated criteria and recommends, and a person approves
or denies. An automated denial is a denial nobody can explain in a hearing.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import BusinessRuleViolation, NotFound, ValidationFailed
from app.logging import get_logger
from app.models.audit import AuditAction, AuditOutcome, AuditSeverity
from app.models.leasing import (
    Applicant,
    ApplicantRole,
    Application,
    ApplicationStatus,
    ScreeningRecommendation,
    ScreeningResult,
    ScreeningStatus,
)
from app.models.sequences import SequenceKey
from app.models.types import quantize_money, utcnow
from app.services.audit.recorder import record_audit_event
from app.services.common.numbering import next_number

__all__ = [
    "DEFAULT_CRITERIA",
    "ApplicationAssessment",
    "ScreeningCriteria",
    "approve_application",
    "assess_application",
    "convert_to_lease",
    "create_application",
    "deny_application",
    "record_consent",
    "record_screening",
    "request_screening",
    "submit_application",
]

log = get_logger("services.leasing.applications")

ZERO = Decimal("0")

#: A screening report older than this is not evidence about today.
SCREENING_VALIDITY = dt.timedelta(days=90)

#: How long an approved application holds a unit before it lapses.
APPLICATION_VALIDITY = dt.timedelta(days=30)


@dataclass(frozen=True)
class ScreeningCriteria:
    """The thresholds an application is measured against.

    Snapshotted onto the application when it is decided, so the decision can be
    explained against the criteria that were actually in force.
    """

    minimum_income_ratio: Decimal = Decimal("3.0")
    minimum_credit_score: int = 620
    #: Eviction history is a decline in most policies; criminal history is
    #: deliberately *not* an automatic decline, because a blanket bar has been
    #: found to violate fair-housing law in several jurisdictions. It routes to
    #: review, where an individual assessment can be recorded.
    evictions_decline: bool = True
    criminal_history_declines: bool = False
    guarantor_income_ratio: Decimal = Decimal("5.0")

    def as_snapshot(self) -> dict:
        return {
            "minimum_income_ratio": str(self.minimum_income_ratio),
            "minimum_credit_score": self.minimum_credit_score,
            "evictions_decline": self.evictions_decline,
            "criminal_history_declines": self.criminal_history_declines,
            "guarantor_income_ratio": str(self.guarantor_income_ratio),
        }


DEFAULT_CRITERIA = ScreeningCriteria()


@dataclass
class ApplicationAssessment:
    """What the criteria say, and why. Not a decision."""

    recommendation: ScreeningRecommendation
    reasons: list[str] = field(default_factory=list)
    income_ratio: Decimal | None = None
    lowest_credit_score: int | None = None
    missing: list[str] = field(default_factory=list)

    @property
    def is_decidable(self) -> bool:
        """Whether there is enough evidence to decide at all."""
        return not self.missing


# ---------------------------------------------------------------------------
# Creating and submitting
# ---------------------------------------------------------------------------


def create_application(
    session: Session,
    *,
    org_id: str,
    property_id: str,
    unit_id: str | None = None,
    lead_id: str | None = None,
    desired_move_in: dt.date | None = None,
    lease_term_months: int = 12,
    quoted_rent: Decimal | None = None,
    application_fee: Decimal | None = None,
    actor_id: str | None = None,
) -> Application:
    """Open an application. It starts as a draft and holds nothing."""
    if quoted_rent is not None and quoted_rent < ZERO:
        raise ValidationFailed("A quoted rent cannot be negative.")

    application = Application(
        org_id=org_id,
        application_number=next_number(session, SequenceKey.APPLICATION, org_id=org_id),
        lead_id=lead_id,
        property_id=property_id,
        unit_id=unit_id,
        status=ApplicationStatus.DRAFT,
        desired_move_in=desired_move_in,
        lease_term_months=lease_term_months,
        quoted_rent=quantize_money(quoted_rent) if quoted_rent is not None else None,
        application_fee=quantize_money(application_fee) if application_fee is not None else None,
    )
    session.add(application)
    session.flush()

    record_audit_event(
        action=AuditAction.APPLICATION_SUBMITTED,
        resource_type="Application",
        resource_id=application.id,
        resource_label=application.application_number,
        payload={"status": str(application.status), "property_id": property_id},
        reason="Application opened.",
        org_id=org_id,
        actor_id=actor_id,
        session=session,
    )
    return application


def add_applicant(
    session: Session,
    *,
    application: Application,
    first_name: str,
    last_name: str,
    role: ApplicantRole = ApplicantRole.PRIMARY,
    email: str | None = None,
    phone: str | None = None,
    monthly_income: Decimal | None = None,
    employer_name: str | None = None,
    resident_id: str | None = None,
) -> Applicant:
    if application.is_decided:
        raise BusinessRuleViolation("A decided application cannot take new applicants.")
    if not first_name or not last_name:
        raise ValidationFailed("An applicant needs a first and last name.")

    applicant = Applicant(
        org_id=application.org_id,
        application_id=application.id,
        role=role,
        first_name=first_name[:100],
        last_name=last_name[:100],
        email=(email or "").strip().lower() or None,
        phone=phone,
        monthly_income=quantize_money(monthly_income) if monthly_income is not None else None,
        employer_name=employer_name,
        resident_id=resident_id,
    )
    session.add(applicant)
    session.flush()
    return applicant


def record_consent(
    session: Session, *, applicant: Applicant, ip_address: str, at: dt.datetime | None = None
) -> Applicant:
    """Record that this person agreed to be screened.

    The address is kept because "they consented" without a record of when and
    from where is the same as no consent at all when it is challenged.
    """
    if not ip_address or not ip_address.strip():
        raise ValidationFailed("Consent must record where it was given from.")

    applicant.screening_consent_at = at or utcnow()
    applicant.screening_consent_ip = ip_address.strip()[:45]
    session.flush()

    record_audit_event(
        action=AuditAction.SCREENING_COMPLETED,
        resource_type="Applicant",
        resource_id=applicant.id,
        resource_label=f"{applicant.first_name} {applicant.last_name}",
        severity=AuditSeverity.NOTICE,
        payload={"consent": True, "ip": applicant.screening_consent_ip},
        reason="Applicant consented to screening.",
        org_id=applicant.org_id,
        session=session,
    )
    return applicant


def submit_application(
    session: Session, *, application: Application, actor_id: str | None = None
) -> Application:
    """Move a draft into the pipeline."""
    if application.status != ApplicationStatus.DRAFT:
        raise BusinessRuleViolation(
            f"A {application.status} application cannot be submitted again."
        )
    if not application.applicants:
        raise ValidationFailed("An application needs at least one applicant.")
    if not any(a.role == ApplicantRole.PRIMARY for a in application.applicants):
        raise ValidationFailed("An application needs a primary applicant.")

    application.status = ApplicationStatus.SUBMITTED
    application.submitted_at = utcnow()
    application.expires_at = utcnow() + APPLICATION_VALIDITY
    session.flush()

    record_audit_event(
        action=AuditAction.APPLICATION_SUBMITTED,
        resource_type="Application",
        resource_id=application.id,
        resource_label=application.application_number,
        payload={"applicants": len(application.applicants)},
        reason="Application submitted.",
        org_id=application.org_id,
        actor_id=actor_id,
        session=session,
    )
    return application


# ---------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------


def request_screening(
    session: Session,
    *,
    application: Application,
    applicant: Applicant,
    provider: str = "manual",
) -> ScreeningResult:
    """Open a screening request. Refuses without recorded consent.

    This is the check the whole module exists to make unbypassable: a consumer
    report pulled without consent is a statutory violation, and the only place
    it can be reliably prevented is before the request is made.
    """
    if applicant.screening_consent_at is None:
        record_audit_event(
            action=AuditAction.SCREENING_COMPLETED,
            resource_type="Applicant",
            resource_id=applicant.id,
            outcome=AuditOutcome.DENIED,
            severity=AuditSeverity.CRITICAL,
            payload={"reason": "no recorded consent"},
            reason="Screening refused: the applicant has not consented.",
            org_id=applicant.org_id,
            session=session,
        )
        raise BusinessRuleViolation(
            f"{applicant.first_name} {applicant.last_name} has not consented to screening. "
            "Record consent before requesting a report."
        )
    if applicant.application_id != application.id:
        raise ValidationFailed("That applicant is not on this application.")

    screening = ScreeningResult(
        org_id=application.org_id,
        application_id=application.id,
        applicant_id=applicant.id,
        provider=provider,
        status=ScreeningStatus.PENDING,
        requested_at=utcnow(),
    )
    session.add(screening)

    if application.status == ApplicationStatus.SUBMITTED:
        application.status = ApplicationStatus.SCREENING
    session.flush()
    return screening


def record_screening(
    session: Session,
    *,
    screening: ScreeningResult,
    recommendation: ScreeningRecommendation,
    credit_score: int | None = None,
    has_eviction_history: bool | None = None,
    has_criminal_record: bool | None = None,
    verified_monthly_income: Decimal | None = None,
    factors: list | None = None,
    provider_reference: str | None = None,
) -> ScreeningResult:
    """Record what the report said.

    ``factors`` are the structured reasons an adverse-action notice must carry.
    A decline with no factors is recorded but flagged, because a notice cannot
    be written from it.
    """
    if screening.status == ScreeningStatus.COMPLETED:
        raise BusinessRuleViolation("That screening has already been recorded.")
    if credit_score is not None and not 300 <= credit_score <= 900:
        raise ValidationFailed("That is not a credit score.")

    screening.status = ScreeningStatus.COMPLETED
    screening.recommendation = recommendation
    screening.credit_score = credit_score
    screening.has_eviction_history = has_eviction_history
    screening.has_criminal_record = has_criminal_record
    screening.income_verified = verified_monthly_income is not None
    screening.verified_monthly_income = (
        quantize_money(verified_monthly_income) if verified_monthly_income is not None else None
    )
    screening.factors = factors or []
    screening.provider_reference = provider_reference
    screening.completed_at = utcnow()
    screening.expires_at = utcnow() + SCREENING_VALIDITY
    session.flush()

    if recommendation == ScreeningRecommendation.DECLINE and not screening.factors:
        log.warning(
            "a declining screening carries no factors, so no adverse-action notice "
            "can be written from it",
            extra={"event": "screening.no_factors", "screening_id": screening.id},
        )

    application = session.get(Application, screening.application_id)
    if application is not None and application.status == ApplicationStatus.SCREENING:
        outstanding = [
            result
            for result in application.screenings
            if result.status in (ScreeningStatus.PENDING, ScreeningStatus.NOT_STARTED)
        ]
        if not outstanding:
            application.status = ApplicationStatus.PENDING_REVIEW
        session.flush()

    record_audit_event(
        action=AuditAction.SCREENING_COMPLETED,
        resource_type="ScreeningResult",
        resource_id=screening.id,
        severity=AuditSeverity.NOTICE,
        payload={
            "recommendation": str(recommendation),
            "credit_score": credit_score,
            "factors": screening.factors,
        },
        reason="Screening result recorded.",
        org_id=screening.org_id,
        session=session,
    )
    return screening


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------


def assess_application(
    session: Session,
    *,
    application: Application,
    criteria: ScreeningCriteria = DEFAULT_CRITERIA,
) -> ApplicationAssessment:
    """Measure the application against the criteria. Recommends; never decides.

    Reports what is *missing* as well as what fails, because an application
    denied for want of a document is a different conversation from one denied
    on its merits.
    """
    assessment = ApplicationAssessment(recommendation=ScreeningRecommendation.REVIEW)
    #: Tracked separately from the recommendation, because starting the
    #: recommendation at REVIEW and testing it later can never reach DECLINE.
    needs_individual_assessment = False

    rent = application.quoted_rent
    if rent is None or rent <= ZERO:
        assessment.missing.append("a quoted rent to measure income against")
    else:
        income = _verified_income(application)
        if income is None:
            assessment.missing.append("verified income for the applicants")
        else:
            assessment.income_ratio = (income / rent).quantize(Decimal("0.01"))
            has_guarantor = any(a.role == ApplicantRole.GUARANTOR for a in application.applicants)
            required = (
                criteria.guarantor_income_ratio if has_guarantor else criteria.minimum_income_ratio
            )
            if assessment.income_ratio < required:
                assessment.reasons.append(
                    f"Income is {assessment.income_ratio}× the rent, below the "
                    f"{required}× required."
                )

    completed = [s for s in application.screenings if s.status == ScreeningStatus.COMPLETED]
    if not completed:
        assessment.missing.append("a completed screening")
    else:
        stale = [s for s in completed if not s.is_usable]
        if stale:
            assessment.missing.append("an in-date screening (the report on file has expired)")

        scores = [s.credit_score for s in completed if s.credit_score is not None]
        if scores:
            assessment.lowest_credit_score = min(scores)
            if assessment.lowest_credit_score < criteria.minimum_credit_score:
                assessment.reasons.append(
                    f"Credit score {assessment.lowest_credit_score} is below the "
                    f"{criteria.minimum_credit_score} required."
                )

        if criteria.evictions_decline and any(s.has_eviction_history for s in completed):
            assessment.reasons.append("An applicant has eviction history.")

        if any(s.has_criminal_record for s in completed):
            if criteria.criminal_history_declines:
                assessment.reasons.append("An applicant has criminal history.")
            else:
                # Deliberately review rather than decline: a blanket bar has
                # been held to violate fair-housing law, and an individualised
                # assessment has to be recorded by a person.
                assessment.reasons.append(
                    "An applicant has criminal history, which requires an "
                    "individual assessment rather than an automatic decline."
                )
                needs_individual_assessment = True

        if any(s.recommendation == ScreeningRecommendation.DECLINE for s in completed):
            assessment.reasons.append("The screening provider recommends declining.")

    if assessment.missing:
        assessment.recommendation = ScreeningRecommendation.REVIEW
        return assessment

    if not assessment.reasons:
        assessment.recommendation = ScreeningRecommendation.APPROVE
        assessment.reasons.append("Meets every stated criterion.")
    elif needs_individual_assessment:
        assessment.recommendation = ScreeningRecommendation.REVIEW
    else:
        assessment.recommendation = ScreeningRecommendation.DECLINE
    return assessment


def _verified_income(application: Application) -> Decimal | None:
    """Verified income where a report gives it, stated income otherwise."""
    verified = {
        screening.applicant_id: screening.verified_monthly_income
        for screening in application.screenings
        if screening.verified_monthly_income is not None
    }
    total = ZERO
    seen = False
    for applicant in application.applicants:
        if applicant.role == ApplicantRole.OCCUPANT:
            continue  # An occupant is not financially responsible.
        amount = verified.get(applicant.id, applicant.monthly_income)
        if amount is not None:
            total += amount
            seen = True
    return quantize_money(total) if seen else None


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def approve_application(
    session: Session,
    *,
    application: Application,
    decided_by_id: str,
    criteria: ScreeningCriteria = DEFAULT_CRITERIA,
    conditions: dict | None = None,
    reason: str | None = None,
) -> Application:
    """Approve, optionally with conditions."""
    _assert_decidable(application, decided_by_id)

    application.status = (
        ApplicationStatus.CONDITIONALLY_APPROVED if conditions else ApplicationStatus.APPROVED
    )
    application.decision_conditions = conditions or {}
    application.decision_reason = reason
    application.decided_at = utcnow()
    application.decided_by_id = decided_by_id
    application.approval_policy_snapshot = criteria.as_snapshot()
    application.expires_at = utcnow() + APPLICATION_VALIDITY
    session.flush()

    record_audit_event(
        action=AuditAction.APPLICATION_DECIDED,
        resource_type="Application",
        resource_id=application.id,
        resource_label=application.application_number,
        severity=AuditSeverity.NOTICE,
        payload={
            "decision": str(application.status),
            "conditions": application.decision_conditions,
            "criteria": application.approval_policy_snapshot,
        },
        reason=reason or "Application approved.",
        org_id=application.org_id,
        actor_id=decided_by_id,
        session=session,
    )
    return application


def deny_application(
    session: Session,
    *,
    application: Application,
    decided_by_id: str,
    reasons: list[str],
    criteria: ScreeningCriteria = DEFAULT_CRITERIA,
) -> Application:
    """Deny, with the reasons an adverse-action notice needs.

    Refused without them. A denial nobody can explain is indefensible under
    fair housing, and the reasons are the notice's required content - so
    "record them later" is not an option the service offers.
    """
    _assert_decidable(application, decided_by_id)

    stated = [reason.strip() for reason in (reasons or []) if reason and reason.strip()]
    if not stated:
        raise ValidationFailed(
            "A denial must state its reasons. They are the content of the "
            "adverse-action notice, and the basis of any fair-housing review."
        )

    application.status = ApplicationStatus.DENIED
    application.decision_reason = "; ".join(stated)[:4000]
    application.decided_at = utcnow()
    application.decided_by_id = decided_by_id
    # Snapshotted so "why was this denied" answers against the thresholds in
    # force at the decision, not against whatever they later become.
    application.approval_policy_snapshot = criteria.as_snapshot()
    session.flush()

    record_audit_event(
        action=AuditAction.APPLICATION_DECIDED,
        resource_type="Application",
        resource_id=application.id,
        resource_label=application.application_number,
        severity=AuditSeverity.NOTICE,
        outcome=AuditOutcome.DENIED,
        payload={"reasons": stated, "criteria": application.approval_policy_snapshot},
        reason=application.decision_reason,
        org_id=application.org_id,
        actor_id=decided_by_id,
        session=session,
    )
    return application


def _assert_decidable(application: Application, decided_by_id: str) -> None:
    if not decided_by_id:
        raise ValidationFailed("A decision must be attributed to a person.")
    if application.is_decided:
        raise BusinessRuleViolation(
            f"This application was already {application.status} on "
            f"{application.decided_at:%Y-%m-%d}."
        )
    if application.status == ApplicationStatus.DRAFT:
        raise BusinessRuleViolation("A draft application has not been submitted yet.")


def withdraw_application(
    session: Session, *, application: Application, reason: str | None = None
) -> Application:
    if application.is_decided:
        raise BusinessRuleViolation("A decided application cannot be withdrawn.")
    application.status = ApplicationStatus.WITHDRAWN
    application.decision_reason = reason
    application.decided_at = utcnow()
    session.flush()
    return application


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def convert_to_lease(
    session: Session,
    *,
    application: Application,
    start_date: dt.date,
    end_date: dt.date,
    rent_amount: Decimal | None = None,
    security_deposit: Decimal | None = None,
    actor_id: str | None = None,
):  # noqa: ANN201
    """Turn an approved application into a lease.

    Refuses anything not approved, and refuses an approval that has lapsed -
    an application approved four months ago was approved against circumstances
    that have since changed.
    """
    from app.models.leasing import Lease, LeaseStatus

    if application.status not in (
        ApplicationStatus.APPROVED,
        ApplicationStatus.CONDITIONALLY_APPROVED,
    ):
        raise BusinessRuleViolation(f"A {application.status} application cannot become a lease.")
    if application.expires_at is not None and application.expires_at <= utcnow():
        raise BusinessRuleViolation(
            "That approval has lapsed. The circumstances it was granted against "
            "are no longer current, so it needs deciding again."
        )
    if application.lease_id is not None:
        raise BusinessRuleViolation("That application has already become a lease.")
    if end_date <= start_date:
        raise ValidationFailed("A lease must end after it starts.")

    rent = rent_amount if rent_amount is not None else application.quoted_rent
    if rent is None or rent <= ZERO:
        raise ValidationFailed("A lease needs a rent.")

    lease = Lease(
        org_id=application.org_id,
        lease_number=next_number(session, SequenceKey.LEASE, org_id=application.org_id),
        property_id=application.property_id,
        unit_id=application.unit_id,
        status=LeaseStatus.DRAFT,
        start_date=start_date,
        end_date=end_date,
        rent_amount=quantize_money(rent),
        security_deposit=quantize_money(security_deposit or rent),
    )
    session.add(lease)
    session.flush()

    application.lease_id = lease.id
    application.status = ApplicationStatus.CONVERTED
    session.flush()

    record_audit_event(
        action=AuditAction.LEASE_CREATED,
        resource_type="Lease",
        resource_id=lease.id,
        resource_label=lease.lease_number,
        severity=AuditSeverity.NOTICE,
        payload={
            "from_application": application.application_number,
            "rent": str(lease.rent_amount),
        },
        reason="Lease created from an approved application.",
        org_id=application.org_id,
        actor_id=actor_id,
        session=session,
    )
    return lease


def expire_stale_applications(session: Session, *, org_id: str) -> int:
    """Lapse applications nobody progressed. Idempotent."""
    stale = (
        session.execute(
            select(Application).where(
                Application.org_id == org_id,
                Application.status.in_(
                    [
                        ApplicationStatus.SUBMITTED,
                        ApplicationStatus.SCREENING,
                        ApplicationStatus.PENDING_REVIEW,
                        ApplicationStatus.APPROVED,
                        ApplicationStatus.CONDITIONALLY_APPROVED,
                    ]
                ),
                Application.expires_at.is_not(None),
                Application.expires_at <= utcnow(),
            )
        )
        .scalars()
        .all()
    )
    for application in stale:
        application.status = ApplicationStatus.EXPIRED
    if stale:
        session.flush()
    return len(stale)


def application_by_number(session: Session, *, org_id: str, number: str) -> Application:
    application = session.execute(
        select(Application).where(
            Application.org_id == org_id,
            Application.application_number == number,
            Application.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if application is None:
        raise NotFound(f"No application numbered {number!r}.")
    return application

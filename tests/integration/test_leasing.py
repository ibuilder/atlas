"""Applications, screening, renewals, and move-outs.

The tests that matter are the refusals, because this is the part of the system
with the most law attached: screening without consent, denying without reasons,
withholding more than is held, honouring a lapsed offer.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.errors import BusinessRuleViolation, ValidationFailed
from app.models.leasing import (
    ApplicantRole,
    ApplicationStatus,
    LeaseStatus,
    ScreeningRecommendation,
)
from app.models.types import utcnow
from app.services.leasing.applications import (
    ScreeningCriteria,
    add_applicant,
    approve_application,
    assess_application,
    convert_to_lease,
    create_application,
    deny_application,
    expire_stale_applications,
    record_consent,
    record_screening,
    request_screening,
    submit_application,
    withdraw_application,
)
from app.services.leasing.tenancy import (
    Deduction,
    accept_renewal,
    decline_renewal,
    deductions_from_inspection,
    expire_stale_offers,
    give_notice,
    offer_renewal,
    overdue_dispositions,
    record_move_out,
    settle_deposit,
)

pytestmark = pytest.mark.integration

DECIDER = "019fea00-0000-7000-8000-0000000000d1"
TODAY = utcnow().date()


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------


@pytest.fixture()
def application(db, org, scope, property_record, unit_record):
    record = create_application(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        unit_id=unit_record.id,
        quoted_rent=Decimal("2000.00"),
        desired_move_in=TODAY + dt.timedelta(days=30),
    )
    db.session.commit()
    return record


def _applicant(db, application, *, income="7000.00", role=ApplicantRole.PRIMARY, name="Dana"):
    person = add_applicant(
        db.session,
        application=application,
        first_name=name,
        last_name="Okafor",
        role=role,
        monthly_income=Decimal(income) if income else None,
    )
    db.session.commit()
    return person


def _consented(db, application, **kwargs):
    person = _applicant(db, application, **kwargs)
    record_consent(db.session, applicant=person, ip_address="203.0.113.9")
    db.session.commit()
    return person


def test_an_application_starts_as_a_draft(db, org, scope, application):
    assert application.status == ApplicationStatus.DRAFT
    assert application.application_number


def test_submitting_needs_a_primary_applicant(db, org, scope, application):
    _applicant(db, application, role=ApplicantRole.OCCUPANT)
    with pytest.raises(ValidationFailed):
        submit_application(db.session, application=application)


def test_submitting_an_empty_application_is_refused(db, org, scope, application):
    with pytest.raises(ValidationFailed):
        submit_application(db.session, application=application)


def test_a_submitted_application_gets_an_expiry(db, org, scope, application):
    _applicant(db, application)
    submit_application(db.session, application=application)
    db.session.commit()

    assert application.status == ApplicationStatus.SUBMITTED
    assert application.expires_at > utcnow()


# ------------------------------------------------------------------- consent


def test_screening_without_consent_is_refused(db, org, scope, application):
    """A consumer report pulled without consent is a statutory violation."""
    person = _applicant(db, application)
    submit_application(db.session, application=application)
    db.session.commit()

    with pytest.raises(BusinessRuleViolation) as exc:
        request_screening(db.session, application=application, applicant=person)
    assert "not consented" in str(exc.value)


def test_that_refusal_is_audited_as_critical(db, org, scope, application):
    from app.models.audit import AuditEvent, AuditSeverity

    person = _applicant(db, application)
    submit_application(db.session, application=application)
    db.session.commit()

    with pytest.raises(BusinessRuleViolation):
        request_screening(db.session, application=application, applicant=person)
    db.session.commit()

    assert [
        event
        for event in db.session.query(AuditEvent).all()
        if event.severity == AuditSeverity.CRITICAL
    ]


def test_consent_records_when_and_from_where(db, org, scope, application):
    """ "They consented" with no record of when is the same as no consent."""
    person = _consented(db, application)
    assert person.screening_consent_at is not None
    assert person.screening_consent_ip == "203.0.113.9"


def test_consent_without_an_address_is_refused(db, org, scope, application):
    person = _applicant(db, application)
    with pytest.raises(ValidationFailed):
        record_consent(db.session, applicant=person, ip_address="   ")


def test_screening_proceeds_once_consent_is_recorded(db, org, scope, application):
    person = _consented(db, application)
    submit_application(db.session, application=application)
    db.session.commit()

    screening = request_screening(db.session, application=application, applicant=person)
    db.session.commit()
    assert application.status == ApplicationStatus.SCREENING
    assert screening.requested_at is not None


# ----------------------------------------------------------------- screening


def _screened(db, application, person, **kwargs):
    screening = request_screening(db.session, application=application, applicant=person)
    params = {
        "recommendation": ScreeningRecommendation.APPROVE,
        "credit_score": 720,
        "has_eviction_history": False,
        "has_criminal_record": False,
        "verified_monthly_income": Decimal("7000.00"),
    }
    params.update(kwargs)
    result = record_screening(db.session, screening=screening, **params)
    db.session.commit()
    return result


def test_a_completed_screening_moves_the_application_to_review(db, org, scope, application):
    person = _consented(db, application)
    submit_application(db.session, application=application)
    db.session.commit()
    _screened(db, application, person)

    assert application.status == ApplicationStatus.PENDING_REVIEW


def test_a_screening_cannot_be_recorded_twice(db, org, scope, application):
    person = _consented(db, application)
    submit_application(db.session, application=application)
    db.session.commit()
    screening = request_screening(db.session, application=application, applicant=person)
    record_screening(
        db.session, screening=screening, recommendation=ScreeningRecommendation.APPROVE
    )
    db.session.commit()

    with pytest.raises(BusinessRuleViolation):
        record_screening(
            db.session, screening=screening, recommendation=ScreeningRecommendation.DECLINE
        )


def test_an_impossible_credit_score_is_refused(db, org, scope, application):
    person = _consented(db, application)
    submit_application(db.session, application=application)
    db.session.commit()
    screening = request_screening(db.session, application=application, applicant=person)

    with pytest.raises(ValidationFailed):
        record_screening(
            db.session,
            screening=screening,
            recommendation=ScreeningRecommendation.APPROVE,
            credit_score=9000,
        )


# ---------------------------------------------------------------- assessment


def _ready(db, application, **screening_kwargs):
    person = _consented(db, application)
    submit_application(db.session, application=application)
    db.session.commit()
    _screened(db, application, person, **screening_kwargs)
    return person


def test_a_clean_application_is_recommended_for_approval(db, org, scope, application):
    _ready(db, application)
    assessment = assess_application(db.session, application=application)

    assert assessment.recommendation == ScreeningRecommendation.APPROVE
    assert assessment.income_ratio == Decimal("3.50")
    assert assessment.is_decidable


def test_thin_income_is_a_decline_with_the_arithmetic(db, org, scope, application):
    _ready(db, application, verified_monthly_income=Decimal("4000.00"))
    assessment = assess_application(db.session, application=application)

    assert assessment.recommendation == ScreeningRecommendation.DECLINE
    assert assessment.income_ratio == Decimal("2.00")
    assert any("below the" in reason for reason in assessment.reasons)


def test_a_guarantor_raises_the_income_bar(db, org, scope, application):
    """A guarantor exists because the applicant is thin, so the bar goes up."""
    person = _consented(db, application)
    _applicant(db, application, role=ApplicantRole.GUARANTOR, income="1000.00", name="Guy")
    submit_application(db.session, application=application)
    db.session.commit()
    _screened(db, application, person, verified_monthly_income=Decimal("7000.00"))

    assessment = assess_application(db.session, application=application)
    assert assessment.recommendation == ScreeningRecommendation.DECLINE


def test_an_occupant_does_not_count_towards_income(db, org, scope, application):
    person = _consented(db, application)
    _applicant(db, application, role=ApplicantRole.OCCUPANT, income="9000.00", name="Kid")
    submit_application(db.session, application=application)
    db.session.commit()
    _screened(db, application, person, verified_monthly_income=Decimal("4000.00"))

    assessment = assess_application(db.session, application=application)
    assert assessment.income_ratio == Decimal("2.00")


def test_a_low_credit_score_is_a_reason(db, org, scope, application):
    _ready(db, application, credit_score=540)
    assessment = assess_application(db.session, application=application)

    assert assessment.lowest_credit_score == 540
    assert any("Credit score 540" in reason for reason in assessment.reasons)


def test_eviction_history_declines(db, org, scope, application):
    _ready(db, application, has_eviction_history=True)
    assessment = assess_application(db.session, application=application)
    assert assessment.recommendation == ScreeningRecommendation.DECLINE


def test_criminal_history_routes_to_review_rather_than_declining(db, org, scope, application):
    """A blanket bar has been held to violate fair housing; a person decides."""
    _ready(db, application, has_criminal_record=True)
    assessment = assess_application(db.session, application=application)

    assert assessment.recommendation == ScreeningRecommendation.REVIEW
    assert any("individual assessment" in reason for reason in assessment.reasons)


def test_criminal_history_can_decline_where_policy_says_so(db, org, scope, application):
    _ready(db, application, has_criminal_record=True)
    strict = ScreeningCriteria(criminal_history_declines=True)
    assessment = assess_application(db.session, application=application, criteria=strict)
    assert assessment.recommendation == ScreeningRecommendation.DECLINE


def test_a_missing_screening_is_reported_as_missing_not_failed(db, org, scope, application):
    """Denied for want of a document is a different conversation from denied on merit."""
    _applicant(db, application)
    submit_application(db.session, application=application)
    db.session.commit()

    assessment = assess_application(db.session, application=application)
    assert not assessment.is_decidable
    assert any("screening" in item for item in assessment.missing)


def test_a_stale_screening_is_not_evidence_about_today(db, org, scope, application):
    _ready(db, application)
    screening = application.screenings[0]
    screening.expires_at = utcnow() - dt.timedelta(days=1)
    db.session.commit()

    assessment = assess_application(db.session, application=application)
    assert any("in-date" in item for item in assessment.missing)


# ------------------------------------------------------------------ deciding


def test_approving_snapshots_the_criteria(db, org, scope, application):
    """ "Why was this decided so" must answer against the thresholds then."""
    _ready(db, application)
    approve_application(db.session, application=application, decided_by_id=DECIDER)
    db.session.commit()

    assert application.status == ApplicationStatus.APPROVED
    assert application.approval_policy_snapshot["minimum_credit_score"] == 620
    assert application.decided_by_id == DECIDER


def test_approving_with_conditions_is_a_different_status(db, org, scope, application):
    _ready(db, application)
    approve_application(
        db.session,
        application=application,
        decided_by_id=DECIDER,
        conditions={"additional_deposit": "1000.00"},
    )
    db.session.commit()

    assert application.status == ApplicationStatus.CONDITIONALLY_APPROVED
    assert application.decision_conditions["additional_deposit"] == "1000.00"


def test_a_denial_without_reasons_is_refused(db, org, scope, application):
    """The reasons are the adverse-action notice. There is no "record later"."""
    _ready(db, application)
    with pytest.raises(ValidationFailed) as exc:
        deny_application(db.session, application=application, decided_by_id=DECIDER, reasons=[])
    assert "adverse-action" in str(exc.value)


def test_blank_reasons_do_not_count(db, org, scope, application):
    _ready(db, application)
    with pytest.raises(ValidationFailed):
        deny_application(
            db.session, application=application, decided_by_id=DECIDER, reasons=["  ", ""]
        )


def test_a_denial_records_its_reasons(db, org, scope, application):
    _ready(db, application, credit_score=520)
    deny_application(
        db.session,
        application=application,
        decided_by_id=DECIDER,
        reasons=["Credit score below the stated minimum", "Insufficient verified income"],
    )
    db.session.commit()

    assert application.status == ApplicationStatus.DENIED
    assert "Credit score below" in application.decision_reason
    assert application.approval_policy_snapshot


def test_an_unattributed_decision_is_refused(db, org, scope, application):
    _ready(db, application)
    with pytest.raises(ValidationFailed):
        approve_application(db.session, application=application, decided_by_id="")


def test_a_decided_application_cannot_be_decided_again(db, org, scope, application):
    _ready(db, application)
    approve_application(db.session, application=application, decided_by_id=DECIDER)
    db.session.commit()

    with pytest.raises(BusinessRuleViolation):
        deny_application(
            db.session, application=application, decided_by_id=DECIDER, reasons=["Changed mind"]
        )


def test_a_draft_cannot_be_decided(db, org, scope, application):
    _applicant(db, application)
    with pytest.raises(BusinessRuleViolation):
        approve_application(db.session, application=application, decided_by_id=DECIDER)


def test_withdrawing_is_available_before_a_decision(db, org, scope, application):
    _ready(db, application)
    withdraw_application(db.session, application=application, reason="Took another flat.")
    db.session.commit()
    assert application.status == ApplicationStatus.WITHDRAWN


# ---------------------------------------------------------------- conversion


def test_an_approved_application_becomes_a_lease(db, org, scope, application):
    _ready(db, application)
    approve_application(db.session, application=application, decided_by_id=DECIDER)
    db.session.commit()

    lease = convert_to_lease(
        db.session,
        application=application,
        start_date=TODAY + dt.timedelta(days=30),
        end_date=TODAY + dt.timedelta(days=395),
    )
    db.session.commit()

    assert lease.rent_amount == Decimal("2000.0000")
    assert application.lease_id == lease.id
    assert application.status == ApplicationStatus.CONVERTED


def test_a_deposit_of_zero_is_honoured_rather_than_defaulted(db, org, scope, application):
    """A waived deposit is an answer, not a missing one.

    Treating the falsy zero as "unspecified" writes a month's rent as the
    deposit, and move-out then refunds money the resident never paid.
    """
    _ready(db, application)
    approve_application(db.session, application=application, decided_by_id=DECIDER)
    db.session.commit()

    lease = convert_to_lease(
        db.session,
        application=application,
        start_date=TODAY + dt.timedelta(days=30),
        end_date=TODAY + dt.timedelta(days=395),
        security_deposit=Decimal("0"),
    )
    db.session.commit()

    assert lease.security_deposit == Decimal("0.0000")


def test_an_unspecified_deposit_still_falls_back_to_the_rent(db, org, scope, application):
    _ready(db, application)
    approve_application(db.session, application=application, decided_by_id=DECIDER)
    db.session.commit()

    lease = convert_to_lease(
        db.session,
        application=application,
        start_date=TODAY + dt.timedelta(days=30),
        end_date=TODAY + dt.timedelta(days=395),
    )
    db.session.commit()

    assert lease.security_deposit == lease.rent_amount


def test_a_denied_application_cannot_become_a_lease(db, org, scope, application):
    _ready(db, application)
    deny_application(db.session, application=application, decided_by_id=DECIDER, reasons=["Income"])
    db.session.commit()

    with pytest.raises(BusinessRuleViolation):
        convert_to_lease(
            db.session,
            application=application,
            start_date=TODAY,
            end_date=TODAY + dt.timedelta(days=365),
        )


def test_a_lapsed_approval_cannot_become_a_lease(db, org, scope, application):
    """Approved against circumstances that are four months stale."""
    _ready(db, application)
    approve_application(db.session, application=application, decided_by_id=DECIDER)
    application.expires_at = utcnow() - dt.timedelta(days=1)
    db.session.commit()

    with pytest.raises(BusinessRuleViolation) as exc:
        convert_to_lease(
            db.session,
            application=application,
            start_date=TODAY,
            end_date=TODAY + dt.timedelta(days=365),
        )
    assert "lapsed" in str(exc.value)


def test_converting_twice_is_refused(db, org, scope, application):
    _ready(db, application)
    approve_application(db.session, application=application, decided_by_id=DECIDER)
    db.session.commit()
    convert_to_lease(
        db.session,
        application=application,
        start_date=TODAY,
        end_date=TODAY + dt.timedelta(days=365),
    )
    db.session.commit()

    with pytest.raises(BusinessRuleViolation):
        convert_to_lease(
            db.session,
            application=application,
            start_date=TODAY,
            end_date=TODAY + dt.timedelta(days=365),
        )


def test_stale_applications_expire_idempotently(db, org, scope, application):
    _applicant(db, application)
    submit_application(db.session, application=application)
    application.expires_at = utcnow() - dt.timedelta(days=1)
    db.session.commit()

    assert expire_stale_applications(db.session, org_id=org.id) == 1
    db.session.commit()
    assert expire_stale_applications(db.session, org_id=org.id) == 0
    assert application.status == ApplicationStatus.EXPIRED


# ---------------------------------------------------------------------------
# Renewals
# ---------------------------------------------------------------------------


def _offer(db, lease, rent="2200.00", **kwargs):
    return offer_renewal(
        db.session,
        lease=lease,
        offered_rent=Decimal(rent),
        proposed_start=lease.end_date + dt.timedelta(days=1),
        proposed_end=lease.end_date + dt.timedelta(days=366),
        **kwargs,
    )


def test_an_offer_records_the_increase(db, org, scope, lease_record):
    lease_record.status = LeaseStatus.ACTIVE
    db.session.commit()
    renewal = _offer(db, lease_record)
    db.session.commit()

    assert renewal.status == "offered"
    assert renewal.rent_increase == Decimal("2200.0000") - lease_record.rent_amount


def test_two_open_offers_are_refused(db, org, scope, lease_record):
    """A resident must never be holding two different prices."""
    lease_record.status = LeaseStatus.ACTIVE
    db.session.commit()
    _offer(db, lease_record)
    db.session.commit()

    with pytest.raises(BusinessRuleViolation):
        _offer(db, lease_record, rent="2400.00")


def test_accepting_creates_a_lease_on_the_offered_terms(db, org, scope, lease_record):
    """Not on today's asking rent."""
    lease_record.status = LeaseStatus.ACTIVE
    db.session.commit()
    renewal = _offer(db, lease_record, rent="2200.00")
    db.session.commit()

    new_lease = accept_renewal(db.session, renewal=renewal)
    db.session.commit()

    assert new_lease.rent_amount == Decimal("2200.0000")
    assert renewal.new_lease_id == new_lease.id
    assert lease_record.status == LeaseStatus.RENEWED
    assert renewal.new_lease_id == new_lease.id


def test_a_lapsed_offer_cannot_be_accepted(db, org, scope, lease_record):
    lease_record.status = LeaseStatus.ACTIVE
    db.session.commit()
    renewal = _offer(db, lease_record)
    renewal.offer_expires_at = utcnow() - dt.timedelta(days=1)
    db.session.commit()

    with pytest.raises(BusinessRuleViolation) as exc:
        accept_renewal(db.session, renewal=renewal)
    assert "expired" in str(exc.value)


def test_declining_records_the_reason(db, org, scope, lease_record):
    lease_record.status = LeaseStatus.ACTIVE
    db.session.commit()
    renewal = _offer(db, lease_record)
    decline_renewal(db.session, renewal=renewal, reason="Moving out of the area.")
    db.session.commit()

    assert renewal.status == "declined"
    assert renewal.declined_reason == "Moving out of the area."


def test_a_terminated_lease_cannot_be_renewed(db, org, scope, lease_record):
    lease_record.status = LeaseStatus.TERMINATED
    db.session.commit()
    with pytest.raises(BusinessRuleViolation):
        _offer(db, lease_record)


def test_stale_offers_expire_idempotently(db, org, scope, lease_record):
    lease_record.status = LeaseStatus.ACTIVE
    db.session.commit()
    renewal = _offer(db, lease_record)
    renewal.offer_expires_at = utcnow() - dt.timedelta(days=2)
    db.session.commit()

    assert expire_stale_offers(db.session, org_id=org.id) == 1
    db.session.commit()
    assert expire_stale_offers(db.session, org_id=org.id) == 0


# ---------------------------------------------------------------------------
# Move-out and deposit disposition
# ---------------------------------------------------------------------------


@pytest.fixture()
def trust_account(db, org, scope, accounts):
    from app.models.accounting import BankAccount, BankAccountType
    from app.services.accounting.chart import AccountCode

    record = BankAccount(
        org_id=org.id,
        code="TRUST",
        name="Security deposit trust",
        account_type=BankAccountType.TRUST,
        gl_account_id=accounts[AccountCode.CASH_TRUST].id,
        is_trust=True,
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture()
def move_out(db, org, scope, lease_record, trust_account):
    """Notice given on a lease whose deposit was actually collected.

    Collected through the service rather than assigned, because what notice
    captures is what is *held* - and a lease that specifies a deposit nobody
    took is exactly the case that used to refund money never received.
    """
    from app.services.accounting.deposits import collect_deposit

    lease_record.status = LeaseStatus.ACTIVE
    lease_record.security_deposit = Decimal("2000.00")
    db.session.commit()

    collect_deposit(
        db.session,
        org_id=org.id,
        lease_id=lease_record.id,
        bank_account_id=trust_account.id,
        amount=Decimal("2000.00"),
        effective_date=TODAY - dt.timedelta(days=365),
    )
    db.session.commit()

    record = give_notice(
        db.session,
        lease=lease_record,
        notice_date=TODAY - dt.timedelta(days=90),
        scheduled_date=TODAY - dt.timedelta(days=60),
        reason="End of term.",
    )
    db.session.commit()
    return record


def test_notice_captures_what_is_held(db, org, scope, move_out):
    assert move_out.status == "notice_given"
    assert move_out.deposit_held == Decimal("2000.0000")


def test_notice_twice_is_refused(db, org, scope, lease_record, move_out):
    with pytest.raises(BusinessRuleViolation):
        give_notice(
            db.session,
            lease=lease_record,
            notice_date=TODAY,
            scheduled_date=TODAY + dt.timedelta(days=30),
        )


def test_the_statutory_clock_starts_at_the_move_out(db, org, scope, move_out):
    """Stored, not recomputed: a recomputed deadline drifts with the setting."""
    actual = TODAY - dt.timedelta(days=10)
    record_move_out(db.session, move_out=move_out, actual_date=actual, disposition_days=21)
    db.session.commit()

    assert move_out.disposition_due_by == actual + dt.timedelta(days=21)
    assert move_out.status == "moved_out"


def test_recording_a_move_out_terminates_the_lease(db, org, scope, lease_record, move_out):
    record_move_out(db.session, move_out=move_out, actual_date=TODAY - dt.timedelta(days=10))
    db.session.commit()
    assert lease_record.status == LeaseStatus.TERMINATED


def test_settling_before_the_move_out_is_refused(db, org, scope, move_out):
    with pytest.raises(BusinessRuleViolation):
        settle_deposit(db.session, move_out=move_out, deductions=[], settled_by_id=DECIDER)


def test_a_clean_move_out_returns_everything(db, org, scope, move_out):
    record_move_out(db.session, move_out=move_out, actual_date=TODAY - dt.timedelta(days=5))
    settle_deposit(db.session, move_out=move_out, deductions=[], settled_by_id=DECIDER)
    db.session.commit()

    assert move_out.deposit_deductions == Decimal("0.0000")
    assert move_out.deposit_refunded == Decimal("2000.0000")
    assert move_out.status == "settled"


def test_deductions_reduce_the_refund_and_are_itemised(db, org, scope, move_out):
    record_move_out(db.session, move_out=move_out, actual_date=TODAY - dt.timedelta(days=5))
    settle_deposit(
        db.session,
        move_out=move_out,
        deductions=[
            Deduction(description="Kitchen: scorched worktop", amount=Decimal("380.00")),
            Deduction(description="Carpet cleaning beyond fair wear", amount=Decimal("120.00")),
        ],
        settled_by_id=DECIDER,
    )
    db.session.commit()

    assert move_out.deposit_deductions == Decimal("500.0000")
    assert move_out.deposit_refunded == Decimal("1500.0000")
    assert len(move_out.deduction_detail) == 2
    assert move_out.deduction_detail[0]["description"].startswith("Kitchen")


def test_settling_releases_the_money_from_the_trust(db, org, scope, move_out, trust_account):
    """Accounting for a disposition is not the same as making it.

    Recording the refund on the move-out without releasing the funds leaves the
    trust reconciliation reporting the deposit as still owed to a resident who
    has been paid and left - for ever.
    """
    from app.services.accounting.deposits import deposit_balance

    record_move_out(db.session, move_out=move_out, actual_date=TODAY - dt.timedelta(days=5))
    settle_deposit(
        db.session,
        move_out=move_out,
        deductions=[Deduction(description="Kitchen: scorched worktop", amount=Decimal("380.00"))],
        settled_by_id=DECIDER,
    )
    db.session.commit()

    assert deposit_balance(db.session, org_id=org.id, lease_id=move_out.lease_id) == Decimal(
        "0.0000"
    )


def test_a_deduction_with_no_description_is_refused(db, org, scope, move_out):
    """ "Cleaning - $400" with nothing behind it is what gets disallowed."""
    record_move_out(db.session, move_out=move_out, actual_date=TODAY - dt.timedelta(days=5))
    with pytest.raises(ValidationFailed) as exc:
        settle_deposit(
            db.session,
            move_out=move_out,
            deductions=[Deduction(description="   ", amount=Decimal("400.00"))],
            settled_by_id=DECIDER,
        )
    assert "description" in str(exc.value)


def test_a_deduction_with_no_amount_is_refused(db, org, scope, move_out):
    record_move_out(db.session, move_out=move_out, actual_date=TODAY - dt.timedelta(days=5))
    with pytest.raises(ValidationFailed):
        settle_deposit(
            db.session,
            move_out=move_out,
            deductions=[Deduction(description="Cleaning", amount=Decimal("0"))],
            settled_by_id=DECIDER,
        )


def test_withholding_more_than_is_held_is_refused(db, org, scope, move_out):
    """It is a claim against the resident, not a disposition."""
    record_move_out(db.session, move_out=move_out, actual_date=TODAY - dt.timedelta(days=5))
    with pytest.raises(BusinessRuleViolation) as exc:
        settle_deposit(
            db.session,
            move_out=move_out,
            deductions=[Deduction(description="Extensive damage", amount=Decimal("5000.00"))],
            settled_by_id=DECIDER,
        )
    assert "exceed" in str(exc.value)
    assert move_out.deposit_refunded == Decimal("0.0000")


def test_settling_twice_is_refused(db, org, scope, move_out):
    record_move_out(db.session, move_out=move_out, actual_date=TODAY - dt.timedelta(days=5))
    settle_deposit(db.session, move_out=move_out, deductions=[], settled_by_id=DECIDER)
    db.session.commit()

    with pytest.raises(BusinessRuleViolation):
        settle_deposit(db.session, move_out=move_out, deductions=[], settled_by_id=DECIDER)


def test_a_late_disposition_is_audited_as_critical(db, org, scope, move_out):
    """Past the deadline the deductions are usually forfeit. Worth noticing."""
    from app.models.audit import AuditEvent, AuditSeverity

    record_move_out(
        db.session,
        move_out=move_out,
        actual_date=TODAY - dt.timedelta(days=60),
        disposition_days=21,
    )
    settle_deposit(
        db.session,
        move_out=move_out,
        deductions=[Deduction(description="Cleaning", amount=Decimal("100.00"))],
        settled_by_id=DECIDER,
    )
    db.session.commit()

    critical = [
        event
        for event in db.session.query(AuditEvent).all()
        if event.severity == AuditSeverity.CRITICAL and "AFTER" in (event.reason or "")
    ]
    assert critical


def test_an_on_time_disposition_is_not(db, org, scope, move_out):
    from app.models.audit import AuditEvent

    record_move_out(db.session, move_out=move_out, actual_date=TODAY - dt.timedelta(days=2))
    settle_deposit(db.session, move_out=move_out, deductions=[], settled_by_id=DECIDER)
    db.session.commit()

    assert not [
        event for event in db.session.query(AuditEvent).all() if "AFTER" in (event.reason or "")
    ]


def test_overdue_dispositions_are_reportable(db, org, scope, move_out):
    record_move_out(
        db.session,
        move_out=move_out,
        actual_date=TODAY - dt.timedelta(days=60),
        disposition_days=21,
    )
    db.session.commit()

    overdue = overdue_dispositions(db.session, org_id=org.id)
    assert [record.id for record in overdue] == [move_out.id]


def test_a_settled_disposition_leaves_the_overdue_list(db, org, scope, move_out):
    record_move_out(
        db.session,
        move_out=move_out,
        actual_date=TODAY - dt.timedelta(days=60),
        disposition_days=21,
    )
    settle_deposit(db.session, move_out=move_out, deductions=[], settled_by_id=DECIDER)
    db.session.commit()

    assert overdue_dispositions(db.session, org_id=org.id) == []


def test_deductions_come_from_the_inspection_that_evidenced_them(
    db, org, scope, property_record, unit_record, move_out
):
    """The defensible path: an item, on a checklist, costed at the time."""
    from app.models.maintenance import InspectionKind, InspectionTemplate, ItemResult
    from app.services.maintenance.inspections import (
        ItemFinding,
        record_finding,
        schedule_inspection,
        start_inspection,
    )

    template = InspectionTemplate(
        org_id=org.id,
        code="MO",
        name="Move-out",
        kind=InspectionKind.MOVE_OUT,
        version=1,
        sections=[{"section": "Kitchen", "items": [{"name": "Worktop"}, {"name": "Floor"}]}],
    )
    db.session.add(template)
    db.session.commit()

    inspection = schedule_inspection(
        db.session,
        org_id=org.id,
        kind=InspectionKind.MOVE_OUT,
        property_id=property_record.id,
        unit_id=unit_record.id,
        template=template,
    )
    start_inspection(db.session, inspection=inspection)
    for item in inspection.items:
        record_finding(
            db.session,
            inspection=inspection,
            finding=ItemFinding(
                item_id=item.id,
                result=ItemResult.FAIL if item.name == "Worktop" else ItemResult.PASS,
                notes="Scorch damage." if item.name == "Worktop" else None,
                remedy_cost=Decimal("380.00") if item.name == "Worktop" else None,
                is_resident_responsible=item.name == "Worktop",
            ),
        )
    db.session.commit()

    deductions = deductions_from_inspection(db.session, inspection_id=inspection.id)
    assert len(deductions) == 1
    assert deductions[0].amount == Decimal("380.0000")
    assert "Scorch damage" in deductions[0].description
    assert deductions[0].inspection_item_id


def test_move_outs_do_not_cross_organizations(db, org, other_org, scope, move_out):
    record_move_out(
        db.session,
        move_out=move_out,
        actual_date=TODAY - dt.timedelta(days=60),
        disposition_days=21,
    )
    db.session.commit()
    assert overdue_dispositions(db.session, org_id=other_org.id) == []

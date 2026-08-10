"""Human checkpoints.

The interesting tests are the ones about time and change: an approval that has
gone stale, and a record that moved after somebody approved it.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.errors import (
    ApprovalRequired,
    BusinessRuleViolation,
    PermissionDenied,
    ValidationFailed,
)
from app.models.automation import ApprovalStatus
from app.models.types import utcnow
from app.services.automation.approvals import (
    approve,
    consume_approval,
    expire_stale_approvals,
    payload_fingerprint,
    pending_approvals,
    reject,
    request_approval,
)

pytestmark = pytest.mark.integration

REQUESTER = "11111111-1111-1111-1111-111111111111"
APPROVER = "22222222-2222-2222-2222-222222222222"


def _request(db, org, **overrides):
    params = {
        "kind": "bill_payment",
        "subject_type": "bill",
        "subject_id": "33333333-3333-3333-3333-333333333333",
        "payload": {"vendor": "Acme Plumbing", "amount": "4200.00", "account": "operating"},
        "amount": Decimal("4200.00"),
        "requested_by_id": REQUESTER,
    }
    params.update(overrides)
    approval = request_approval(db.session, org_id=org.id, **params)
    db.session.commit()
    return approval


# ------------------------------------------------------------ separation


def test_a_requester_cannot_approve_their_own_request(db, org, scope):
    approval = _request(db, org)
    with pytest.raises(PermissionDenied):
        approve(db.session, approval=approval, decided_by_id=REQUESTER)
    assert approval.status == ApprovalStatus.PENDING


def test_a_different_person_can_approve(db, org, scope):
    approval = _request(db, org)
    decision = approve(db.session, approval=approval, decided_by_id=APPROVER, note="Verified.")
    db.session.commit()

    assert decision.granted is True
    assert approval.status == ApprovalStatus.APPROVED
    assert approval.decided_by_id == APPROVER
    assert approval.decided_at is not None


def test_an_anonymous_decision_is_refused(db, org, scope):
    """An approval is a moment where a person took responsibility."""
    approval = _request(db, org)
    with pytest.raises(PermissionDenied):
        approve(db.session, approval=approval, decided_by_id="")


def test_a_decided_approval_cannot_be_decided_again(db, org, scope):
    approval = _request(db, org)
    approve(db.session, approval=approval, decided_by_id=APPROVER)
    db.session.commit()
    with pytest.raises(BusinessRuleViolation):
        reject(db.session, approval=approval, decided_by_id=APPROVER, reason="Changed my mind.")


def test_rejection_requires_a_reason(db, org, scope):
    approval = _request(db, org)
    with pytest.raises(ValidationFailed):
        reject(db.session, approval=approval, decided_by_id=APPROVER, reason="   ")


def test_a_rejected_approval_does_not_authorise_anything(db, org, scope):
    approval = _request(db, org)
    reject(db.session, approval=approval, decided_by_id=APPROVER, reason="Vendor is not compliant.")
    db.session.commit()

    assert approval.status == ApprovalStatus.REJECTED
    with pytest.raises(ApprovalRequired):
        consume_approval(db.session, approval=approval)


# ------------------------------------------------------------------ expiry


def test_an_approval_expires_before_it_is_decided(db, org, scope):
    approval = _request(db, org, ttl=dt.timedelta(hours=1))
    approval.expires_at = utcnow() - dt.timedelta(minutes=1)
    db.session.commit()

    with pytest.raises(BusinessRuleViolation):
        approve(db.session, approval=approval, decided_by_id=APPROVER)


def test_an_approval_that_lapses_after_it_was_granted_blocks_the_action(db, org, scope):
    """An approval granted in March does not authorise a payment in September."""
    approval = _request(db, org)
    approve(db.session, approval=approval, decided_by_id=APPROVER)
    approval.expires_at = utcnow() - dt.timedelta(seconds=1)
    db.session.commit()

    with pytest.raises(ApprovalRequired):
        consume_approval(db.session, approval=approval)
    assert approval.status == ApprovalStatus.EXPIRED


def test_the_sweep_expires_stale_requests_and_is_idempotent(db, org, scope):
    approval = _request(db, org)
    approval.expires_at = utcnow() - dt.timedelta(days=1)
    db.session.commit()

    assert expire_stale_approvals(db.session, org_id=org.id) == 1
    db.session.commit()
    assert expire_stale_approvals(db.session, org_id=org.id) == 0
    assert approval.status == ApprovalStatus.EXPIRED


def test_pending_excludes_lapsed_requests(db, org, scope):
    live = _request(db, org)
    lapsed = _request(db, org, subject_id="44444444-4444-4444-4444-444444444444")
    lapsed.expires_at = utcnow() - dt.timedelta(days=1)
    db.session.commit()

    waiting = pending_approvals(db.session, org_id=org.id)
    assert [a.id for a in waiting] == [live.id]


# ------------------------------------------------------------- the snapshot


def test_the_payload_is_snapshotted_when_the_approval_is_raised(db, org, scope):
    approval = _request(db, org)
    assert approval.payload["amount"] == "4200.00"
    assert approval.payload["_fingerprint"]


def test_a_changed_record_no_longer_carries_the_approval(db, org, scope):
    """The approver authorised $4,200. They did not authorise $42,000."""
    approval = _request(db, org)
    approve(db.session, approval=approval, decided_by_id=APPROVER)
    db.session.commit()

    with pytest.raises(ApprovalRequired) as exc:
        consume_approval(
            db.session,
            approval=approval,
            acting_payload={
                "vendor": "Acme Plumbing",
                "amount": "42000.00",
                "account": "operating",
            },
        )
    assert "changed" in str(exc.value).lower()


def test_an_unchanged_record_passes(db, org, scope):
    approval = _request(db, org)
    approve(db.session, approval=approval, decided_by_id=APPROVER)
    db.session.commit()

    consumed = consume_approval(
        db.session,
        approval=approval,
        acting_payload={"vendor": "Acme Plumbing", "amount": "4200.00", "account": "operating"},
    )
    assert consumed.status == ApprovalStatus.APPROVED


def test_the_fingerprint_ignores_key_order(db, org, scope):
    """Otherwise a round-trip through JSON would look like tampering."""
    first = payload_fingerprint({"a": 1, "b": 2})
    second = payload_fingerprint({"b": 2, "a": 1})
    assert first == second


def test_the_fingerprint_notices_a_single_changed_digit(db, org, scope):
    assert payload_fingerprint({"amount": "4200.00"}) != payload_fingerprint({"amount": "4200.01"})


# --------------------------------------------------------------- isolation


def test_approvals_do_not_cross_organizations(db, org, other_org, scope):
    _request(db, org)
    assert pending_approvals(db.session, org_id=other_org.id) == []


def test_the_audit_trail_records_who_decided_and_why(db, org, scope):
    from app.models.audit import AuditAction, AuditEvent

    approval = _request(db, org)
    approve(db.session, approval=approval, decided_by_id=APPROVER, note="Invoice matches the PO.")
    db.session.commit()

    granted = (
        db.session.query(AuditEvent).filter(AuditEvent.action == AuditAction.APPROVAL_GRANTED).one()
    )
    assert granted.actor_id == APPROVER
    assert granted.payload["requested_by"] == REQUESTER
    assert granted.reason == "Invoice matches the PO."

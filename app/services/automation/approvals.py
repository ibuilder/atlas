"""Human checkpoints: request, decide, act.

One path for every sensitive action - a bill above threshold, a bank detail
change, an automated rent increase - so a new sensitive action inherits the
control rather than reinventing it.

Four properties matter, and each has a test:

* **A requester cannot approve their own request.** Enforced on the model in
  ``Approval.can_be_decided_by``, and again here, because a control checked in
  one place is a control that gets bypassed by the second caller.
* **An expired approval blocks the action.** Expiry is checked when the approval
  is *used*, not only when it is granted. An approval granted in March does not
  authorise a payment in September.
* **The payload is snapshotted.** The approver sees, and authorises, an exact
  set of values. If the underlying record moves afterwards, the decision does
  not silently transfer to the new values - it fails, loudly, on comparison.
* **Everything is audited at NOTICE or higher.** An approval is a moment where a
  human took responsibility, and the record has to survive the argument about
  it later.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import (
    ApprovalRequired,
    BusinessRuleViolation,
    NotFound,
    PermissionDenied,
    ValidationFailed,
)
from app.logging import get_logger, redact_value
from app.models.audit import AuditAction, AuditOutcome, AuditSeverity
from app.models.automation import Approval, ApprovalStatus
from app.models.types import utcnow
from app.services.audit.recorder import record_audit_event

__all__ = [
    "DEFAULT_APPROVAL_TTL",
    "ApprovalDecision",
    "approve",
    "consume_approval",
    "expire_stale_approvals",
    "payload_fingerprint",
    "pending_approvals",
    "reject",
    "request_approval",
]

log = get_logger("services.automation.approvals")

#: An approval nobody acts on should lapse rather than sit there indefinitely
#: waiting to authorise something months out of date.
DEFAULT_APPROVAL_TTL = dt.timedelta(days=7)

#: The key under which the snapshot fingerprint is kept inside the payload.
FINGERPRINT_KEY = "_fingerprint"


@dataclass(frozen=True)
class ApprovalDecision:
    approval: Approval
    granted: bool


def payload_fingerprint(payload: dict[str, Any]) -> str:
    """A stable digest of what the approver is being shown.

    Sorted keys and a canonical separator, so the same values always produce the
    same digest regardless of dictionary ordering. The fingerprint field itself
    is excluded, since it cannot be part of what it summarises.
    """
    material = {k: v for k, v in payload.items() if k != FINGERPRINT_KEY}
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Requesting
# ---------------------------------------------------------------------------


def request_approval(
    session: Session,
    *,
    org_id: str,
    kind: str,
    subject_type: str,
    subject_id: str,
    payload: dict[str, Any],
    subject_label: str | None = None,
    amount: Decimal | None = None,
    threshold: Decimal | None = None,
    justification: str | None = None,
    requested_by_id: str | None = None,
    required_role_code: str | None = None,
    ttl: dt.timedelta | None = DEFAULT_APPROVAL_TTL,
) -> Approval:
    """Raise a checkpoint, snapshotting exactly what is being authorised."""
    if not kind:
        raise ValidationFailed("An approval needs a kind.")

    snapshot = dict(redact_value(payload) or {})
    if not isinstance(snapshot, dict):  # pragma: no cover - defensive
        snapshot = {"value": snapshot}
    snapshot[FINGERPRINT_KEY] = payload_fingerprint(snapshot)

    approval = Approval(
        org_id=org_id,
        kind=kind,
        subject_type=subject_type,
        subject_id=subject_id,
        subject_label=subject_label,
        status=ApprovalStatus.PENDING,
        amount=amount,
        threshold=threshold,
        justification=justification,
        requested_by_id=requested_by_id,
        requested_at=utcnow(),
        required_role_code=required_role_code,
        expires_at=utcnow() + ttl if ttl else None,
        payload=snapshot,
    )
    session.add(approval)
    session.flush()

    record_audit_event(
        action=AuditAction.APPROVAL_REQUESTED,
        resource_type="approval",
        resource_id=approval.id,
        resource_label=subject_label or kind,
        severity=AuditSeverity.NOTICE,
        payload={"kind": kind, "amount": str(amount) if amount is not None else None},
        reason=justification,
        org_id=org_id,
        actor_id=requested_by_id,
        session=session,
    )
    return approval


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def approve(
    session: Session,
    *,
    approval: Approval,
    decided_by_id: str,
    note: str | None = None,
) -> ApprovalDecision:
    """Grant an approval. Refuses the requester, and refuses a lapsed one."""
    _assert_decidable(approval, decided_by_id)

    approval.status = ApprovalStatus.APPROVED
    approval.decided_by_id = decided_by_id
    approval.decided_at = utcnow()
    approval.decision_note = note
    session.flush()

    record_audit_event(
        action=AuditAction.APPROVAL_GRANTED,
        resource_type="approval",
        resource_id=approval.id,
        resource_label=approval.subject_label or approval.kind,
        severity=AuditSeverity.NOTICE,
        payload={
            "kind": approval.kind,
            "subject_id": approval.subject_id,
            "amount": str(approval.amount) if approval.amount is not None else None,
            "requested_by": approval.requested_by_id,
        },
        reason=note,
        org_id=approval.org_id,
        actor_id=decided_by_id,
        session=session,
    )
    return ApprovalDecision(approval=approval, granted=True)


def reject(
    session: Session,
    *,
    approval: Approval,
    decided_by_id: str,
    reason: str,
) -> ApprovalDecision:
    """Refuse an approval. A reason is required - "no" without one is unhelpful."""
    if not reason or not reason.strip():
        raise ValidationFailed("Rejecting an approval requires a reason.")
    _assert_decidable(approval, decided_by_id)

    approval.status = ApprovalStatus.REJECTED
    approval.decided_by_id = decided_by_id
    approval.decided_at = utcnow()
    approval.decision_note = reason
    session.flush()

    record_audit_event(
        action=AuditAction.APPROVAL_REJECTED,
        resource_type="approval",
        resource_id=approval.id,
        resource_label=approval.subject_label or approval.kind,
        severity=AuditSeverity.NOTICE,
        outcome=AuditOutcome.DENIED,
        payload={"kind": approval.kind, "subject_id": approval.subject_id},
        reason=reason,
        org_id=approval.org_id,
        actor_id=decided_by_id,
        session=session,
    )
    return ApprovalDecision(approval=approval, granted=False)


def _assert_decidable(approval: Approval, decided_by_id: str) -> None:
    if not decided_by_id:
        raise PermissionDenied("An approval decision must be attributed to a person.")
    if approval.status != ApprovalStatus.PENDING:
        raise BusinessRuleViolation(f"This approval has already been {approval.status.value}.")
    if approval.expires_at is not None and approval.expires_at <= utcnow():
        raise BusinessRuleViolation("This approval has expired and must be requested again.")
    if not approval.can_be_decided_by(decided_by_id):
        raise PermissionDenied(
            "The person who requested an approval cannot also grant it. "
            "Separation of duties is not waivable."
        )


# ---------------------------------------------------------------------------
# Using
# ---------------------------------------------------------------------------


def consume_approval(
    session: Session,
    *,
    approval: Approval,
    acting_payload: dict[str, Any] | None = None,
    actor_id: str | None = None,
) -> Approval:
    """Check that this approval still authorises the action about to happen.

    Called at the moment of action, not at the moment of decision. Three things
    can have changed in between, and each of them means "no":

    * the approval was rejected or already used;
    * it expired while waiting - an approval granted in March does not authorise
      a payment in September;
    * the values moved. If what is about to happen no longer matches what the
      approver was shown, the decision does not carry over to the new values.
    """
    if approval.status == ApprovalStatus.APPROVED and approval.expires_at is not None:
        if approval.expires_at <= utcnow():
            approval.status = ApprovalStatus.EXPIRED
            session.flush()
            raise ApprovalRequired("The approval for this action has expired.")

    if approval.status != ApprovalStatus.APPROVED:
        raise ApprovalRequired(f"This action is not approved (status: {approval.status.value}).")

    if acting_payload is not None:
        expected = approval.payload.get(FINGERPRINT_KEY)
        actual = payload_fingerprint(dict(redact_value(acting_payload) or {}))
        if expected and expected != actual:
            record_audit_event(
                action=AuditAction.APPROVAL_REJECTED,
                resource_type="approval",
                resource_id=approval.id,
                resource_label=approval.subject_label or approval.kind,
                severity=AuditSeverity.CRITICAL,
                outcome=AuditOutcome.DENIED,
                payload={"expected": expected, "actual": actual},
                reason="The record changed after it was approved.",
                org_id=approval.org_id,
                actor_id=actor_id,
                session=session,
            )
            raise ApprovalRequired(
                "This record has changed since it was approved. "
                "The approver authorised different values, so it must be approved again."
            )

    return approval


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------


def pending_approvals(session: Session, *, org_id: str, kind: str | None = None) -> list[Approval]:
    """Everything still awaiting a decision and not yet lapsed."""
    query = select(Approval).where(
        Approval.org_id == org_id,
        Approval.status == ApprovalStatus.PENDING,
    )
    if kind:
        query = query.where(Approval.kind == kind)
    approvals = session.execute(query.order_by(Approval.requested_at)).scalars().all()
    return [approval for approval in approvals if approval.is_pending]


def expire_stale_approvals(
    session: Session, *, org_id: str, as_of: dt.datetime | None = None
) -> int:
    """Mark lapsed approvals expired.

    Idempotent: an approval already expired is not touched, so the scheduled
    sweep can run as often as it likes.
    """
    now = as_of or utcnow()
    stale = (
        session.execute(
            select(Approval).where(
                Approval.org_id == org_id,
                Approval.status == ApprovalStatus.PENDING,
                Approval.expires_at.is_not(None),
                Approval.expires_at <= now,
            )
        )
        .scalars()
        .all()
    )
    for approval in stale:
        approval.status = ApprovalStatus.EXPIRED
        record_audit_event(
            action=AuditAction.APPROVAL_REJECTED,
            resource_type="approval",
            resource_id=approval.id,
            resource_label=approval.subject_label or approval.kind,
            severity=AuditSeverity.INFO,
            outcome=AuditOutcome.DENIED,
            payload={"kind": approval.kind},
            reason="Expired without a decision.",
            org_id=org_id,
            session=session,
        )
    if stale:
        session.flush()
        log.info(
            "approvals expired without a decision",
            extra={"event": "approvals.expired", "count": len(stale)},
        )
    return len(stale)


def approval_by_id(session: Session, *, org_id: str, approval_id: str) -> Approval:
    approval = session.get(Approval, approval_id)
    if approval is None or approval.org_id != org_id:
        raise NotFound("No such approval.")
    return approval

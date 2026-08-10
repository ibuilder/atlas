"""Tamper-evident audit trail.

A log table anyone can ``UPDATE`` is not an audit trail. Atlas makes tampering
*detectable* by chaining every event to its predecessor:

.. code-block:: text

    entry_hash = SHA256( previous_hash || canonical_json(event fields) )

Each organization has its own chain and its own monotonic sequence. Altering or
removing any historical row breaks every hash after it, and
``verify_chain()`` finds the exact row where the break starts. Combined with
append-only enforcement (an ORM guard here, database triggers in the RLS
migration), an operator with write access can still damage the trail - they just
cannot do it *quietly*, which is the property compliance actually requires.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.models.base import BaseModel, TenantModel
from app.models.types import GUID, JSONType, UTCDateTime, enum_column, utcnow

__all__ = [
    "AuditAction",
    "AuditChainHead",
    "AuditEvent",
    "AuditOutcome",
    "AuditSeverity",
    "GENESIS_HASH",
    "canonical_json",
    "compute_entry_hash",
]

#: The predecessor hash of the first event in any chain.
GENESIS_HASH = "0" * 64


class AuditSeverity(StrEnum):
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    CRITICAL = "critical"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
    PENDING_APPROVAL = "pending_approval"


class AuditAction:
    """Canonical action names, in ``domain.verb`` form.

    Deliberately constants rather than an enum: tenants extend Atlas with custom
    automations that emit their own actions, and a closed enum would force a
    migration for every one of them. The convention is enforced by review and by
    :func:`app.services.audit.recorder.validate_action`.
    """

    # Identity
    AUTH_LOGIN_SUCCEEDED = "auth.login_succeeded"
    AUTH_LOGIN_FAILED = "auth.login_failed"
    AUTH_LOGOUT = "auth.logout"
    AUTH_LOCKED = "auth.account_locked"
    AUTH_PASSWORD_CHANGED = "auth.password_changed"
    AUTH_PASSWORD_RESET_REQUESTED = "auth.password_reset_requested"
    AUTH_PASSWORD_RESET_COMPLETED = "auth.password_reset_completed"
    AUTH_MFA_ENROLLED = "auth.mfa_enrolled"
    AUTH_MFA_DISABLED = "auth.mfa_disabled"
    AUTH_MFA_VERIFIED = "auth.mfa_verified"
    AUTH_MFA_FAILED = "auth.mfa_failed"
    AUTH_SESSION_REVOKED = "auth.session_revoked"
    AUTH_TOKEN_ISSUED = "auth.token_issued"
    AUTH_TOKEN_REVOKED = "auth.token_revoked"
    AUTH_PERMISSION_DENIED = "auth.permission_denied"

    # Administration
    USER_CREATED = "user.created"
    USER_UPDATED = "user.updated"
    USER_DISABLED = "user.disabled"
    ROLE_ASSIGNED = "role.assigned"
    ROLE_REVOKED = "role.revoked"
    ROLE_UPDATED = "role.updated"

    # Portfolio
    ORG_CREATED = "org.created"
    ORG_UPDATED = "org.updated"
    PROPERTY_CREATED = "property.created"
    PROPERTY_UPDATED = "property.updated"
    UNIT_CREATED = "unit.created"
    UNIT_UPDATED = "unit.updated"
    OWNER_CREATED = "owner.created"
    OWNERSHIP_CHANGED = "ownership.changed"

    # Leasing
    LEAD_CREATED = "lead.created"
    APPLICATION_SUBMITTED = "application.submitted"
    APPLICATION_DECIDED = "application.decided"
    SCREENING_COMPLETED = "screening.completed"
    LEASE_CREATED = "lease.created"
    LEASE_ACTIVATED = "lease.activated"
    LEASE_TERMINATED = "lease.terminated"
    LEASE_RENEWED = "lease.renewed"

    # Collections
    NOTICE_ISSUED = "notice.issued"
    LATE_FEE_ASSESSED = "collections.late_fee_assessed"
    DELINQUENCY_ESCALATED = "collections.escalated"

    # Accounting - the actions auditors actually ask about
    JOURNAL_POSTED = "ledger.journal_posted"
    JOURNAL_REVERSED = "ledger.journal_reversed"
    INVOICE_ISSUED = "ar.invoice_issued"
    INVOICE_VOIDED = "ar.invoice_voided"
    PAYMENT_RECEIVED = "ar.payment_received"
    PAYMENT_APPLIED = "ar.payment_applied"
    PAYMENT_REFUNDED = "ar.payment_refunded"
    BILL_RECORDED = "ap.bill_recorded"
    BILL_APPROVED = "ap.bill_approved"
    BILL_PAID = "ap.bill_paid"
    BANK_ACCOUNT_CHANGED = "bank.account_changed"
    RECONCILIATION_COMPLETED = "bank.reconciliation_completed"
    PERIOD_CLOSED = "accounting.period_closed"
    PERIOD_REOPENED = "accounting.period_reopened"
    OWNER_DISTRIBUTION = "owner.distribution_issued"
    STATEMENT_GENERATED = "owner.statement_generated"

    # Maintenance
    REQUEST_CREATED = "maintenance.request_created"
    WORK_ORDER_CREATED = "maintenance.work_order_created"
    WORK_ORDER_ASSIGNED = "maintenance.work_order_assigned"
    WORK_ORDER_COMPLETED = "maintenance.work_order_completed"
    WORK_ORDER_SLA_BREACHED = "maintenance.sla_breached"
    INSPECTION_SCHEDULED = "maintenance.inspection_scheduled"
    INSPECTION_COMPLETED = "maintenance.inspection_completed"

    # Vendors
    VENDOR_CREATED = "vendor.created"
    VENDOR_COMPLIANCE_UPDATED = "vendor.compliance_updated"
    VENDOR_SUSPENDED = "vendor.suspended"

    # Documents
    DOCUMENT_UPLOADED = "document.uploaded"
    DOCUMENT_DOWNLOADED = "document.downloaded"
    DOCUMENT_DELETED = "document.deleted"
    DOCUMENT_SHARED = "document.shared"
    DOCUMENT_QUARANTINED = "document.quarantined"

    # Automation and integration
    AUTOMATION_TRIGGERED = "automation.triggered"
    AUTOMATION_ACTION_EXECUTED = "automation.action_executed"
    AUTOMATION_RULE_FAILED = "automation.rule_failed"
    AUTOMATION_RULE_DISABLED = "automation.rule_disabled"
    AUTOMATION_CASCADE_BLOCKED = "automation.cascade_blocked"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_REJECTED = "approval.rejected"
    WEBHOOK_DELIVERED = "webhook.delivered"
    WEBHOOK_FAILED = "webhook.failed"
    INTEGRATION_CONFIGURED = "integration.configured"
    DATA_EXPORTED = "data.exported"
    PII_ACCESSED = "privacy.pii_accessed"


def canonical_json(payload: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace.

    The hash is only meaningful if the same logical payload always serialises to
    exactly the same bytes, on every Python version and every host.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def compute_entry_hash(
    *,
    previous_hash: str,
    org_id: str,
    sequence: int,
    action: str,
    resource_type: str | None,
    resource_id: str | None,
    actor_id: str | None,
    occurred_at: dt.datetime,
    outcome: str,
    payload: dict[str, Any] | None,
) -> str:
    """Hash one audit entry, binding it to its predecessor."""
    body = canonical_json(
        {
            "action": action,
            "actor_id": actor_id,
            "occurred_at": occurred_at.astimezone(dt.UTC).isoformat(),
            "org_id": org_id,
            "outcome": outcome,
            "payload": payload or {},
            "resource_id": resource_id,
            "resource_type": resource_type,
            "sequence": sequence,
        }
    )
    return hashlib.sha256(f"{previous_hash}{body}".encode()).hexdigest()


class AuditChainHead(BaseModel):
    """The tip of one organization's audit chain.

    A dedicated row per organization, updated under a row lock, is what makes
    sequence allocation atomic under concurrency. Deriving the next sequence
    with ``MAX(sequence) + 1`` instead would hand two concurrent writers the same
    number and silently fork the chain.
    """

    __tablename__ = "audit_chain_heads"
    __table_args__ = (UniqueConstraint("org_id", name="uq_audit_chain_heads_org"),)

    org_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_hash: Mapped[str] = mapped_column(String(64), nullable=False, default=GENESIS_HASH)
    last_event_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)


class AuditEvent(TenantModel):
    """One immutable, hash-chained record of something that happened."""

    __tablename__ = "audit_events"
    __table_args__ = (
        UniqueConstraint("org_id", "sequence", name="uq_audit_events_org_sequence"),
        Index("ix_audit_events_org_occurred", "org_id", "occurred_at"),
        Index("ix_audit_events_resource", "org_id", "resource_type", "resource_id"),
        Index("ix_audit_events_actor", "org_id", "actor_id", "occurred_at"),
        Index("ix_audit_events_action", "org_id", "action", "occurred_at"),
        Index("ix_audit_events_org_created", "org_id", "created_at"),
    )

    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, index=True
    )

    action: Mapped[str] = mapped_column(String(120), nullable=False)
    severity: Mapped[AuditSeverity] = mapped_column(
        enum_column(AuditSeverity), nullable=False, default=AuditSeverity.INFO
    )
    outcome: Mapped[AuditOutcome] = mapped_column(
        enum_column(AuditOutcome), nullable=False, default=AuditOutcome.SUCCESS
    )

    resource_type: Mapped[str | None] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(GUID)
    resource_label: Mapped[str | None] = mapped_column(String(255))

    actor_id: Mapped[str | None] = mapped_column(GUID)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False, default="user")
    #: Denormalised so a rendered audit book stays readable after a user is
    #: renamed or erased.
    actor_label: Mapped[str | None] = mapped_column(String(255))

    correlation_id: Mapped[str | None] = mapped_column(String(128), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="http")

    #: What changed. For updates this is a before/after diff, already filtered
    #: through the PII policy - an audit trail must not become the widest PII
    #: surface in the system.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    reason: Mapped[str | None] = mapped_column(Text)

    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    def recompute_hash(self) -> str:
        return compute_entry_hash(
            previous_hash=self.previous_hash,
            org_id=self.org_id,
            sequence=self.sequence,
            action=self.action,
            resource_type=self.resource_type,
            resource_id=self.resource_id,
            actor_id=self.actor_id,
            occurred_at=self.occurred_at,
            outcome=str(self.outcome),
            payload=self.payload,
        )

    @property
    def is_intact(self) -> bool:
        """Whether this row still hashes to its stored digest."""
        return self.entry_hash == self.recompute_hash()


# ---------------------------------------------------------------------------
# Append-only enforcement
# ---------------------------------------------------------------------------


class AuditImmutabilityError(RuntimeError):
    """Raised on any attempt to modify or delete a persisted audit event."""


@event.listens_for(Session, "before_flush")
def _enforce_audit_immutability(session: Session, flush_context: Any, instances: Any) -> None:
    """Block updates and deletes of audit rows at the ORM boundary.

    The database triggers installed by the RLS migration are the real
    enforcement; this exists so the mistake is caught in development with a
    clear message rather than as an opaque database error in production.
    """
    for obj in session.dirty:
        if isinstance(obj, (AuditEvent,)) and session.is_modified(obj, include_collections=False):
            raise AuditImmutabilityError(
                f"Audit event {obj.id} is immutable; corrections are recorded as new events."
            )
    for obj in session.deleted:
        if isinstance(obj, AuditEvent):
            raise AuditImmutabilityError(
                f"Audit event {obj.id} cannot be deleted; use the retention purge procedure."
            )

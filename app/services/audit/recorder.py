"""Writing to the audit trail.

Every mutating service calls :func:`record_audit_event`. The chain head row is
locked for the duration, so sequence allocation and hash linking are atomic even
under concurrent writers - two requests cannot be handed the same sequence
number and fork the chain.

Payloads pass through the same redaction filter as application logs. An audit
trail that faithfully records a resident's date of birth on every update becomes
the largest and longest-retained personal-data store in the system, which is the
opposite of what it is for.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.context import current_context
from app.logging import audit_logger, get_logger, redact_value
from app.models.audit import (
    GENESIS_HASH,
    AuditChainHead,
    AuditEvent,
    AuditOutcome,
    AuditSeverity,
    compute_entry_hash,
)
from app.models.base import unscoped
from app.models.types import utcnow
from app.observability import AUDIT_EVENTS
from app.services.common.unit_of_work import lock_row

__all__ = ["diff_payload", "record_audit_event", "validate_action"]

log = get_logger("services.audit")

_ACTION_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

#: Fields never worth diffing - they change on every write and would drown the
#: signal in noise.
_DIFF_IGNORE = frozenset({"updated_at", "updated_by_id", "created_at", "created_by_id"})


def validate_action(action: str) -> str:
    """Enforce the ``domain.verb`` convention."""
    if not _ACTION_RE.match(action):
        raise ValueError(f"Audit action {action!r} must match 'domain.verb' in lower snake case.")
    return action


def record_audit_event(
    *,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    resource_label: str | None = None,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    severity: AuditSeverity = AuditSeverity.INFO,
    payload: dict[str, Any] | None = None,
    reason: str | None = None,
    org_id: str | None = None,
    actor_id: str | None = None,
    actor_label: str | None = None,
    session: Session | None = None,
    commit: bool = False,
) -> AuditEvent | None:
    """Append one event to the organization's chain.

    Returns ``None`` when there is no organization to attribute the event to -
    which happens for pre-authentication failures, and is why login attempts have
    their own table.
    """
    validate_action(action)

    if session is None:
        from app.extensions import current_session

        session = current_session()

    ctx = current_context()
    org_id = org_id or (ctx.org_id if ctx else None)
    if not org_id:
        log.debug(
            "audit event skipped: no organization scope",
            extra={"event": "audit.skipped", "action": action},
        )
        return None

    actor_id = actor_id or (ctx.actor_id if ctx else None)
    occurred_at = utcnow()
    safe_payload = redact_value(payload or {})
    if not isinstance(safe_payload, dict):  # pragma: no cover - defensive
        safe_payload = {"value": safe_payload}

    # Unscoped deliberately. The recorder is a trusted writer that is always
    # given an explicit organization, and it has to work before one is bound -
    # a failed sign-in must still be recorded, and provisioning writes the
    # creation event for an organization that is not yet the caller's scope.
    with unscoped(session):
        head = _chain_head(session, org_id)
        sequence = head.last_sequence + 1
    previous_hash = head.last_hash or GENESIS_HASH

    entry_hash = compute_entry_hash(
        previous_hash=previous_hash,
        org_id=org_id,
        sequence=sequence,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_label=resource_label,
        actor_id=actor_id,
        occurred_at=occurred_at,
        outcome=str(outcome),
        severity=str(severity),
        payload=safe_payload,
        reason=reason,
    )

    event = AuditEvent(
        org_id=org_id,
        sequence=sequence,
        occurred_at=occurred_at,
        action=action,
        severity=severity,
        outcome=outcome,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_label=resource_label,
        actor_id=actor_id,
        actor_type=(ctx.actor_type if ctx else "system"),
        actor_label=actor_label,
        correlation_id=(ctx.correlation_id if ctx else None),
        ip_address=(ctx.ip_address if ctx else None),
        user_agent=(ctx.user_agent if ctx else None),
        source=(ctx.source if ctx else "system"),
        payload=safe_payload,
        reason=reason,
        previous_hash=previous_hash,
        entry_hash=entry_hash,
    )

    head.last_sequence = sequence
    head.last_hash = entry_hash
    head.last_event_at = occurred_at

    # The insert stays inside the escape too: the row-level-security policy
    # applies WITH CHECK to writes, and an event written for an organization
    # that is not the caller's current scope - the sign-in and provisioning
    # cases - would otherwise be refused by the database.
    with unscoped(session):
        session.add(event)
        session.flush()

    AUDIT_EVENTS.labels(action).inc()
    # Mirror to the SIEM pipeline. The database row is the system of record;
    # this is for near-real-time alerting.
    audit_logger().info(
        action,
        extra={
            "event": "audit",
            "audit_action": action,
            "audit_sequence": sequence,
            "audit_outcome": str(outcome),
            "resource_type": resource_type,
            "resource_id": resource_id,
            "org_id": org_id,
        },
    )

    if commit:
        session.commit()
    return event


def _chain_head(session: Session, org_id: str) -> AuditChainHead:
    """Fetch or create the chain head, holding a write lock on it."""
    head = lock_row(session, AuditChainHead, AuditChainHead.org_id == org_id)
    if head is not None:
        return head

    head = AuditChainHead(org_id=org_id, last_sequence=0, last_hash=GENESIS_HASH)
    session.add(head)
    session.flush()
    return head


def diff_payload(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    ignore: set[str] | None = None,
) -> dict[str, Any]:
    """Build a compact before/after payload for an update event.

    Only changed fields are recorded. Storing the full row on every update makes
    the audit table the largest in the database and the diff impossible to read.
    """
    skip = _DIFF_IGNORE | (ignore or set())
    before = before or {}
    after = after or {}
    changes: dict[str, Any] = {}

    for key in sorted(set(before) | set(after)):
        if key in skip:
            continue
        old = before.get(key)
        new = after.get(key)
        if old != new:
            changes[key] = {"from": old, "to": new}

    return {"changes": changes} if changes else {}


def verify_chain(session: Session | None = None, *, org_id: str) -> dict[str, Any]:
    """Walk an organization's audit chain and report the first break.

    Run as a scheduled integrity check and on demand during an investigation.
    A break means a row was altered or removed after the fact - which is
    precisely the event the chain exists to make undeniable.
    """
    if session is None:
        from app.extensions import current_session

        session = current_session()

    stmt = (
        select(AuditEvent)
        .where(AuditEvent.org_id == org_id)
        .order_by(AuditEvent.sequence)
        .execution_options(atlas_unscoped=True)
    )

    expected_previous = GENESIS_HASH
    expected_sequence = 1
    checked = 0

    for event in session.execute(stmt).scalars():
        if event.sequence != expected_sequence:
            return _break(checked, event, "sequence_gap", expected_sequence)
        if event.previous_hash != expected_previous:
            return _break(checked, event, "broken_link", expected_sequence)
        if event.entry_hash != event.recompute_hash():
            return _break(checked, event, "content_modified", expected_sequence)

        expected_previous = event.entry_hash
        expected_sequence += 1
        checked += 1

    return {"intact": True, "events_checked": checked, "org_id": org_id}


def _break(checked: int, event: AuditEvent, kind: str, expected_sequence: int) -> dict[str, Any]:
    log.critical(
        "audit chain integrity failure",
        extra={
            "event": "security.audit_chain_broken",
            "kind": kind,
            "org_id": event.org_id,
            "sequence": event.sequence,
            "expected_sequence": expected_sequence,
        },
    )
    return {
        "intact": False,
        "events_checked": checked,
        "org_id": event.org_id,
        "failure": kind,
        "at_sequence": event.sequence,
        "expected_sequence": expected_sequence,
        "event_id": event.id,
    }

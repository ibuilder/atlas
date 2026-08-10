"""The single place a domain event is announced.

Two things want to hear about a change: outside systems, through the webhook
outbox, and automation rules, through the engine. Both are served here so a
service that raises an event does not have to remember either of them - and so
the two can never drift apart, which is how "the webhook fired but the rule
didn't" becomes a support ticket.

Announcing happens inside the caller's transaction. If the change rolls back,
so does the event and anything a rule did in response; the outbox row and the
change it describes commit together or not at all.

Neither consumer may break the caller. A webhook endpoint that is misconfigured
and a rule that is badly written are both somebody's afternoon, not a failed
rent payment.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.logging import get_logger

__all__ = ["emit_domain_event"]

log = get_logger("services.events")


def emit_domain_event(
    session: Session,
    *,
    org_id: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict[str, Any] | None = None,
    actor_id: str | None = None,
) -> None:
    """Record a domain event and let automation rules react to it."""
    body = payload or {}

    try:
        from app.services.integration.webhooks import publish_event

        publish_event(
            session,
            org_id=org_id,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=body,
        )
    except Exception:  # noqa: BLE001 - the outbox must not fail the caller
        log.exception(
            "could not record a domain event in the outbox",
            extra={"event": "events.outbox_failed", "event_type": event_type},
        )

    try:
        from app.services.automation.engine import dispatch_event

        dispatch_event(
            session,
            org_id=org_id,
            event_type=event_type,
            payload=body,
            subject_type=aggregate_type,
            subject_id=aggregate_id,
            actor_id=actor_id,
        )
    except Exception:  # noqa: BLE001 - nor may a rule
        log.exception(
            "automation dispatch failed",
            extra={"event": "events.dispatch_failed", "event_type": event_type},
        )

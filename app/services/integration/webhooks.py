"""Outbound webhooks: outbox, fan-out, signed delivery, backoff, replay.

Three properties make this safe rather than merely working.

**The outbox is written in the caller's transaction.** A domain event is
recorded alongside the change that caused it, so "save the payment, then call
the webhook" cannot lose the event when the process dies in between, or emit it
for a change that rolled back.

**Deliveries are signed and replay-resistant.** Each request carries
``t=<unix>,v1=<hmac>`` over ``"<timestamp>.<body>"``. A receiver that checks the
timestamp cannot be fed a captured request an hour later, and one that compares
in constant time cannot be probed byte by byte.

**Destinations are validated.** Webhook URLs are customer-supplied, which makes
a dispatcher a server-side request forgery engine by default: point one at the
cloud metadata endpoint and it will happily fetch credentials on the attacker's
behalf. Private, loopback, and link-local addresses are refused unless the
deployment explicitly opts in.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ValidationFailed
from app.logging import get_logger
from app.models.audit import AuditAction, AuditSeverity
from app.models.integration import (
    DeliveryStatus,
    OutboxEvent,
    WebhookDelivery,
    WebhookEndpoint,
)
from app.models.types import utcnow
from app.observability import WEBHOOK_DELIVERIES
from app.services.audit.recorder import record_audit_event

__all__ = [
    "SIGNATURE_HEADER",
    "deliver_due",
    "fan_out_pending",
    "publish_event",
    "replay_delivery",
    "sign_payload",
    "verify_signature",
]

log = get_logger("services.integration.webhooks")

SIGNATURE_HEADER = "X-Atlas-Signature"
EVENT_HEADER = "X-Atlas-Event"
DELIVERY_HEADER = "X-Atlas-Delivery"
USER_AGENT = "Atlas-Webhooks/1.0"

#: Consecutive failures before an endpoint is disabled. Retrying a dead receiver
#: forever costs us a worker and costs them a flood when they come back.
DISABLE_AFTER_FAILURES = 20


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def sign_payload(secret: str, body: str, timestamp: int | None = None) -> str:
    """Produce the signature header value for a body.

    The timestamp is inside the signed string, not merely alongside it -
    otherwise an attacker could replay a captured body with a fresh timestamp
    and the signature would still verify.
    """
    from app.security.crypto import hmac_sha256

    stamp = timestamp if timestamp is not None else int(time.time())
    return f"t={stamp},v1={hmac_sha256(secret, f'{stamp}.{body}')}"


def verify_signature(secret: str, header: str, body: str, tolerance_seconds: int = 300) -> bool:
    """Verify a signature header. Provided so receivers can copy it verbatim."""
    from app.security.crypto import compare_digest, hmac_sha256

    parts = dict(piece.split("=", 1) for piece in (header or "").split(",") if "=" in piece)
    stamp, signature = parts.get("t"), parts.get("v1")
    if not stamp or not signature:
        return False

    try:
        age = abs(int(time.time()) - int(stamp))
    except ValueError:
        return False
    if age > tolerance_seconds:
        return False

    return compare_digest(signature, hmac_sha256(secret, f"{stamp}.{body}"))


# ---------------------------------------------------------------------------
# Destination validation
# ---------------------------------------------------------------------------


def assert_safe_url(url: str, *, allow_private: bool = False) -> None:
    """Refuse destinations that would turn the dispatcher into an SSRF proxy."""
    parsed = urlparse(url)

    if parsed.scheme not in ("https", "http"):
        raise ValidationFailed("A webhook URL must use http or https.")
    if parsed.scheme == "http" and not allow_private:
        raise ValidationFailed("Webhook URLs must use https.")
    if not parsed.hostname:
        raise ValidationFailed("A webhook URL must include a host.")
    if allow_private:
        return

    try:
        resolved = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise ValidationFailed("The webhook host could not be resolved.") from exc

    for family, _type, _proto, _canon, sockaddr in resolved:
        address = ipaddress.ip_address(sockaddr[0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local  # 169.254.169.254 - cloud metadata
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            log.warning(
                "webhook destination refused",
                extra={
                    "event": "security.webhook_ssrf_blocked",
                    "host": parsed.hostname,
                    "resolved": str(address),
                },
            )
            raise ValidationFailed(
                "That webhook destination is not permitted. "
                "Private, loopback, and link-local addresses are refused."
            )
        del family


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------


def publish_event(
    session: Session,
    *,
    org_id: str,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict | None = None,
) -> OutboxEvent:
    """Record a domain event in the caller's transaction.

    Nothing is sent here. The dispatcher picks it up after the transaction
    commits, which is what makes the event and the change it describes atomic.
    """
    from app.context import current_context
    from app.logging import redact_value

    ctx = current_context()
    event = OutboxEvent(
        org_id=org_id,
        event_type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        payload=redact_value(payload or {}),
        correlation_id=ctx.correlation_id if ctx else None,
        actor_id=ctx.actor_id if ctx else None,
    )
    session.add(event)
    session.flush()
    return event


def fan_out_pending(session: Session, *, org_id: str, limit: int = 200) -> int:
    """Turn unpublished outbox events into per-endpoint delivery rows.

    Separating fan-out from delivery is what makes the unique constraint on
    (endpoint, event) meaningful: a dispatcher restart re-runs fan-out, the
    constraint absorbs the duplicate, and no receiver sees the same event twice.
    """
    events = (
        session.execute(
            select(OutboxEvent)
            .where(OutboxEvent.org_id == org_id, OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.created_at)
            .limit(limit)
        )
        .scalars()
        .all()
    )
    if not events:
        return 0

    endpoints = (
        session.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.org_id == org_id,
                WebhookEndpoint.is_active.is_(True),
                WebhookEndpoint.disabled_at.is_(None),
            )
        )
        .scalars()
        .all()
    )

    created = 0
    now = utcnow()
    for event in events:
        for endpoint in endpoints:
            if not endpoint.subscribes_to(event.event_type):
                continue
            exists = (
                session.execute(
                    select(WebhookDelivery.id).where(
                        WebhookDelivery.endpoint_id == endpoint.id,
                        WebhookDelivery.event_id == event.id,
                    )
                )
                .scalars()
                .first()
            )
            if exists:
                continue
            session.add(
                WebhookDelivery(
                    org_id=org_id,
                    endpoint_id=endpoint.id,
                    event_id=event.id,
                    event_type=event.event_type,
                    payload=_envelope(event),
                    status=DeliveryStatus.PENDING,
                    next_attempt_at=now,
                    max_attempts=current_app.config["SETTINGS"].webhook_max_attempts,
                )
            )
            created += 1
        # Marked published once fanned out, whether or not any endpoint wanted
        # it - an event nobody subscribes to is delivered, vacuously.
        event.published_at = now

    session.flush()
    return created


def _envelope(event: OutboxEvent) -> dict:
    return {
        "id": event.id,
        "type": event.event_type,
        "created_at": event.occurred_at.isoformat(),
        "data": {
            "object": event.aggregate_type,
            "id": event.aggregate_id,
            "attributes": event.payload,
        },
    }


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryOutcome:
    delivered: int
    failed: int
    dead_lettered: int


def deliver_due(session: Session, *, org_id: str, limit: int = 100) -> DeliveryOutcome:
    """Attempt every delivery whose backoff has elapsed."""
    settings = current_app.config["SETTINGS"]
    now = utcnow()

    due = (
        session.execute(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.org_id == org_id,
                WebhookDelivery.status.in_([DeliveryStatus.PENDING, DeliveryStatus.RETRYING]),
                WebhookDelivery.next_attempt_at <= now,
            )
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(limit)
        )
        .scalars()
        .all()
    )

    delivered = failed = dead = 0
    for delivery in due:
        endpoint = session.get(WebhookEndpoint, delivery.endpoint_id)
        if endpoint is None or not endpoint.is_active or endpoint.disabled_at is not None:
            delivery.status = DeliveryStatus.DEAD_LETTERED
            delivery.dead_lettered_at = now
            delivery.error_message = "endpoint is disabled"
            dead += 1
            continue

        outcome = _attempt(delivery, endpoint, settings)
        if outcome:
            delivered += 1
            endpoint.consecutive_failures = 0
            endpoint.last_success_at = utcnow()
        else:
            failed += 1
            endpoint.consecutive_failures += 1
            endpoint.last_failure_at = utcnow()
            if delivery.status == DeliveryStatus.DEAD_LETTERED:
                dead += 1
            if endpoint.consecutive_failures >= DISABLE_AFTER_FAILURES:
                endpoint.disabled_at = utcnow()
                endpoint.disabled_reason = (
                    f"{endpoint.consecutive_failures} consecutive delivery failures"
                )
                log.warning(
                    "webhook endpoint auto-disabled",
                    extra={"event": "webhook.endpoint_disabled", "endpoint_id": endpoint.id},
                )

    session.flush()
    return DeliveryOutcome(delivered, failed, dead)


def _attempt(delivery: WebhookDelivery, endpoint: WebhookEndpoint, settings) -> bool:  # noqa: ANN001
    """Send one delivery. Returns whether the receiver accepted it."""
    body = json.dumps(delivery.payload, separators=(",", ":"), sort_keys=True, default=str)
    started = time.perf_counter()

    delivery.attempts += 1
    delivery.status = DeliveryStatus.IN_FLIGHT

    try:
        assert_safe_url(endpoint.url, allow_private=not settings.force_https)
        request = urllib.request.Request(  # noqa: S310 - scheme validated above  # nosec B310
            endpoint.url,
            data=body.encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                SIGNATURE_HEADER: sign_payload(endpoint.signing_secret, body),
                EVENT_HEADER: delivery.event_type,
                DELIVERY_HEADER: delivery.id,
            },
        )
        # The scheme is constrained to http/https and the resolved address is
        # checked against private, loopback, and link-local ranges by
        # assert_safe_url immediately above; the scanners cannot see that.
        with urllib.request.urlopen(  # noqa: S310  # nosec B310
            request, timeout=settings.webhook_timeout_seconds
        ) as response:
            status = response.status
            preview = response.read(2000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        preview = exc.read(2000).decode("utf-8", "replace") if exc.fp else ""
    except Exception as exc:  # noqa: BLE001 - any transport failure is a failed attempt
        status = None
        preview = str(exc)[:500]

    delivery.duration_ms = int((time.perf_counter() - started) * 1000)
    delivery.response_status = status
    delivery.response_body = preview[:2000]

    if status is not None and 200 <= status < 300:
        delivery.status = DeliveryStatus.DELIVERED
        delivery.delivered_at = utcnow()
        delivery.error_message = None
        WEBHOOK_DELIVERIES.labels(delivery.event_type, "delivered").inc()
        return True

    delivery.error_message = f"HTTP {status}" if status else preview[:500]

    if delivery.attempts >= delivery.max_attempts:
        delivery.status = DeliveryStatus.DEAD_LETTERED
        delivery.dead_lettered_at = utcnow()
        delivery.next_attempt_at = None
        WEBHOOK_DELIVERIES.labels(delivery.event_type, "dead_lettered").inc()
        log.warning(
            "webhook delivery dead-lettered",
            extra={
                "event": "webhook.dead_lettered",
                "delivery_id": delivery.id,
                "attempts": delivery.attempts,
            },
        )
    else:
        delivery.status = DeliveryStatus.RETRYING
        delivery.next_attempt_at = utcnow() + dt.timedelta(seconds=delivery.backoff_delay_seconds())
        WEBHOOK_DELIVERIES.labels(delivery.event_type, "retrying").inc()

    return False


def replay_delivery(
    session: Session, *, delivery: WebhookDelivery, actor_id: str | None = None
) -> WebhookDelivery:
    """Re-queue a dead-lettered delivery once its receiver is healthy again."""
    if delivery.status != DeliveryStatus.DEAD_LETTERED:
        raise ValidationFailed("Only a dead-lettered delivery can be replayed.")

    delivery.status = DeliveryStatus.PENDING
    delivery.attempts = 0
    delivery.next_attempt_at = utcnow()
    delivery.dead_lettered_at = None
    delivery.error_message = None
    session.flush()

    record_audit_event(
        action=AuditAction.WEBHOOK_DELIVERED,
        resource_type="WebhookDelivery",
        resource_id=delivery.id,
        resource_label=delivery.event_type,
        payload={"action": "replayed"},
        severity=AuditSeverity.NOTICE,
        org_id=delivery.org_id,
        actor_id=actor_id,
        session=session,
    )
    return delivery

"""Webhook signing, SSRF protection, delivery, backoff, and replay.

A webhook dispatcher fetches attacker-influenced URLs from inside the network
perimeter, so these are security tests first.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from app.errors import ValidationFailed
from app.models.integration import DeliveryStatus, WebhookDelivery, WebhookEndpoint
from app.services.integration import webhooks

pytestmark = [pytest.mark.security, pytest.mark.integration]

SECRET = "whsec_test_0123456789abcdef"


@pytest.fixture()
def endpoint(db, org, scope):
    record = WebhookEndpoint(
        org_id=org.id,
        name="Receiver",
        url="https://hooks.example.com/atlas",
        signing_secret=SECRET,
        subscribed_events=["*"],
        is_active=True,
    )
    db.session.add(record)
    db.session.commit()
    return record


class _Response:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, status: int, body: bytes = b"ok") -> None:
        self.status = status
        self._body = body

    def read(self, _size: int = -1) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ------------------------------------------------------------------ signing


def test_signature_round_trips():
    body = '{"id":"evt_1","type":"lease.created"}'
    header = webhooks.sign_payload(SECRET, body)
    assert webhooks.verify_signature(SECRET, header, body)


def test_signature_rejects_a_modified_body():
    body = '{"amount":"100.00"}'
    header = webhooks.sign_payload(SECRET, body)
    assert not webhooks.verify_signature(SECRET, header, '{"amount":"9000.00"}')


def test_signature_rejects_the_wrong_secret():
    body = '{"id":"evt_1"}'
    header = webhooks.sign_payload(SECRET, body)
    assert not webhooks.verify_signature("whsec_someone_elses_secret", header, body)


def test_signature_rejects_a_stale_timestamp():
    """Replay protection: a captured request cannot be replayed an hour later."""
    body = '{"id":"evt_1"}'
    header = webhooks.sign_payload(SECRET, body, timestamp=int(time.time()) - 3600)
    assert not webhooks.verify_signature(SECRET, header, body, tolerance_seconds=300)


def test_timestamp_is_covered_by_the_signature():
    """Swapping in a fresh timestamp must invalidate a captured signature."""
    body = '{"id":"evt_1"}'
    old = int(time.time()) - 3600
    header = webhooks.sign_payload(SECRET, body, timestamp=old)
    forged = header.replace(f"t={old}", f"t={int(time.time())}")
    assert not webhooks.verify_signature(SECRET, forged, body)


@pytest.mark.parametrize("header", ["", "garbage", "t=123", "v1=abc", "t=abc,v1=def"])
def test_malformed_signature_headers_are_rejected(header):
    assert not webhooks.verify_signature(SECRET, header, "{}")


# --------------------------------------------------------------------- SSRF


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/hook",
        "https://127.0.0.1/hook",
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata
        "https://10.0.0.5/hook",
        "https://192.168.1.10/hook",
        "https://[::1]/hook",
    ],
)
def test_internal_destinations_are_refused(url):
    """A dispatcher is an SSRF engine unless it refuses these."""
    with pytest.raises(ValidationFailed):
        webhooks.assert_safe_url(url)


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "gopher://x/1"])
def test_non_http_schemes_are_refused(url):
    with pytest.raises(ValidationFailed):
        webhooks.assert_safe_url(url)


def test_plaintext_http_is_refused_by_default():
    with pytest.raises(ValidationFailed, match="https"):
        webhooks.assert_safe_url("http://example.com/hook")


# ------------------------------------------------------------------ outbox


def test_publish_writes_to_the_outbox_without_sending(db, org, scope):
    event = webhooks.publish_event(
        db.session,
        org_id=org.id,
        event_type="lease.created",
        aggregate_type="lease",
        aggregate_id="019fea00-0000-7000-8000-00000000001a",
        payload={"rent": "2400.00"},
    )
    db.session.commit()

    assert event.published_at is None
    assert db.session.query(WebhookDelivery).count() == 0


def test_publish_redacts_secrets_from_the_payload(db, org, scope):
    """The outbox must not become a credential store."""
    event = webhooks.publish_event(
        db.session,
        org_id=org.id,
        event_type="user.created",
        aggregate_type="user",
        aggregate_id="019fea00-0000-7000-8000-00000000001b",
        payload={"email": "resident@example.com", "password": "hunter2"},
    )
    db.session.commit()

    assert event.payload["password"] == "[REDACTED]"
    assert "resident@example.com" not in str(event.payload)


def test_fan_out_creates_one_delivery_per_subscriber(db, org, scope, endpoint):
    webhooks.publish_event(
        db.session,
        org_id=org.id,
        event_type="lease.created",
        aggregate_type="lease",
        aggregate_id="019fea00-0000-7000-8000-00000000001c",
    )
    created = webhooks.fan_out_pending(db.session, org_id=org.id)
    db.session.commit()

    assert created == 1
    assert db.session.query(WebhookDelivery).count() == 1


def test_fan_out_is_idempotent(db, org, scope, endpoint):
    """A dispatcher restart must not deliver the same event twice."""
    webhooks.publish_event(
        db.session,
        org_id=org.id,
        event_type="lease.created",
        aggregate_type="lease",
        aggregate_id="019fea00-0000-7000-8000-00000000001d",
    )
    webhooks.fan_out_pending(db.session, org_id=org.id)
    db.session.commit()
    again = webhooks.fan_out_pending(db.session, org_id=org.id)
    db.session.commit()

    assert again == 0
    assert db.session.query(WebhookDelivery).count() == 1


def test_endpoints_only_receive_events_they_subscribe_to(db, org, scope, endpoint):
    endpoint.subscribed_events = ["payment.received"]
    db.session.commit()

    webhooks.publish_event(
        db.session,
        org_id=org.id,
        event_type="lease.created",
        aggregate_type="lease",
        aggregate_id="019fea00-0000-7000-8000-00000000001e",
    )
    assert webhooks.fan_out_pending(db.session, org_id=org.id) == 0


# ---------------------------------------------------------------- delivery


def _queue(db, org, endpoint) -> WebhookDelivery:
    webhooks.publish_event(
        db.session,
        org_id=org.id,
        event_type="lease.created",
        aggregate_type="lease",
        aggregate_id="019fea00-0000-7000-8000-00000000001f",
    )
    webhooks.fan_out_pending(db.session, org_id=org.id)
    db.session.commit()
    return db.session.query(WebhookDelivery).one()


def test_successful_delivery_is_signed_and_marked(db, org, scope, endpoint):
    captured = {}

    def _fake(request, timeout=None):  # noqa: ANN001, ARG001
        captured["headers"] = dict(request.headers)
        captured["body"] = request.data.decode()
        return _Response(200)

    delivery = _queue(db, org, endpoint)
    with (
        patch.object(webhooks.urllib.request, "urlopen", _fake),
        patch.object(webhooks, "assert_safe_url", lambda *a, **k: None),
    ):
        outcome = webhooks.deliver_due(db.session, org_id=org.id)
    db.session.commit()

    assert outcome.delivered == 1
    assert delivery.status == DeliveryStatus.DELIVERED
    assert delivery.delivered_at is not None

    # Header keys are capitalised by urllib; find ours case-insensitively.
    signature = next(v for k, v in captured["headers"].items() if k.lower() == "x-atlas-signature")
    assert webhooks.verify_signature(SECRET, signature, captured["body"])


def test_failure_schedules_a_retry_with_backoff(db, org, scope, endpoint):
    delivery = _queue(db, org, endpoint)
    with (
        patch.object(webhooks.urllib.request, "urlopen", lambda *a, **k: _Response(500)),
        patch.object(webhooks, "assert_safe_url", lambda *a, **k: None),
    ):
        outcome = webhooks.deliver_due(db.session, org_id=org.id)
    db.session.commit()

    assert outcome.failed == 1
    assert delivery.status == DeliveryStatus.RETRYING
    assert delivery.attempts == 1
    assert delivery.next_attempt_at is not None
    assert delivery.response_status == 500


def test_exhausted_attempts_dead_letter(db, org, scope, endpoint):
    delivery = _queue(db, org, endpoint)
    delivery.max_attempts = 2
    db.session.commit()

    with (
        patch.object(webhooks.urllib.request, "urlopen", lambda *a, **k: _Response(503)),
        patch.object(webhooks, "assert_safe_url", lambda *a, **k: None),
    ):
        for _ in range(2):
            delivery.next_attempt_at = webhooks.utcnow()
            webhooks.deliver_due(db.session, org_id=org.id)
    db.session.commit()

    assert delivery.status == DeliveryStatus.DEAD_LETTERED
    assert delivery.dead_lettered_at is not None
    # Retained rather than discarded, so an operator can replay it.
    assert db.session.query(WebhookDelivery).count() == 1


def test_backoff_grows_and_is_capped():
    delivery = WebhookDelivery(attempts=1)
    assert delivery.backoff_delay_seconds() == 30
    delivery.attempts = 3
    assert delivery.backoff_delay_seconds() == 120
    delivery.attempts = 50
    assert delivery.backoff_delay_seconds() == 6 * 60 * 60


def test_replay_requeues_a_dead_letter(db, org, scope, endpoint):
    delivery = _queue(db, org, endpoint)
    delivery.status = DeliveryStatus.DEAD_LETTERED
    delivery.attempts = 8
    db.session.commit()

    webhooks.replay_delivery(db.session, delivery=delivery)
    db.session.commit()

    assert delivery.status == DeliveryStatus.PENDING
    assert delivery.attempts == 0


def test_only_dead_letters_can_be_replayed(db, org, scope, endpoint):
    delivery = _queue(db, org, endpoint)
    with pytest.raises(ValidationFailed, match="dead-lettered"):
        webhooks.replay_delivery(db.session, delivery=delivery)


def test_disabled_endpoint_dead_letters_its_queue(db, org, scope, endpoint):
    delivery = _queue(db, org, endpoint)
    endpoint.disabled_at = webhooks.utcnow()
    db.session.commit()

    outcome = webhooks.deliver_due(db.session, org_id=org.id)
    db.session.commit()

    assert outcome.dead_lettered == 1
    assert delivery.status == DeliveryStatus.DEAD_LETTERED

"""Integration surface: outbox, webhooks, idempotency, imports, connections.

Two patterns carry most of the weight here.

**The transactional outbox.** A domain event is written in the *same
transaction* as the change that caused it. A separate dispatcher publishes it
afterwards. Without this, "save the payment, then call the webhook" loses events
whenever the process dies in between - and duplicates them whenever the retry
succeeds after the first attempt actually worked.

**Idempotency keys.** External callers retry; networks are honest about nothing.
A key plus a request-body digest lets a retry return the original response
instead of charging a resident twice, and lets a *different* body under the same
key be rejected loudly rather than silently accepted.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import SoftDeleteMixin, TenantModel
from app.models.types import GUID, EncryptedText, JSONType, UTCDateTime, enum_column, utcnow

__all__ = [
    "DeliveryStatus",
    "IdempotencyRecord",
    "ImportJob",
    "ImportStatus",
    "InboundEvent",
    "IntegrationConnection",
    "OutboxEvent",
    "WebhookDelivery",
    "WebhookEndpoint",
]


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    DELIVERED = "delivered"
    RETRYING = "retrying"
    FAILED = "failed"
    #: Exhausted retries. Retained for operator replay rather than discarded.
    DEAD_LETTERED = "dead_lettered"


class ImportStatus(StrEnum):
    PENDING = "pending"
    VALIDATING = "validating"
    READY = "ready"
    PROCESSING = "processing"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutboxEvent(TenantModel):
    """A domain event, written transactionally with the change it describes."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_events_unpublished", "published_at", "created_at"),
        Index("ix_outbox_events_aggregate", "org_id", "aggregate_type", "aggregate_id"),
        Index("ix_outbox_events_org_created", "org_id", "created_at"),
    )

    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    aggregate_type: Mapped[str] = mapped_column(String(40), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(GUID, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    occurred_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    published_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    correlation_id: Mapped[str | None] = mapped_column(String(128), index=True)
    actor_id: Mapped[str | None] = mapped_column(GUID)


class WebhookEndpoint(TenantModel, SoftDeleteMixin):
    """A customer-registered destination for outbound events."""

    __tablename__ = "webhook_endpoints"
    __table_args__ = (
        Index("ix_webhook_endpoints_org_active", "org_id", "is_active"),
        Index("ix_webhook_endpoints_org_created", "org_id", "created_at"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    #: Encrypted. Signs every delivery so the receiver can verify origin.
    signing_secret: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    #: Event names, or ``["*"]``.
    subscribed_events: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[str | None] = mapped_column(Text)
    #: Endpoints that fail persistently are disabled rather than retried forever.
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    disabled_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    disabled_reason: Mapped[str | None] = mapped_column(String(255))
    last_success_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    last_failure_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    deliveries: Mapped[list[WebhookDelivery]] = relationship(
        back_populates="endpoint", cascade="all, delete-orphan", passive_deletes=True
    )

    def subscribes_to(self, event_type: str) -> bool:
        events = self.subscribed_events or []
        return "*" in events or event_type in events


class WebhookDelivery(TenantModel):
    """One attempt-tracked delivery of one event to one endpoint."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("endpoint_id", "event_id", name="uq_webhook_deliveries_endpoint_event"),
        Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),
        Index("ix_webhook_deliveries_org_created", "org_id", "created_at"),
    )

    endpoint_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The outbox event this carries. Unique per endpoint, so a dispatcher
    #: restart cannot double-deliver.
    event_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("outbox_events.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    status: Mapped[DeliveryStatus] = mapped_column(
        enum_column(DeliveryStatus), nullable=False, default=DeliveryStatus.PENDING, index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)

    response_status: Mapped[int | None] = mapped_column(Integer)
    #: Truncated: a receiver's error page is diagnostic, not something to store
    #: in full for every failure.
    response_body: Mapped[str | None] = mapped_column(String(2000))
    error_message: Mapped[str | None] = mapped_column(String(500))
    delivered_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    dead_lettered_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    endpoint: Mapped[WebhookEndpoint] = relationship(back_populates="deliveries")

    def backoff_delay_seconds(self) -> int:
        """Exponential backoff with a ceiling: 30s, 60s, 120s ... capped at 6h."""
        return min(30 * (2 ** max(0, self.attempts - 1)), 6 * 60 * 60)


class InboundEvent(TenantModel):
    """A received webhook, deduplicated by provider event id.

    Payment processors deliver at-least-once and reorder freely. Recording the
    provider's own event id under a unique constraint is what turns "we might
    post this payment twice" into "we cannot".
    """

    __tablename__ = "inbound_events"
    __table_args__ = (
        UniqueConstraint("provider", "external_id", name="uq_inbound_events_provider_external"),
        Index("ix_inbound_events_unprocessed", "processed_at", "created_at"),
        Index("ix_inbound_events_org_created", "org_id", "created_at"),
    )

    provider: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    signature_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    received_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    processed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    processing_error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class IdempotencyRecord(TenantModel):
    """A completed (or in-flight) idempotent request."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        UniqueConstraint("org_id", "idempotency_key", name="uq_idempotency_org_key"),
        Index("ix_idempotency_records_expiry", "expires_at"),
        Index("ix_idempotency_records_org_created", "org_id", "created_at"),
    )

    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False)
    #: Digest of the request body. A retry with the *same* key and a *different*
    #: body is a client bug, and is rejected rather than silently served.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Set while the original request is still executing, so a concurrent retry
    #: waits or fails fast instead of running the same mutation twice.
    locked_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    completed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    resource_id: Mapped[str | None] = mapped_column(GUID)
    expires_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)

    @property
    def is_complete(self) -> bool:
        return self.completed_at is not None


class IntegrationConnection(TenantModel, SoftDeleteMixin):
    """Configuration for one external provider."""

    __tablename__ = "integration_connections"
    __table_args__ = (
        UniqueConstraint("org_id", "provider", "kind", name="uq_integration_connections_provider"),
        Index("ix_integration_connections_org_created", "org_id", "created_at"),
    )

    provider: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    #: ``payments``, ``screening``, ``esign``, ``accounting``, ``sms``.
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)

    #: Encrypted blob. Never returned by any API, at any permission level.
    credentials: Mapped[str | None] = mapped_column(EncryptedText)
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    #: Maps external identifiers onto canonical ones, both directions.
    field_mappings: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="inactive")
    is_sandbox: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(String(500))
    last_verified_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)


class ImportJob(TenantModel):
    """A bulk data import, with per-row error reporting and replay."""

    __tablename__ = "import_jobs"
    __table_args__ = (
        Index("ix_import_jobs_org_status", "org_id", "status"),
        Index("ix_import_jobs_org_created", "org_id", "created_at"),
    )

    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    filename: Mapped[str | None] = mapped_column(String(255))
    document_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("documents.id", ondelete="SET NULL"), index=True
    )

    status: Mapped[ImportStatus] = mapped_column(
        enum_column(ImportStatus), nullable=False, default=ImportStatus.PENDING, index=True
    )
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: ``[{"row": 42, "field": "postal_code", "message": "..."}]`` - surfaced to
    #: the operator so a failed import can be corrected and replayed, rather
    #: than abandoned because row 42 of 8,000 was wrong.
    errors: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    options: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    #: Validation-only pass, so an operator sees every error before any write.
    is_dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    replay_of_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("import_jobs.id", ondelete="SET NULL"), index=True
    )

    @property
    def progress_percent(self) -> int:
        if not self.total_rows:
            return 0
        return int(self.processed_rows / self.total_rows * 100)

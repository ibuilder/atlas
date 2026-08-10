"""The document graph.

Most systems store a file *under* something - a lease folder, a work order
attachment. Atlas stores the file once and links it to everything it relates to
through :class:`DocumentLink`. One certificate of insurance is simultaneously
the vendor's compliance record, an attachment on four work orders, and evidence
in a claim. Copying it four times means four things to expire, three of which
nobody updates.

Uploads are quarantined until scanned. The storage key is never derived from the
user-supplied filename, and every retrieval goes through a signed, expiring URL -
so a leaked link is a time-boxed leak of one document rather than a directory.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import SoftDeleteMixin, TenantModel
from app.models.types import GUID, JSONType, UTCDateTime, enum_column, utcnow

__all__ = [
    "Document",
    "DocumentCategory",
    "DocumentLink",
    "DocumentShare",
    "DocumentVisibility",
    "OcrStatus",
    "RetentionClass",
    "ScanStatus",
]


class DocumentCategory(StrEnum):
    LEASE = "lease"
    APPLICATION = "application"
    IDENTIFICATION = "identification"
    INVOICE = "invoice"
    BILL = "bill"
    RECEIPT = "receipt"
    STATEMENT = "statement"
    INSPECTION = "inspection"
    MAINTENANCE = "maintenance"
    INSURANCE = "insurance"
    COMPLIANCE = "compliance"
    WARRANTY = "warranty"
    PERMIT = "permit"
    NOTICE = "notice"
    CORRESPONDENCE = "correspondence"
    PHOTO = "photo"
    REPORT = "report"
    OTHER = "other"


class DocumentVisibility(StrEnum):
    """Who may see a document, before per-object permissions are applied."""

    INTERNAL = "internal"
    RESIDENT = "resident"
    OWNER = "owner"
    VENDOR = "vendor"
    SHARED_LINK = "shared_link"


class ScanStatus(StrEnum):
    PENDING = "pending"
    CLEAN = "clean"
    INFECTED = "infected"
    FAILED = "failed"
    SKIPPED = "skipped"


class OcrStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class RetentionClass(StrEnum):
    """Drives the purge schedule. Deleting a lease after three years because it
    shared a policy with a photo is a discovery problem, not a storage saving."""

    TRANSIENT = "transient"  # 90 days
    OPERATIONAL = "operational"  # 3 years
    FINANCIAL = "financial"  # 7 years
    LEGAL = "legal"  # 10 years
    PERMANENT = "permanent"


#: Retention in days by class; ``None`` means keep indefinitely.
RETENTION_DAYS: dict[RetentionClass, int | None] = {
    RetentionClass.TRANSIENT: 90,
    RetentionClass.OPERATIONAL: 365 * 3,
    RetentionClass.FINANCIAL: 365 * 7,
    RetentionClass.LEGAL: 365 * 10,
    RetentionClass.PERMANENT: None,
}


class Document(TenantModel, SoftDeleteMixin):
    """A stored file and its metadata."""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("org_id", "storage_key", name="uq_documents_org_storage_key"),
        Index("ix_documents_org_category", "org_id", "category"),
        Index("ix_documents_checksum", "org_id", "checksum_sha256"),
        Index("ix_documents_scan_status", "org_id", "scan_status"),
        Index("ix_documents_retention", "org_id", "retention_until"),
        Index("ix_documents_org_created", "org_id", "created_at"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: What the user called it. Never used to build a storage path.
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Content digest. Deduplicates uploads and detects silent corruption.
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    storage_backend: Mapped[str] = mapped_column(String(20), nullable=False, default="local")
    #: Opaque, tenant-prefixed, generated key.
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)

    category: Mapped[DocumentCategory] = mapped_column(
        enum_column(DocumentCategory), nullable=False, default=DocumentCategory.OTHER, index=True
    )
    visibility: Mapped[DocumentVisibility] = mapped_column(
        enum_column(DocumentVisibility), nullable=False, default=DocumentVisibility.INTERNAL
    )
    tags: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)

    # -- safety pipeline --------------------------------------------------
    # Covered by the composite (org_id, scan_status) index above; a standalone
    # index on scan_status alone would be low-cardinality and never chosen.
    scan_status: Mapped[ScanStatus] = mapped_column(
        enum_column(ScanStatus), nullable=False, default=ScanStatus.PENDING
    )
    scanned_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    scan_detail: Mapped[str | None] = mapped_column(String(255))
    #: Quarantined objects live under a separate storage prefix and are never
    #: served, whatever permissions the requester holds.
    is_quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # -- extraction -------------------------------------------------------
    ocr_status: Mapped[OcrStatus] = mapped_column(
        enum_column(OcrStatus), nullable=False, default=OcrStatus.NOT_APPLICABLE
    )
    ocr_text: Mapped[str | None] = mapped_column(Text)
    ocr_completed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    #: Structured fields lifted from the document - lease dates, invoice totals.
    extracted: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    extraction_confidence: Mapped[int | None] = mapped_column(Integer)

    # -- lifecycle --------------------------------------------------------
    retention_class: Mapped[RetentionClass] = mapped_column(
        enum_column(RetentionClass), nullable=False, default=RetentionClass.OPERATIONAL
    )
    retention_until: Mapped[dt.date | None] = mapped_column(Date, index=True)
    #: A legal hold outranks every retention rule and every delete request.
    legal_hold: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    legal_hold_reason: Mapped[str | None] = mapped_column(String(255))

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    supersedes_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("documents.id", ondelete="SET NULL"), index=True
    )
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    uploaded_by_id: Mapped[str | None] = mapped_column(GUID, index=True)
    last_accessed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    download_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    links: Mapped[list[DocumentLink]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_servable(self) -> bool:
        """Whether this document may be handed to a client at all."""
        return (
            not self.is_quarantined
            and self.scan_status in (ScanStatus.CLEAN, ScanStatus.SKIPPED)
            and not self.is_deleted
        )

    @property
    def is_purgeable(self) -> bool:
        if self.legal_hold or self.retention_until is None:
            return False
        return self.retention_until <= utcnow().date()

    def apply_retention(self, from_date: dt.date | None = None) -> None:
        days = RETENTION_DAYS[self.retention_class]
        if days is None:
            self.retention_until = None
        else:
            self.retention_until = (from_date or utcnow().date()) + dt.timedelta(days=days)


class DocumentLink(TenantModel):
    """An edge between a document and any other entity.

    A generalized edge table rather than a nullable foreign key per entity type.
    The alternative - ``lease_id``, ``work_order_id``, ``invoice_id``, ... all
    nullable - grows a column per feature and can never express "this photo is
    evidence on the inspection *and* the resulting work order".
    """

    __tablename__ = "document_links"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "entity_type", "entity_id", "relation", name="uq_document_links_edge"
        ),
        Index("ix_document_links_entity", "org_id", "entity_type", "entity_id"),
        Index("ix_document_links_org_created", "org_id", "created_at"),
    )

    document_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Table-ish name of the far side: ``lease``, ``work_order``, ``vendor``.
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(GUID, nullable=False, index=True)
    #: The nature of the edge: ``attachment``, ``evidence``, ``signed_copy``.
    relation: Mapped[str] = mapped_column(String(40), nullable=False, default="attachment")
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(String(255))

    document: Mapped[Document] = relationship(back_populates="links")


class DocumentShare(TenantModel):
    """A time-boxed external share link."""

    __tablename__ = "document_shares"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_document_shares_token"),
        Index("ix_document_shares_org_created", "org_id", "created_at"),
    )

    document_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    recipient_email: Mapped[str | None] = mapped_column(String(320))
    expires_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    max_downloads: Mapped[int | None] = mapped_column(Integer)
    download_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    #: Watermarks the rendered copy with recipient and timestamp.
    watermark: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_accessed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    last_accessed_ip: Mapped[str | None] = mapped_column(String(45))

    @property
    def is_usable(self) -> bool:
        if self.revoked_at is not None or self.expires_at <= utcnow():
            return False
        return self.max_downloads is None or self.download_count < self.max_downloads

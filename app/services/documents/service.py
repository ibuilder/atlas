"""Document lifecycle: upload, scan, link, retrieve, purge.

Uploads land in quarantine and stay there. Nothing is servable until the scan
pipeline clears it — including to the person who just uploaded it, because
"trusted because it is theirs" is how a malicious file reaches a colleague.

Retrieval is by signed, expiring URL rather than by a guessable path, so a link
that leaks is a time-boxed leak of one document rather than a directory.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from typing import BinaryIO

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import BusinessRuleViolation, NotFound, ValidationFailed
from app.logging import get_logger
from app.models.audit import AuditAction, AuditSeverity
from app.models.documents import (
    Document,
    DocumentCategory,
    DocumentLink,
    DocumentVisibility,
    RetentionClass,
    ScanStatus,
)
from app.models.types import utcnow
from app.observability import DOCUMENTS_SCANNED
from app.services.audit.recorder import record_audit_event
from app.services.documents.storage import (
    build_storage_key,
    digest_and_size,
    get_storage,
    sniff_content_type,
    validate_filename,
)

__all__ = [
    "SIGNED_URL_SALT",
    "link_document",
    "record_scan_result",
    "resolve_signed_token",
    "sign_document_token",
    "unlink_document",
    "upload_document",
]

log = get_logger("services.documents")

SIGNED_URL_SALT = "atlas.document.download"

#: Retention follows the *kind* of document, not a global default. A lease and a
#: maintenance photo have very different obligations, and applying one policy to
#: both means either keeping snapshots for a decade or destroying a contract
#: after three years.
_RETENTION_BY_CATEGORY: dict[DocumentCategory, RetentionClass] = {
    DocumentCategory.LEASE: RetentionClass.LEGAL,
    DocumentCategory.APPLICATION: RetentionClass.LEGAL,
    DocumentCategory.NOTICE: RetentionClass.LEGAL,
    DocumentCategory.COMPLIANCE: RetentionClass.LEGAL,
    DocumentCategory.INVOICE: RetentionClass.FINANCIAL,
    DocumentCategory.BILL: RetentionClass.FINANCIAL,
    DocumentCategory.RECEIPT: RetentionClass.FINANCIAL,
    DocumentCategory.STATEMENT: RetentionClass.FINANCIAL,
    DocumentCategory.INSURANCE: RetentionClass.FINANCIAL,
    DocumentCategory.WARRANTY: RetentionClass.FINANCIAL,
    DocumentCategory.PERMIT: RetentionClass.PERMANENT,
    # Identification is held only as long as the decision it supported needs it.
    DocumentCategory.IDENTIFICATION: RetentionClass.TRANSIENT,
    DocumentCategory.PHOTO: RetentionClass.OPERATIONAL,
    DocumentCategory.INSPECTION: RetentionClass.OPERATIONAL,
    DocumentCategory.MAINTENANCE: RetentionClass.OPERATIONAL,
    DocumentCategory.CORRESPONDENCE: RetentionClass.OPERATIONAL,
    DocumentCategory.REPORT: RetentionClass.OPERATIONAL,
    DocumentCategory.OTHER: RetentionClass.OPERATIONAL,
}


def retention_class_for(category: DocumentCategory) -> RetentionClass:
    return _RETENTION_BY_CATEGORY.get(category, RetentionClass.OPERATIONAL)


def upload_document(
    session: Session,
    *,
    org_id: str,
    stream: BinaryIO,
    filename: str,
    declared_content_type: str | None = None,
    name: str | None = None,
    category: DocumentCategory = DocumentCategory.OTHER,
    visibility: DocumentVisibility = DocumentVisibility.INTERNAL,
    description: str | None = None,
    tags: list[str] | None = None,
    uploaded_by_id: str | None = None,
    tenant_prefix: str | None = None,
    links: list[tuple[str, str, str]] | None = None,
) -> Document:
    """Validate, store, and register an upload.

    ``links`` are ``(entity_type, entity_id, relation)`` triples applied in the
    same transaction, so a document is never briefly orphaned.
    """
    settings = current_app.config["SETTINGS"]

    safe_name, extension = validate_filename(filename)
    checksum, size, head = digest_and_size(stream, max_bytes=settings.upload_max_bytes)
    content_type = sniff_content_type(head, declared_content_type, safe_name)

    # Content-addressed deduplication. The same lease PDF attached from three
    # places is one object with three links, not three objects that drift.
    #
    # Restricted to documents the uploader already has a claim on. Deduplicating
    # against *any* matching document would hand back a row they may not read:
    # residents hold DOCUMENT_UPLOAD, so uploading a file whose bytes match an
    # internal notice would disclose that document's identity, name, and
    # filename - none of which they supplied.
    existing = (
        session.execute(
            select(Document).where(
                Document.org_id == org_id,
                Document.checksum_sha256 == checksum,
                Document.size_bytes == size,
                Document.uploaded_by_id == uploaded_by_id,
            )
        )
        .scalars()
        .first()
        if uploaded_by_id is not None
        else None
    )

    if existing is not None:
        log.info(
            "duplicate upload linked to the existing document",
            extra={"event": "document.deduplicated", "document_id": existing.id},
        )
        for entity_type, entity_id, relation in links or []:
            link_document(
                session,
                document=existing,
                entity_type=entity_type,
                entity_id=entity_id,
                relation=relation,
            )
        return existing

    scan_required = settings.malware_scan_required
    key = build_storage_key(
        tenant_prefix=tenant_prefix or f"org/{org_id}",
        extension=extension,
    )

    document = Document(
        org_id=org_id,
        name=name or safe_name,
        description=description,
        original_filename=safe_name,
        content_type=content_type,
        size_bytes=size,
        checksum_sha256=checksum,
        storage_backend=settings.storage_backend,
        storage_key=key,
        category=category,
        visibility=visibility,
        tags=tags or [],
        scan_status=ScanStatus.PENDING if scan_required else ScanStatus.SKIPPED,
        is_quarantined=scan_required,
        uploaded_by_id=uploaded_by_id,
        # Set explicitly rather than relying on the column default: that default
        # is not applied until flush, and the retention date is computed from it
        # here.
        retention_class=retention_class_for(category),
    )
    document.apply_retention()
    session.add(document)
    # Flushed before the bytes are written. If the insert fails - a unique key
    # collision, a foreign key violation - the object is never created, so a
    # rolled-back upload cannot leave an unreferenced object in the bucket.
    session.flush()
    get_storage().put(key, stream)

    for entity_type, entity_id, relation in links or []:
        link_document(
            session,
            document=document,
            entity_type=entity_type,
            entity_id=entity_id,
            relation=relation,
        )

    record_audit_event(
        action=AuditAction.DOCUMENT_UPLOADED,
        resource_type="Document",
        resource_id=document.id,
        resource_label=document.name,
        payload={
            "content_type": content_type,
            "size_bytes": size,
            "category": str(category),
            "quarantined": scan_required,
        },
        org_id=org_id,
        session=session,
    )

    # The scan is enqueued by the caller *after* it commits. Enqueuing here
    # would race: a worker can pick the job up before the transaction lands and
    # find no row, and a rolled-back upload would leave a task hunting a
    # document that never existed.
    return document


def record_scan_result(
    session: Session,
    *,
    document: Document,
    clean: bool,
    detail: str | None = None,
) -> Document:
    """Apply a scan verdict and release or destroy the object accordingly."""
    document.scanned_at = utcnow()
    document.scan_detail = detail

    if not clean:
        document.scan_status = ScanStatus.INFECTED
        document.is_quarantined = True
        DOCUMENTS_SCANNED.labels("infected").inc()
        record_audit_event(
            action=AuditAction.DOCUMENT_QUARANTINED,
            resource_type="Document",
            resource_id=document.id,
            resource_label=document.name,
            payload={"detail": detail},
            severity=AuditSeverity.CRITICAL,
            org_id=document.org_id,
            session=session,
        )
        session.flush()
        return document

    # Released by flag, not by moving the object. Renaming across storage and
    # the database cannot be made atomic: if the move lands and the commit then
    # fails, the row points at a key that no longer exists and the document is
    # unreadable forever. Every retrieval path already gates on ``is_servable``,
    # which is derived from these two columns and so cannot disagree with itself.
    document.scan_status = ScanStatus.CLEAN
    document.is_quarantined = False
    DOCUMENTS_SCANNED.labels("clean").inc()
    session.flush()
    return document


def link_document(
    session: Session,
    *,
    document: Document,
    entity_type: str,
    entity_id: str,
    relation: str = "attachment",
    is_primary: bool = False,
    note: str | None = None,
) -> DocumentLink:
    """Attach a document to any entity. Idempotent for an identical edge.

    This is the document graph: one certificate of insurance is the vendor's
    compliance record, an attachment on four work orders, and evidence in a
    claim - with one expiry date rather than four copies that drift.
    """
    entity_type = (entity_type or "").strip().lower()
    if not entity_type or not entity_id:
        raise ValidationFailed("A document link requires an entity type and identifier.")

    existing = (
        session.execute(
            select(DocumentLink).where(
                DocumentLink.document_id == document.id,
                DocumentLink.entity_type == entity_type,
                DocumentLink.entity_id == entity_id,
                DocumentLink.relation == relation,
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing

    link = DocumentLink(
        org_id=document.org_id,
        document_id=document.id,
        entity_type=entity_type,
        entity_id=entity_id,
        relation=relation,
        is_primary=is_primary,
        note=note,
    )
    session.add(link)
    session.flush()
    return link


def unlink_document(session: Session, *, link: DocumentLink) -> None:
    session.delete(link)
    session.flush()


def documents_for(
    session: Session, *, org_id: str, entity_type: str, entity_id: str
) -> list[Document]:
    """Every document linked to one entity."""
    stmt = (
        select(Document)
        .join(DocumentLink, DocumentLink.document_id == Document.id)
        .where(
            Document.org_id == org_id,
            DocumentLink.entity_type == entity_type.lower(),
            DocumentLink.entity_id == entity_id,
        )
        .order_by(DocumentLink.sort_order, Document.created_at.desc())
    )
    return list(session.execute(stmt).scalars())


# ---------------------------------------------------------------------------
# Signed retrieval
# ---------------------------------------------------------------------------


def _serializer() -> URLSafeTimedSerializer:
    settings = current_app.config["SETTINGS"]
    return URLSafeTimedSerializer(settings.secret_key.get_secret_value(), salt=SIGNED_URL_SALT)


def sign_document_token(document: Document, *, actor_id: str | None = None) -> str:
    """Mint a time-limited retrieval token.

    The actor is embedded so a leaked link is attributable, and so a download can
    be audited against the person the link was issued to rather than whoever
    happens to present it.
    """
    return _serializer().dumps({"d": document.id, "o": document.org_id, "a": actor_id})


def resolve_signed_token(session: Session, token: str) -> Document:
    """Validate a token and return its document, or raise.

    Resolved unscoped, deliberately. A signed link is designed to be emailed, so
    the recipient has no session and middleware has bound no organization -
    under strict tenancy a scoped lookup raises on every such request, breaking
    the feature for the exact case it exists for.

    The tenant check does not disappear, it moves: the organization is carried
    inside the signed payload and compared below. The signature establishes
    authority here, and it cannot be forged without the secret key.
    """
    from app.models.base import unscoped

    settings = current_app.config["SETTINGS"]
    try:
        payload = _serializer().loads(token, max_age=settings.signed_url_ttl_seconds)
    except SignatureExpired as exc:
        raise ValidationFailed("This download link has expired.") from exc
    except BadSignature as exc:
        raise NotFound("The requested document was not found.") from exc

    with unscoped(session):
        document = session.get(Document, payload.get("d"))

    if document is None or document.org_id != payload.get("o"):
        raise NotFound("The requested document was not found.")
    return document


def open_document(session: Session, *, document: Document, actor_id: str | None = None) -> BinaryIO:
    """Return a readable stream, refusing anything not cleared to be served."""
    if not document.is_servable:
        raise BusinessRuleViolation(
            "This document is not available for download."
            if document.scan_status != ScanStatus.PENDING
            else "This document is still being scanned."
        )

    document.last_accessed_at = utcnow()
    document.download_count += 1

    record_audit_event(
        action=AuditAction.DOCUMENT_DOWNLOADED,
        resource_type="Document",
        resource_id=document.id,
        resource_label=document.name,
        payload={"content_type": document.content_type},
        org_id=document.org_id,
        actor_id=actor_id,
        session=session,
    )
    session.flush()
    return get_storage().get(document.storage_key)


def purge_expired_documents(session: Session, *, org_id: str, limit: int = 500) -> int:
    """Delete documents past their retention date.

    A legal hold outranks every retention rule; ``is_purgeable`` already accounts
    for it, and the filter here is belt and braces on the most destructive
    operation in the system.
    """
    today = dt.date.today()
    candidates = (
        session.execute(
            select(Document)
            .where(
                Document.org_id == org_id,
                Document.legal_hold.is_(False),
                Document.retention_until.is_not(None),
                Document.retention_until <= today,
            )
            .limit(limit)
        )
        .scalars()
        .all()
    )

    purged = 0
    for document in candidates:
        if not document.is_purgeable:
            continue
        record_audit_event(
            action=AuditAction.DOCUMENT_DELETED,
            resource_type="Document",
            resource_id=document.id,
            resource_label=document.name,
            payload={
                "reason": "retention_expired",
                "retained_until": str(document.retention_until),
            },
            severity=AuditSeverity.NOTICE,
            org_id=org_id,
            session=session,
        )
        document.soft_delete(reason="retention_expired")
        purged += 1

    session.flush()
    return purged


def delete_purged_objects(session: Session, *, org_id: str, limit: int = 500) -> int:
    """Destroy the bytes of documents already committed as purged.

    Deliberately a second pass, run after :func:`purge_expired_documents` has
    committed. Deleting objects in the same transaction risks the worst possible
    ordering: the bytes go, the commit then fails, and the row reverts to
    claiming a document that no longer exists. Splitting the two makes the
    failure mode an orphaned object - wasteful, recoverable, detectable -
    instead of silent data loss.
    """
    from app.models.base import include_deleted

    storage = get_storage()
    deleted = 0

    with include_deleted(session):
        candidates = (
            session.execute(
                select(Document)
                .where(
                    Document.org_id == org_id,
                    Document.deleted_at.is_not(None),
                    Document.delete_reason == "retention_expired",
                )
                .limit(limit)
            )
            .scalars()
            .all()
        )

    for document in candidates:
        try:
            storage.delete(document.storage_key)
            deleted += 1
        except Exception:  # noqa: BLE001 - one bad object must not stop the sweep
            log.exception(
                "failed to delete a purged object",
                extra={"event": "document.purge_failed", "document_id": document.id},
            )
    return deleted

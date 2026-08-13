"""Document upload, linking, and signed retrieval.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from flask import Response, request, send_file, url_for
from flask_login import current_user
from sqlalchemy import select

from app.api.helpers import (
    paginate,
    parse_body,
    parse_query,
    respond,
    respond_collection,
    respond_created,
)
from app.api.v1 import api_v1_bp
from app.errors import NotFound, ValidationFailed
from app.extensions import current_session, db
from app.middleware import require_org_scope
from app.models.documents import Document, DocumentCategory, DocumentLink, DocumentVisibility
from app.schemas.operations import DocumentLinkCreate, DocumentListQuery, DocumentOut
from app.security.permissions import Perm
from app.security.policies import require
from app.services.common.unit_of_work import transaction
from app.services.documents import service as documents

__all__ = []


def _get_document(document_id: str) -> Document:
    record = db.session.get(Document, document_id)
    if record is None:
        raise NotFound("That document was not found.")
    return record


@api_v1_bp.get("/documents", endpoint="documents_list")
def list_documents() -> Response:
    """List documents, optionally scoped to one linked entity."""
    require(Perm.DOCUMENT_READ)
    query = parse_query(DocumentListQuery)
    org_id = require_org_scope()

    stmt = select(Document).where(Document.org_id == org_id)
    if query.category:
        stmt = stmt.where(Document.category == query.category)
    if query.entity_type and query.entity_id:
        # EXISTS rather than a join: a document may hold several relations to
        # the same entity (an attachment and a signed copy), and a join would
        # return it once per link - consuming page slots and corrupting the
        # keyset cursor's has_more calculation.
        stmt = stmt.where(
            select(DocumentLink.id)
            .where(
                DocumentLink.document_id == Document.id,
                DocumentLink.entity_type == query.entity_type.lower(),
                DocumentLink.entity_id == query.entity_id,
            )
            .exists()
        )
    if query.q:
        stmt = stmt.where(Document.name.ilike(f"%{query.q}%"))

    page = paginate(current_session(), stmt, Document, limit=query.limit, cursor=query.cursor)
    return respond_collection(page, DocumentOut)


@api_v1_bp.post("/documents", endpoint="documents_create")
def upload_document() -> Response:
    """Upload a document.

    Multipart. The file lands in quarantine and is not retrievable until the
    scan pipeline clears it - including by the uploader, because "safe because
    it is mine" is how a malicious file reaches a colleague.

    Optional form fields: ``name``, ``category``, ``visibility``, ``description``,
    ``entity_type`` and ``entity_id`` to link on creation.
    """
    require(Perm.DOCUMENT_UPLOAD)
    org_id = require_org_scope()

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise ValidationFailed(
            "A file is required.",
            details=[{"field": "file", "message": "Attach a file under the 'file' field."}],
        )

    form = request.form
    links: list[tuple[str, str, str]] = []
    if form.get("entity_type") and form.get("entity_id"):
        links.append((form["entity_type"], form["entity_id"], form.get("relation") or "attachment"))

    category = _enum_or_default(DocumentCategory, form.get("category"), DocumentCategory.OTHER)
    visibility = _enum_or_default(
        DocumentVisibility, form.get("visibility"), DocumentVisibility.INTERNAL
    )

    organization = _organization(org_id)

    with transaction() as session:
        record = documents.upload_document(
            session,
            org_id=org_id,
            stream=upload.stream,
            filename=upload.filename,
            declared_content_type=upload.mimetype,
            name=form.get("name") or None,
            category=category,
            visibility=visibility,
            description=form.get("description") or None,
            uploaded_by_id=current_user.id,
            tenant_prefix=getattr(organization, "storage_prefix", None),
            links=links,
        )

    # Enqueued after the commit: a worker that picks the job up before the
    # transaction lands would find no row.
    if record.is_quarantined:
        from app.tasks.jobs import scan_document

        scan_document.delay(record.id)

    return respond_created(
        DocumentOut.model_validate(record, from_attributes=True),
        location=url_for("api_v1.documents_get", document_id=record.id),
    )


@api_v1_bp.get("/documents/<id:document_id>", endpoint="documents_get")
def get_document(document_id: str) -> Response:
    """Document metadata, including scan state."""
    record = _get_document(document_id)
    require(Perm.DOCUMENT_READ, record)
    return respond(DocumentOut.model_validate(record, from_attributes=True))


@api_v1_bp.post("/documents/<id:document_id>/links", endpoint="documents_link")
def link_document(document_id: str) -> Response:
    """Attach a document to another entity.

    This is the document graph: one certificate of insurance can be the vendor's
    compliance record and evidence on four work orders, with one expiry date
    rather than four copies that drift apart.
    """
    record = _get_document(document_id)
    require(Perm.DOCUMENT_UPLOAD, record)
    payload = parse_body(DocumentLinkCreate)

    with transaction() as session:
        link = documents.link_document(
            session,
            document=record,
            entity_type=payload.entity_type,
            entity_id=payload.entity_id,
            relation=payload.relation,
            is_primary=payload.is_primary,
        )

    return respond_created(
        {
            "id": link.id,
            "document_id": link.document_id,
            "entity_type": link.entity_type,
            "entity_id": link.entity_id,
            "relation": link.relation,
        }
    )


@api_v1_bp.post("/documents/<id:document_id>/download-url", endpoint="documents_download_url")
def create_download_url(document_id: str) -> Response:
    """Mint a signed, expiring retrieval link."""
    record = _get_document(document_id)
    require(Perm.DOCUMENT_READ, record)

    if not record.is_servable:
        raise ValidationFailed(
            "This document is not yet available."
            if record.scan_status == "pending"
            else "This document is not available for download."
        )

    from flask import current_app

    token = documents.sign_document_token(record, actor_id=current_user.id)
    return respond(
        {
            "url": url_for("api_v1.documents_download", token=token, _external=False),
            "expires_in": current_app.config["SETTINGS"].signed_url_ttl_seconds,
        }
    )


@api_v1_bp.get("/documents/download/<token>", endpoint="documents_download")
def download_document(token: str) -> Response:
    """Retrieve a document by signed token.

    Authenticated by the token rather than by session, so a link can be emailed.
    The token is time-limited and carries the organization it was minted for, and
    a quarantined document is refused regardless of a valid token.
    """
    with transaction() as session:
        record = documents.resolve_signed_token(session, token)
        stream = documents.open_document(session, document=record)

    response = send_file(
        stream,
        mimetype=record.content_type,
        as_attachment=True,
        download_name=record.original_filename,
    )
    # Never let an intermediary cache a tenant's document.
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Belt and braces against a stored HTML document executing in our origin.
    response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


def _enum_or_default(enum_cls, value, default):  # noqa: ANN001, ANN202
    if not value:
        return default
    try:
        return enum_cls(value)
    except ValueError as exc:
        valid = ", ".join(member.value for member in enum_cls)
        raise ValidationFailed(f"Unknown value {value!r}. Expected one of: {valid}.") from exc


def _organization(org_id: str):  # noqa: ANN202
    from app.models.org import Organization

    return db.session.get(Organization, org_id)

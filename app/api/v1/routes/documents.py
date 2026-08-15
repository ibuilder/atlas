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
from app.models.documents import (
    Document,
    DocumentCategory,
    DocumentLink,
    DocumentVisibility,
    SignatureEnvelope,
)
from app.schemas.operations import (
    DocumentLinkCreate,
    DocumentListQuery,
    DocumentOut,
    EnvelopeCreate,
    EnvelopeListQuery,
    EnvelopeOut,
    EnvelopeSignerOut,
    EnvelopeVoid,
    ExtractionDecision,
)
from app.security.permissions import Perm
from app.security.policies import require
from app.services.common.unit_of_work import transaction
from app.services.documents import esign, extraction
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


# ------------------------------------------------------------------- e-sign
#
# Raising and withdrawing envelopes is staff work behind ESIGN_MANAGE. Signing
# is not here: a signer is authorised by being named on the envelope, and that
# happens in their own portal where the consent wording is shown.


@api_v1_bp.get("/envelopes", endpoint="envelopes_list")
def list_envelopes() -> Response:
    """Envelopes, newest first, optionally by subject or status."""
    require(Perm.DOCUMENT_READ)
    org_id = require_org_scope()

    query = parse_query(EnvelopeListQuery)

    stmt = select(SignatureEnvelope).where(
        SignatureEnvelope.org_id == org_id, SignatureEnvelope.deleted_at.is_(None)
    )
    if query.status:
        stmt = stmt.where(SignatureEnvelope.status == query.status)
    if query.subject_type and query.subject_id:
        stmt = stmt.where(
            SignatureEnvelope.subject_type == query.subject_type,
            SignatureEnvelope.subject_id == query.subject_id,
        )

    page = paginate(
        current_session(), stmt, SignatureEnvelope, limit=query.limit, cursor=query.cursor
    )
    return respond_collection(page, EnvelopeOut)


@api_v1_bp.get("/envelopes/<id:envelope_id>", endpoint="envelopes_get")
def get_envelope(envelope_id: str) -> Response:
    """One envelope with its parties and their consent records."""
    require(Perm.DOCUMENT_READ)
    org_id = require_org_scope()

    record = db.session.get(SignatureEnvelope, envelope_id)
    if record is None or record.org_id != org_id or record.deleted_at is not None:
        raise NotFound("That envelope was not found.")

    return respond(
        {
            **EnvelopeOut.model_validate(record, from_attributes=True).model_dump(mode="json"),
            "signers": [
                EnvelopeSignerOut.model_validate(signer, from_attributes=True).model_dump(
                    mode="json"
                )
                for signer in record.signers
            ],
        }
    )


@api_v1_bp.post("/envelopes", endpoint="envelopes_create")
def create_signature_envelope() -> Response:
    """Draft an envelope. Nothing is delivered until it is sent."""
    require(Perm.ESIGN_MANAGE)
    payload = parse_body(EnvelopeCreate)
    org_id = require_org_scope()

    with transaction() as session:
        record = esign.create_envelope(
            session,
            org_id=org_id,
            document_id=payload.document_id,
            title=payload.title,
            reference=payload.reference,
            signers=[
                esign.SignerInput(name=s.name, email=s.email, role=s.role) for s in payload.signers
            ],
            subject_type=payload.subject_type,
            subject_id=payload.subject_id,
            expires_in_days=payload.expires_in_days,
            actor_id=current_user.id,
        )

    return respond_created(
        EnvelopeOut.model_validate(record, from_attributes=True),
        location=f"/api/v1/envelopes/{record.id}",
    )


@api_v1_bp.post("/envelopes/<id:envelope_id>/send", endpoint="envelopes_send")
def send_signature_envelope(envelope_id: str) -> Response:
    """Deliver it, pinning the document being signed."""
    require(Perm.ESIGN_MANAGE)
    org_id = require_org_scope()

    with transaction() as session:
        record = session.get(SignatureEnvelope, envelope_id)
        if record is None or record.org_id != org_id or record.deleted_at is not None:
            raise NotFound("That envelope was not found.")
        esign.send_envelope(session, envelope=record, actor_id=current_user.id)

    return respond(EnvelopeOut.model_validate(record, from_attributes=True))


@api_v1_bp.post("/envelopes/<id:envelope_id>/void", endpoint="envelopes_void")
def void_signature_envelope(envelope_id: str) -> Response:
    """Withdraw it. A completed envelope cannot be withdrawn."""
    require(Perm.ESIGN_MANAGE)
    payload = parse_body(EnvelopeVoid)
    org_id = require_org_scope()

    with transaction() as session:
        record = session.get(SignatureEnvelope, envelope_id)
        if record is None or record.org_id != org_id or record.deleted_at is not None:
            raise NotFound("That envelope was not found.")
        esign.void_envelope(
            session, envelope=record, reason=payload.reason, actor_id=current_user.id
        )

    return respond(EnvelopeOut.model_validate(record, from_attributes=True))


# -------------------------------------------------------------- extraction
#
# Extraction produces *suggestions*, and the only path from a suggestion to a
# value the system will act on is a person accepting it. That accept is
# attributed and audited, so "why does it say this?" answers with a name and a
# sentence rather than a shrug.
#
# The extraction itself is derived from the document's text on every call
# rather than stored. A stored one drifts from the document after a re-OCR
# without anybody noticing, and a suggestion that no longer matches its
# evidence is worse than no suggestion at all.


@api_v1_bp.get("/documents/<id:document_id>/extraction", endpoint="documents_extraction_get")
def get_extraction(document_id: str) -> Response:
    """What this document appears to say, with the evidence for each reading."""
    require(Perm.DOCUMENT_READ)
    # Called for the refusal, not the value: an unscoped request must not fall
    # through to the ORM guard and read as "not found" rather than "no tenant".
    require_org_scope()

    document = _get_document(document_id)
    result = extraction.extraction_for(document)
    return respond(
        {
            "document_id": document.id,
            "kind": result.kind,
            "is_confident": result.is_confident,
            "review_threshold": extraction.REVIEW_THRESHOLD,
            "suggestions": [
                {
                    "field": suggestion.field,
                    "value": str(suggestion.value),
                    "confidence": suggestion.confidence,
                    "needs_review": suggestion.needs_review,
                    # The text it was read from, so a caller can check rather
                    # than trust. A score with nothing behind it just moves the
                    # guess from the machine to whoever reads the response.
                    "evidence": suggestion.evidence,
                    "accepted_at": (
                        suggestion.accepted_at.isoformat() if suggestion.accepted_at else None
                    ),
                    "accepted_by_id": suggestion.accepted_by_id,
                    "rejected_at": (
                        suggestion.rejected_at.isoformat() if suggestion.rejected_at else None
                    ),
                }
                for suggestion in result.suggestions
            ],
            #: Fields looked for and not found. As wrong as a bad reading and
            #: much easier to overlook.
            "missing": result.missing,
        }
    )


@api_v1_bp.post(
    "/documents/<id:document_id>/extraction/<field_name>", endpoint="documents_extraction_decide"
)
def decide_extraction(document_id: str, field_name: str) -> Response:
    """Accept a reading, correct it, or throw it out."""
    require(Perm.DOCUMENT_EXTRACTION_REVIEW)
    payload = parse_body(ExtractionDecision)
    org_id = require_org_scope()

    with transaction() as session:
        document = _get_document(document_id)
        result = extraction.extraction_for(document)
        if payload.decision == "accept":
            extraction.accept_suggestion(
                session,
                extraction=result,
                field_name=field_name,
                accepted_by_id=current_user.id,
                org_id=org_id,
                value=payload.value,
            )
        else:
            extraction.reject_suggestion(
                result, field_name=field_name, rejected_by_id=current_user.id
            )
        extraction.record_decisions(document, result)
        decided = result.by_field(field_name)

    return respond(
        {
            "field": field_name,
            "value": str(decided.value) if decided else None,
            "accepted_at": decided.accepted_at.isoformat()
            if decided and decided.accepted_at
            else None,
            "rejected_at": decided.rejected_at.isoformat()
            if decided and decided.rejected_at
            else None,
        }
    )

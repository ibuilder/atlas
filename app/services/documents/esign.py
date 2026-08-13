"""Electronic signature: the envelope lifecycle, behind an adapter.

Atlas does not implement a signature provider. It implements the *lifecycle* -
draft, sent, partially signed, completed, and the ways it can fail - so that
adopting DocuSign, Dropbox Sign, or an in-house flow is a configuration change
rather than a rewrite. The same shape as document scanning, for the same reason.

Three rules make a signature evidence rather than decoration, and each is
enforced here rather than left to the provider.

**A signature is on a specific artifact.** The document's SHA-256 is captured
when the envelope is sent and re-checked before it completes. Without that,
"they signed it" and "they signed *this*" are different claims and only the
weaker one is provable; with it, a document swapped underneath an open envelope
fails loudly instead of quietly inheriting somebody's consent.

**Consent evidence is captured at the moment, not reconstructed.** What the
signer typed, from which address, with which client, and when. Under most
electronic signature statutes this is what enforceability rests on, and none of
it can be recovered after the fact.

**An envelope completes only when every signer has signed.** Not most of them,
not the ones the caller remembered to check. A lease executed on one of two
required signatures is not executed.

The default provider is ``internal``: signing happens in Atlas, through the
portal, and the record above *is* the evidence. It is a real implementation
rather than a stub - a great many operators need nothing else - and it is
honest about being a typed-name signature rather than a certificate-backed one.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from typing import Protocol

from flask import current_app
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import BusinessRuleViolation, NotFound, ValidationFailed
from app.logging import get_logger
from app.models.audit import AuditAction, AuditOutcome, AuditSeverity
from app.models.documents import (
    Document,
    EnvelopeSigner,
    EnvelopeStatus,
    SignatureEnvelope,
    SignerStatus,
)
from app.models.types import utcnow
from app.services.audit.recorder import record_audit_event

__all__ = [
    "DEFAULT_CONSENT",
    "HttpProvider",
    "DEFAULT_EXPIRY_DAYS",
    "EsignProvider",
    "InternalProvider",
    "SignerInput",
    "create_envelope",
    "decline_envelope",
    "envelopes_for_subject",
    "expire_envelopes",
    "get_provider",
    "record_signature",
    "sha256_of",
    "send_envelope",
    "void_envelope",
]

log = get_logger("services.documents.esign")

#: An envelope nobody signs should lapse rather than sit open indefinitely,
#: because an open envelope is a document somebody can still sign a year later.
DEFAULT_EXPIRY_DAYS = 30

#: Shown to the signer and stored with the signature. Wording matters: consent
#: to *do business electronically* is a distinct thing from agreement to the
#: document, and statutes generally want both.
DEFAULT_CONSENT = (
    "By typing my name below I agree to do business electronically and I adopt "
    "the typed name as my signature on this document."
)


@dataclass(frozen=True)
class SignerInput:
    """One party to be added to an envelope."""

    name: str
    email: str
    role: str | None = None


class EsignProvider(Protocol):
    """What a signature provider has to do.

    Deliberately small. Everything about *what a signature means* lives in this
    module; a provider only has to deliver the envelope and tell us who signed.
    """

    name: str

    def send(self, envelope: SignatureEnvelope) -> str | None:
        """Deliver the envelope. Returns the provider's own identifier, if any."""
        ...


class InternalProvider:
    """Signing inside Atlas, through the portal.

    A real implementation, not a placeholder: the signer opens the document from
    their portal, types their name, and the consent record captured at that
    moment is the evidence. What it is not is a certificate-backed digital
    signature, and it does not claim to be - see the ADR.
    """

    name = "internal"

    def send(self, envelope: SignatureEnvelope) -> str | None:
        log.info(
            "envelope ready for internal signature",
            extra={
                "event": "esign.sent",
                "envelope_id": envelope.id,
                "signers": len(envelope.signers),
            },
        )
        return None


class HttpProvider:
    """A remote provider, configured per deployment.

    Unimplemented on purpose rather than half-implemented: every provider's API
    differs enough that a generic client would be wrong for all of them, and a
    stub that silently succeeds is worse than one that refuses. Selecting
    ``http`` without supplying an implementation fails at send time, loudly, in
    a deployment - not in production on the first real lease.
    """

    name = "http"

    def send(self, envelope: SignatureEnvelope) -> str | None:
        raise ValidationFailed(
            "The 'http' e-sign backend is selected but no provider client is "
            "configured. Register one, or set ESIGN_BACKEND=mock to sign inside "
            "Atlas."
        )


def get_provider() -> EsignProvider:
    """The configured provider. ``mock`` means signing happens in Atlas."""
    backend = current_app.config["SETTINGS"].esign_backend
    return HttpProvider() if backend == "http" else InternalProvider()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _document(session: Session, *, org_id: str, document_id: str) -> Document:
    record = session.get(Document, document_id)
    if record is None or record.org_id != org_id or record.deleted_at is not None:
        raise NotFound("That document was not found.")
    return record


def _digest(document: Document) -> str:
    """The document's content hash, computed at upload and stored with it."""
    return document.checksum_sha256


def create_envelope(
    session: Session,
    *,
    org_id: str,
    document_id: str,
    title: str,
    reference: str,
    signers: list[SignerInput],
    subject_type: str | None = None,
    subject_id: str | None = None,
    expires_in_days: int = DEFAULT_EXPIRY_DAYS,
    actor_id: str | None = None,
) -> SignatureEnvelope:
    """Draft an envelope. Nothing is delivered until it is sent."""
    if not signers:
        raise ValidationFailed("An envelope needs at least one signer.")

    seen: set[str] = set()
    for signer in signers:
        address = (signer.email or "").strip().lower()
        if not address or "@" not in address:
            raise ValidationFailed(f"{signer.email!r} is not an email address.")
        if not (signer.name or "").strip():
            raise ValidationFailed("Every signer needs a name.")
        if address in seen:
            raise ValidationFailed(
                f"{address} appears twice. One party signs once; two rows for the "
                "same person makes 'everyone has signed' ambiguous."
            )
        seen.add(address)

    document = _document(session, org_id=org_id, document_id=document_id)

    envelope = SignatureEnvelope(
        org_id=org_id,
        reference=reference,
        title=title,
        document_id=document.id,
        subject_type=subject_type,
        subject_id=subject_id,
        status=EnvelopeStatus.DRAFT,
        provider=get_provider().name,
        expires_at=utcnow() + dt.timedelta(days=expires_in_days),
    )
    session.add(envelope)
    session.flush()

    for index, signer in enumerate(signers, start=1):
        session.add(
            EnvelopeSigner(
                org_id=org_id,
                envelope=envelope,
                sequence=index,
                name=signer.name.strip()[:150],
                email=signer.email.strip().lower()[:255],
                role=signer.role,
            )
        )
    session.flush()

    record_audit_event(
        action=AuditAction.ENVELOPE_CREATED,
        resource_type="SignatureEnvelope",
        resource_id=envelope.id,
        resource_label=reference,
        severity=AuditSeverity.NOTICE,
        payload={
            "document_id": document.id,
            "signers": [s.email for s in envelope.signers],
            "subject_type": subject_type,
            "subject_id": subject_id,
        },
        reason="Signature envelope drafted.",
        org_id=org_id,
        actor_id=actor_id,
        session=session,
    )
    return envelope


def send_envelope(
    session: Session, *, envelope: SignatureEnvelope, actor_id: str | None = None
) -> SignatureEnvelope:
    """Deliver it, and pin the artifact being signed.

    The document digest is captured here. From this moment the envelope is a
    signature on *that* document, and completion re-checks it.
    """
    if envelope.status != EnvelopeStatus.DRAFT:
        raise BusinessRuleViolation(f"A {envelope.status} envelope cannot be sent.")

    document = _document(session, org_id=envelope.org_id, document_id=envelope.document_id)
    envelope.document_sha256 = _digest(document)
    envelope.status = EnvelopeStatus.SENT
    envelope.sent_at = utcnow()
    envelope.external_id = get_provider().send(envelope)
    session.flush()

    record_audit_event(
        action=AuditAction.ENVELOPE_SENT,
        resource_type="SignatureEnvelope",
        resource_id=envelope.id,
        resource_label=envelope.reference,
        severity=AuditSeverity.NOTICE,
        payload={
            "recipients": [s.email for s in envelope.signers],
            "document_sha256": envelope.document_sha256,
            "expires_at": envelope.expires_at.isoformat() if envelope.expires_at else None,
        },
        reason="Signature envelope sent.",
        org_id=envelope.org_id,
        actor_id=actor_id,
        session=session,
    )
    return envelope


def _signer(envelope: SignatureEnvelope, email: str) -> EnvelopeSigner:
    address = (email or "").strip().lower()
    for signer in envelope.signers:
        if signer.email == address:
            return signer
    raise NotFound("That signer is not a party to this envelope.")


def record_signature(
    session: Session,
    *,
    envelope: SignatureEnvelope,
    email: str,
    typed_name: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
    consent_text: str = DEFAULT_CONSENT,
    signed_at: dt.datetime | None = None,
) -> SignatureEnvelope:
    """Record one party's signature, with the evidence that makes it one.

    Completion is decided here rather than by the caller: when the last pending
    signer signs, the envelope completes and the artifact is re-verified.
    """
    if envelope.status not in (EnvelopeStatus.SENT, EnvelopeStatus.PARTIALLY_SIGNED):
        raise BusinessRuleViolation(
            f"A {envelope.status} envelope cannot be signed. Send it first, and "
            "check it has not expired or been voided."
        )
    if envelope.expires_at is not None and utcnow() > envelope.expires_at:
        raise BusinessRuleViolation(
            "That envelope expired. Issue a new one rather than signing a stale " "document."
        )

    name = (typed_name or "").strip()
    if not name:
        raise ValidationFailed("A signature needs a typed name.")

    signer = _signer(envelope, email)
    if signer.status == SignerStatus.SIGNED:
        return envelope
    if signer.status == SignerStatus.DECLINED:
        raise BusinessRuleViolation("That party declined. A decline is not reversible.")

    signer.status = SignerStatus.SIGNED
    signer.signed_at = signed_at or utcnow()
    signer.typed_name = name[:150]
    signer.signed_ip = (ip_address or "")[:45] or None
    signer.signed_user_agent = (user_agent or "")[:255] or None
    signer.consent_text = consent_text
    session.flush()

    record_audit_event(
        action=AuditAction.ENVELOPE_SIGNED,
        resource_type="EnvelopeSigner",
        resource_id=signer.id,
        resource_label=f"{envelope.reference} - {signer.email}",
        severity=AuditSeverity.NOTICE,
        payload={
            "envelope_id": envelope.id,
            "typed_name": signer.typed_name,
            "ip": signer.signed_ip,
            "user_agent": signer.signed_user_agent,
        },
        reason="Signature recorded.",
        org_id=envelope.org_id,
        session=session,
    )

    if envelope.outstanding_signers:
        envelope.status = EnvelopeStatus.PARTIALLY_SIGNED
        session.flush()
        return envelope

    return _complete(session, envelope=envelope)


def _complete(session: Session, *, envelope: SignatureEnvelope) -> SignatureEnvelope:
    """Close a fully signed envelope, after checking the artifact is unchanged."""
    document = _document(session, org_id=envelope.org_id, document_id=envelope.document_id)
    current = _digest(document)

    if envelope.document_sha256 and current and current != envelope.document_sha256:
        # The thing they consented to is not the thing on file. Refusing is the
        # only safe answer: completing would attribute their signature to a
        # document they never saw.
        envelope.status = EnvelopeStatus.VOIDED
        envelope.voided_reason = "The document changed after the envelope was sent."
        session.flush()

        record_audit_event(
            action=AuditAction.ENVELOPE_VOIDED,
            resource_type="SignatureEnvelope",
            resource_id=envelope.id,
            resource_label=envelope.reference,
            severity=AuditSeverity.CRITICAL,
            outcome=AuditOutcome.FAILURE,
            payload={"sent_sha256": envelope.document_sha256, "current_sha256": current},
            reason=(
                "Envelope voided: the document changed after it was sent, so the "
                "signatures cannot be attributed to it."
            ),
            org_id=envelope.org_id,
            session=session,
        )
        raise BusinessRuleViolation(
            "The document changed after this envelope was sent, so the signatures "
            "on it cannot be attributed to the file now on record. The envelope "
            "has been voided; issue a new one."
        )

    envelope.status = EnvelopeStatus.COMPLETED
    envelope.completed_at = utcnow()
    session.flush()

    _apply_to_subject(session, envelope=envelope)

    record_audit_event(
        action=AuditAction.ENVELOPE_COMPLETED,
        resource_type="SignatureEnvelope",
        resource_id=envelope.id,
        resource_label=envelope.reference,
        severity=AuditSeverity.NOTICE,
        payload={
            "signers": [
                {
                    "email": s.email,
                    "typed_name": s.typed_name,
                    "signed_at": s.signed_at.isoformat() if s.signed_at else None,
                    "ip": s.signed_ip,
                }
                for s in envelope.signers
            ],
            "document_sha256": envelope.document_sha256,
        },
        reason="Signature envelope completed by every party.",
        org_id=envelope.org_id,
        session=session,
    )
    return envelope


def _apply_to_subject(session: Session, *, envelope: SignatureEnvelope) -> None:
    """Let the signed thing know it was signed.

    Only a lease today. Kept in one place so the next subject type is a branch
    here rather than a duty on every caller to remember.
    """
    if envelope.subject_type != "lease" or not envelope.subject_id:
        return

    from app.models.leasing import Lease

    lease = session.get(Lease, envelope.subject_id)
    if lease is None or lease.org_id != envelope.org_id:
        return

    lease.esign_envelope_id = envelope.id
    lease.executed_at = envelope.completed_at
    session.flush()


def decline_envelope(
    session: Session,
    *,
    envelope: SignatureEnvelope,
    email: str,
    reason: str,
) -> SignatureEnvelope:
    """One party refuses, which ends it for everybody.

    A partly signed envelope with a refusal in it is not a document anyone can
    rely on, so the whole envelope declines rather than sitting in a state that
    reads as nearly done.
    """
    if envelope.is_terminal:
        raise BusinessRuleViolation(f"A {envelope.status} envelope cannot be declined.")

    text = (reason or "").strip()
    if not text:
        raise ValidationFailed("A decline needs a reason on the record.")

    signer = _signer(envelope, email)
    signer.status = SignerStatus.DECLINED
    signer.declined_at = utcnow()
    signer.decline_reason = text[:255]

    envelope.status = EnvelopeStatus.DECLINED
    session.flush()

    record_audit_event(
        action=AuditAction.ENVELOPE_DECLINED,
        resource_type="SignatureEnvelope",
        resource_id=envelope.id,
        resource_label=envelope.reference,
        severity=AuditSeverity.WARNING,
        outcome=AuditOutcome.FAILURE,
        payload={"declined_by": signer.email, "reason": text},
        reason="Signature declined.",
        org_id=envelope.org_id,
        session=session,
    )
    return envelope


def void_envelope(
    session: Session,
    *,
    envelope: SignatureEnvelope,
    reason: str,
    actor_id: str | None = None,
) -> SignatureEnvelope:
    """Withdraw an envelope. A completed one cannot be withdrawn."""
    if envelope.status == EnvelopeStatus.COMPLETED:
        raise BusinessRuleViolation(
            "A completed envelope cannot be voided. The signatures on it happened; "
            "supersede it with a new document instead."
        )
    text = (reason or "").strip()
    if not text:
        raise ValidationFailed("Voiding an envelope needs a reason.")

    envelope.status = EnvelopeStatus.VOIDED
    envelope.voided_reason = text[:255]
    session.flush()

    record_audit_event(
        action=AuditAction.ENVELOPE_VOIDED,
        resource_type="SignatureEnvelope",
        resource_id=envelope.id,
        resource_label=envelope.reference,
        severity=AuditSeverity.WARNING,
        payload={"reason": text},
        reason="Signature envelope voided.",
        org_id=envelope.org_id,
        actor_id=actor_id,
        session=session,
    )
    return envelope


def expire_envelopes(session: Session, *, org_id: str) -> int:
    """Lapse envelopes nobody completed. Idempotent.

    An envelope left open is a document somebody can still sign a year later,
    against terms that have moved on.
    """
    now = utcnow()
    stale = (
        session.execute(
            select(SignatureEnvelope).where(
                SignatureEnvelope.org_id == org_id,
                SignatureEnvelope.deleted_at.is_(None),
                SignatureEnvelope.status.in_(
                    [EnvelopeStatus.SENT, EnvelopeStatus.PARTIALLY_SIGNED]
                ),
                SignatureEnvelope.expires_at < now,
            )
        )
        .scalars()
        .all()
    )
    for envelope in stale:
        envelope.status = EnvelopeStatus.EXPIRED
    if stale:
        session.flush()
    return len(stale)


def envelopes_for_subject(
    session: Session, *, org_id: str, subject_type: str, subject_id: str
) -> list[SignatureEnvelope]:
    """Every envelope raised against one record, newest first."""
    return list(
        session.execute(
            select(SignatureEnvelope)
            .where(
                SignatureEnvelope.org_id == org_id,
                SignatureEnvelope.subject_type == subject_type,
                SignatureEnvelope.subject_id == subject_id,
                SignatureEnvelope.deleted_at.is_(None),
            )
            .order_by(SignatureEnvelope.created_at.desc())
        )
        .scalars()
        .all()
    )


def sha256_of(payload: bytes) -> str:
    """The digest helper the document layer and this module agree on."""
    return hashlib.sha256(payload).hexdigest()

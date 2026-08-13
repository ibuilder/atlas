"""Electronic signature: what makes one evidence rather than decoration.

Three tests carry this module. An envelope does not complete until every party
has signed. The consent record is captured at the moment rather than
reconstructed. And a document swapped underneath an open envelope voids it
loudly instead of quietly inheriting somebody's consent.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.errors import BusinessRuleViolation, NotFound, ValidationFailed
from app.models.documents import EnvelopeStatus, SignerStatus
from app.services.documents.esign import (
    DEFAULT_CONSENT,
    SignerInput,
    create_envelope,
    decline_envelope,
    envelopes_for_subject,
    expire_envelopes,
    record_signature,
    send_envelope,
    void_envelope,
)

pytestmark = pytest.mark.integration

ACTOR = "019fea00-0000-7000-8000-00000000c001"
RESIDENT = SignerInput(name="Dana Okonjo", email="dana@test.local", role="resident")
GUARANTOR = SignerInput(name="Sam Okonjo", email="sam@test.local", role="guarantor")


@pytest.fixture()
def document(db, org, scope):
    """A stored document with a real content digest."""
    from app.models.documents import Document, DocumentCategory, ScanStatus

    record = Document(
        org_id=org.id,
        name="Lease agreement",
        original_filename="lease.pdf",
        storage_key="documents/lease.pdf",
        content_type="application/pdf",
        size_bytes=2048,
        checksum_sha256="a" * 64,
        category=DocumentCategory.LEASE,
        scan_status=ScanStatus.CLEAN,
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture()
def envelope(db, org, scope, document, lease_record):
    record = create_envelope(
        db.session,
        org_id=org.id,
        document_id=document.id,
        title="Lease agreement",
        reference="ENV-0001",
        signers=[RESIDENT, GUARANTOR],
        subject_type="lease",
        subject_id=lease_record.id,
        actor_id=ACTOR,
    )
    db.session.commit()
    return record


# ---------------------------------------------------------------------------
# Drafting and sending
# ---------------------------------------------------------------------------


def test_an_envelope_drafts_with_its_signers(db, org, scope, envelope):
    assert envelope.status == EnvelopeStatus.DRAFT
    assert [signer.email for signer in envelope.signers] == [RESIDENT.email, GUARANTOR.email]
    assert [signer.sequence for signer in envelope.signers] == [1, 2]


def test_an_envelope_needs_a_signer(db, org, scope, document):
    with pytest.raises(ValidationFailed):
        create_envelope(
            db.session,
            org_id=org.id,
            document_id=document.id,
            title="Nobody signs this",
            reference="ENV-EMPTY",
            signers=[],
        )


def test_the_same_party_cannot_appear_twice(db, org, scope, document):
    """Two rows for one person makes 'everyone has signed' ambiguous."""
    with pytest.raises(ValidationFailed) as exc:
        create_envelope(
            db.session,
            org_id=org.id,
            document_id=document.id,
            title="Doubled",
            reference="ENV-DUP",
            signers=[RESIDENT, SignerInput(name="D. Okonjo", email="DANA@test.local")],
        )
    assert "appears twice" in str(exc.value)


def test_a_missing_document_is_not_found(db, org, scope):
    with pytest.raises(NotFound):
        create_envelope(
            db.session,
            org_id=org.id,
            document_id="019fea00-0000-7000-8000-0000000000ff",
            title="Nothing",
            reference="ENV-GONE",
            signers=[RESIDENT],
        )


def test_sending_pins_the_document_digest(db, org, scope, envelope, document):
    """From this moment it is a signature on *that* document."""
    send_envelope(db.session, envelope=envelope, actor_id=ACTOR)
    db.session.commit()

    assert envelope.status == EnvelopeStatus.SENT
    assert envelope.sent_at is not None
    assert envelope.document_sha256 == document.checksum_sha256


def test_an_unsent_envelope_cannot_be_signed(db, org, scope, envelope):
    with pytest.raises(BusinessRuleViolation) as exc:
        record_signature(
            db.session, envelope=envelope, email=RESIDENT.email, typed_name="Dana Okonjo"
        )
    assert "Send it first" in str(exc.value)


def test_sending_twice_is_refused(db, org, scope, envelope):
    send_envelope(db.session, envelope=envelope)
    with pytest.raises(BusinessRuleViolation):
        send_envelope(db.session, envelope=envelope)


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def test_one_signature_of_two_does_not_complete_it(db, org, scope, envelope):
    """A lease executed on one of two required signatures is not executed."""
    send_envelope(db.session, envelope=envelope)
    record_signature(db.session, envelope=envelope, email=RESIDENT.email, typed_name="Dana Okonjo")
    db.session.commit()

    assert envelope.status == EnvelopeStatus.PARTIALLY_SIGNED
    assert envelope.completed_at is None
    assert len(envelope.outstanding_signers) == 1


def test_the_last_signature_completes_it(db, org, scope, envelope, lease_record):
    send_envelope(db.session, envelope=envelope)
    record_signature(db.session, envelope=envelope, email=RESIDENT.email, typed_name="Dana Okonjo")
    record_signature(db.session, envelope=envelope, email=GUARANTOR.email, typed_name="Sam Okonjo")
    db.session.commit()

    assert envelope.status == EnvelopeStatus.COMPLETED
    assert envelope.completed_at is not None
    # And the thing that was signed knows it was signed.
    assert lease_record.esign_envelope_id == envelope.id
    assert lease_record.executed_at == envelope.completed_at


def test_the_consent_record_is_captured_at_the_moment(db, org, scope, envelope):
    """It is what enforceability rests on, and cannot be recovered later."""
    send_envelope(db.session, envelope=envelope)
    record_signature(
        db.session,
        envelope=envelope,
        email=RESIDENT.email,
        typed_name="Dana Okonjo",
        ip_address="198.51.100.24",
        user_agent="Mozilla/5.0 (portal)",
    )
    db.session.commit()

    signer = next(s for s in envelope.signers if s.email == RESIDENT.email)
    assert signer.status == SignerStatus.SIGNED
    assert signer.typed_name == "Dana Okonjo"
    assert signer.signed_ip == "198.51.100.24"
    assert signer.signed_user_agent == "Mozilla/5.0 (portal)"
    assert signer.consent_text == DEFAULT_CONSENT
    assert signer.signed_at is not None


def test_a_signature_needs_a_typed_name(db, org, scope, envelope):
    send_envelope(db.session, envelope=envelope)
    with pytest.raises(ValidationFailed):
        record_signature(db.session, envelope=envelope, email=RESIDENT.email, typed_name="  ")


def test_signing_twice_is_a_no_op(db, org, scope, envelope):
    send_envelope(db.session, envelope=envelope)
    record_signature(db.session, envelope=envelope, email=RESIDENT.email, typed_name="Dana")
    first = next(s for s in envelope.signers if s.email == RESIDENT.email).signed_at
    record_signature(db.session, envelope=envelope, email=RESIDENT.email, typed_name="Dana again")
    assert next(s for s in envelope.signers if s.email == RESIDENT.email).signed_at == first


def test_a_stranger_cannot_sign(db, org, scope, envelope):
    send_envelope(db.session, envelope=envelope)
    with pytest.raises(NotFound):
        record_signature(
            db.session, envelope=envelope, email="nobody@test.local", typed_name="Nobody"
        )


def test_an_expired_envelope_cannot_be_signed(db, org, scope, envelope):
    """An open envelope is a document somebody can still sign a year later."""
    send_envelope(db.session, envelope=envelope)
    envelope.expires_at = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    db.session.flush()

    with pytest.raises(BusinessRuleViolation) as exc:
        record_signature(
            db.session, envelope=envelope, email=RESIDENT.email, typed_name="Dana Okonjo"
        )
    assert "expired" in str(exc.value)


# ---------------------------------------------------------------------------
# The artifact check, which is the whole point
# ---------------------------------------------------------------------------


def test_a_document_swapped_after_sending_voids_the_envelope(
    db, org, scope, envelope, document, lease_record
):
    """'They signed it' and 'they signed *this*' are different claims."""
    send_envelope(db.session, envelope=envelope)
    record_signature(db.session, envelope=envelope, email=RESIDENT.email, typed_name="Dana Okonjo")

    # Somebody replaces the file behind the envelope.
    document.checksum_sha256 = "b" * 64
    db.session.flush()

    with pytest.raises(BusinessRuleViolation) as exc:
        record_signature(
            db.session, envelope=envelope, email=GUARANTOR.email, typed_name="Sam Okonjo"
        )
    assert "changed after this envelope was sent" in str(exc.value)

    assert envelope.status == EnvelopeStatus.VOIDED
    assert lease_record.esign_envelope_id is None


def test_the_swap_is_audited_as_critical(db, org, scope, envelope, document):
    from app.models.audit import AuditEvent, AuditSeverity

    send_envelope(db.session, envelope=envelope)
    record_signature(db.session, envelope=envelope, email=RESIDENT.email, typed_name="Dana")
    document.checksum_sha256 = "c" * 64
    db.session.flush()

    with pytest.raises(BusinessRuleViolation):
        record_signature(db.session, envelope=envelope, email=GUARANTOR.email, typed_name="Sam")
    db.session.commit()

    critical = [
        event
        for event in db.session.query(AuditEvent).all()
        if event.severity == AuditSeverity.CRITICAL
    ]
    assert len(critical) == 1
    assert critical[0].payload["current_sha256"] == "c" * 64


# ---------------------------------------------------------------------------
# Declining, voiding, expiring
# ---------------------------------------------------------------------------


def test_one_decline_ends_it_for_everybody(db, org, scope, envelope):
    """A partly signed envelope with a refusal in it is not relied upon."""
    send_envelope(db.session, envelope=envelope)
    record_signature(db.session, envelope=envelope, email=RESIDENT.email, typed_name="Dana")
    decline_envelope(
        db.session,
        envelope=envelope,
        email=GUARANTOR.email,
        reason="Not willing to guarantee this term.",
    )
    db.session.commit()

    assert envelope.status == EnvelopeStatus.DECLINED
    declined = next(s for s in envelope.signers if s.email == GUARANTOR.email)
    assert declined.status == SignerStatus.DECLINED
    assert declined.declined_at is not None


def test_a_decline_needs_a_reason(db, org, scope, envelope):
    send_envelope(db.session, envelope=envelope)
    with pytest.raises(ValidationFailed):
        decline_envelope(db.session, envelope=envelope, email=RESIDENT.email, reason="")


def test_a_declined_party_cannot_then_sign(db, org, scope, envelope):
    send_envelope(db.session, envelope=envelope)
    decline_envelope(db.session, envelope=envelope, email=RESIDENT.email, reason="Changed my mind.")
    with pytest.raises(BusinessRuleViolation):
        record_signature(db.session, envelope=envelope, email=RESIDENT.email, typed_name="Dana")


def test_voiding_needs_a_reason(db, org, scope, envelope):
    with pytest.raises(ValidationFailed):
        void_envelope(db.session, envelope=envelope, reason="   ")


def test_a_completed_envelope_cannot_be_voided(db, org, scope, envelope):
    """The signatures on it happened. Supersede it instead."""
    send_envelope(db.session, envelope=envelope)
    record_signature(db.session, envelope=envelope, email=RESIDENT.email, typed_name="Dana")
    record_signature(db.session, envelope=envelope, email=GUARANTOR.email, typed_name="Sam")

    with pytest.raises(BusinessRuleViolation) as exc:
        void_envelope(db.session, envelope=envelope, reason="Sent in error.")
    assert "supersede it" in str(exc.value)


def test_expiry_lapses_only_open_envelopes(db, org, scope, envelope, document):
    send_envelope(db.session, envelope=envelope)
    envelope.expires_at = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)

    finished = create_envelope(
        db.session,
        org_id=org.id,
        document_id=document.id,
        title="Already done",
        reference="ENV-DONE",
        signers=[RESIDENT],
    )
    send_envelope(db.session, envelope=finished)
    record_signature(db.session, envelope=finished, email=RESIDENT.email, typed_name="Dana")
    finished.expires_at = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    db.session.commit()

    assert expire_envelopes(db.session, org_id=org.id) == 1
    assert envelope.status == EnvelopeStatus.EXPIRED
    assert finished.status == EnvelopeStatus.COMPLETED
    # Idempotent.
    assert expire_envelopes(db.session, org_id=org.id) == 0


def test_envelopes_are_findable_by_subject(db, org, scope, envelope, lease_record):
    found = envelopes_for_subject(
        db.session, org_id=org.id, subject_type="lease", subject_id=lease_record.id
    )
    assert [record.id for record in found] == [envelope.id]


def test_the_http_backend_refuses_rather_than_pretending(db, org, scope, envelope, app):
    """A stub that silently succeeds is worse than one that says no."""
    app.config["SETTINGS"].esign_backend = "http"
    try:
        with pytest.raises(ValidationFailed) as exc:
            send_envelope(db.session, envelope=envelope)
        assert "no provider client is configured" in str(exc.value)
    finally:
        app.config["SETTINGS"].esign_backend = "mock"

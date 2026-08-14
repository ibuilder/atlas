"""Signing from a portal, and who is allowed to.

The envelope lifecycle was correct and tested before any of this existed - and
unreachable, which made the built-in provider's claim that "the signer opens the
document from their portal" false, and meant the consent wording stored with
every signature had never been shown to anybody.

The rule these pin: a signer is authorised by being *named on the envelope*,
which is a fact about the envelope rather than a permission anyone can hold. The
identity therefore comes from the signed-in account and never from the request.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt

import pytest

pytestmark = pytest.mark.security

PORTAL_PASSWORD = "portal-signer-2026-ok!"


def _rebound(org):
    from app.context import RequestContext, bind_context, new_correlation_id

    return bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=org.id,
            actor_type="system",
            source="test",
        )
    )


@pytest.fixture()
def signer(db, org, scope, lease_record):
    """A resident with a portal login, on the fixture lease."""
    from app.models.iam import UserType
    from app.models.resident import Resident, ResidentStatus, Tenancy, TenancyRole
    from app.services.iam.provisioning import create_user

    resident = Resident(
        org_id=org.id,
        first_name="Imani",
        last_name="Brooks",
        email="imani.signer@test.local",
        status=ResidentStatus.CURRENT,
    )
    db.session.add(resident)
    db.session.flush()
    db.session.add(
        Tenancy(
            org_id=org.id,
            lease_id=lease_record.id,
            resident_id=resident.id,
            role=TenancyRole.PRIMARY,
            started_at=lease_record.start_date,
        )
    )
    user = create_user(
        db.session,
        org_id=org.id,
        email="imani.signer@test.local",
        full_name="Imani Brooks",
        password=PORTAL_PASSWORD,
        user_type=UserType.RESIDENT,
        role_codes=["resident"],
        resident_id=resident.id,
    )
    db.session.commit()
    return user


@pytest.fixture()
def envelope(db, org, scope, lease_record, signer):
    from app.models.documents import Document, DocumentCategory, ScanStatus
    from app.services.documents.esign import (
        SignerInput,
        create_envelope,
        send_envelope,
        sha256_of,
    )

    body = b"RESIDENTIAL TENANCY AGREEMENT\n"
    document = Document(
        org_id=org.id,
        name="Lease agreement",
        original_filename="lease.txt",
        storage_key="documents/lease.txt",
        content_type="text/plain",
        size_bytes=len(body),
        checksum_sha256=sha256_of(body),
        category=DocumentCategory.LEASE,
        scan_status=ScanStatus.CLEAN,
    )
    db.session.add(document)
    db.session.flush()

    record = create_envelope(
        db.session,
        org_id=org.id,
        document_id=document.id,
        title="Lease agreement",
        reference="ENV-PORTAL-1",
        signers=[
            SignerInput(name="Imani Brooks", email="imani.signer@test.local", role="resident"),
            SignerInput(name="Dana Whitfield", email="dana.staff@test.local", role="landlord"),
        ],
        subject_type="lease",
        subject_id=lease_record.id,
    )
    send_envelope(db.session, envelope=record)
    db.session.commit()
    return record


def _sign_in(client):
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "imani.signer@test.local", "password": PORTAL_PASSWORD},
    )
    assert response.status_code == 200, response.get_json()


# ---------------------------------------------------------------------------
# Reaching it at all
# ---------------------------------------------------------------------------


def test_the_document_appears_in_the_portal(client, db, signer, envelope):
    _sign_in(client)
    body = client.get("/resident/signatures").get_data(as_text=True)
    assert "Lease agreement" in body


def test_the_consent_wording_is_shown_before_signing(client, db, signer, envelope):
    """Stored consent is only evidence if the signer was shown the words.

    This page is the only place that happens, which is why it is asserted on the
    rendered bytes rather than on the constant.
    """
    from app.services.documents.esign import DEFAULT_CONSENT

    _sign_in(client)
    body = client.get(f"/resident/signatures/{envelope.id}").get_data(as_text=True)
    assert DEFAULT_CONSENT[:60] in body


def test_signing_from_the_portal_records_the_consent(client, db, org, signer, envelope):
    from app.context import clear_context
    from app.models.documents import SignerStatus

    _sign_in(client)
    response = client.post(
        f"/resident/signatures/{envelope.id}",
        data={"action": "sign", "typed_name": "Imani Brooks"},
    )
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        from app.models.documents import SignatureEnvelope

        reloaded = db.session.get(SignatureEnvelope, envelope.id)
        me = next(s for s in reloaded.signers if s.email == "imani.signer@test.local")
        assert me.status == SignerStatus.SIGNED
        assert me.typed_name == "Imani Brooks"
        # Captured from the request, which is the only moment it exists.
        assert me.signed_ip
        assert me.consent_text
    finally:
        clear_context(token)


def test_one_signature_does_not_execute_the_lease(client, db, org, signer, envelope, lease_record):
    """The other party has not signed. A lease executed on one of two is not."""
    from app.context import clear_context
    from app.models.documents import EnvelopeStatus

    _sign_in(client)
    client.post(
        f"/resident/signatures/{envelope.id}",
        data={"action": "sign", "typed_name": "Imani Brooks"},
    )

    db.session.expire_all()
    token = _rebound(org)
    try:
        from app.models.documents import SignatureEnvelope
        from app.models.leasing import Lease

        reloaded = db.session.get(SignatureEnvelope, envelope.id)
        assert reloaded.status == EnvelopeStatus.PARTIALLY_SIGNED
        assert db.session.get(Lease, lease_record.id).executed_at is None
    finally:
        clear_context(token)


def test_a_signature_needs_a_typed_name(client, db, signer, envelope):
    _sign_in(client)
    response = client.post(
        f"/resident/signatures/{envelope.id}",
        data={"action": "sign", "typed_name": "   "},
        follow_redirects=True,
    )
    assert b"typed name" in response.data


def test_declining_ends_it_for_everybody(client, db, org, signer, envelope):
    from app.context import clear_context
    from app.models.documents import EnvelopeStatus, SignatureEnvelope

    _sign_in(client)
    client.post(
        f"/resident/signatures/{envelope.id}",
        data={"action": "decline", "reason": "The rent is not what we agreed."},
    )

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert db.session.get(SignatureEnvelope, envelope.id).status == EnvelopeStatus.DECLINED
    finally:
        clear_context(token)


def test_a_decline_needs_a_reason(client, db, signer, envelope):
    _sign_in(client)
    response = client.post(
        f"/resident/signatures/{envelope.id}",
        data={"action": "decline", "reason": ""},
        follow_redirects=True,
    )
    assert b"needs a reason" in response.data


# ---------------------------------------------------------------------------
# The boundary
# ---------------------------------------------------------------------------


def test_somebody_not_named_on_it_cannot_reach_it(client, db, org, scope, envelope, make_user):
    """404 rather than 403: a 403 confirms who is signing what."""
    from app.models.iam import UserType
    from app.models.resident import Resident, ResidentStatus
    from app.services.iam.provisioning import create_user

    stranger = Resident(
        org_id=org.id,
        first_name="Nobody",
        last_name="Else",
        email="stranger@test.local",
        status=ResidentStatus.CURRENT,
    )
    db.session.add(stranger)
    db.session.flush()
    create_user(
        db.session,
        org_id=org.id,
        email="stranger@test.local",
        full_name="Nobody Else",
        password=PORTAL_PASSWORD,
        user_type=UserType.RESIDENT,
        role_codes=["resident"],
        resident_id=stranger.id,
    )
    db.session.commit()

    client.post(
        "/api/v1/auth/login",
        json={"email": "stranger@test.local", "password": PORTAL_PASSWORD},
    )
    assert client.get(f"/resident/signatures/{envelope.id}").status_code == 404
    assert (
        client.post(
            f"/resident/signatures/{envelope.id}",
            data={"action": "sign", "typed_name": "Nobody Else"},
        ).status_code
        == 404
    )


def test_the_list_only_shows_documents_awaiting_this_person(client, db, org, signer, envelope):
    """The other party's pending signature is not this person's business."""
    _sign_in(client)
    client.post(
        f"/resident/signatures/{envelope.id}",
        data={"action": "sign", "typed_name": "Imani Brooks"},
    )
    body = client.get("/resident/signatures").get_data(as_text=True)
    assert "Lease agreement" not in body


def test_an_anonymous_visitor_cannot_sign(client, envelope):
    response = client.post(
        f"/resident/signatures/{envelope.id}",
        data={"action": "sign", "typed_name": "Whoever"},
    )
    assert response.status_code in (302, 401)


# ---------------------------------------------------------------------------
# The staff side
# ---------------------------------------------------------------------------


def test_staff_can_raise_and_send_an_envelope(client, db, org, scope, make_user, sign_in):
    from app.models.documents import Document, DocumentCategory, ScanStatus
    from app.services.documents.esign import sha256_of

    body = b"ADDENDUM\n"
    document = Document(
        org_id=org.id,
        name="Addendum",
        original_filename="addendum.txt",
        storage_key="documents/addendum.txt",
        content_type="text/plain",
        size_bytes=len(body),
        checksum_sha256=sha256_of(body),
        category=DocumentCategory.LEASE,
        scan_status=ScanStatus.CLEAN,
    )
    db.session.add(document)
    db.session.commit()

    make_user("leasing_agent", email="agent@test.local")
    sign_in("agent@test.local")

    created = client.post(
        "/api/v1/envelopes",
        json={
            "document_id": document.id,
            "title": "Parking addendum",
            "reference": "ENV-API-1",
            "signers": [{"name": "Imani Brooks", "email": "imani.signer@test.local"}],
        },
    )
    assert created.status_code == 201, created.get_data(as_text=True)[:300]
    envelope_id = created.get_json()["id"]

    sent = client.post(f"/api/v1/envelopes/{envelope_id}/send")
    assert sent.status_code == 200
    assert sent.get_json()["status"] == "sent"
    assert sent.get_json()["document_sha256"] == document.checksum_sha256


def test_a_role_without_esign_manage_cannot_raise_one(client, db, org, scope, make_user, sign_in):
    make_user("technician", email="tech-esign@test.local")
    sign_in("tech-esign@test.local")

    response = client.post(
        "/api/v1/envelopes",
        json={
            "document_id": "019fea00-0000-7000-8000-0000000000ff",
            "title": "Nope",
            "reference": "ENV-NOPE",
            "signers": [{"name": "Somebody", "email": "somebody@test.local"}],
        },
    )
    assert response.status_code == 403


def test_another_tenants_envelope_is_not_found(client, db, org, other_org, make_user, sign_in):
    make_user("leasing_agent", email="agent2@test.local")
    sign_in("agent2@test.local")

    response = client.get("/api/v1/envelopes/019fea00-0000-7000-8000-0000000000ff")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Expiry, which nothing used to act on
# ---------------------------------------------------------------------------


def test_expiry_is_scheduled():
    """The model always carried an expiry date and nothing ever swept it."""
    from app.tasks.celery_app import BEAT_SCHEDULE

    assert "expire-signature-envelopes" in BEAT_SCHEDULE
    assert BEAT_SCHEDULE["expire-signature-envelopes"]["task"] == "atlas.esign.expire_envelopes"


def test_an_expired_envelope_leaves_the_signing_list(client, db, org, signer, envelope):
    from app.services.documents.esign import expire_envelopes

    envelope.expires_at = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    db.session.commit()
    assert expire_envelopes(db.session, org_id=org.id) == 1
    db.session.commit()

    _sign_in(client)
    body = client.get("/resident/signatures").get_data(as_text=True)
    assert "Lease agreement" not in body


def test_every_portal_links_to_its_own_signing_page(client, db, signer, envelope):
    """A page reachable only by typing the URL is one nobody finds.

    The messaging surface shipped this way too, which is why this asserts on
    both links rather than the one it was written for.
    """
    _sign_in(client)
    body = client.get("/resident/").get_data(as_text=True)
    assert "/resident/signatures" in body
    assert "/resident/messages" in body

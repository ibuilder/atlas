"""The extraction review queue, from the console and the API.

The rule this whole module exists for: a reading is a *suggestion*, and the
only path from one to a value the system will act on is a person accepting it,
with their name against it. Everything else here serves that — the evidence
shown beside each value so the reviewer checks rather than trusts, the
correction offered in the accept form so the common misread digit does not
force a reject-and-retype, and the queue ordered by lowest confidence so the
readings a machine got wrong are the ones a person reaches first.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.security

LEASE_TEXT = """RESIDENTIAL LEASE AGREEMENT

Monthly rent: $2,150.00 payable on the first of each month.
Security deposit: $2,150.00 held in trust.
Lease start date: 2026-03-01
Lease end date: 2027-02-28
"""

MURKY_TEXT = """LEASE

rent: 1800
deposit: 1800
commencing 03/04/2026
ending 02/03/2027
"""


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
def reviewer(db, org, scope, make_user, sign_in):
    """Holds DOCUMENT_READ and DOCUMENT_UPLOAD."""
    make_user("property_manager", email="reviewer@test.local")
    sign_in("reviewer@test.local")
    return "reviewer@test.local"


def _document(db, org, text: str, name: str = "Lease agreement"):
    from app.models.documents import Document, DocumentCategory, OcrStatus, ScanStatus
    from app.models.types import utcnow

    record = Document(
        org_id=org.id,
        name=name,
        original_filename="lease.pdf",
        storage_key=f"documents/{name}.pdf",
        content_type="application/pdf",
        size_bytes=2048,
        checksum_sha256="a" * 64,
        category=DocumentCategory.LEASE,
        scan_status=ScanStatus.CLEAN,
        is_quarantined=False,
        ocr_status=OcrStatus.COMPLETED,
        ocr_text=text,
        ocr_completed_at=utcnow(),
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture()
def lease_document(db, org, scope):
    return _document(db, org, LEASE_TEXT)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_the_page_shows_the_evidence_beside_every_value(client, lease_document, reviewer):
    """A score with nothing behind it moves the guess to the person."""
    response = client.get(f"/admin/extractions/{lease_document.id}")
    assert response.status_code == 200
    assert b"2150.00" in response.data
    # The text the reading came from, not just the reading.
    assert b"Monthly rent" in response.data or b"monthly rent" in response.data


def test_missing_fields_are_named_not_silently_absent(client, db, org, scope, reviewer):
    """A missing field is as wrong as a bad one and much easier to overlook."""
    document = _document(db, org, "LEASE\n\nMonthly rent: $2,150.00\n", name="Partial lease")

    response = client.get(f"/admin/extractions/{document.id}")
    assert response.status_code == 200
    assert b"Looked for and did not find" in response.data


def test_the_queue_puts_the_least_confident_first(client, db, org, scope, reviewer):
    from app.context import clear_context
    from app.services.documents.extraction import extraction_for, record_decisions

    murky = _document(db, org, MURKY_TEXT, name="Murky lease")
    clear = _document(db, org, LEASE_TEXT, name="Clear lease")

    token = _rebound(org)
    try:
        for document in (murky, clear):
            record_decisions(document, extraction_for(document))
        db.session.commit()
        assert murky.extraction_confidence <= clear.extraction_confidence
    finally:
        clear_context(token)

    response = client.get("/admin/extractions")
    assert response.status_code == 200
    assert response.data.index(b"Murky lease") < response.data.index(b"Clear lease")


def test_a_document_with_no_text_is_not_in_the_queue(client, db, org, scope, reviewer):
    """Not a failure — simply not this queue's business."""
    from app.models.documents import Document, DocumentCategory, ScanStatus

    silent = Document(
        org_id=org.id,
        name="Unread scan",
        original_filename="scan.pdf",
        storage_key="documents/scan.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        checksum_sha256="b" * 64,
        category=DocumentCategory.LEASE,
        scan_status=ScanStatus.CLEAN,
    )
    db.session.add(silent)
    db.session.commit()

    response = client.get("/admin/extractions")
    assert b"Unread scan" not in response.data


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def test_accepting_a_reading_records_who_decided(client, db, org, lease_document, reviewer):
    """The only path from extracted text to a value the system acts on."""
    from app.context import clear_context
    from app.models.documents import Document

    response = client.post(
        f"/admin/extractions/{lease_document.id}",
        data={"action": "accept", "field": "rent_amount", "value": "2150.00"},
    )
    assert response.status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        decided = db.session.get(Document, lease_document.id).extracted["rent_amount"]
        assert decided["value"] == "2150.00"
        assert decided["accepted_at"]
        assert decided["accepted_by_id"]
        # The evidence travels with the decision, so "why does it say this?"
        # is answerable later.
        assert decided["evidence"]
    finally:
        clear_context(token)


def test_a_reviewer_can_correct_a_misread_digit(client, db, org, lease_document, reviewer):
    """The common case, and it must not cost the evidence link."""
    from app.context import clear_context
    from app.models.documents import Document

    client.post(
        f"/admin/extractions/{lease_document.id}",
        data={"action": "accept", "field": "rent_amount", "value": "2151.00"},
    )

    db.session.expire_all()
    token = _rebound(org)
    try:
        decided = db.session.get(Document, lease_document.id).extracted["rent_amount"]
        assert decided["value"] == "2151.00"
        assert decided["evidence"]
    finally:
        clear_context(token)


def test_a_decision_survives_a_reload(client, db, org, lease_document, reviewer):
    """The extraction is re-derived; the decisions are what persist."""
    client.post(
        f"/admin/extractions/{lease_document.id}",
        data={"action": "accept", "field": "rent_amount", "value": "2150.00"},
    )

    response = client.get(f"/admin/extractions/{lease_document.id}")
    assert b"accepted" in response.data


def test_a_rejected_reading_cannot_then_be_accepted(client, db, org, lease_document, reviewer):
    client.post(
        f"/admin/extractions/{lease_document.id}",
        data={"action": "reject", "field": "rent_amount"},
    )
    response = client.post(
        f"/admin/extractions/{lease_document.id}",
        data={"action": "accept", "field": "rent_amount", "value": "2150.00"},
        follow_redirects=True,
    )
    assert b"already been rejected" in response.data


def test_an_accepted_reading_cannot_then_be_rejected(client, db, org, lease_document, reviewer):
    client.post(
        f"/admin/extractions/{lease_document.id}",
        data={"action": "accept", "field": "rent_amount", "value": "2150.00"},
    )
    response = client.post(
        f"/admin/extractions/{lease_document.id}",
        data={"action": "reject", "field": "rent_amount"},
        follow_redirects=True,
    )
    assert b"already been accepted" in response.data


def test_a_field_the_extractor_never_read_is_refused(client, db, org, lease_document, reviewer):
    response = client.post(
        f"/admin/extractions/{lease_document.id}",
        data={"action": "accept", "field": "pet_deposit", "value": "100"},
        follow_redirects=True,
    )
    assert b"No suggestion" in response.data


# ---------------------------------------------------------------------------
# Who may, and whose
# ---------------------------------------------------------------------------


def test_an_auditor_can_read_but_not_decide(client, db, org, lease_document, make_user, sign_in):
    make_user("auditor", email="extract-readonly@test.local")
    sign_in("extract-readonly@test.local")

    assert client.get(f"/admin/extractions/{lease_document.id}").status_code == 200
    response = client.post(
        f"/admin/extractions/{lease_document.id}",
        data={"action": "accept", "field": "rent_amount"},
    )
    assert response.status_code == 403


def test_another_tenants_document_is_not_found(client, db, org, other_org, reviewer):
    from app.context import RequestContext, bind_context, clear_context, new_correlation_id
    from app.models.documents import Document, DocumentCategory, OcrStatus, ScanStatus

    token = bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=other_org.id,
            actor_type="system",
            source="test",
        )
    )
    try:
        theirs = Document(
            org_id=other_org.id,
            name="Their lease",
            original_filename="theirs.pdf",
            storage_key="documents/theirs.pdf",
            content_type="application/pdf",
            size_bytes=1024,
            checksum_sha256="c" * 64,
            category=DocumentCategory.LEASE,
            scan_status=ScanStatus.CLEAN,
            ocr_status=OcrStatus.COMPLETED,
            ocr_text=LEASE_TEXT,
        )
        db.session.add(theirs)
        db.session.commit()
        theirs_id = theirs.id
    finally:
        clear_context(token)

    assert client.get(f"/admin/extractions/{theirs_id}").status_code == 404
    assert (
        client.post(
            f"/admin/extractions/{theirs_id}", data={"action": "accept", "field": "rent_amount"}
        ).status_code
        == 404
    )


def test_an_anonymous_visitor_cannot_reach_the_queue(client, lease_document):
    assert client.get("/admin/extractions").status_code in (302, 401)
    assert client.post(f"/admin/extractions/{lease_document.id}", data={}).status_code in (302, 401)


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


def test_the_api_returns_evidence_and_confidence(client, lease_document, reviewer):
    body = client.get(f"/api/v1/documents/{lease_document.id}/extraction").get_json()
    assert body["kind"] == "lease"
    assert body["suggestions"]
    for suggestion in body["suggestions"]:
        assert suggestion["evidence"]
        assert 0.0 <= suggestion["confidence"] <= 1.0
        assert "needs_review" in suggestion


def test_the_api_accepts_with_attribution(client, db, org, lease_document, reviewer):
    from app.context import clear_context
    from app.models.documents import Document

    response = client.post(
        f"/api/v1/documents/{lease_document.id}/extraction/rent_amount",
        json={"decision": "accept", "value": "2150.00"},
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["accepted_at"]

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert db.session.get(Document, lease_document.id).extracted["rent_amount"]["value"] == (
            "2150.00"
        )
    finally:
        clear_context(token)


def test_the_api_rejects_an_unknown_decision_at_the_schema(client, lease_document, reviewer):
    response = client.post(
        f"/api/v1/documents/{lease_document.id}/extraction/rent_amount",
        json={"decision": "maybe"},
    )
    assert response.status_code == 422


def test_the_api_refuses_a_document_it_has_no_extractor_for(client, db, org, scope, reviewer):
    from app.models.documents import Document, DocumentCategory, OcrStatus, ScanStatus

    other = Document(
        org_id=org.id,
        name="A photograph",
        original_filename="photo.jpg",
        storage_key="documents/photo.jpg",
        content_type="image/jpeg",
        size_bytes=512,
        checksum_sha256="d" * 64,
        category=DocumentCategory.PHOTO,
        scan_status=ScanStatus.CLEAN,
        ocr_status=OcrStatus.COMPLETED,
        ocr_text="a wall",
    )
    db.session.add(other)
    db.session.commit()

    response = client.get(f"/api/v1/documents/{other.id}/extraction")
    assert response.status_code in (400, 409, 422)


def test_a_technician_cannot_decide_through_the_api(client, lease_document, make_user, sign_in):
    make_user("technician", email="tech-extract@test.local")
    sign_in("tech-extract@test.local")

    response = client.post(
        f"/api/v1/documents/{lease_document.id}/extraction/rent_amount",
        json={"decision": "accept"},
    )
    assert response.status_code == 403

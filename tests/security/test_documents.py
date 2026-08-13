"""Document upload, quarantine, the link graph, and signed retrieval.

The upload path is the largest untrusted-input surface in the product, so these
are security tests before they are feature tests.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import io

import pytest

from app.errors import BusinessRuleViolation, ValidationFailed
from app.models.documents import Document, ScanStatus
from app.services.documents import service as documents
from app.services.documents.scanner import StructuralScanner
from app.services.documents.storage import (
    build_storage_key,
    sniff_content_type,
    validate_filename,
)

pytestmark = [pytest.mark.security, pytest.mark.integration]

PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def _upload(db, org, payload: bytes, filename: str, **kwargs):
    return documents.upload_document(
        db.session,
        org_id=org.id,
        stream=io.BytesIO(payload),
        filename=filename,
        declared_content_type=kwargs.pop("declared", None),
        **kwargs,
    )


# --------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "filename",
    ["payload.php", "shell.sh", "app.exe", "lib.so", "archive.tar.gz.js", "noextension"],
)
def test_disallowed_extensions_are_refused(filename):
    with pytest.raises(ValidationFailed):
        validate_filename(filename)


def test_path_components_are_stripped_from_the_display_name():
    """A client may legitimately send a path; the directory is never wanted."""
    safe, suffix = validate_filename("../../../etc/passwd.pdf")
    assert "/" not in safe and "\\" not in safe
    assert ".." not in safe
    assert suffix == ".pdf"


@pytest.mark.parametrize(
    "payload",
    [b"<?php system($_GET[0]); ?>", b"MZ\x90\x00", b"\x7fELF\x02", b"#!/bin/sh\nrm -rf /"],
)
def test_executable_content_is_refused_whatever_the_extension(payload):
    """The extension is a claim. The first bytes are the fact."""
    with pytest.raises(ValidationFailed):
        sniff_content_type(payload, "application/pdf", "invoice.pdf")


def test_declared_type_contradicting_content_is_refused():
    with pytest.raises(ValidationFailed, match="does not match"):
        sniff_content_type(PNG, "application/pdf", "statement.pdf")


def test_matching_declaration_is_accepted():
    assert sniff_content_type(PDF, "application/pdf", "lease.pdf") == "application/pdf"
    assert sniff_content_type(PNG, "image/png", "photo.png") == "image/png"


def test_jpeg_aliases_do_not_fight():
    """image/jpg and image/jpeg are the same thing to everyone except a parser."""
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32
    assert sniff_content_type(jpeg, "image/jpg", "photo.jpg") == "image/jpeg"


def test_storage_key_never_contains_the_filename():
    """Keys built from user input leak resident names and invite traversal."""
    key = build_storage_key(tenant_prefix="org/acme", extension=".pdf")
    assert "lease" not in key
    assert key.startswith("org/acme/")
    assert key.endswith(".pdf")
    assert build_storage_key(tenant_prefix="org/acme", extension=".pdf") != key


def test_oversized_upload_is_refused(db, org, scope, app):
    limit = app.config["SETTINGS"].upload_max_bytes
    with pytest.raises(ValidationFailed, match="exceeds"):
        _upload(db, org, b"%PDF-" + b"\x00" * (limit + 10), "huge.pdf")


def test_empty_upload_is_refused(db, org, scope):
    with pytest.raises(ValidationFailed, match="empty"):
        _upload(db, org, b"", "empty.pdf")


# ------------------------------------------------------------------ upload


def test_upload_stores_metadata_and_digest(db, org, scope):
    document = _upload(db, org, PDF, "Lease Agreement.pdf", declared="application/pdf")
    db.session.commit()

    assert document.content_type == "application/pdf"
    assert document.size_bytes == len(PDF)
    assert len(document.checksum_sha256) == 64
    assert document.original_filename == "Lease Agreement.pdf"
    assert document.retention_until is not None


def test_identical_content_is_deduplicated(db, org, scope):
    """One lease PDF attached from three places is one object, not three."""
    uploader = "019fea00-0000-7000-8000-000000000001"
    first = _upload(db, org, PDF, "lease.pdf", declared="application/pdf", uploaded_by_id=uploader)
    second = _upload(
        db, org, PDF, "lease-copy.pdf", declared="application/pdf", uploaded_by_id=uploader
    )
    db.session.commit()

    assert first.id == second.id
    assert db.session.query(Document).count() == 1


def test_dedup_does_not_hand_back_another_users_document(db, org, scope):
    """Residents hold DOCUMENT_UPLOAD.

    Deduplicating across uploaders would answer "upload this file" with someone
    else's document id, name, and filename - none of which the uploader
    supplied, and which they may have no right to read.
    """
    owner = "019fea00-0000-7000-8000-00000000000a"
    stranger = "019fea00-0000-7000-8000-00000000000b"

    theirs = _upload(
        db, org, PDF, "internal-notice.pdf", declared="application/pdf", uploaded_by_id=owner
    )
    mine = _upload(db, org, PDF, "mine.pdf", declared="application/pdf", uploaded_by_id=stranger)
    db.session.commit()

    assert mine.id != theirs.id
    assert mine.original_filename == "mine.pdf"


# -------------------------------------------------------------- quarantine


def test_uploads_are_quarantined_and_unservable(db, org, scope, app):
    app.config["SETTINGS"].malware_scan_required = True
    try:
        document = _upload(db, org, PDF, "unscanned.pdf", declared="application/pdf")
        db.session.commit()

        assert document.is_quarantined
        assert document.scan_status == ScanStatus.PENDING
        assert not document.is_servable

        with pytest.raises(BusinessRuleViolation, match="still being scanned"):
            documents.open_document(db.session, document=document)
    finally:
        app.config["SETTINGS"].malware_scan_required = False


def test_clean_scan_releases_the_document(db, org, scope, app):
    app.config["SETTINGS"].malware_scan_required = True
    try:
        document = _upload(db, org, PDF, "clean.pdf", declared="application/pdf")
        db.session.commit()
        key_before = document.storage_key

        documents.record_scan_result(db.session, document=document, clean=True)
        db.session.commit()

        assert document.scan_status == ScanStatus.CLEAN
        assert not document.is_quarantined
        assert document.is_servable
        # The key is stable for the document's lifetime: renaming across storage
        # and the database cannot be made atomic, and quarantine is enforced by
        # is_servable, which is derived from columns that cannot disagree.
        assert document.storage_key == key_before
    finally:
        app.config["SETTINGS"].malware_scan_required = False


def test_infected_scan_keeps_the_document_quarantined(db, org, scope, app):
    app.config["SETTINGS"].malware_scan_required = True
    try:
        document = _upload(db, org, PDF, "suspect.pdf", declared="application/pdf")
        db.session.commit()

        documents.record_scan_result(db.session, document=document, clean=False, detail="EICAR")
        db.session.commit()

        assert document.scan_status == ScanStatus.INFECTED
        assert document.is_quarantined
        assert not document.is_servable
    finally:
        app.config["SETTINGS"].malware_scan_required = False


def test_structural_scanner_catches_the_eicar_test_file():
    result = StructuralScanner().scan(io.BytesIO(EICAR))
    assert not result.clean
    assert "EICAR" in (result.detail or "")


def test_structural_scanner_flags_active_content():
    pdf_with_js = PDF + b"\n/JavaScript (app.alert('x'))\n"
    result = StructuralScanner().scan(io.BytesIO(pdf_with_js))
    assert not result.clean


def test_structural_scanner_passes_an_ordinary_document():
    assert StructuralScanner().scan(io.BytesIO(PDF)).clean


# ------------------------------------------------------------- link graph


def test_one_document_links_to_many_entities(db, org, scope, lease_record, property_record):
    """The differentiating design: one object, many relationships."""
    document = _upload(db, org, PDF, "coi.pdf", declared="application/pdf")
    documents.link_document(
        db.session, document=document, entity_type="lease", entity_id=lease_record.id
    )
    documents.link_document(
        db.session,
        document=document,
        entity_type="property",
        entity_id=property_record.id,
        relation="evidence",
    )
    db.session.commit()

    from_lease = documents.documents_for(
        db.session, org_id=org.id, entity_type="lease", entity_id=lease_record.id
    )
    from_property = documents.documents_for(
        db.session, org_id=org.id, entity_type="property", entity_id=property_record.id
    )

    assert [d.id for d in from_lease] == [document.id]
    assert [d.id for d in from_property] == [document.id]
    assert db.session.query(Document).count() == 1


def test_linking_is_idempotent(db, org, scope, lease_record):
    document = _upload(db, org, PDF, "dup.pdf", declared="application/pdf")
    first = documents.link_document(
        db.session, document=document, entity_type="lease", entity_id=lease_record.id
    )
    second = documents.link_document(
        db.session, document=document, entity_type="lease", entity_id=lease_record.id
    )
    db.session.commit()
    assert first.id == second.id


# ---------------------------------------------------------- signed access


def test_signed_token_round_trips(db, org, scope):
    document = _upload(db, org, PDF, "signed.pdf", declared="application/pdf")
    db.session.commit()

    token = documents.sign_document_token(document)
    resolved = documents.resolve_signed_token(db.session, token)
    assert resolved.id == document.id


def test_tampered_token_is_rejected(db, org, scope):
    from app.errors import NotFound

    document = _upload(db, org, PDF, "tamper.pdf", declared="application/pdf")
    db.session.commit()

    token = documents.sign_document_token(document)
    with pytest.raises(NotFound):
        documents.resolve_signed_token(db.session, token[:-4] + "AAAA")


def test_expired_token_is_rejected(db, org, scope, app):
    document = _upload(db, org, PDF, "expiry.pdf", declared="application/pdf")
    db.session.commit()
    token = documents.sign_document_token(document)

    original = app.config["SETTINGS"].signed_url_ttl_seconds
    app.config["SETTINGS"].signed_url_ttl_seconds = -1
    try:
        with pytest.raises(ValidationFailed, match="expired"):
            documents.resolve_signed_token(db.session, token)
    finally:
        app.config["SETTINGS"].signed_url_ttl_seconds = original


def test_download_records_an_audit_event(db, org, scope):
    from app.models.audit import AuditEvent

    document = _upload(db, org, PDF, "audited.pdf", declared="application/pdf")
    db.session.commit()

    documents.open_document(db.session, document=document)
    db.session.commit()

    downloads = (
        db.session.query(AuditEvent).filter(AuditEvent.action == "document.downloaded").count()
    )
    assert downloads == 1
    assert document.download_count == 1


# -------------------------------------------------------------- retention


def test_legal_hold_survives_retention_expiry(db, org, scope):
    """A hold outranks every retention rule. This is the one that matters."""
    import datetime as dt

    document = _upload(db, org, PDF, "held.pdf", declared="application/pdf")
    document.retention_until = dt.date.today() - dt.timedelta(days=1)
    document.legal_hold = True
    db.session.commit()

    assert not document.is_purgeable
    assert documents.purge_expired_documents(db.session, org_id=org.id) == 0
    assert not document.is_deleted


def test_expired_documents_are_purged(db, org, scope):
    import datetime as dt

    document = _upload(db, org, PDF, "stale.pdf", declared="application/pdf")
    document.retention_until = dt.date.today() - dt.timedelta(days=1)
    db.session.commit()

    assert documents.purge_expired_documents(db.session, org_id=org.id) == 1
    db.session.commit()
    assert document.is_deleted


# -------------------------------------------------------------------- API


def test_api_upload_and_download_round_trip(client, org, make_user, sign_in):
    make_user("org_admin", email="uploader@test.local")
    sign_in("uploader@test.local")

    created = client.post(
        "/api/v1/documents",
        data={
            "file": (io.BytesIO(PDF), "lease.pdf", "application/pdf"),
            "category": "lease",
        },
        content_type="multipart/form-data",
    )
    assert created.status_code == 201, created.get_json()
    document_id = created.get_json()["id"]

    url = client.post(f"/api/v1/documents/{document_id}/download-url")
    assert url.status_code == 200
    downloaded = client.get(url.get_json()["url"])

    assert downloaded.status_code == 200
    assert downloaded.data == PDF
    assert "no-store" in downloaded.headers["Cache-Control"]
    assert downloaded.headers["X-Content-Type-Options"] == "nosniff"


def test_api_rejects_a_disguised_executable(client, org, make_user, sign_in):
    make_user("org_admin", email="attacker@test.local")
    sign_in("attacker@test.local")

    response = client.post(
        "/api/v1/documents",
        data={"file": (io.BytesIO(b"MZ\x90\x00\x03"), "invoice.pdf", "application/pdf")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "validation_failed"


def test_signed_link_works_without_a_session(client, org, make_user, sign_in):
    """Regression: the emailed-link case, which is the whole point of the feature.

    A recipient has no session, so middleware binds no organization. Resolving
    the document through a tenant-scoped lookup raised under strict tenancy and
    turned every such request into a 404 - and the original test missed it
    because it signed in first.
    """
    make_user("org_admin", email="sharer@test.local")
    sign_in("sharer@test.local")

    created = client.post(
        "/api/v1/documents",
        data={"file": (io.BytesIO(PDF), "shared.pdf", "application/pdf")},
        content_type="multipart/form-data",
    )
    url = client.post(f"/api/v1/documents/{created.get_json()['id']}/download-url").get_json()[
        "url"
    ]

    # Drop every cookie: this is now an anonymous request carrying only the link.
    client.delete_cookie("atlas_session")
    # And clear the identity map, so the lookup genuinely reaches the database.
    # Without this the document would be served from cache and the test would
    # pass whether or not the scoping bug was present.
    from app.extensions import db as _db

    _db.session.expunge_all()

    anonymous = client.get(url)

    assert anonymous.status_code == 200, anonymous.get_json()
    assert anonymous.data == PDF


def test_api_download_requires_a_valid_token(client, org, make_user, sign_in):
    make_user("org_admin", email="tokenless@test.local")
    sign_in("tokenless@test.local")
    assert client.get("/api/v1/documents/download/not-a-real-token").status_code == 404


# ---------------------------------------------------------------------------
# Scanner selection
#
# The adapter existing and the deployment using it are different claims. These
# assert the second, which is the one that was not true.
# ---------------------------------------------------------------------------


def test_the_configured_scanner_is_the_one_that_runs(app):
    """`malware_scanner` was read with a getattr default that always won.

    The setting did not exist, so the fallback fired every time and selecting
    ClamAV was impossible - the adapter was unreachable by configuration.
    """
    from app.services.documents.scanner import ClamAVScanner, StructuralScanner, get_scanner

    with app.app_context():
        app.extensions.pop("atlas_scanner", None)
        app.config["SETTINGS"].malware_scanner = "clamav"
        app.config["SETTINGS"].clamav_host = "scanner.internal"
        app.config["SETTINGS"].clamav_port = 3311
        try:
            scanner = get_scanner()
            assert isinstance(scanner, ClamAVScanner)
            assert scanner.host == "scanner.internal"
            assert scanner.port == 3311
        finally:
            app.extensions.pop("atlas_scanner", None)
            app.config["SETTINGS"].malware_scanner = "structural"
            app.config["SETTINGS"].clamav_host = "127.0.0.1"
            app.config["SETTINGS"].clamav_port = 3310

        assert isinstance(get_scanner(), StructuralScanner)
        app.extensions.pop("atlas_scanner", None)


def test_an_unreachable_scanner_fails_closed(app):
    """Failing open would release unscanned files every time clamd restarts."""
    import io

    from app.services.documents.scanner import ClamAVScanner

    # Port 1 is reserved and nothing listens on it.
    result = ClamAVScanner(host="127.0.0.1", port=1, timeout=1).scan(io.BytesIO(b"anything"))
    assert not result.clean
    assert "unavailable" in (result.detail or "")


def test_the_reference_deployment_scans_uploads():
    """A compose file that ships without a scanner is one nobody adds later."""
    from pathlib import Path

    import yaml

    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(encoding="utf-8")
    )

    assert "clamav" in compose["services"], "the reference deployment has no scanner"
    assert "clamav" in compose["services"]["web"]["depends_on"]
    env = compose["services"]["web"]["environment"]
    assert env["CLAMAV_HOST"] == "clamav"
    assert "clamav" in env["MALWARE_SCANNER"]

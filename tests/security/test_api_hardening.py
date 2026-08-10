"""HTTP-level hardening: authentication, headers, errors, and idempotency.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.security


def test_api_requires_authentication_by_default(client):
    response = client.get("/api/v1/properties")
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "authentication_required"


def test_errors_use_the_stable_envelope(client):
    response = client.get("/api/v1/does-not-exist")
    body = response.get_json()

    assert response.status_code == 404
    assert set(body["error"]) >= {"code", "message", "details"}
    assert body["error"]["code"] == "not_found"


def test_security_headers_are_present(client):
    response = client.get("/healthz")
    headers = response.headers

    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert "Content-Security-Policy" in headers
    # Werkzeug's version banner is free reconnaissance.
    assert headers["Server"] == "Atlas"


def test_csp_forbids_inline_script(client):
    policy = client.get("/").headers["Content-Security-Policy"]
    assert "script-src" in policy
    assert "'unsafe-inline'" not in policy
    assert "object-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy


def test_tenant_responses_are_not_cacheable(client, make_user, sign_in):
    make_user("org_admin", email="cache@test.local")
    sign_in("cache@test.local")

    response = client.get("/api/v1/properties")
    assert "no-store" in response.headers["Cache-Control"]


def test_correlation_id_is_echoed(client):
    response = client.get("/healthz", headers={"X-Correlation-ID": "abcdef0123456789"})
    assert response.headers["X-Correlation-ID"] == "abcdef0123456789"


@pytest.mark.parametrize(
    "supplied",
    [
        "short",  # below the minimum length
        "has spaces in it",  # whitespace is not in the allowed set
        "x" * 200,  # beyond the maximum length
        "<script>alert(1)</script>",  # markup
        "id;DROP TABLE users",  # punctuation outside the allowed set
    ],
)
def test_malformed_correlation_id_is_replaced(client, supplied):
    """An inbound header reaches the logs, so it cannot carry an injection."""
    response = client.get("/healthz", headers={"X-Correlation-ID": supplied})
    echoed = response.headers["X-Correlation-ID"]

    assert echoed != supplied
    assert echoed.isalnum()


def test_login_does_not_reveal_whether_an_account_exists(client, make_user):
    make_user("org_admin", email="known@test.local")

    unknown = client.post(
        "/api/v1/auth/login", json={"email": "nobody@test.local", "password": "whatever-long"}
    )
    wrong = client.post(
        "/api/v1/auth/login", json={"email": "known@test.local", "password": "wrong-password-here"}
    )

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.get_json()["error"]["message"] == wrong.get_json()["error"]["message"]


def test_repeated_failures_lock_the_account(client, make_user, app):
    make_user("org_admin", email="lockme@test.local")
    limit = app.config["SETTINGS"].login_max_attempts

    for _ in range(limit):
        client.post(
            "/api/v1/auth/login",
            json={"email": "lockme@test.local", "password": "definitely-wrong"},
        )

    response = client.post(
        "/api/v1/auth/login",
        json={"email": "lockme@test.local", "password": "correct-horse-battery-staple-42"},
    )
    assert response.status_code == 423
    assert response.get_json()["error"]["code"] == "account_locked"


def test_validation_errors_name_the_offending_field(client, make_user, sign_in):
    make_user("org_admin", email="validator@test.local")
    sign_in("validator@test.local")

    response = client.post("/api/v1/properties", json={"name": "No code supplied"})
    body = response.get_json()

    assert response.status_code == 422
    assert body["error"]["code"] == "validation_failed"
    assert any(detail["field"] == "code" for detail in body["error"]["details"])


def test_unknown_fields_are_rejected(client, make_user, sign_in):
    """A misspelled field must fail loudly, not be silently dropped."""
    make_user("org_admin", email="strict@test.local")
    sign_in("strict@test.local")

    response = client.post(
        "/api/v1/properties",
        json={
            "name": "Test",
            "code": "T1",
            "address_line1": "1 Road",
            "city": "Town",
            "region": "TS",
            "postal_code": "00001",
            "yearBuilt": 1990,  # camelCase typo
        },
    )
    assert response.status_code == 422


def test_idempotent_retry_replays_the_original_response(client, make_user, sign_in):
    make_user("org_admin", email="idem@test.local")
    sign_in("idem@test.local")

    payload = {
        "name": "Idempotent House",
        "code": "IDEM",
        "address_line1": "2 Road",
        "city": "Town",
        "region": "TS",
        "postal_code": "00003",
    }
    headers = {"Idempotency-Key": "key-abc-123"}

    first = client.post("/api/v1/properties", json=payload, headers=headers)
    second = client.post("/api/v1/properties", json=payload, headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.headers.get("Idempotent-Replay") == "true"
    assert first.get_json()["id"] == second.get_json()["id"]


def test_same_key_different_body_is_a_conflict(client, make_user, sign_in):
    make_user("org_admin", email="idem2@test.local")
    sign_in("idem2@test.local")

    headers = {"Idempotency-Key": "key-xyz-789"}
    base = {
        "address_line1": "3 Road",
        "city": "Town",
        "region": "TS",
        "postal_code": "00004",
    }

    client.post(
        "/api/v1/properties", json={**base, "name": "First", "code": "ID1"}, headers=headers
    )
    response = client.post(
        "/api/v1/properties", json={**base, "name": "Second", "code": "ID2"}, headers=headers
    )

    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "idempotency_conflict"


def test_etag_precondition_prevents_lost_updates(client, make_user, sign_in):
    make_user("org_admin", email="etag@test.local")
    sign_in("etag@test.local")

    created = client.post(
        "/api/v1/properties",
        json={
            "name": "Concurrent",
            "code": "CONC",
            "address_line1": "4 Road",
            "city": "Town",
            "region": "TS",
            "postal_code": "00005",
        },
    ).get_json()

    fetched = client.get(f"/api/v1/properties/{created['id']}")
    etag = fetched.headers["ETag"]

    # Someone else writes first.
    client.patch(f"/api/v1/properties/{created['id']}", json={"name": "Changed by another"})

    # Our stale tag is now refused rather than overwriting their change.
    response = client.patch(
        f"/api/v1/properties/{created['id']}",
        json={"name": "Changed by us"},
        headers={"If-Match": etag},
    )
    assert response.status_code == 412
    assert response.get_json()["error"]["code"] == "precondition_failed"


def test_conditional_get_returns_not_modified(client, make_user, sign_in):
    make_user("org_admin", email="cond@test.local")
    sign_in("cond@test.local")

    created = client.post(
        "/api/v1/properties",
        json={
            "name": "Cacheable",
            "code": "CACHE",
            "address_line1": "5 Road",
            "city": "Town",
            "region": "TS",
            "postal_code": "00006",
        },
    ).get_json()

    first = client.get(f"/api/v1/properties/{created['id']}")
    second = client.get(
        f"/api/v1/properties/{created['id']}", headers={"If-None-Match": first.headers["ETag"]}
    )
    assert second.status_code == 304


def test_openapi_document_is_valid_and_describes_the_routes(client):
    document = client.get("/openapi.json").get_json()

    assert document["openapi"] == "3.1.0"
    assert "/api/v1/properties" in document["paths"]
    assert "bearerAuth" in document["components"]["securitySchemes"]
    assert document["info"]["license"]["identifier"] == "MIT"

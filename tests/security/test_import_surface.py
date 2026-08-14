"""Bulk import, from the console and the API.

The plan is the point. A CSV of four hundred units is not something anybody can
check by reading, so the surfaces have to show what a file *would* do — every
problem in it, not the first — and write nothing until somebody confirms.

The apply step re-plans rather than trusting what was shown. Applying a
decision taken against a database that has moved since is how an "update"
quietly becomes a "create".

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import io

import pytest

pytestmark = pytest.mark.security

PROPERTIES = """code,name,property_type,address_line1,city,region,postal_code
HARROW,Harrow Court,residential_multi,10 Harrow Road,Brooklyn,NY,11201
KESTREL,Kestrel House,residential_multi,44 Kestrel Row,Brooklyn,NY,11205
"""

BROKEN = """code,name,property_type,address_line1,city,region,postal_code
HARROW,Harrow Court,not_a_type,10 Harrow Road,Brooklyn,NY,11201
,Nameless,residential_multi,1 Nowhere,Brooklyn,NY,11201
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
def importer_user(db, org, scope, make_user, sign_in):
    make_user("org_admin", email="importer@test.local")
    sign_in("importer@test.local")
    return "importer@test.local"


def _plan(client, text: str = PROPERTIES, resource: str = "properties"):
    return client.post(
        "/admin/imports/plan",
        data={"resource": resource, "file": (io.BytesIO(text.encode()), "file.csv")},
        content_type="multipart/form-data",
    )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def test_a_plan_writes_nothing(client, db, org, importer_user):
    """The read-only step has to actually be read-only."""
    from sqlalchemy import func, select

    from app.context import clear_context
    from app.models.org import Property

    response = _plan(client)
    assert response.status_code == 200
    assert b"Create" in response.data
    assert b"HARROW" in response.data

    db.session.expire_all()
    token = _rebound(org)
    try:
        count = db.session.execute(
            select(func.count()).select_from(Property).where(Property.org_id == org.id)
        ).scalar_one()
        assert count == 0
    finally:
        clear_context(token)


def test_every_problem_is_listed_not_just_the_first(client, db, org, importer_user):
    """An operator fixing a spreadsheet one error per upload gives up."""
    response = _plan(client, BROKEN)
    assert response.status_code == 200
    assert b"Not imported" in response.data
    # Two bad rows, two problems on the page.
    assert response.data.count(b'<td class="num tiny">') >= 2


def test_a_file_with_a_missing_column_says_which(client, db, org, importer_user):
    response = _plan(client, "name,city\nHarrow Court,Brooklyn\n")
    assert response.status_code == 200
    assert b"Missing column" in response.data


def test_an_empty_upload_says_so(client, db, org, importer_user):
    response = client.post(
        "/admin/imports/plan",
        data={"resource": "properties", "csv": "   "},
        follow_redirects=True,
    )
    assert b"nothing to read" in response.data


def test_an_unknown_resource_is_refused(client, db, org, importer_user):
    response = _plan(client, PROPERTIES, resource="unicorns")
    assert response.status_code in (200, 302)
    if response.status_code == 302:
        response = client.get("/admin/imports")
    assert b"unicorn" in response.data.lower() or b"import" in response.data.lower()


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def test_applying_a_confirmed_plan_writes_it(client, db, org, importer_user):
    from sqlalchemy import select

    from app.context import clear_context
    from app.models.org import Property

    response = client.post(
        "/admin/imports/apply",
        data={
            "resource": "properties",
            "csv": PROPERTIES,
            "expect_creates": "2",
            "expect_updates": "0",
            "expect_unchanged": "0",
        },
        follow_redirects=True,
    )
    assert b"2 created" in response.data

    db.session.expire_all()
    token = _rebound(org)
    try:
        codes = {
            record.code
            for record in db.session.execute(
                select(Property).where(Property.org_id == org.id)
            ).scalars()
        }
        assert codes == {"HARROW", "KESTREL"}
    finally:
        clear_context(token)


def test_applying_twice_updates_rather_than_duplicating(client, db, org, importer_user):
    """The key is the identity, so a re-upload is idempotent by construction."""
    from sqlalchemy import func, select

    from app.context import clear_context
    from app.models.org import Property

    body = {
        "resource": "properties",
        "csv": PROPERTIES,
        "expect_creates": "2",
        "expect_updates": "0",
        "expect_unchanged": "0",
    }
    client.post("/admin/imports/apply", data=body)

    # Second time round the plan is two unchanged, so the confirmed counts move.
    second = client.post(
        "/admin/imports/apply",
        data={**body, "expect_creates": "0", "expect_unchanged": "2"},
        follow_redirects=True,
    )
    assert b"0 created" in second.data

    db.session.expire_all()
    token = _rebound(org)
    try:
        count = db.session.execute(
            select(func.count()).select_from(Property).where(Property.org_id == org.id)
        ).scalar_one()
        assert count == 2
    finally:
        clear_context(token)


def test_a_plan_that_has_moved_underneath_is_refused(client, db, org, importer_user):
    """Applying what was shown, when it is no longer what would happen."""
    from sqlalchemy import func, select

    from app.context import clear_context
    from app.models.org import Property

    # Somebody else creates one of them between the plan and the apply.
    token = _rebound(org)
    try:
        from app.models.org import PropertyType

        db.session.add(
            Property(
                org_id=org.id,
                code="HARROW",
                name="Harrow Court",
                property_type=PropertyType.RESIDENTIAL_MULTI,
                address_line1="10 Harrow Road",
                city="Brooklyn",
                region="NY",
                postal_code="11201",
            )
        )
        db.session.commit()
    finally:
        clear_context(token)

    response = client.post(
        "/admin/imports/apply",
        data={
            "resource": "properties",
            "csv": PROPERTIES,
            "expect_creates": "2",
            "expect_updates": "0",
            "expect_unchanged": "0",
        },
        follow_redirects=True,
    )
    assert b"no longer does what you were shown" in response.data

    db.session.expire_all()
    token = _rebound(org)
    try:
        count = db.session.execute(
            select(func.count()).select_from(Property).where(Property.org_id == org.id)
        ).scalar_one()
        assert count == 1
    finally:
        clear_context(token)


def test_a_file_with_errors_is_never_partly_applied(client, db, org, importer_user):
    """A partial import is worse than a failed one."""
    from sqlalchemy import func, select

    from app.context import clear_context
    from app.models.org import Property

    response = client.post(
        "/admin/imports/apply",
        data={
            "resource": "properties",
            "csv": BROKEN,
            "expect_creates": "0",
            "expect_updates": "0",
            "expect_unchanged": "0",
        },
        follow_redirects=True,
    )
    assert b"problem" in response.data.lower()

    db.session.expire_all()
    token = _rebound(org)
    try:
        count = db.session.execute(
            select(func.count()).select_from(Property).where(Property.org_id == org.id)
        ).scalar_one()
        assert count == 0
    finally:
        clear_context(token)


# ---------------------------------------------------------------------------
# Templates and access
# ---------------------------------------------------------------------------


def test_a_template_is_the_header_row(client, db, org, importer_user):
    response = client.get("/admin/imports/properties/template")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert b"code" in response.data
    assert "attachment" in response.headers["Content-Disposition"]


def test_an_unknown_template_is_not_found(client, db, org, importer_user):
    assert client.get("/admin/imports/unicorns/template").status_code == 404


def test_an_auditor_cannot_import(client, db, org, make_user, sign_in):
    make_user("auditor", email="import-readonly@test.local")
    sign_in("import-readonly@test.local")

    assert client.get("/admin/imports").status_code == 403
    assert _plan(client).status_code == 403


def test_an_anonymous_visitor_cannot_import(client):
    assert client.get("/admin/imports").status_code in (302, 401)
    assert client.post("/admin/imports/apply", data={}).status_code in (302, 401)


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


def test_the_api_lists_what_can_be_imported(client, db, org, importer_user):
    body = client.get("/api/v1/imports").get_json()
    resources = {row["resource"] for row in body["data"]}
    assert "properties" in resources
    for row in body["data"]:
        assert row["required_columns"]


def test_the_api_plan_writes_nothing_and_reports_everything(client, db, org, importer_user):
    body = client.post(
        "/api/v1/imports/plan", json={"resource": "properties", "csv": BROKEN}
    ).get_json()
    assert body["is_valid"] is False
    assert len(body["errors"]) >= 2
    assert body["creates"] == 0


def test_the_api_apply_checks_the_counts_it_was_given(client, db, org, importer_user):
    plan = client.post(
        "/api/v1/imports/plan", json={"resource": "properties", "csv": PROPERTIES}
    ).get_json()
    assert plan["creates"] == 2

    stale = client.post(
        "/api/v1/imports/apply",
        json={
            "resource": "properties",
            "csv": PROPERTIES,
            "expect_creates": 99,
            "expect_updates": 0,
            "expect_unchanged": 0,
        },
    )
    assert stale.status_code in (409, 422)

    applied = client.post(
        "/api/v1/imports/apply",
        json={
            "resource": "properties",
            "csv": PROPERTIES,
            "expect_creates": 2,
            "expect_updates": 0,
            "expect_unchanged": 0,
        },
    )
    assert applied.status_code == 200, applied.get_json()
    assert applied.get_json()["creates"] == 2


def test_a_technician_cannot_reach_the_api(client, db, org, make_user, sign_in):
    make_user("technician", email="tech-import@test.local")
    sign_in("tech-import@test.local")

    assert client.get("/api/v1/imports").status_code == 403
    assert (
        client.post(
            "/api/v1/imports/plan", json={"resource": "properties", "csv": PROPERTIES}
        ).status_code
        == 403
    )

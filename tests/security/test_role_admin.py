"""The role administration console.

These are permission tests wearing a UI. The screens answer "who can do what?",
and the thing that must hold is that they are themselves guarded - a view that
lists everybody's authority is a reconnaissance page if it renders for anyone
who asks.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.security


#: Every console page. Kept as a list so a new page has to be added here,
#: which is how the regression below stays caught: the whole console rendered
#: a 500 for months because nothing asked it to render.
CONSOLE_PAGES = (
    "/admin/",
    "/admin/properties",
    "/admin/work-orders",
    "/admin/ownership",
    "/admin/messages",
    "/admin/ledger",
    "/admin/deposits",
    "/admin/audit",
    "/admin/roles",
    "/admin/users",
)


@pytest.mark.parametrize("path", CONSOLE_PAGES)
def test_every_console_page_renders(client, make_user, sign_in, path):
    """A page that raises on render is a page nobody ever loaded in a test."""
    make_user("org_admin", email="console@test.local")
    sign_in("console@test.local")

    response = client.get(path)
    assert response.status_code == 200, response.get_data(as_text=True)[:400]


def test_roles_are_hidden_from_an_anonymous_visitor(client):
    response = client.get("/admin/roles")
    assert response.status_code in (302, 401)


def test_people_are_hidden_from_an_anonymous_visitor(client):
    response = client.get("/admin/users")
    assert response.status_code in (302, 401)


def test_an_administrator_sees_the_roles(client, make_user, sign_in):
    make_user("org_admin", email="roles-admin@test.local")
    sign_in("roles-admin@test.local")

    response = client.get("/admin/roles")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Roles and permissions" in body
    assert "org_admin" in body


def test_a_role_without_the_permission_cannot_see_them(client, make_user, sign_in):
    """The screen listing everyone's authority is not public to signed-in users."""
    make_user("technician", email="tech@test.local")
    sign_in("tech@test.local")

    response = client.get("/admin/roles")
    assert response.status_code == 403


def test_a_role_detail_page_lists_grants_and_holders(client, make_user, sign_in, db, org):
    from sqlalchemy import select

    from app.context import RequestContext, bind_context, clear_context, new_correlation_id
    from app.models.iam import Role

    make_user("org_admin", email="detail-admin@test.local")
    sign_in("detail-admin@test.local")

    # The request cycle leaves no ambient scope behind, so reading the role id
    # here needs its own.
    token = bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=org.id,
            actor_type="system",
            source="test",
        )
    )
    try:
        role_id = (
            db.session.execute(select(Role).where(Role.org_id == org.id, Role.code == "org_admin"))
            .scalar_one()
            .id
        )
    finally:
        clear_context(token)

    response = client.get(f"/admin/roles/{role_id}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Grants" in body
    assert "Holders" in body
    assert "detail-admin@test.local" in body


def test_a_role_from_another_organization_is_not_found(client, make_user, sign_in, db, other_org):
    """Not 403: confirming it exists is itself a disclosure."""
    from app.context import RequestContext, bind_context, clear_context, new_correlation_id

    token = bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=other_org.id,
            actor_type="system",
            source="test",
        )
    )
    try:
        from app.models.iam import Role

        foreign = Role(org_id=other_org.id, code="rival_role", name="Rival Role")
        db.session.add(foreign)
        db.session.commit()
        foreign_id = foreign.id
    finally:
        clear_context(token)

    make_user("org_admin", email="cross-admin@test.local")
    sign_in("cross-admin@test.local")

    response = client.get(f"/admin/roles/{foreign_id}")
    assert response.status_code == 404


def test_the_people_page_shows_who_holds_what(client, make_user, sign_in):
    make_user("org_admin", email="people-admin@test.local")
    make_user("technician", email="someone-else@test.local")
    sign_in("people-admin@test.local")

    response = client.get("/admin/users")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "someone-else@test.local" in body
    assert "people-admin@test.local" in body


def test_the_people_search_filters(client, make_user, sign_in):
    make_user("org_admin", email="search-admin@test.local")
    make_user("technician", email="findme@test.local")
    make_user("technician", email="hideme@test.local")
    sign_in("search-admin@test.local")

    response = client.get("/admin/users?q=findme")
    body = response.get_data(as_text=True)
    assert "findme@test.local" in body
    # The signed-in user's own address is in the topbar regardless, so the
    # assertion has to be about a third person who should not match.
    assert "hideme@test.local" not in body


def test_people_from_another_organization_never_appear(client, make_user, sign_in, db, other_org):
    from app.context import RequestContext, bind_context, clear_context, new_correlation_id

    token = bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=other_org.id,
            actor_type="system",
            source="test",
        )
    )
    try:
        from app.models.iam import User, UserStatus

        db.session.add(
            User(
                org_id=other_org.id,
                email="rival@rival.test",
                full_name="Rival Person",
                status=UserStatus.ACTIVE,
                password_hash="x",
            )
        )
        db.session.commit()
    finally:
        clear_context(token)

    make_user("org_admin", email="iso-admin@test.local")
    sign_in("iso-admin@test.local")

    body = client.get("/admin/users").get_data(as_text=True)
    assert "rival@rival.test" not in body
    assert "Rival Person" not in body

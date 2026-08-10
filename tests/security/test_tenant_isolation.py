"""Tenant isolation.

The property the whole design rests on. These tests try to reach across the
boundary the way a bug or an attacker would - by identifier, by relationship, by
aggregate, and by header - and assert that every route is closed.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.context import RequestContext, bind_context, clear_context, new_correlation_id
from app.errors import TenantIsolationViolation
from app.models.base import set_strict_tenancy, unscoped
from app.models.org import Property, PropertyType

pytestmark = pytest.mark.security


def _make_property(db, org_id: str, code: str) -> Property:
    with _scoped(org_id):
        record = Property(
            org_id=org_id,
            code=code,
            name=f"Building {code}",
            property_type=PropertyType.RESIDENTIAL_MULTI,
            address_line1="1 Somewhere",
            city="Town",
            region="TS",
            postal_code="00002",
        )
        db.session.add(record)
        db.session.commit()
        return record


class _scoped:
    """Bind an organization scope for a block."""

    def __init__(self, org_id: str | None) -> None:
        self.ctx = RequestContext(correlation_id=new_correlation_id(), org_id=org_id, source="test")
        self.token = None

    def __enter__(self):
        self.token = bind_context(self.ctx)
        return self.ctx

    def __exit__(self, *exc):
        clear_context(self.token)


def test_query_returns_only_current_tenants_rows(db, org, other_org):
    _make_property(db, org.id, "AAA")
    _make_property(db, other_org.id, "BBB")

    with _scoped(org.id):
        visible = db.session.execute(select(Property)).scalars().all()

    assert [record.code for record in visible] == ["AAA"]


def test_get_by_id_cannot_reach_another_tenant(db, org, other_org):
    foreign = _make_property(db, other_org.id, "FOREIGN")
    foreign_id = foreign.id
    # Clear the identity map so the lookup actually reaches the database. A
    # request never shares a session across tenants, but the test would
    # otherwise assert on a cached object rather than on the guard.
    db.session.expunge_all()

    with _scoped(org.id):
        # Knowing the exact primary key must not be enough.
        assert db.session.get(Property, foreign_id) is None


def test_aggregates_are_scoped(db, org, other_org):
    _make_property(db, org.id, "ONE")
    _make_property(db, other_org.id, "TWO")
    _make_property(db, other_org.id, "THREE")

    with _scoped(org.id):
        count = db.session.execute(select(func.count()).select_from(Property)).scalar_one()

    # A COUNT that ignores scoping leaks portfolio size even without leaking rows.
    assert count == 1


def test_strict_mode_refuses_unscoped_tenant_queries(db, org):
    _make_property(db, org.id, "STRICT")
    set_strict_tenancy(True)
    try:
        with _scoped(None), pytest.raises(TenantIsolationViolation):
            db.session.execute(select(Property)).scalars().all()
    finally:
        set_strict_tenancy(False)


def test_unscoped_escape_hatch_is_explicit(db, org, other_org):
    _make_property(db, org.id, "X1")
    _make_property(db, other_org.id, "X2")

    with _scoped(org.id):
        scoped_codes = {p.code for p in db.session.execute(select(Property)).scalars()}
        with unscoped(db.session):
            all_codes = {p.code for p in db.session.execute(select(Property)).scalars()}

    assert scoped_codes == {"X1"}
    assert all_codes == {"X1", "X2"}


def test_soft_deleted_rows_are_hidden_by_default(db, org):
    record = _make_property(db, org.id, "GONE")

    with _scoped(org.id):
        record.soft_delete(reason="test")
        db.session.commit()
        assert db.session.execute(select(Property)).scalars().all() == []

        from app.models.base import include_deleted

        with include_deleted(db.session):
            found = db.session.execute(select(Property)).scalars().all()
        assert [p.code for p in found] == ["GONE"]


def test_api_rejects_cross_tenant_organization_header(
    client, db, org, other_org, make_user, sign_in
):
    """Asking to act on another tenant is reported as absence, not refusal."""
    make_user("org_admin", email="scoped@test.local")
    sign_in("scoped@test.local")

    response = client.get("/api/v1/properties", headers={"X-Atlas-Organization": other_org.id})

    # 404 rather than 403: a 403 would confirm the organization exists.
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


def test_api_list_excludes_other_tenants(client, db, org, other_org, make_user, sign_in):
    _make_property(db, org.id, "MINE")
    _make_property(db, other_org.id, "THEIRS")

    make_user("org_admin", email="lister@test.local")
    sign_in("lister@test.local")

    response = client.get("/api/v1/properties")
    assert response.status_code == 200
    codes = {item["code"] for item in response.get_json()["data"]}
    assert codes == {"MINE"}


def test_audit_chains_are_independent_per_tenant(db, org, other_org):
    """One tenant's activity must not advance another's sequence."""
    from app.models.audit import AuditEvent
    from app.services.audit.recorder import record_audit_event

    def sequences(org_id: str) -> list[int]:
        with _scoped(org_id):
            return sorted(
                event.sequence for event in db.session.execute(select(AuditEvent)).scalars()
            )

    # Provisioning already wrote events, so measure the delta rather than
    # assuming an empty chain.
    before_mine = sequences(org.id)
    before_theirs = sequences(other_org.id)

    with _scoped(org.id):
        record_audit_event(action="test.first", org_id=org.id)
        record_audit_event(action="test.second", org_id=org.id)
        db.session.commit()

    after_mine = sequences(org.id)
    after_theirs = sequences(other_org.id)

    assert len(after_mine) == len(before_mine) + 2
    # The other tenant's chain is untouched, and its sequences are its own.
    assert after_theirs == before_theirs
    assert after_mine[-1] == len(after_mine)
    assert after_theirs[-1] == len(after_theirs)

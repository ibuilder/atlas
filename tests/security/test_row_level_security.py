"""Row-level security: the third isolation layer.

PostgreSQL only. Skipped on SQLite, which has no equivalent â€” and the skip is
loud rather than silent, because "these tests passed" must never mean "these
tests did not run".

The interesting assertion is the last one: a **raw SQL** query, bypassing the
ORM entirely, still cannot see another tenant's rows. That is the whole point of
this layer â€” layers one and two only protect paths that go through them.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.context import RequestContext, bind_context, clear_context, new_correlation_id
from app.models.org import Property, PropertyType
from app.models.rls import ORG_SETTING, policy_sql_for, tables_missing_policies

pytestmark = [pytest.mark.security, pytest.mark.integration]


@pytest.fixture()
def pg(db):
    """Skip unless the suite is running against PostgreSQL."""
    if db.engine.dialect.name != "postgresql":
        pytest.skip("row-level security requires PostgreSQL")
    return db


@pytest.fixture()
def rls_enabled(pg):
    """Apply the policies to the schema created by ``create_all``.

    The test schema is built by metadata rather than by migrations, so the
    policies the migration installs are applied here explicitly.
    """
    from app.models.registry import tenant_tables

    with pg.engine.begin() as connection:
        for table in tenant_tables():
            for statement in policy_sql_for(table.name):
                connection.execute(text(statement))

        # A superuser bypasses row-level security unconditionally, and FORCE
        # does not change that. The CI database user is a superuser, so without
        # a dedicated unprivileged role these tests would pass while proving
        # nothing. This mirrors the deployment requirement in SECURITY.md.
        connection.execute(
            text("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'atlas_app') THEN
                        CREATE ROLE atlas_app NOLOGIN NOBYPASSRLS;
                    END IF;
                END $$;
                """)
        )
        connection.execute(text("GRANT USAGE ON SCHEMA public TO atlas_app"))
        connection.execute(
            text("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO atlas_app")
        )
        connection.execute(
            text("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO atlas_app")
        )

    yield pg
    # End the test's transaction before altering the tables. ALTER TABLE needs an
    # ACCESS EXCLUSIVE lock, and an open session transaction holds it off until
    # the statement timeout fires.
    pg.session.rollback()
    pg.session.remove()
    with pg.engine.begin() as connection:
        for table in tenant_tables():
            connection.execute(text(f"ALTER TABLE {table.name} DISABLE ROW LEVEL SECURITY"))
            # Dropped as well as disabled. The schema now outlives the test -
            # it is built once per session - so a policy left behind is a
            # policy the next test inherits.
            connection.execute(
                text(f"DROP POLICY IF EXISTS atlas_tenant_isolation ON {table.name}")
            )


def _fresh_transaction(db) -> None:  # noqa: ANN001
    """End any open transaction so the next query begins a new one.

    The tenant variable is issued by an ``after_begin`` hook, so it belongs to
    the transaction that was open when the scope was entered. Binding a scope
    part-way through an existing transaction cannot retroactively set it.

    That is correct behaviour rather than a limitation to work around: in a
    request, middleware binds the context before any query runs, so the first
    statement opens its transaction with the tenant already known. Tests have to
    reproduce that ordering explicitly.
    """
    db.session.rollback()


def _as_app_role(db) -> None:  # noqa: ANN001
    """Start a fresh transaction as the unprivileged application role.

    Both halves matter. The rollback ensures the tenant variable is issued for
    the transaction the query will actually run in; ``SET ROLE`` drops the
    superuser privilege that would otherwise bypass every policy.
    """
    _fresh_transaction(db)
    # LOCAL, so the role reverts with the transaction. A bare SET ROLE persists
    # on the pooled connection and would silently follow into the next test.
    db.session.execute(text("SET LOCAL ROLE atlas_app"))


class _scoped:
    def __init__(self, org_id: str | None) -> None:
        self.ctx = RequestContext(correlation_id=new_correlation_id(), org_id=org_id, source="test")
        self.token = None

    def __enter__(self):
        self.token = bind_context(self.ctx)
        return self.ctx

    def __exit__(self, *exc):
        clear_context(self.token)


def _property(db, org_id: str, code: str) -> str:
    with _scoped(org_id):
        record = Property(
            org_id=org_id,
            code=code,
            name=f"Building {code}",
            property_type=PropertyType.RESIDENTIAL_MULTI,
            address_line1="1 Somewhere",
            city="Town",
            region="TS",
            postal_code="00010",
        )
        db.session.add(record)
        db.session.commit()
        return record.id


def test_every_tenant_table_has_a_policy(rls_enabled):
    """Drift detection: a new tenant table must enrol itself."""
    with rls_enabled.engine.connect() as connection:
        missing = tables_missing_policies(connection)
    assert missing == [], f"tables without an isolation policy: {missing}"


def test_session_variable_is_set_per_transaction(rls_enabled, org):
    """The tenant travels as transaction-local state, not connection state."""
    with _scoped(org.id):
        _as_app_role(rls_enabled)
        value = rls_enabled.session.execute(
            text(f"SELECT current_setting('{ORG_SETTING}', true)")
        ).scalar_one()
    assert value == org.id


def test_raw_sql_cannot_read_another_tenant(rls_enabled, org, other_org):
    """The assertion that justifies this layer existing.

    No ORM, no loader criteria, no service scoping - just SQL against the table.
    Layers one and two are entirely bypassed here.
    """
    _property(rls_enabled, org.id, "MINE")
    foreign_id = _property(rls_enabled, other_org.id, "THEIRS")
    rls_enabled.session.expunge_all()

    with _scoped(org.id):
        _as_app_role(rls_enabled)
        rows = rls_enabled.session.execute(text("SELECT code FROM properties")).scalars().all()
        assert set(rows) == {"MINE"}

        # Even addressing the row by primary key returns nothing.
        found = rls_enabled.session.execute(
            text("SELECT code FROM properties WHERE id = :id"), {"id": foreign_id}
        ).scalar_one_or_none()
        assert found is None


def test_raw_aggregate_cannot_count_another_tenant(rls_enabled, org, other_org):
    _property(rls_enabled, org.id, "A1")
    _property(rls_enabled, other_org.id, "B1")
    _property(rls_enabled, other_org.id, "B2")
    rls_enabled.session.expunge_all()

    with _scoped(org.id):
        _as_app_role(rls_enabled)
        count = rls_enabled.session.execute(text("SELECT count(*) FROM properties")).scalar_one()
    assert count == 1


def test_write_check_refuses_a_foreign_org_id(rls_enabled, org, other_org):
    """``WITH CHECK`` stops a write *into* another tenant, not just reads from it."""
    from sqlalchemy.exc import ProgrammingError

    # The role must be assumed *inside* the tenant scope: the transaction it
    # starts is the one that carries the tenant variable, and starting it first
    # would run the insert with the bypass set.
    with _scoped(org.id):
        _as_app_role(rls_enabled)
        with pytest.raises((ProgrammingError, Exception)) as exc:
            rls_enabled.session.execute(
                text(
                    "INSERT INTO properties "
                    "(id, org_id, code, name, property_type, status, address_line1, city, "
                    " region, postal_code, country, total_units, settings, attributes, "
                    " created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :org, 'HACK', 'Smuggled', "
                    "'residential_multi', 'active', '1 X', 'Y', 'Z', '00001', 'US', 0, "
                    "'{}', '{}', now(), now())"
                ),
                {"org": other_org.id},
            )
            rls_enabled.session.flush()

    assert "policy" in str(exc.value).lower() or "row-level security" in str(exc.value).lower()
    rls_enabled.session.rollback()


def test_bypass_is_scoped_to_the_transaction(rls_enabled, org, other_org):
    """An unscoped block must not leave the bypass set on a pooled connection."""
    _property(rls_enabled, other_org.id, "HIDDEN")
    rls_enabled.session.expunge_all()

    from app.models.base import unscoped

    with _scoped(None), unscoped(rls_enabled.session):
        everything = rls_enabled.session.execute(
            text("SELECT count(*) FROM properties")
        ).scalar_one()
    rls_enabled.session.commit()
    assert everything >= 1

    # A new transaction under a tenant scope must be filtered again - if the
    # bypass had been set at connection level rather than SET LOCAL, this would
    # still see everything.
    with _scoped(org.id):
        _as_app_role(rls_enabled)
        scoped_count = rls_enabled.session.execute(
            text("SELECT count(*) FROM properties")
        ).scalar_one()
    assert scoped_count == 0

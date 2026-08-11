"""Shared test fixtures.

Each test gets an empty database. The schema is built once per session and the
*data* is cleared between tests, rather than the tables being created and
dropped around every one.

That distinction is worth stating, because the obvious version is what this
used to do and it does not scale. Creating and dropping eighty-seven tables per
test is nearly free on SQLite and punishing on PostgreSQL: every ``DROP TABLE``
takes an ACCESS EXCLUSIVE lock, so teardown blocks behind any transaction a
test left open, and the suite went from four minutes to twenty-five - close
enough to a hang to be mistaken for one.

Clearing data keeps the property the suite actually depends on. Tests here
assert on tenant isolation and audit-chain continuity, so they must *really*
commit; a shared-transaction-and-rollback scheme would make those assertions
meaningless. A truncate between tests leaves commits real and the next test's
database genuinely empty.

Point ``DATABASE_URL`` at PostgreSQL to run the identical suite against the
production dialect.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from flask.testing import FlaskClient

from app import create_app
from app.context import RequestContext, bind_context, clear_context, new_correlation_id
from app.extensions import db as _db

TEST_PASSWORD = "correct-horse-battery-staple-42"


@pytest.fixture(scope="session")
def app():
    """Application configured for testing.

    ``expire_on_commit`` is disabled for the suite. With it on, touching any
    attribute after a commit issues a refresh SELECT - which, outside a bound
    organization scope, the tenancy guard correctly refuses. That is the guard
    working as designed, but it makes ordinary assertions on committed objects
    impossible to write. Keeping loaded values after commit is also what the
    request lifecycle effectively provides, since a request commits once at the
    end.
    """
    application = create_app("testing")
    with application.app_context():
        _db.session.remove()
        _db.session.configure(expire_on_commit=False)
        # Once, not per test. See the module docstring.
        _db.create_all()
        try:
            yield application
        finally:
            _db.session.rollback()
            _db.session.remove()
            _drop_all_ignoring_constraints()


@pytest.fixture()
def db(app):
    """An empty database per test.

    Teardown rolls back and discards the session *before* clearing tables: a
    test that fails mid-flush leaves the session in a failed-transaction state,
    and anything issued on it would fail too - turning one real failure into a
    cascade of unrelated ones.
    """
    try:
        yield _db
    finally:
        _db.session.rollback()
        _db.session.remove()
        _truncate_all()
        _db.session.remove()


def _truncate_all() -> None:
    """Empty every table, leaving the schema in place.

    One statement on PostgreSQL rather than a DROP and CREATE per table. The
    difference is not cosmetic: DDL there needs an ACCESS EXCLUSIVE lock on
    every table, which is both slow and blocked by any transaction still open.
    """
    engine = _db.engine
    tables = list(_db.metadata.sorted_tables)
    if not tables:  # pragma: no cover - defensive
        return

    if engine.dialect.name == "postgresql":
        names = ", ".join(f'"{table.name}"' for table in tables)
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"  # noqa: S608
            )
        return

    with engine.begin() as connection:
        if engine.dialect.name == "sqlite":
            # Deleting in dependency order would work, but self-referencing rows
            # - a journal entry pointing at the entry it reverses - leave no
            # order that satisfies every constraint.
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            for table in reversed(tables):
                connection.execute(table.delete())
        finally:
            if engine.dialect.name == "sqlite":
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def _drop_all_ignoring_constraints() -> None:
    """Drop every table, suspending referential enforcement for the duration.

    Foreign keys are enforced during the test - that is the point of the SQLite
    pragma - but they get in the way of teardown: self-referencing rows (a
    journal entry pointing at the entry it reverses) leave no drop order that
    satisfies every constraint. Enforcement is restored immediately afterwards,
    so the next test still runs against a database that checks its keys.
    """
    engine = _db.engine
    is_sqlite = engine.dialect.name == "sqlite"

    if is_sqlite:
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
    try:
        _db.drop_all()
    finally:
        if is_sqlite:
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.commit()


class IsolatedClient(FlaskClient):
    """Test client that clears request-scoped globals before each request.

    The suite keeps one application context open so fixtures and service-level
    tests can use ``db.session`` directly. Flask reuses an already-active
    application context for test-client requests rather than pushing a fresh
    one, so ``g`` - and with it Flask-Login's cached user, the authorization
    context, and the idempotency claim - would otherwise survive from one
    request into the next.

    A real server tears the context down after every request. This restores that
    behaviour for the harness; without it, tests pass or fail depending on who
    was signed in three tests ago.
    """

    _REQUEST_SCOPED_KEYS = (
        "_login_user",
        "_atlas_authz_context",
        "atlas_context",
        "_atlas_idempotency",
        "_atlas_started",
    )

    def open(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        from flask import g, has_app_context

        if has_app_context():
            for key in self._REQUEST_SCOPED_KEYS:
                g.pop(key, None)
        return super().open(*args, **kwargs)


@pytest.fixture()
def client(app, db):
    app.test_client_class = IsolatedClient
    return app.test_client()


def _provision_org(db, *, name: str, slug: str):
    """Create a tenant and its chart of accounts.

    Two phases on purpose. The organization does not exist yet during the first,
    so there is no tenant scope to bind; the chart of accounts is tenant data and
    is seeded under the new organization's own scope. Strict tenancy in the test
    environment rejects anything less careful, which is the point of running the
    suite with it enabled.
    """
    from app.context import system_context, use_context
    from app.services.accounting.chart import seed_chart_of_accounts
    from app.services.iam.provisioning import create_organization

    with use_context(system_context("test")):
        organization = create_organization(db.session, name=name, slug=slug)
        db.session.commit()

    with use_context(system_context("test", org_id=organization.id)):
        seed_chart_of_accounts(db.session, organization.id)
        db.session.commit()

    return organization


@pytest.fixture()
def org(db):
    """A provisioned organization with roles and a chart of accounts."""
    return _provision_org(db, name="Testco Property", slug="testco")


@pytest.fixture()
def other_org(db):
    """A second tenant. Its only job is to be inaccessible from the first."""
    return _provision_org(db, name="Rival Holdings", slug="rival")


@pytest.fixture()
def scope(org):
    """Bind the ambient request context to ``org`` for the test's duration."""
    ctx = RequestContext(
        correlation_id=new_correlation_id(), org_id=org.id, actor_type="system", source="test"
    )
    token = bind_context(ctx)
    yield ctx
    clear_context(token)


@pytest.fixture()
def accounts(org, scope, db):
    """The chart of accounts, keyed by code."""
    from sqlalchemy import select

    from app.models.accounting import Account

    return {
        account.code: account
        for account in db.session.execute(select(Account).where(Account.org_id == org.id)).scalars()
    }


@pytest.fixture()
def make_user(org, db):
    """Factory for users with a given role."""
    from app.services.iam.provisioning import create_user

    created: list = []

    def _make(role_code: str = "org_admin", email: str | None = None, **kwargs):
        from app.models.iam import UserType

        index = len(created)
        user = create_user(
            db.session,
            org_id=kwargs.pop("org_id", org.id),
            email=email or f"user{index}.{role_code}@test.local",
            full_name=kwargs.pop("full_name", f"Test {role_code.title()} {index}"),
            password=kwargs.pop("password", TEST_PASSWORD),
            user_type=kwargs.pop("user_type", UserType.STAFF),
            role_codes=[role_code] if role_code else [],
            **kwargs,
        )
        db.session.commit()
        created.append(user)
        return user

    return _make


@pytest.fixture()
def admin_user(make_user):
    return make_user("org_admin", email="admin@test.local")


@pytest.fixture()
def property_record(org, scope, db):
    from app.models.org import Property, PropertyType

    record = Property(
        org_id=org.id,
        code="TST",
        name="Test House",
        property_type=PropertyType.RESIDENTIAL_MULTI,
        address_line1="1 Test Way",
        city="Testville",
        region="TS",
        postal_code="00001",
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture()
def unit_record(org, scope, db, property_record):
    from app.models.org import Unit, UnitStatus

    record = Unit(
        org_id=org.id,
        property_id=property_record.id,
        unit_number="1A",
        bedrooms=2,
        market_rent=Decimal("2400.00"),
        status=UnitStatus.VACANT_READY,
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture()
def lease_record(org, scope, db, unit_record):
    from app.models.leasing import Lease, LeaseStatus
    from app.models.sequences import SequenceKey
    from app.services.common.numbering import next_number

    start = dt.date.today().replace(day=1)
    record = Lease(
        org_id=org.id,
        lease_number=next_number(db.session, SequenceKey.LEASE, org_id=org.id),
        property_id=unit_record.property_id,
        unit_id=unit_record.id,
        status=LeaseStatus.ACTIVE,
        start_date=start,
        end_date=start + dt.timedelta(days=364),
        rent_amount=Decimal("2400.00"),
        security_deposit=Decimal("2400.00"),
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture()
def vendor_record(org, scope, db):
    from app.models.vendor import ComplianceStatus, Vendor, VendorStatus

    record = Vendor(
        org_id=org.id,
        code="VND",
        name="Test Trades",
        status=VendorStatus.ACTIVE,
        compliance_status=ComplianceStatus.VALID,
        compliance_expires_at=dt.date.today() + dt.timedelta(days=180),
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture()
def sign_in(client, db):
    """Authenticate a user through the real login endpoint."""

    def _sign_in(email: str, password: str = TEST_PASSWORD):
        response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.get_json()
        return response.get_json()

    return _sign_in


def _system_context():
    from app.context import system_context, use_context

    return use_context(system_context("test"))

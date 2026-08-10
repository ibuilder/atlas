"""The restore verification command.

A runbook step that has never been executed is a document. This is the step
that decides whether a restore is usable, so it gets tested like anything else
that can say "yes" when the answer is "no".

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from decimal import Decimal

import pytest

pytestmark = pytest.mark.integration


def _run(app, *args):
    runner = app.test_cli_runner()
    return runner.invoke(args=["atlas", "verify-restore", *args])


def test_a_healthy_database_passes(app, db, org, scope, accounts):
    """The three data checks pass on a healthy database.

    Run with --no-strict because the fourth check is about the *schema*, and
    the test database is built by ``create_all`` rather than by migrations - so
    it genuinely has no row-level-security policies on PostgreSQL. That is the
    check working, not failing; asserting a clean exit here would mean
    asserting that a database without RLS is fine.
    """
    result = _run(app, "--no-strict")

    assert "[ok] field encryption key round-trips" in result.output
    assert "[ok] audit chain intact" in result.output
    assert "[ok] ledger balances" in result.output


def test_a_broken_ledger_fails_the_restore(app, db, org, scope, accounts):
    """A partial restore passes every row count and fails this."""
    import datetime as dt

    from sqlalchemy import update

    from app.models.accounting import JournalLine
    from app.services.accounting.chart import AccountCode
    from app.services.accounting.ledger import LineInput, post_journal_entry

    post_journal_entry(
        db.session,
        org_id=org.id,
        entry_date=dt.date(2026, 3, 1),
        description="Rent",
        lines=[
            LineInput(account_id=accounts[AccountCode.CASH_OPERATING].id, debit=Decimal("500")),
            LineInput(account_id=accounts[AccountCode.RENTAL_INCOME].id, credit=Decimal("500")),
        ],
    )
    db.session.commit()

    # Simulate a restore that lost rows: change one side without the other.
    db.session.execute(
        update(JournalLine)
        .where(JournalLine.org_id == org.id, JournalLine.debit > 0)
        .values(debit=Decimal("400"))
    )
    db.session.commit()

    result = _run(app)
    assert result.exit_code == 1
    assert "ledger out of balance" in result.output
    assert "not usable" in result.output


def test_a_broken_audit_chain_fails_the_restore(app, db, org, scope):
    """A restore that loses audit continuity cannot be attested to afterwards."""
    from sqlalchemy import update

    from app.models.audit import AuditAction, AuditEvent
    from app.models.base import unscoped
    from app.services.audit.recorder import record_audit_event

    for index in range(3):
        record_audit_event(
            action=AuditAction.PROPERTY_CREATED,
            resource_type="Property",
            resource_label=f"probe-{index}",
            org_id=org.id,
            session=db.session,
        )
    db.session.commit()

    # Tamper: alter a recorded event, which is exactly what the chain exists to
    # make undeniable.
    with unscoped(db.session):
        target = (
            db.session.query(AuditEvent)
            .filter(AuditEvent.org_id == org.id)
            .order_by(AuditEvent.sequence)
            .first()
        )
        db.session.execute(
            update(AuditEvent).where(AuditEvent.id == target.id).values(resource_label="tampered")
        )
        db.session.commit()

    result = _run(app)
    assert result.exit_code == 1
    assert "audit chain broken" in result.output


def test_no_strict_reports_without_failing(app, db, org, scope, accounts):
    """For a drill where the operator wants the whole picture before deciding."""
    from sqlalchemy import update

    from app.models.audit import AuditAction, AuditEvent
    from app.models.base import unscoped
    from app.services.audit.recorder import record_audit_event

    record_audit_event(
        action=AuditAction.PROPERTY_CREATED,
        resource_type="Property",
        resource_label="probe",
        org_id=org.id,
        session=db.session,
    )
    db.session.commit()

    with unscoped(db.session):
        target = db.session.query(AuditEvent).filter(AuditEvent.org_id == org.id).first()
        db.session.execute(
            update(AuditEvent).where(AuditEvent.id == target.id).values(resource_label="tampered")
        )
        db.session.commit()

    result = _run(app, "--no-strict")
    assert result.exit_code == 0
    assert "not usable" in result.output


def test_the_row_level_security_check_reports_per_dialect(app, db, org, scope):
    """Stated either way, never silently passed over.

    On SQLite there is no such thing to check and the command says so. On
    PostgreSQL it looks, and against a ``create_all`` schema it correctly
    reports the policies missing - which is precisely the restore failure the
    check exists to catch.
    """
    result = _run(app, "--no-strict")

    if db.engine.dialect.name == "postgresql":
        assert "row-level security" in result.output
    else:
        assert "row-level security: not PostgreSQL" in result.output

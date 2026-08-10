"""Reports: building, rendering, and delivering them.

The acceptance case is the last one: recipients resolve at send time, so a
departed employee stops receiving the books.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.errors import NotFound, ValidationFailed
from app.models.iam import User, UserStatus
from app.models.reporting import ReportFormat, ReportStatus, ScheduledReport
from app.models.types import utcnow
from app.services.reporting import (
    known_reports,
    next_occurrence,
    render,
    report_definition,
    resolve_recipients,
    run_due_schedules,
    run_report,
)

pytestmark = pytest.mark.integration

COLUMNS = ["property", "rent"]
ROWS = [
    {"property": "Larkspur Court", "rent": Decimal("3100.00")},
    {"property": "O'Hare (North) \\ Annexe", "rent": Decimal("1875.50")},
    {"property": "Café Terrace", "rent": Decimal("2400.00")},
]


def _schedule(db, org, **overrides):
    params = {
        "name": "Weekly rent roll",
        "report_code": "rent_roll",
        "schedule": "0 6 * * *",
        "format": ReportFormat.CSV,
        "parameters": {},
        "recipients": [{"type": "email", "address": "owner@example.com"}],
        "is_active": True,
        "next_run_at": utcnow() - dt.timedelta(minutes=5),
    }
    params.update(overrides)
    record = ScheduledReport(org_id=org.id, **params)
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture()
def staff_user(db, org, scope):
    user = User(
        org_id=org.id,
        email="Bookkeeper@Example.com",
        full_name="Bookkeeper",
        status=UserStatus.ACTIVE,
        password_hash="x",
    )
    db.session.add(user)
    db.session.commit()
    return user


# --------------------------------------------------------------- renderers


def test_csv_carries_a_bom_so_excel_reads_utf8():
    rendered = render(fmt=ReportFormat.CSV, title="Rent roll", columns=COLUMNS, rows=ROWS)
    assert rendered.content.startswith(b"\xef\xbb\xbf")
    assert b"Larkspur Court" in rendered.content
    assert rendered.extension == "csv"


def test_json_keeps_the_column_order():
    rendered = render(fmt=ReportFormat.JSON, title="Rent roll", columns=COLUMNS, rows=ROWS)
    import json

    body = json.loads(rendered.content)
    assert body["columns"] == COLUMNS
    assert body["row_count"] == 3


def test_html_escapes_its_data():
    """A property name is not markup."""
    rendered = render(
        fmt=ReportFormat.HTML,
        title="Rent roll",
        columns=["property"],
        rows=[{"property": "<script>alert(1)</script>"}],
    )
    assert b"<script>alert" not in rendered.content
    assert b"&lt;script&gt;" in rendered.content


def test_pdf_is_a_real_pdf():
    rendered = render(fmt=ReportFormat.PDF, title="Rent roll", columns=COLUMNS, rows=ROWS)
    assert rendered.content.startswith(b"%PDF-1.4")
    assert rendered.content.rstrip().endswith(b"%%EOF")
    assert b"/Type /Catalog" in rendered.content
    assert b"startxref" in rendered.content
    assert rendered.content_type == "application/pdf"


def test_a_parenthesis_in_the_data_does_not_corrupt_the_pdf():
    """An unbalanced bracket in a property name would otherwise break the file."""
    rendered = render(
        fmt=ReportFormat.PDF,
        title="Rent roll",
        columns=["property"],
        rows=[{"property": "The Annexe (rear"}],
    )
    assert rendered.content.startswith(b"%PDF-")
    # Escaped, so the only unescaped parens are PDF's own string delimiters.
    assert rb"\(rear" in rendered.content


def test_a_long_report_paginates():
    rows = [{"property": f"Unit {n}", "rent": Decimal("1000")} for n in range(120)]
    rendered = render(fmt=ReportFormat.PDF, title="Rent roll", columns=COLUMNS, rows=rows)
    assert (
        rendered.content.count(b"/Type /Page\n") >= 3
        or rendered.content.count(b"/Type /Page ") >= 3
    )


def test_an_empty_report_still_renders():
    for fmt in (ReportFormat.CSV, ReportFormat.JSON, ReportFormat.HTML, ReportFormat.PDF):
        rendered = render(fmt=fmt, title="Nothing here", columns=COLUMNS, rows=[])
        assert rendered.size > 0


# ---------------------------------------------------------------- registry


def test_the_catalogue_lists_its_reports():
    assert "rent_roll" in known_reports()
    assert "trial_balance" in known_reports()


def test_an_unknown_report_code_is_refused():
    with pytest.raises(NotFound):
        report_definition("definitely_not_a_report")


# ---------------------------------------------------------------- running


def test_a_report_run_records_its_outcome(db, org, scope, accounts, lease_record):
    run = run_report(db.session, org_id=org.id, report_code="rent_roll", store=False)
    db.session.commit()

    assert run.status == ReportStatus.COMPLETED
    assert run.row_count is not None
    assert run.duration_ms is not None
    assert run.error_message is None


def test_a_failing_report_is_recorded_not_raised(db, org, scope):
    """A broken report is data, not a crash in the scheduler."""
    run = run_report(db.session, org_id=org.id, report_code="no_such_report", store=False)
    db.session.commit()

    assert run.status == ReportStatus.FAILED
    assert "no_such_report" in (run.error_message or "")


def test_the_output_is_stored_as_a_document(db, org, scope, accounts):
    from app.models.documents import Document

    run = run_report(db.session, org_id=org.id, report_code="trial_balance")
    db.session.commit()

    assert run.document_id is not None
    document = db.session.get(Document, run.document_id)
    assert document.original_filename.endswith(".csv")


def test_the_trial_balance_reads_the_ledger(db, org, scope, accounts, lease_record):
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

    run = run_report(
        db.session,
        org_id=org.id,
        report_code="trial_balance",
        parameters={"as_of": "2026-03-31"},
        store=False,
    )
    db.session.commit()
    assert run.row_count == 2


# ------------------------------------------------------------- recipients


def test_an_email_recipient_resolves(db, org, scope):
    resolved = resolve_recipients(
        db.session, org_id=org.id, recipients=[{"type": "email", "address": "Owner@Example.com "}]
    )
    assert [r.address for r in resolved] == ["owner@example.com"]


def test_a_user_recipient_resolves_to_their_current_address(db, org, scope, staff_user):
    resolved = resolve_recipients(
        db.session, org_id=org.id, recipients=[{"type": "user", "id": staff_user.id}]
    )
    assert [r.address for r in resolved] == ["bookkeeper@example.com"]

    staff_user.email = "New.Address@example.com"
    db.session.commit()
    again = resolve_recipients(
        db.session, org_id=org.id, recipients=[{"type": "user", "id": staff_user.id}]
    )
    assert [r.address for r in again] == ["new.address@example.com"]


def test_a_departed_employee_stops_receiving_the_books(db, org, scope, staff_user):
    """The acceptance case. Resolution at send time is the whole point."""
    staff_user.status = UserStatus.DEACTIVATED
    db.session.commit()

    resolved = resolve_recipients(
        db.session, org_id=org.id, recipients=[{"type": "user", "id": staff_user.id}]
    )
    assert resolved == []


def test_a_user_from_another_organization_is_not_a_recipient(db, org, other_org, scope, staff_user):
    resolved = resolve_recipients(
        db.session, org_id=other_org.id, recipients=[{"type": "user", "id": staff_user.id}]
    )
    assert resolved == []


def test_duplicate_addresses_collapse(db, org, scope, staff_user):
    resolved = resolve_recipients(
        db.session,
        org_id=org.id,
        recipients=[
            {"type": "user", "id": staff_user.id},
            {"type": "email", "address": "bookkeeper@example.com"},
        ],
    )
    assert len(resolved) == 1


def test_a_malformed_recipient_is_skipped_not_fatal(db, org, scope):
    resolved = resolve_recipients(
        db.session,
        org_id=org.id,
        recipients=[
            {"type": "email", "address": "not-an-address"},
            {"type": "carrier_pigeon", "id": "x"},
            {"type": "email", "address": "real@example.com"},
        ],
    )
    assert [r.address for r in resolved] == ["real@example.com"]


def test_too_many_recipients_is_refused(db, org, scope):
    many = [{"type": "email", "address": f"person{n}@example.com"} for n in range(60)]
    with pytest.raises(ValidationFailed):
        resolve_recipients(db.session, org_id=org.id, recipients=many)


# ------------------------------------------------------------- scheduling


def test_a_due_schedule_runs_and_delivers(db, org, scope, accounts, app):
    from app.services.notifications.mailer import get_mailer

    _schedule(db, org)
    outcome = run_due_schedules(db.session, org_id=org.id)
    db.session.commit()

    assert outcome.delivered == 1
    assert outcome.runs[0].status == ReportStatus.COMPLETED
    assert outcome.runs[0].delivered_at is not None
    sent = get_mailer().outbox
    assert [message.to for message in sent] == ["owner@example.com"]


def test_a_schedule_not_yet_due_does_not_run(db, org, scope, accounts):
    _schedule(db, org, next_run_at=utcnow() + dt.timedelta(hours=1))
    outcome = run_due_schedules(db.session, org_id=org.id)
    assert outcome.runs == []


def test_the_next_run_advances_even_when_delivery_finds_nobody(db, org, scope, accounts):
    """Otherwise a bad recipient list means the report runs every minute forever."""
    schedule = _schedule(db, org, recipients=[])
    outcome = run_due_schedules(db.session, org_id=org.id)
    db.session.commit()

    assert outcome.skipped_no_recipients == 1
    assert schedule.next_run_at > utcnow()


def test_repeated_failures_disable_a_schedule(db, org, scope):
    schedule = _schedule(db, org, report_code="no_such_report")
    for _ in range(5):
        schedule.next_run_at = utcnow() - dt.timedelta(minutes=1)
        run_due_schedules(db.session, org_id=org.id)
        db.session.commit()

    assert schedule.consecutive_failures >= 5
    assert schedule.is_active is False


def test_a_success_resets_the_failure_count(db, org, scope, accounts):
    schedule = _schedule(db, org)
    schedule.consecutive_failures = 3
    db.session.commit()

    run_due_schedules(db.session, org_id=org.id)
    db.session.commit()
    assert schedule.consecutive_failures == 0


def test_the_delivery_is_audited_with_who_received_it(db, org, scope, accounts):
    from app.models.audit import AuditAction, AuditEvent

    _schedule(db, org)
    run_due_schedules(db.session, org_id=org.id)
    db.session.commit()

    event = (
        db.session.query(AuditEvent).filter(AuditEvent.action == AuditAction.DATA_EXPORTED).one()
    )
    assert event.payload["recipient_count"] == 1
    # Addresses go through the usual redaction: identifiable, not harvestable.
    assert event.payload["recipients"][0].endswith("@example.com")


# ----------------------------------------------------------- cron subset


def test_a_daily_schedule_lands_at_its_hour(db, org, scope):
    schedule = _schedule(db, org, schedule="30 7 * * *")
    following = next_occurrence(schedule, after=dt.datetime(2026, 3, 10, 9, 0, tzinfo=dt.UTC))
    assert (following.hour, following.minute) == (7, 30)
    assert following.date() == dt.date(2026, 3, 11)


def test_a_monthly_schedule_lands_on_its_day(db, org, scope):
    schedule = _schedule(db, org, schedule="0 6 1 * *")
    following = next_occurrence(schedule, after=dt.datetime(2026, 3, 10, 9, 0, tzinfo=dt.UTC))
    assert following.day == 1
    assert following.month == 4


def test_a_weekly_schedule_lands_on_its_weekday(db, org, scope):
    #: Cron day 1 is Monday.
    schedule = _schedule(db, org, schedule="0 6 * * 1")
    following = next_occurrence(schedule, after=dt.datetime(2026, 3, 10, 9, 0, tzinfo=dt.UTC))
    assert following.weekday() == 0


def test_a_nonsense_expression_still_produces_a_next_run(db, org, scope):
    """A typo in a cron field must not wedge the scheduler."""
    schedule = _schedule(db, org, schedule="not a cron expression")
    following = next_occurrence(schedule, after=utcnow())
    assert following > utcnow()


# ------------------------------------------------------------- isolation


def test_schedules_do_not_cross_organizations(db, org, other_org, scope, accounts):
    _schedule(db, org)
    outcome = run_due_schedules(db.session, org_id=other_org.id)
    assert outcome.runs == []

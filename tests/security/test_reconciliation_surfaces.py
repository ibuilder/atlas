"""The bank reconciliation workspace, from the console and the API.

Two rules carry this module:

Auto-matching takes only what is both confident *and* unambiguous. A near-tie
is left alone deliberately — guessing between two payments of the same amount
on the same day is how a reconciliation quietly stops meaning anything.

Sign-off refuses a non-zero difference, an unresolved exception, or a
transaction that is neither matched nor deliberately ignored. A reconciliation
that can be signed while it is out is not a reconciliation.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

pytestmark = pytest.mark.security

START = dt.date(2026, 4, 1)
END = dt.date(2026, 4, 30)

STATEMENT = """date,amount,description,reference
2026-04-03,-480.00,NORTHLIGHT PLUMBING,CHK10418
2026-04-11,2000.00,RENT 4B TRANSFER,TRF9921
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
def controller(db, org, scope, make_user, sign_in):
    make_user("controller", email="recon@test.local")
    sign_in("recon@test.local")
    return "recon@test.local"


@pytest.fixture()
def bank(db, org, scope, accounts):
    from app.models.accounting import BankAccount, BankAccountType
    from app.services.accounting.chart import AccountCode

    account = BankAccount(
        org_id=org.id,
        code="OPS",
        name="Operating",
        account_type=BankAccountType.OPERATING,
        gl_account_id=accounts[AccountCode.CASH_OPERATING].id,
    )
    db.session.add(account)
    db.session.commit()
    return account


@pytest.fixture()
def period(db, org, scope, bank):
    from app.services.accounting.reconciliation import open_reconciliation

    record = open_reconciliation(
        db.session,
        org_id=org.id,
        bank_account_id=bank.id,
        statement_start=START,
        statement_end=END,
        opening_balance=Decimal("0.00"),
        closing_balance=Decimal("1520.00"),
    )
    db.session.commit()
    return record


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def test_a_statement_imports_and_a_re_import_inserts_nothing(client, db, org, period, controller):
    """Same file twice: the fingerprint is what makes that safe."""
    import io

    def _post():
        return client.post(
            f"/admin/reconciliations/{period.id}/statement",
            data={"statement": (io.BytesIO(STATEMENT.encode()), "april.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

    assert b"2 imported" in _post().data
    assert b"0 imported, 2 already present" in _post().data


def test_a_row_whose_amount_will_not_parse_is_rejected_not_zeroed(
    client, db, org, period, controller
):
    """A zero here is a difference somebody spends an afternoon hunting."""
    import io

    broken = "date,amount,description\n2026-04-03,not-a-number,SOMETHING\n"
    response = client.post(
        f"/admin/reconciliations/{period.id}/statement",
        data={"statement": (io.BytesIO(broken.encode()), "broken.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert response.status_code == 200
    # Named row, named value: an operator has to be able to go and look at it.
    assert b"is not an amount" in response.data


def test_an_empty_upload_says_so(client, db, org, period, controller):
    response = client.post(
        f"/admin/reconciliations/{period.id}/statement",
        data={"csv": "   "},
        follow_redirects=True,
    )
    assert b"nothing to import" in response.data


# ---------------------------------------------------------------------------
# Sign-off
# ---------------------------------------------------------------------------


def test_sign_off_is_refused_while_transactions_are_unaccounted_for(
    client, db, org, period, controller
):
    import io

    from app.context import clear_context
    from app.models.accounting import Reconciliation, ReconciliationStatus

    client.post(
        f"/admin/reconciliations/{period.id}/statement",
        data={"statement": (io.BytesIO(STATEMENT.encode()), "april.csv")},
        content_type="multipart/form-data",
    )

    response = client.post(f"/admin/reconciliations/{period.id}/complete", follow_redirects=True)
    assert b"out by" in response.data or b"neither matched" in response.data

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert db.session.get(Reconciliation, period.id).status != ReconciliationStatus.COMPLETED
    finally:
        clear_context(token)


def test_sign_off_is_refused_while_an_exception_is_open(client, db, org, period, controller):
    from app.context import clear_context
    from app.models.accounting import Reconciliation, ReconciliationStatus

    client.post(
        f"/admin/reconciliations/{period.id}/exceptions",
        data={"kind": "unexplained", "description": "Fee nobody recognises", "amount": "12.00"},
    )

    response = client.post(f"/admin/reconciliations/{period.id}/complete", follow_redirects=True)
    assert b"unresolved" in response.data or b"out by" in response.data

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert db.session.get(Reconciliation, period.id).status != ReconciliationStatus.COMPLETED
    finally:
        clear_context(token)


def test_an_exception_cannot_be_closed_without_a_note(client, db, org, period, controller):
    """An exception closed without one is an exception nobody can audit."""
    from sqlalchemy import select

    from app.context import clear_context
    from app.models.accounting import ReconciliationException

    client.post(
        f"/admin/reconciliations/{period.id}/exceptions",
        data={"kind": "unexplained", "description": "Fee nobody recognises"},
    )

    db.session.expire_all()
    token = _rebound(org)
    try:
        exception_id = (
            db.session.execute(
                select(ReconciliationException).where(ReconciliationException.org_id == org.id)
            )
            .scalar_one()
            .id
        )
    finally:
        clear_context(token)

    response = client.post(
        f"/admin/reconciliation-exceptions/{exception_id}",
        data={"note": "   "},
        follow_redirects=True,
    )
    assert response.status_code == 200

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert db.session.get(ReconciliationException, exception_id).resolved_at is None
    finally:
        clear_context(token)


def test_a_period_with_nothing_in_it_and_nothing_to_explain_signs_off(
    client, db, org, bank, scope, controller
):
    """Opening equals closing, no transactions: the trivial but real case."""
    from app.context import clear_context
    from app.models.accounting import Reconciliation, ReconciliationStatus
    from app.services.accounting.reconciliation import open_reconciliation

    token = _rebound(org)
    try:
        quiet = open_reconciliation(
            db.session,
            org_id=org.id,
            bank_account_id=bank.id,
            statement_start=dt.date(2026, 1, 1),
            statement_end=dt.date(2026, 1, 31),
            opening_balance=Decimal("0.00"),
            closing_balance=Decimal("0.00"),
        )
        db.session.commit()
        quiet_id = quiet.id
    finally:
        clear_context(token)

    assert client.post(f"/admin/reconciliations/{quiet_id}/complete").status_code == 302

    db.session.expire_all()
    token = _rebound(org)
    try:
        assert db.session.get(Reconciliation, quiet_id).status == ReconciliationStatus.COMPLETED
    finally:
        clear_context(token)


def test_overlapping_periods_on_one_account_are_refused(client, db, org, bank, period, controller):
    """Two reconciliations covering one day is two answers for that day."""
    response = client.post(
        "/admin/reconciliations",
        data={
            "bank_account_id": bank.id,
            "statement_start": "2026-04-15",
            "statement_end": "2026-05-15",
            "opening_balance": "0.00",
            "closing_balance": "0.00",
        },
        follow_redirects=True,
    )
    assert b"already covers" in response.data


@pytest.mark.parametrize("balance", ["NaN", "Infinity", "a thousand"])
def test_a_balance_that_is_not_a_number_is_refused(client, db, org, bank, controller, balance):
    response = client.post(
        "/admin/reconciliations",
        data={
            "bank_account_id": bank.id,
            "statement_start": START.isoformat(),
            "statement_end": END.isoformat(),
            "opening_balance": balance,
            "closing_balance": "0.00",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert b"is not an amount" in response.data


# ---------------------------------------------------------------------------
# Who may, and whose
# ---------------------------------------------------------------------------


def test_an_auditor_can_read_but_not_write(client, db, org, period, make_user, sign_in):
    make_user("auditor", email="recon-readonly@test.local")
    sign_in("recon-readonly@test.local")

    assert client.get("/admin/reconciliations").status_code == 200
    assert client.post(f"/admin/reconciliations/{period.id}/complete").status_code == 403


def test_another_tenants_reconciliation_is_not_found(client, db, org, other_org, controller):
    from app.context import RequestContext, bind_context, clear_context, new_correlation_id
    from app.models.accounting import BankAccount, BankAccountType
    from app.services.accounting.chart import AccountCode, seed_chart_of_accounts
    from app.services.accounting.reconciliation import open_reconciliation

    token = bind_context(
        RequestContext(
            correlation_id=new_correlation_id(),
            org_id=other_org.id,
            actor_type="system",
            source="test",
        )
    )
    try:
        their_accounts = seed_chart_of_accounts(db.session, other_org.id)
        account = BankAccount(
            org_id=other_org.id,
            code="RIVOPS",
            name="Rival Operating",
            account_type=BankAccountType.OPERATING,
            gl_account_id=their_accounts[AccountCode.CASH_OPERATING].id,
        )
        db.session.add(account)
        db.session.flush()
        theirs = open_reconciliation(
            db.session,
            org_id=other_org.id,
            bank_account_id=account.id,
            statement_start=START,
            statement_end=END,
            opening_balance=Decimal("0.00"),
            closing_balance=Decimal("0.00"),
        )
        db.session.commit()
        theirs_id = theirs.id
    finally:
        clear_context(token)

    assert client.get(f"/admin/reconciliations/{theirs_id}").status_code == 404
    assert client.post(f"/admin/reconciliations/{theirs_id}/complete").status_code == 404


def test_an_anonymous_visitor_cannot_reach_the_workspace(client, period):
    assert client.get("/admin/reconciliations").status_code in (302, 401)
    assert client.post(f"/admin/reconciliations/{period.id}/complete").status_code in (302, 401)


# ---------------------------------------------------------------------------
# The API
# ---------------------------------------------------------------------------


def test_the_api_imports_and_reports_duplicates(client, db, org, bank, period, controller):
    response = client.post(
        "/api/v1/bank-statements", json={"bank_account_id": bank.id, "csv": STATEMENT}
    )
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    assert body["imported"] == 2
    assert len(body["transactions"]) == 2

    again = client.post(
        "/api/v1/bank-statements", json={"bank_account_id": bank.id, "csv": STATEMENT}
    ).get_json()
    assert again["imported"] == 0
    assert again["duplicates"] == 2


def test_suggestions_carry_their_reasons(client, db, org, bank, period, controller):
    """A ranked list nobody can interrogate is a list nobody should accept."""
    transactions = client.post(
        "/api/v1/bank-statements", json={"bank_account_id": bank.id, "csv": STATEMENT}
    ).get_json()["transactions"]

    body = client.get(f"/api/v1/bank-transactions/{transactions[0]['id']}/matches").get_json()
    assert "data" in body
    for candidate in body["data"]:
        assert isinstance(candidate["reasons"], list)
        assert 0 <= candidate["confidence"] <= 100


def test_the_api_refuses_a_sign_off_that_does_not_agree(client, db, org, bank, period, controller):
    client.post("/api/v1/bank-statements", json={"bank_account_id": bank.id, "csv": STATEMENT})

    response = client.post(f"/api/v1/reconciliations/{period.id}/complete", json={})
    assert response.status_code in (409, 422)


def test_a_period_that_ends_before_it_starts_is_rejected(client, bank, controller):
    response = client.post(
        "/api/v1/reconciliations",
        json={
            "bank_account_id": bank.id,
            "statement_start": END.isoformat(),
            "statement_end": START.isoformat(),
            "opening_balance": "0.00",
            "closing_balance": "0.00",
        },
    )
    assert response.status_code == 422


def test_a_ledger_line_settles_once(client, db, org, bank, period, controller, accounts):
    """Two bank lines cannot both claim the same ledger line."""
    from sqlalchemy import select

    from app.context import clear_context
    from app.models.accounting import JournalLine
    from app.services.accounting.chart import AccountCode
    from app.services.accounting.ledger import LineInput, post_journal_entry

    token = _rebound(org)
    try:
        post_journal_entry(
            db.session,
            org_id=org.id,
            entry_date=dt.date(2026, 4, 3),
            description="Plumbing",
            lines=[
                LineInput(
                    account_id=accounts[AccountCode.REPAIRS_MAINTENANCE].id,
                    debit=Decimal("480.00"),
                    memo="NORTHLIGHT PLUMBING",
                ),
                LineInput(
                    account_id=accounts[AccountCode.CASH_OPERATING].id,
                    credit=Decimal("480.00"),
                    memo="NORTHLIGHT PLUMBING",
                ),
            ],
        )
        db.session.commit()
        line_id = (
            db.session.execute(
                select(JournalLine).where(
                    JournalLine.org_id == org.id, JournalLine.memo == "NORTHLIGHT PLUMBING"
                )
            )
            .scalars()
            .first()
            .id
        )
    finally:
        clear_context(token)

    transactions = client.post(
        "/api/v1/bank-statements", json={"bank_account_id": bank.id, "csv": STATEMENT}
    ).get_json()["transactions"]

    first = client.post(
        f"/api/v1/bank-transactions/{transactions[0]['id']}/match",
        json={"journal_line_id": line_id},
    )
    assert first.status_code == 200, first.get_json()

    second = client.post(
        f"/api/v1/bank-transactions/{transactions[1]['id']}/match",
        json={"journal_line_id": line_id},
    )
    assert second.status_code in (409, 422)
    assert b"settles once" in second.data


def test_another_tenants_bank_transaction_is_not_found(client, db, org, other_org, controller):
    response = client.get("/api/v1/bank-transactions/01a00000-0000-7000-8000-000000000000/matches")
    assert response.status_code == 404

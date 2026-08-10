"""Double-entry invariants.

If any of these fail, the books are wrong - which is the one class of bug that
cannot be patched away after the fact.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.errors import BusinessRuleViolation
from app.models.accounting import (
    JournalStatus,
    LedgerImbalanceError,
    PeriodStatus,
)
from app.services.accounting.chart import AccountCode
from app.services.accounting.ledger import (
    LineInput,
    close_period,
    ensure_period,
    post_journal_entry,
    reverse_journal_entry,
    trial_balance,
)

pytestmark = pytest.mark.integration

#: Actor identifiers are UUID columns; a placeholder string is rejected at the
#: type boundary, which is the GUID decorator doing its job.
ACTOR_ID = str(__import__("app.models.types", fromlist=["uuid7"]).uuid7())


def _lines(accounts, amount: Decimal) -> list[LineInput]:
    return [
        LineInput(account_id=accounts[AccountCode.CASH_OPERATING].id, debit=amount),
        LineInput(account_id=accounts[AccountCode.RENTAL_INCOME].id, credit=amount),
    ]


def test_balanced_entry_posts(db, org, scope, accounts):
    entry = post_journal_entry(
        db.session,
        org_id=org.id,
        entry_date=dt.date.today(),
        description="Rent received",
        lines=_lines(accounts, Decimal("1500.00")),
    )
    db.session.commit()

    assert entry.status == JournalStatus.POSTED
    assert entry.total_debit == entry.total_credit == Decimal("1500.0000")
    assert entry.is_balanced
    assert entry.entry_number.startswith("JE-")


def test_unbalanced_entry_is_refused(db, org, scope, accounts):
    with pytest.raises(BusinessRuleViolation) as exc:
        post_journal_entry(
            db.session,
            org_id=org.id,
            entry_date=dt.date.today(),
            description="Broken",
            lines=[
                LineInput(account_id=accounts[AccountCode.CASH_OPERATING].id, debit=Decimal("100")),
                LineInput(account_id=accounts[AccountCode.RENTAL_INCOME].id, credit=Decimal("90")),
            ],
        )
    assert exc.value.code == "ledger_unbalanced"


def test_zero_value_entry_is_refused(db, org, scope, accounts):
    from app.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        post_journal_entry(
            db.session,
            org_id=org.id,
            entry_date=dt.date.today(),
            description="Nothing",
            lines=[
                LineInput(account_id=accounts[AccountCode.CASH_OPERATING].id, debit=Decimal("0")),
                LineInput(account_id=accounts[AccountCode.RENTAL_INCOME].id, credit=Decimal("0")),
            ],
        )


def test_control_accounts_reject_manual_postings(db, org, scope, accounts):
    """AR and AP are maintained by the system, not by hand."""
    with pytest.raises(BusinessRuleViolation, match="Control accounts"):
        post_journal_entry(
            db.session,
            org_id=org.id,
            entry_date=dt.date.today(),
            description="Sneaky AR adjustment",
            lines=[
                LineInput(
                    account_id=accounts[AccountCode.ACCOUNTS_RECEIVABLE].id, debit=Decimal("500")
                ),
                LineInput(account_id=accounts[AccountCode.RENTAL_INCOME].id, credit=Decimal("500")),
            ],
        )


def test_posted_entry_cannot_be_edited(db, org, scope, accounts):
    entry = post_journal_entry(
        db.session,
        org_id=org.id,
        entry_date=dt.date.today(),
        description="Immutable",
        lines=_lines(accounts, Decimal("250.00")),
    )
    db.session.commit()

    entry.description = "Tampered"
    with pytest.raises(LedgerImbalanceError, match="immutable"):
        db.session.flush()
    db.session.rollback()


def test_posted_entry_cannot_be_deleted(db, org, scope, accounts):
    entry = post_journal_entry(
        db.session,
        org_id=org.id,
        entry_date=dt.date.today(),
        description="Undeletable",
        lines=_lines(accounts, Decimal("75.00")),
    )
    db.session.commit()

    db.session.delete(entry)
    with pytest.raises(LedgerImbalanceError, match="cannot be deleted"):
        db.session.flush()
    db.session.rollback()


def test_reversal_mirrors_the_original_and_nets_to_zero(db, org, scope, accounts):
    original = post_journal_entry(
        db.session,
        org_id=org.id,
        entry_date=dt.date.today(),
        description="Original",
        lines=_lines(accounts, Decimal("900.00")),
    )
    db.session.commit()

    reversal = reverse_journal_entry(
        db.session, entry=original, reason="Posted to the wrong property"
    )
    db.session.commit()

    assert original.status == JournalStatus.REVERSED
    assert original.reversed_by_id == reversal.id
    assert reversal.reverses_id == original.id

    original_lines = {line.account_id: (line.debit, line.credit) for line in original.lines}
    for line in reversal.lines:
        debit, credit = original_lines[line.account_id]
        assert line.debit == credit
        assert line.credit == debit

    rows = trial_balance(db.session, org_id=org.id)
    assert all(row["balance"] == Decimal("0.0000") for row in rows)


def test_entry_cannot_be_reversed_twice(db, org, scope, accounts):
    entry = post_journal_entry(
        db.session,
        org_id=org.id,
        entry_date=dt.date.today(),
        description="Once only",
        lines=_lines(accounts, Decimal("60.00")),
    )
    db.session.commit()
    reverse_journal_entry(db.session, entry=entry, reason="First correction")
    db.session.commit()

    with pytest.raises(BusinessRuleViolation, match="already been reversed"):
        reverse_journal_entry(db.session, entry=entry, reason="Second attempt")


def test_closed_period_refuses_new_postings(db, org, scope, accounts):
    today = dt.date.today()
    period = ensure_period(db.session, org_id=org.id, on_date=today)
    close_period(db.session, period=period, actor_id=ACTOR_ID)
    db.session.commit()

    assert period.status == PeriodStatus.CLOSED
    with pytest.raises(BusinessRuleViolation) as exc:
        post_journal_entry(
            db.session,
            org_id=org.id,
            entry_date=today,
            description="Too late",
            lines=_lines(accounts, Decimal("10.00")),
        )
    assert exc.value.code == "period_closed"


def test_reopening_a_period_demands_a_reason(db, org, scope):
    from app.errors import ValidationFailed
    from app.services.accounting.ledger import reopen_period

    period = ensure_period(db.session, org_id=org.id, on_date=dt.date.today())
    close_period(db.session, period=period, actor_id=ACTOR_ID)
    db.session.commit()

    with pytest.raises(ValidationFailed):
        reopen_period(db.session, period=period, reason="oops", actor_id=ACTOR_ID)

    reopen_period(
        db.session,
        period=period,
        reason="Correcting a misposted management fee identified in review",
        actor_id=ACTOR_ID,
    )
    assert period.status == PeriodStatus.OPEN


def test_trial_balance_always_balances(db, org, scope, accounts):
    for amount in (Decimal("10.01"), Decimal("0.10"), Decimal("0.20"), Decimal("99999.99")):
        post_journal_entry(
            db.session,
            org_id=org.id,
            entry_date=dt.date.today(),
            description=f"Entry {amount}",
            lines=_lines(accounts, amount),
        )
    db.session.commit()

    rows = trial_balance(db.session, org_id=org.id)
    total_debit = sum(row["debit"] for row in rows)
    total_credit = sum(row["credit"] for row in rows)

    # 0.10 + 0.20 must be exactly 0.30. This is the assertion that fails when
    # money round-trips through a float.
    assert total_debit == total_credit
    assert total_debit == Decimal("100010.3000")


def test_entry_numbers_are_sequential_and_gap_free(db, org, scope, accounts):
    numbers = []
    for _ in range(5):
        entry = post_journal_entry(
            db.session,
            org_id=org.id,
            entry_date=dt.date.today(),
            description="Sequenced",
            lines=_lines(accounts, Decimal("1.00")),
        )
        numbers.append(entry.entry_number)
    db.session.commit()

    assert numbers == [f"JE-{index:06d}" for index in range(1, 6)]

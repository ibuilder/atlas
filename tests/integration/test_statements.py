"""Owner statements: temporal ownership, apportionment, and distributions.

The case that matters is a property changing hands mid-period. A system that
apportions by "who owns it now" pays the wrong person, and the mistake is
invisible until an owner reads their statement.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.errors import BusinessRuleViolation, ValidationFailed
from app.models.accounting import BankAccount, BankAccountType
from app.models.org import OwnerEntity, OwnershipStake, OwnerType
from app.services.accounting.chart import AccountCode
from app.services.accounting.ledger import LineInput, post_journal_entry
from app.services.accounting.statements import (
    generate_statement,
    generate_statements_for_period,
    issue_distribution,
    ownership_share,
    period_activity,
)

pytestmark = pytest.mark.integration

MARCH_START = dt.date(2026, 3, 1)
MARCH_END = dt.date(2026, 3, 31)


@pytest.fixture()
def owner(db, org, scope):
    record = OwnerEntity(
        org_id=org.id, code="OWN1", name="First Owner", owner_type=OwnerType.INDIVIDUAL
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture()
def second_owner(db, org, scope):
    record = OwnerEntity(
        org_id=org.id, code="OWN2", name="Second Owner", owner_type=OwnerType.COMPANY
    )
    db.session.add(record)
    db.session.commit()
    return record


@pytest.fixture()
def bank(db, org, scope, accounts):
    record = BankAccount(
        org_id=org.id,
        code="OPER",
        name="Operating",
        account_type=BankAccountType.OPERATING,
        gl_account_id=accounts[AccountCode.CASH_OPERATING].id,
    )
    db.session.add(record)
    db.session.commit()
    return record


def _stake(db, org, property_record, owner, percentage, start, end=None):
    stake = OwnershipStake(
        org_id=org.id,
        property_id=property_record.id,
        owner_entity_id=owner.id,
        percentage=Decimal(str(percentage)),
        effective_from=start,
        effective_to=end,
    )
    db.session.add(stake)
    db.session.commit()
    return stake


def _activity(db, org, accounts, property_record, income="3000.00", expense="800.00", on=None):
    """Post rent income and a repair expense against the property."""
    when = on or dt.date(2026, 3, 10)
    post_journal_entry(
        db.session,
        org_id=org.id,
        entry_date=when,
        description="Rent",
        lines=[
            LineInput(
                account_id=accounts[AccountCode.CASH_OPERATING].id,
                debit=Decimal(income),
                property_id=property_record.id,
            ),
            LineInput(
                account_id=accounts[AccountCode.RENTAL_INCOME].id,
                credit=Decimal(income),
                property_id=property_record.id,
            ),
        ],
        property_id=property_record.id,
    )
    # Only when there is something to post: the ledger rightly refuses an entry
    # that moves nothing, so a zero-expense scenario posts income alone.
    if Decimal(expense) > 0:
        post_journal_entry(
            db.session,
            org_id=org.id,
            entry_date=when,
            description="Repair",
            lines=[
                LineInput(
                    account_id=accounts[AccountCode.REPAIRS_MAINTENANCE].id,
                    debit=Decimal(expense),
                    property_id=property_record.id,
                ),
                LineInput(
                    account_id=accounts[AccountCode.CASH_OPERATING].id,
                    credit=Decimal(expense),
                    property_id=property_record.id,
                ),
            ],
            property_id=property_record.id,
        )
    db.session.commit()


# ------------------------------------------------------------ apportionment


def test_sole_owner_for_the_whole_period_holds_everything(db, org, scope, property_record, owner):
    _stake(db, org, property_record, owner, 100, dt.date(2020, 1, 1))
    share = ownership_share(
        db.session,
        property_id=property_record.id,
        period_start=MARCH_START,
        period_end=MARCH_END,
        owner_entity_id=owner.id,
    )
    assert share == Decimal("1.000000")


def test_fractional_ownership(db, org, scope, property_record, owner, second_owner):
    _stake(db, org, property_record, owner, 60, dt.date(2020, 1, 1))
    _stake(db, org, property_record, second_owner, 40, dt.date(2020, 1, 1))

    first = ownership_share(
        db.session,
        property_id=property_record.id,
        period_start=MARCH_START,
        period_end=MARCH_END,
        owner_entity_id=owner.id,
    )
    second = ownership_share(
        db.session,
        property_id=property_record.id,
        period_start=MARCH_START,
        period_end=MARCH_END,
        owner_entity_id=second_owner.id,
    )
    assert first == Decimal("0.600000")
    assert second == Decimal("0.400000")
    assert first + second == Decimal("1.000000")


def test_mid_period_transfer_splits_by_days(db, org, scope, property_record, owner, second_owner):
    """The case a "who owns it today" model gets silently wrong.

    Sold on 13 March: the outgoing owner held days 1-13 (thirteen of thirty-one)
    and the incoming owner days 14-31 (eighteen).
    """
    _stake(db, org, property_record, owner, 100, dt.date(2020, 1, 1), dt.date(2026, 3, 13))
    _stake(db, org, property_record, second_owner, 100, dt.date(2026, 3, 14))

    outgoing = ownership_share(
        db.session,
        property_id=property_record.id,
        period_start=MARCH_START,
        period_end=MARCH_END,
        owner_entity_id=owner.id,
    )
    incoming = ownership_share(
        db.session,
        property_id=property_record.id,
        period_start=MARCH_START,
        period_end=MARCH_END,
        owner_entity_id=second_owner.id,
    )

    assert outgoing == (Decimal(13) / Decimal(31)).quantize(Decimal("0.000001"))
    assert incoming == (Decimal(18) / Decimal(31)).quantize(Decimal("0.000001"))
    # Nothing falls between the two owners, and nothing is counted twice.
    assert outgoing + incoming == Decimal("1.000000")


def test_an_owner_who_sold_before_the_period_holds_nothing(db, org, scope, property_record, owner):
    _stake(db, org, property_record, owner, 100, dt.date(2020, 1, 1), dt.date(2026, 2, 28))
    share = ownership_share(
        db.session,
        property_id=property_record.id,
        period_start=MARCH_START,
        period_end=MARCH_END,
        owner_entity_id=owner.id,
    )
    assert share == Decimal("0.000000")


def test_reversed_period_is_refused(db, org, scope, property_record, owner):
    with pytest.raises(ValidationFailed):
        ownership_share(
            db.session,
            property_id=property_record.id,
            period_start=MARCH_END,
            period_end=MARCH_START,
            owner_entity_id=owner.id,
        )


# ---------------------------------------------------------------- activity


def test_activity_reads_from_the_ledger(db, org, scope, accounts, property_record):
    _activity(db, org, accounts, property_record, income="3000.00", expense="800.00")
    activity = period_activity(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        period_start=MARCH_START,
        period_end=MARCH_END,
    )
    assert activity.income == Decimal("3000.0000")
    assert activity.expense == Decimal("800.0000")
    assert activity.net == Decimal("2200.0000")


def test_activity_excludes_other_periods(db, org, scope, accounts, property_record):
    _activity(db, org, accounts, property_record, on=dt.date(2026, 2, 10))
    activity = period_activity(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        period_start=MARCH_START,
        period_end=MARCH_END,
    )
    assert activity.income == ZERO_DEC


ZERO_DEC = Decimal("0.0000")


# --------------------------------------------------------------- statements


def test_statement_apportions_to_the_owner_share(
    db, org, scope, accounts, property_record, owner, second_owner
):
    _stake(db, org, property_record, owner, 75, dt.date(2020, 1, 1))
    _stake(db, org, property_record, second_owner, 25, dt.date(2020, 1, 1))
    _activity(db, org, accounts, property_record, income="4000.00", expense="1000.00")

    major = generate_statement(
        db.session,
        org_id=org.id,
        owner_entity_id=owner.id,
        property_id=property_record.id,
        period_start=MARCH_START,
        period_end=MARCH_END,
    )
    minor = generate_statement(
        db.session,
        org_id=org.id,
        owner_entity_id=second_owner.id,
        property_id=property_record.id,
        period_start=MARCH_START,
        period_end=MARCH_END,
    )
    db.session.commit()

    assert major.total_income == Decimal("3000.0000")
    assert major.total_expense == Decimal("750.0000")
    assert major.net_income == Decimal("2250.0000")
    assert minor.total_income == Decimal("1000.0000")
    assert minor.net_income == Decimal("750.0000")
    # The two statements account for the whole property, with nothing lost.
    assert major.total_income + minor.total_income == Decimal("4000.0000")


def test_management_fee_is_applied(db, org, scope, accounts, property_record, owner):
    org.settings = {**(org.settings or {}), "management_fee_rate": "0.08"}
    _stake(db, org, property_record, owner, 100, dt.date(2020, 1, 1))
    _activity(db, org, accounts, property_record, income="5000.00", expense="0.00")

    statement = generate_statement(
        db.session,
        org_id=org.id,
        owner_entity_id=owner.id,
        property_id=property_record.id,
        period_start=MARCH_START,
        period_end=MARCH_END,
    )
    db.session.commit()

    assert statement.management_fee == Decimal("400.0000")
    assert statement.net_income == Decimal("4600.0000")


def test_an_absurd_fee_rate_is_refused(db, org, scope, accounts, property_record, owner):
    org.settings = {**(org.settings or {}), "management_fee_rate": "8"}  # 800%
    _stake(db, org, property_record, owner, 100, dt.date(2020, 1, 1))

    with pytest.raises(ValidationFailed, match="fraction"):
        generate_statement(
            db.session,
            org_id=org.id,
            owner_entity_id=owner.id,
            property_id=property_record.id,
            period_start=MARCH_START,
            period_end=MARCH_END,
        )


def test_regeneration_updates_rather_than_duplicates(
    db, org, scope, accounts, property_record, owner
):
    """A corrected ledger must be restatable without a second statement."""
    from app.models.accounting import OwnerStatement

    _stake(db, org, property_record, owner, 100, dt.date(2020, 1, 1))
    _activity(db, org, accounts, property_record, income="1000.00", expense="0.00")
    first = generate_statement(
        db.session,
        org_id=org.id,
        owner_entity_id=owner.id,
        property_id=property_record.id,
        period_start=MARCH_START,
        period_end=MARCH_END,
    )
    db.session.commit()

    _activity(db, org, accounts, property_record, income="500.00", expense="0.00")
    second = generate_statement(
        db.session,
        org_id=org.id,
        owner_entity_id=owner.id,
        property_id=property_record.id,
        period_start=MARCH_START,
        period_end=MARCH_END,
    )
    db.session.commit()

    assert first.id == second.id
    assert second.total_income == Decimal("1500.0000")
    assert db.session.query(OwnerStatement).count() == 1


def test_generating_for_a_period_covers_everyone_who_held_a_stake(
    db, org, scope, accounts, property_record, owner, second_owner
):
    """Including the owner who sold part-way through and no longer holds it."""
    _stake(db, org, property_record, owner, 100, dt.date(2020, 1, 1), dt.date(2026, 3, 13))
    _stake(db, org, property_record, second_owner, 100, dt.date(2026, 3, 14))
    _activity(db, org, accounts, property_record, income="3100.00", expense="0.00")

    statements = generate_statements_for_period(
        db.session, org_id=org.id, period_start=MARCH_START, period_end=MARCH_END
    )
    db.session.commit()

    assert len(statements) == 2
    total = sum(s.total_income for s in statements)
    assert total == Decimal("3100.0000")


# ------------------------------------------------------------ distributions


def _statement_with_funds(db, org, accounts, property_record, owner, income="2000.00"):
    _stake(db, org, property_record, owner, 100, dt.date(2020, 1, 1))
    _activity(db, org, accounts, property_record, income=income, expense="0.00")
    statement = generate_statement(
        db.session,
        org_id=org.id,
        owner_entity_id=owner.id,
        property_id=property_record.id,
        period_start=MARCH_START,
        period_end=MARCH_END,
    )
    db.session.commit()
    return statement


def test_distribution_posts_and_reduces_the_balance(
    db, org, scope, accounts, property_record, owner, bank
):
    from app.services.accounting.ledger import trial_balance

    statement = _statement_with_funds(db, org, accounts, property_record, owner)
    issue_distribution(
        db.session,
        statement=statement,
        bank_account_id=bank.id,
        amount=Decimal("1500.00"),
        distribution_date=MARCH_END,
    )
    db.session.commit()

    assert statement.distribution_amount == Decimal("1500.0000")
    assert statement.closing_balance == Decimal("500.0000")

    rows = trial_balance(db.session, org_id=org.id)
    assert sum(r["debit"] for r in rows) == sum(r["credit"] for r in rows)


def test_distribution_cannot_exceed_the_balance(
    db, org, scope, accounts, property_record, owner, bank
):
    statement = _statement_with_funds(db, org, accounts, property_record, owner)
    with pytest.raises(BusinessRuleViolation, match="exceeds"):
        issue_distribution(
            db.session,
            statement=statement,
            bank_account_id=bank.id,
            amount=Decimal("5000.00"),
            distribution_date=MARCH_END,
        )


def test_the_reserve_is_retained(db, org, scope, accounts, property_record, owner, bank):
    """An owner's agreed reserve is not available to distribute."""
    owner.reserve_amount = Decimal("500.00")
    statement = _statement_with_funds(db, org, accounts, property_record, owner, income="2000.00")

    with pytest.raises(BusinessRuleViolation, match="reserve"):
        issue_distribution(
            db.session,
            statement=statement,
            bank_account_id=bank.id,
            amount=Decimal("1600.00"),
            distribution_date=MARCH_END,
        )

    issue_distribution(
        db.session,
        statement=statement,
        bank_account_id=bank.id,
        amount=Decimal("1500.00"),
        distribution_date=MARCH_END,
    )
    db.session.commit()
    assert statement.closing_balance == Decimal("500.0000")


def test_trust_accounts_cannot_fund_a_distribution(
    db, org, scope, accounts, property_record, owner
):
    trust = BankAccount(
        org_id=org.id,
        code="TR",
        name="Deposit Trust",
        account_type=BankAccountType.TRUST,
        gl_account_id=accounts[AccountCode.CASH_TRUST].id,
        is_trust=True,
    )
    db.session.add(trust)
    statement = _statement_with_funds(db, org, accounts, property_record, owner)

    with pytest.raises(BusinessRuleViolation, match="trust account"):
        issue_distribution(
            db.session,
            statement=statement,
            bank_account_id=trust.id,
            amount=Decimal("100.00"),
            distribution_date=MARCH_END,
        )

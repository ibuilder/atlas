"""The deposit subledger: whose money is in the trust, and when.

These exist because the trust reconciliation's third leg used to read a column
nothing in the application ever wrote. Every test here goes through the service
rather than setting a balance by hand - the point is that the ordinary path
populates it.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.errors import BusinessRuleViolation, ValidationFailed
from app.models.accounting import BankAccount, BankAccountType, DepositMovementKind
from app.services.accounting.chart import AccountCode
from app.services.accounting.deposits import (
    collect_deposit,
    deposit_balance,
    deposit_balances,
    holding_account_id,
    release_deposit,
)

pytestmark = pytest.mark.integration

MAY = dt.date(2026, 5, 1)


@pytest.fixture()
def trust_account(db, org, scope, accounts):
    record = BankAccount(
        org_id=org.id,
        code="TRUST",
        name="Security deposit trust",
        account_type=BankAccountType.TRUST,
        gl_account_id=accounts[AccountCode.CASH_TRUST].id,
        is_trust=True,
    )
    db.session.add(record)
    db.session.commit()
    return record


def test_collecting_a_deposit_posts_and_records_it(db, org, scope, trust_account, lease_record):
    """One call, both effects. Doing either alone is how the two drift."""
    from app.models.accounting import JournalEntry

    movement = collect_deposit(
        db.session,
        org_id=org.id,
        lease_id=lease_record.id,
        bank_account_id=trust_account.id,
        amount=Decimal("2400.00"),
        effective_date=MAY,
    )
    db.session.commit()

    assert movement.amount == Decimal("2400.0000")
    assert movement.kind == DepositMovementKind.COLLECTED
    assert movement.journal_entry_id is not None

    entry = db.session.get(JournalEntry, movement.journal_entry_id)
    assert entry.total_debit == entry.total_credit == Decimal("2400.0000")

    assert deposit_balance(db.session, org_id=org.id, lease_id=lease_record.id) == Decimal(
        "2400.0000"
    )


def test_the_lease_cache_follows_the_subledger(db, org, scope, trust_account, lease_record):
    """``Lease.deposit_held`` is a convenience, but it must not be stale."""
    collect_deposit(
        db.session,
        org_id=org.id,
        lease_id=lease_record.id,
        bank_account_id=trust_account.id,
        amount=Decimal("2400.00"),
        effective_date=MAY,
    )
    db.session.commit()
    assert lease_record.deposit_held == Decimal("2400.0000")

    release_deposit(
        db.session,
        org_id=org.id,
        lease_id=lease_record.id,
        bank_account_id=trust_account.id,
        amount=Decimal("400.00"),
        kind=DepositMovementKind.APPLIED,
        effective_date=MAY + dt.timedelta(days=30),
    )
    db.session.commit()
    assert lease_record.deposit_held == Decimal("2000.0000")


def test_a_balance_can_be_asked_about_the_past(db, org, scope, trust_account, lease_record):
    """The reason this is movements rather than a column."""
    collect_deposit(
        db.session,
        org_id=org.id,
        lease_id=lease_record.id,
        bank_account_id=trust_account.id,
        amount=Decimal("2400.00"),
        effective_date=MAY,
    )
    release_deposit(
        db.session,
        org_id=org.id,
        lease_id=lease_record.id,
        bank_account_id=trust_account.id,
        amount=Decimal("2400.00"),
        effective_date=MAY + dt.timedelta(days=60),
    )
    db.session.commit()

    before = deposit_balance(
        db.session, org_id=org.id, lease_id=lease_record.id, as_of=MAY + dt.timedelta(days=1)
    )
    after = deposit_balance(
        db.session, org_id=org.id, lease_id=lease_record.id, as_of=MAY + dt.timedelta(days=90)
    )
    assert before == Decimal("2400.0000")
    assert after == Decimal("0.0000")


def test_releasing_more_than_is_held_is_refused(db, org, scope, trust_account, lease_record):
    """Paying out more than was held for somebody spends another resident's money."""
    collect_deposit(
        db.session,
        org_id=org.id,
        lease_id=lease_record.id,
        bank_account_id=trust_account.id,
        amount=Decimal("2400.00"),
        effective_date=MAY,
    )
    db.session.commit()

    with pytest.raises(BusinessRuleViolation) as exc:
        release_deposit(
            db.session,
            org_id=org.id,
            lease_id=lease_record.id,
            bank_account_id=trust_account.id,
            amount=Decimal("2500.00"),
            effective_date=MAY + dt.timedelta(days=30),
        )
    assert "somebody else's money" in str(exc.value)


def test_an_operating_account_cannot_hold_a_deposit(db, org, scope, accounts, lease_record):
    operating = BankAccount(
        org_id=org.id,
        code="OPER",
        name="Operating",
        account_type=BankAccountType.OPERATING,
        gl_account_id=accounts[AccountCode.CASH_OPERATING].id,
    )
    db.session.add(operating)
    db.session.commit()

    with pytest.raises(ValidationFailed) as exc:
        collect_deposit(
            db.session,
            org_id=org.id,
            lease_id=lease_record.id,
            bank_account_id=operating.id,
            amount=Decimal("2400.00"),
        )
    assert "not a trust account" in str(exc.value)


def test_a_zero_movement_is_refused(db, org, scope, trust_account, lease_record):
    with pytest.raises(ValidationFailed):
        collect_deposit(
            db.session,
            org_id=org.id,
            lease_id=lease_record.id,
            bank_account_id=trust_account.id,
            amount=Decimal("0"),
        )


def test_balances_are_scoped_to_one_account(
    db, org, scope, accounts, trust_account, lease_record, property_record, unit_record
):
    from app.models.leasing import Lease, LeaseStatus
    from app.models.sequences import SequenceKey
    from app.services.common.numbering import next_number

    second_account = BankAccount(
        org_id=org.id,
        code="TRUST2",
        name="Second trust",
        account_type=BankAccountType.TRUST,
        gl_account_id=accounts[AccountCode.CASH_TRUST].id,
        is_trust=True,
    )
    other_lease = Lease(
        org_id=org.id,
        lease_number=next_number(db.session, SequenceKey.LEASE, org_id=org.id),
        property_id=property_record.id,
        unit_id=unit_record.id,
        status=LeaseStatus.ACTIVE,
        start_date=MAY,
        end_date=MAY + dt.timedelta(days=364),
        rent_amount=Decimal("2000.00"),
        security_deposit=Decimal("2000.00"),
    )
    db.session.add_all([second_account, other_lease])
    db.session.commit()

    collect_deposit(
        db.session,
        org_id=org.id,
        lease_id=lease_record.id,
        bank_account_id=trust_account.id,
        amount=Decimal("2400.00"),
        effective_date=MAY,
    )
    collect_deposit(
        db.session,
        org_id=org.id,
        lease_id=other_lease.id,
        bank_account_id=second_account.id,
        amount=Decimal("2000.00"),
        effective_date=MAY,
    )
    db.session.commit()

    first = deposit_balances(db.session, org_id=org.id, bank_account_id=trust_account.id)
    second = deposit_balances(db.session, org_id=org.id, bank_account_id=second_account.id)

    assert first == {lease_record.id: Decimal("2400.0000")}
    assert second == {other_lease.id: Decimal("2000.0000")}
    assert holding_account_id(db.session, org_id=org.id, lease_id=lease_record.id) == (
        trust_account.id
    )


def test_a_lease_with_nothing_held_has_no_holding_account(db, org, scope, lease_record):
    assert holding_account_id(db.session, org_id=org.id, lease_id=lease_record.id) is None

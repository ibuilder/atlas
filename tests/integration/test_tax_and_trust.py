"""Year-end 1099 totals, and the trust three-way reconciliation.

The two tests that carry these modules: a vendor over the threshold with no TIN
is *reported* rather than silently dropped, and a trust whose bank and book
agree perfectly is still called short when the beneficiaries are owed more than
is there.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.errors import NotFound, ValidationFailed
from app.models.accounting import BankAccount, BankAccountType
from app.services.accounting.chart import AccountCode
from app.services.accounting.tax import (
    NEC_THRESHOLD,
    generate_1099_report,
    tax_report_rows,
)
from app.services.accounting.trust import reconcile_trust

pytestmark = pytest.mark.integration

YEAR = 2026
RECORDER = "019fea00-0000-7000-8000-0000000000e1"
APPROVER = "019fea00-0000-7000-8000-0000000000e2"


# ---------------------------------------------------------------------------
# 1099
# ---------------------------------------------------------------------------


@pytest.fixture()
def operating(db, org, scope, accounts):
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


def _paid_vendor(db, org, accounts, operating, *, code, amount, tax_id="12-3456789", **kwargs):
    """A vendor with a bill recorded, approved, and paid in the tax year."""
    from app.models.vendor import ComplianceStatus, Vendor, VendorStatus
    from app.services.accounting.payables import BillLineInput, approve_bill, pay_bill, record_bill

    params = {
        "legal_name": f"{code} Holdings LLC",
        "address_line1": "1 Trade Street",
        "city": "Testville",
        "postal_code": "00001",
        "is_1099_reportable": True,
    }
    params.update(kwargs)

    vendor = Vendor(
        org_id=org.id,
        code=code,
        name=f"{code} Contracting",
        status=VendorStatus.ACTIVE,
        compliance_status=ComplianceStatus.VALID,
        compliance_expires_at=dt.date(YEAR + 1, 6, 1),
        tax_id=tax_id,
        tax_id_last4=(tax_id or "")[-4:] or None,
        **params,
    )
    db.session.add(vendor)
    db.session.commit()

    bill = record_bill(
        db.session,
        org_id=org.id,
        vendor_id=vendor.id,
        bill_date=dt.date(YEAR, 6, 1),
        due_date=dt.date(YEAR, 7, 1),
        lines=[
            BillLineInput(
                description="Services",
                amount=Decimal(amount),
                account_id=accounts[AccountCode.REPAIRS_MAINTENANCE].id,
            )
        ],
        actor_id=RECORDER,
    )
    approve_bill(db.session, bill=bill, approver_id=APPROVER)
    pay_bill(
        db.session,
        bill=bill,
        bank_account_id=operating.id,
        amount=Decimal(amount),
        paid_date=dt.date(YEAR, 7, 15),
    )
    db.session.commit()
    return vendor


def test_a_vendor_over_the_threshold_is_filable(db, org, scope, accounts, operating):
    _paid_vendor(db, org, accounts, operating, code="ACME", amount="4500.00")
    report = generate_1099_report(db.session, org_id=org.id, year=YEAR)
    db.session.commit()

    assert len(report.filable) == 1
    assert report.filable[0].total_paid == Decimal("4500.0000")
    assert report.is_clean


def test_a_vendor_below_the_threshold_is_not_reportable(db, org, scope, accounts, operating):
    _paid_vendor(db, org, accounts, operating, code="SMALL", amount="200.00")
    report = generate_1099_report(db.session, org_id=org.id, year=YEAR)

    assert report.filable == []
    assert len(report.below_threshold) == 1


def test_the_threshold_boundary_is_inclusive(db, org, scope, accounts, operating):
    _paid_vendor(db, org, accounts, operating, code="EXACT", amount=str(NEC_THRESHOLD))
    report = generate_1099_report(db.session, org_id=org.id, year=YEAR)
    assert len(report.filable) == 1


def test_a_vendor_with_no_tin_is_reported_not_dropped(db, org, scope, accounts, operating):
    """The failure that costs money: a run that quietly omits somebody."""
    _paid_vendor(db, org, accounts, operating, code="NOTIN", amount="5000.00", tax_id=None)
    report = generate_1099_report(db.session, org_id=org.id, year=YEAR)
    db.session.commit()

    assert report.filable == []
    assert len(report.blocked) == 1
    assert not report.is_clean
    assert any("W-9" in blocker for blocker in report.blocked[0].blockers)


def test_backup_withholding_is_computed_for_a_missing_tin(db, org, scope, accounts, operating):
    _paid_vendor(db, org, accounts, operating, code="NOTIN", amount="1000.00", tax_id=None)
    report = generate_1099_report(db.session, org_id=org.id, year=YEAR)

    assert report.blocked[0].backup_withholding_due == Decimal("240.0000")


def test_a_missing_legal_name_blocks_filing(db, org, scope, accounts, operating):
    _paid_vendor(db, org, accounts, operating, code="NONAME", amount="5000.00", legal_name=None)
    report = generate_1099_report(db.session, org_id=org.id, year=YEAR)

    assert len(report.blocked) == 1
    assert any("legal name" in blocker for blocker in report.blocked[0].blockers)


def test_a_missing_address_blocks_the_payee_copy(db, org, scope, accounts, operating):
    _paid_vendor(db, org, accounts, operating, code="NOADDR", amount="5000.00", address_line1=None)
    report = generate_1099_report(db.session, org_id=org.id, year=YEAR)
    assert any("address" in blocker for blocker in report.blocked[0].blockers)


def test_a_vendor_marked_not_reportable_is_excluded(db, org, scope, accounts, operating):
    _paid_vendor(
        db, org, accounts, operating, code="CORP", amount="9000.00", is_1099_reportable=False
    )
    report = generate_1099_report(db.session, org_id=org.id, year=YEAR)
    assert report.filable == []


def test_a_voided_payment_is_not_counted(db, org, scope, accounts, operating):
    """A stopped cheque was never paid.

    Counting it overstates the return, and an overstated 1099 is one the payee
    disputes and the filer is held to.
    """
    from sqlalchemy import select

    from app.models.accounting import BillPayment
    from app.models.types import utcnow

    vendor = _paid_vendor(db, org, accounts, operating, code="STOP", amount="5000.00")

    payment = db.session.execute(
        select(BillPayment).where(BillPayment.org_id == org.id)
    ).scalar_one()
    payment.voided_at = utcnow()
    payment.void_reason = "Cheque stopped; never presented."
    db.session.commit()

    report = generate_1099_report(db.session, org_id=org.id, year=YEAR)
    assert report.filable == []
    assert all(total.vendor.id != vendor.id for total in report.totals)


def test_payments_are_counted_by_the_year_they_were_paid(db, org, scope, accounts, operating):
    """A December bill paid in January belongs to January's return."""
    _paid_vendor(db, org, accounts, operating, code="ACME", amount="4500.00")

    assert len(generate_1099_report(db.session, org_id=org.id, year=YEAR).filable) == 1
    assert generate_1099_report(db.session, org_id=org.id, year=YEAR - 1).filable == []


def test_an_implausible_year_is_refused(db, org, scope):
    with pytest.raises(ValidationFailed):
        generate_1099_report(db.session, org_id=org.id, year=1492)


def test_the_export_never_carries_a_full_tin(db, org, scope, accounts, operating):
    """A spreadsheet of TINs is a breach waiting for somebody to email it."""
    _paid_vendor(db, org, accounts, operating, code="ACME", amount="4500.00")
    report = generate_1099_report(db.session, org_id=org.id, year=YEAR)

    rows = tax_report_rows(report)
    assert rows[0]["tin_last4"] == "6789"
    assert "12-3456789" not in str(rows)


def test_a_blocked_run_is_audited_as_a_warning(db, org, scope, accounts, operating):
    from app.models.audit import AuditEvent, AuditSeverity

    _paid_vendor(db, org, accounts, operating, code="NOTIN", amount="5000.00", tax_id=None)
    generate_1099_report(db.session, org_id=org.id, year=YEAR)
    db.session.commit()

    assert [
        event
        for event in db.session.query(AuditEvent).all()
        if event.severity == AuditSeverity.WARNING and "cannot be filed" in (event.reason or "")
    ]


# ---------------------------------------------------------------------------
# Trust
# ---------------------------------------------------------------------------


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


def _second_lease(db, org, property_record, unit_record):
    """Another active lease, so a second beneficiary can exist."""
    from app.models.leasing import Lease, LeaseStatus
    from app.models.sequences import SequenceKey
    from app.services.common.numbering import next_number

    lease = Lease(
        org_id=org.id,
        lease_number=next_number(db.session, SequenceKey.LEASE, org_id=org.id),
        property_id=property_record.id,
        unit_id=unit_record.id,
        status=LeaseStatus.ACTIVE,
        start_date=dt.date(YEAR, 2, 1),
        end_date=dt.date(YEAR + 1, 1, 31),
        rent_amount=Decimal("2000.00"),
        security_deposit=Decimal("2000.00"),
    )
    db.session.add(lease)
    db.session.commit()
    return lease


def _hold_deposit(db, org, trust_account, lease, amount, *, on=None):
    """Take a deposit into trust through the path the application uses.

    Deliberately not hand-posted. Assembling the journal entry and setting the
    lease's balance by hand is what let the beneficiary leg pass its tests while
    nothing in the application populated it at all.
    """
    from app.services.accounting.deposits import collect_deposit

    movement = collect_deposit(
        db.session,
        org_id=org.id,
        lease_id=lease.id,
        bank_account_id=trust_account.id,
        amount=Decimal(amount),
        effective_date=on or dt.date(YEAR, 1, 15),
    )
    db.session.commit()
    return movement


def test_a_balanced_trust_ties_out_three_ways(
    db, org, scope, accounts, trust_account, lease_record
):
    from app.models.leasing import LeaseStatus

    lease_record.status = LeaseStatus.ACTIVE
    _hold_deposit(db, org, trust_account, lease_record, "2000.00")

    position = reconcile_trust(
        db.session,
        org_id=org.id,
        bank_account_id=trust_account.id,
        as_of=dt.date(YEAR, 12, 31),
        bank_balance=Decimal("2000.00"),
    )
    db.session.commit()

    assert position.bank_balance == Decimal("2000.0000")
    assert position.book_balance == Decimal("2000.0000")
    assert position.beneficiary_total == Decimal("2000.0000")
    assert position.is_balanced
    assert position.shortfall == Decimal("0.0000")


def test_bank_and_book_can_agree_while_the_trust_is_short(
    db, org, scope, accounts, trust_account, lease_record, property_record, unit_record
):
    """The whole reason the third leg exists.

    Two deposits collected, then two thousand quietly moved out of the trust to
    operating without being released to anybody. The money really did leave, so
    bank and book agree perfectly and a two-way reconciliation reports it clean.
    The beneficiaries are still owed all four thousand.
    """
    from app.services.accounting.ledger import LineInput, post_journal_entry

    second = _second_lease(db, org, property_record, unit_record)
    _hold_deposit(db, org, trust_account, lease_record, "2000.00")
    _hold_deposit(db, org, trust_account, second, "2000.00")

    post_journal_entry(
        db.session,
        org_id=org.id,
        entry_date=dt.date(YEAR, 6, 1),
        description="Transfer to operating",
        lines=[
            LineInput(account_id=accounts[AccountCode.CASH_OPERATING].id, debit=Decimal("2000.00")),
            LineInput(account_id=accounts[AccountCode.CASH_TRUST].id, credit=Decimal("2000.00")),
        ],
    )
    db.session.commit()

    position = reconcile_trust(
        db.session,
        org_id=org.id,
        bank_account_id=trust_account.id,
        as_of=dt.date(YEAR, 12, 31),
        bank_balance=Decimal("2000.00"),
    )
    db.session.commit()

    assert position.bank_to_book == Decimal("0.0000")  # a two-way check passes
    assert position.beneficiary_total == Decimal("4000.0000")
    assert position.book_to_beneficiaries == Decimal("-2000.0000")
    assert position.shortfall == Decimal("2000.0000")
    assert not position.is_balanced


def test_a_second_trust_account_is_reconciled_against_its_own_deposits(
    db, org, scope, accounts, trust_account, lease_record, property_record, unit_record
):
    """Two trust accounts must not be measured against each other's deposits.

    Scoping beneficiaries by organization alone reports a shortfall on one
    account and an equal surplus on the other - and, worse, ties out cleanly
    when one is genuinely short by exactly what the other is over.
    """
    second_account = BankAccount(
        org_id=org.id,
        code="TRUST2",
        name="Second jurisdiction trust",
        account_type=BankAccountType.TRUST,
        gl_account_id=accounts[AccountCode.CASH_TRUST].id,
        is_trust=True,
    )
    db.session.add(second_account)
    db.session.commit()

    second_lease = _second_lease(db, org, property_record, unit_record)
    _hold_deposit(db, org, trust_account, lease_record, "2000.00")
    _hold_deposit(db, org, second_account, second_lease, "3000.00")

    first = reconcile_trust(
        db.session,
        org_id=org.id,
        bank_account_id=trust_account.id,
        as_of=dt.date(YEAR, 12, 31),
        bank_balance=Decimal("2000.00"),
    )
    assert first.beneficiary_total == Decimal("2000.0000")
    assert [b.lease_number for b in first.beneficiaries] == [lease_record.lease_number]

    other = reconcile_trust(
        db.session,
        org_id=org.id,
        bank_account_id=second_account.id,
        as_of=dt.date(YEAR, 12, 31),
        bank_balance=Decimal("3000.00"),
    )
    assert other.beneficiary_total == Decimal("3000.0000")
    assert [b.lease_number for b in other.beneficiaries] == [second_lease.lease_number]


def test_the_beneficiary_leg_respects_the_as_of_date(
    db, org, scope, accounts, trust_account, lease_record, property_record, unit_record
):
    """A year-end tie-out run later must not see deposits taken since.

    The ledger leg stops at ``as_of``. A beneficiary leg reading current
    balances is being compared against a different point in time, so the
    difference moves depending on the day somebody runs the report.
    """
    second = _second_lease(db, org, property_record, unit_record)
    _hold_deposit(db, org, trust_account, lease_record, "2000.00", on=dt.date(YEAR, 1, 15))
    _hold_deposit(db, org, trust_account, second, "1500.00", on=dt.date(YEAR + 1, 3, 1))

    at_year_end = reconcile_trust(
        db.session,
        org_id=org.id,
        bank_account_id=trust_account.id,
        as_of=dt.date(YEAR, 12, 31),
        bank_balance=Decimal("2000.00"),
    )
    assert at_year_end.beneficiary_total == Decimal("2000.0000")
    assert at_year_end.is_balanced

    later = reconcile_trust(
        db.session,
        org_id=org.id,
        bank_account_id=trust_account.id,
        as_of=dt.date(YEAR + 1, 6, 30),
        bank_balance=Decimal("3500.00"),
    )
    assert later.beneficiary_total == Decimal("3500.0000")


def test_a_negative_held_balance_is_called_an_error(
    db, org, scope, accounts, trust_account, lease_record
):
    """Nobody can be owed less than nothing.

    ``release_deposit`` refuses to over-release, so this is written straight
    into the subledger: the check exists to catch a balance that arrived some
    other way - a bad import, a hand-applied correction - not one the service
    would produce.
    """
    from app.models.accounting import DepositMovement, DepositMovementKind
    from app.models.leasing import LeaseStatus

    lease_record.status = LeaseStatus.ACTIVE
    _hold_deposit(db, org, trust_account, lease_record, "2000.00")

    db.session.add(
        DepositMovement(
            org_id=org.id,
            lease_id=lease_record.id,
            bank_account_id=trust_account.id,
            amount=Decimal("-2500.00"),
            effective_date=dt.date(YEAR, 2, 1),
            kind=DepositMovementKind.ADJUSTMENT,
            reason="Imported from the previous system.",
        )
    )
    db.session.commit()

    position = reconcile_trust(
        db.session,
        org_id=org.id,
        bank_account_id=trust_account.id,
        as_of=dt.date(YEAR, 12, 31),
        bank_balance=Decimal("2000.00"),
    )
    assert any("somebody else's money" in exception for exception in position.exceptions)


def test_a_bank_to_book_difference_is_reported(
    db, org, scope, accounts, trust_account, lease_record
):
    from app.models.leasing import LeaseStatus

    lease_record.status = LeaseStatus.ACTIVE
    _hold_deposit(db, org, trust_account, lease_record, "2000.00")

    position = reconcile_trust(
        db.session,
        org_id=org.id,
        bank_account_id=trust_account.id,
        as_of=dt.date(YEAR, 12, 31),
        bank_balance=Decimal("1800.00"),
    )
    assert any("Bank and book differ" in exception for exception in position.exceptions)


def test_commingled_operating_activity_is_reported(
    db, org, scope, accounts, trust_account, lease_record
):
    """One of these is a licence problem, not a bookkeeping one."""
    from app.services.accounting.ledger import LineInput, post_journal_entry

    post_journal_entry(
        db.session,
        org_id=org.id,
        entry_date=dt.date(YEAR, 3, 1),
        description="Plumber paid from the deposit account",
        lines=[
            LineInput(
                account_id=accounts[AccountCode.REPAIRS_MAINTENANCE].id, debit=Decimal("450.00")
            ),
            LineInput(account_id=accounts[AccountCode.CASH_TRUST].id, credit=Decimal("450.00")),
        ],
    )
    db.session.commit()

    position = reconcile_trust(
        db.session, org_id=org.id, bank_account_id=trust_account.id, as_of=dt.date(YEAR, 12, 31)
    )
    assert any("does not pay operating costs" in e for e in position.exceptions)


def test_no_statement_balance_is_stated_as_not_a_reconciliation(
    db, org, scope, accounts, trust_account
):
    """Honest about what it is rather than reporting a vacuous zero."""
    position = reconcile_trust(
        db.session, org_id=org.id, bank_account_id=trust_account.id, as_of=dt.date(YEAR, 12, 31)
    )
    assert any("not a reconciliation" in exception for exception in position.exceptions)


def test_an_operating_account_is_refused(db, org, scope, accounts, operating):
    with pytest.raises(ValidationFailed) as exc:
        reconcile_trust(db.session, org_id=org.id, bank_account_id=operating.id)
    assert "not a trust account" in str(exc.value)


def test_an_unbalanced_trust_is_audited_as_critical(
    db, org, scope, accounts, trust_account, lease_record
):
    from app.models.audit import AuditEvent, AuditSeverity
    from app.models.leasing import LeaseStatus

    lease_record.status = LeaseStatus.ACTIVE
    _hold_deposit(db, org, trust_account, lease_record, "2000.00")

    reconcile_trust(
        db.session,
        org_id=org.id,
        bank_account_id=trust_account.id,
        as_of=dt.date(YEAR, 12, 31),
        bank_balance=Decimal("100.00"),
    )
    db.session.commit()

    assert [
        event
        for event in db.session.query(AuditEvent).all()
        if event.severity == AuditSeverity.CRITICAL
    ]


def test_trust_accounts_do_not_cross_organizations(db, org, other_org, scope, trust_account):
    with pytest.raises(NotFound):
        reconcile_trust(db.session, org_id=other_org.id, bank_account_id=trust_account.id)

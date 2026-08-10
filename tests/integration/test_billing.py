"""Recurring billing and the delinquency sweep.

Both are scheduled, so the assertions that matter most are the idempotency ones:
running twice must not charge a resident twice.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.models.accounting import ChargeCode, Invoice, InvoiceStatus
from app.models.leasing import ChargeFrequency, LeaseCharge
from app.models.resident import Notice, NoticeKind
from app.services.accounting.billing import (
    generate_recurring_charges,
    prorated_amount,
    sweep_delinquency,
)
from app.services.accounting.chart import AccountCode

pytestmark = pytest.mark.integration


@pytest.fixture()
def rent_code(db, org, scope, accounts):
    code = ChargeCode(
        org_id=org.id,
        code="RENT",
        name="Rent",
        gl_account_id=accounts[AccountCode.RENTAL_INCOME].id,
        is_recurring=True,
        category="rent",
    )
    db.session.add(code)
    db.session.commit()
    return code


def _charge(db, org, lease, rent_code, amount="3100.00", start=None, prorate=True):
    charge = LeaseCharge(
        org_id=org.id,
        lease_id=lease.id,
        charge_code_id=rent_code.id,
        description="Rent",
        amount=Decimal(amount),
        frequency=ChargeFrequency.MONTHLY,
        start_date=start or lease.start_date,
        prorate=prorate,
        is_active=True,
    )
    db.session.add(charge)
    db.session.commit()
    return charge


# ------------------------------------------------------------------ proration


def test_full_cycle_is_not_prorated():
    march = (dt.date(2026, 3, 1), dt.date(2026, 3, 31))
    assert prorated_amount(Decimal("3100"), *march, *march) == Decimal("3100.0000")


def test_partial_cycle_prorates_to_the_day():
    """Eighteen thirty-firsts, not half a month."""
    amount = prorated_amount(
        Decimal("3100"),
        dt.date(2026, 3, 1),
        dt.date(2026, 3, 31),
        dt.date(2026, 3, 14),
        dt.date(2026, 3, 31),
    )
    assert amount == (Decimal("3100") * 18 / 31).quantize(Decimal("0.0001"))


def test_february_prorates_against_its_own_length():
    """Calendar-correct: a short month is not 30 days."""
    amount = prorated_amount(
        Decimal("2800"),
        dt.date(2026, 2, 1),
        dt.date(2026, 2, 28),
        dt.date(2026, 2, 15),
        dt.date(2026, 2, 28),
    )
    assert amount == (Decimal("2800") * 14 / 28).quantize(Decimal("0.0001"))


# -------------------------------------------------------------- recurring


def test_a_full_month_bills_the_full_amount(db, org, scope, accounts, lease_record, rent_code):
    lease_record.start_date = dt.date(2026, 3, 1)
    lease_record.end_date = dt.date(2027, 2, 28)
    _charge(db, org, lease_record, rent_code, "3100.00", start=dt.date(2026, 3, 1))

    run = generate_recurring_charges(db.session, org_id=org.id, through_date=dt.date(2026, 3, 31))
    db.session.commit()

    assert run.cycles_billed == 1
    assert run.invoices[0].total == Decimal("3100.0000")


def test_a_mid_month_move_in_is_prorated(db, org, scope, accounts, lease_record, rent_code):
    lease_record.start_date = dt.date(2026, 3, 14)
    lease_record.end_date = dt.date(2027, 3, 13)
    _charge(db, org, lease_record, rent_code, "3100.00", start=dt.date(2026, 3, 14))

    run = generate_recurring_charges(db.session, org_id=org.id, through_date=dt.date(2026, 3, 31))
    db.session.commit()

    expected = (Decimal("3100") * 18 / 31).quantize(Decimal("0.0001"))
    assert run.invoices[0].total == expected


def test_running_twice_bills_once(db, org, scope, accounts, lease_record, rent_code):
    """The assertion the whole watermark design exists for."""
    lease_record.start_date = dt.date(2026, 3, 1)
    lease_record.end_date = dt.date(2027, 2, 28)
    _charge(db, org, lease_record, rent_code, "3100.00", start=dt.date(2026, 3, 1))

    first = generate_recurring_charges(db.session, org_id=org.id, through_date=dt.date(2026, 3, 31))
    db.session.commit()
    second = generate_recurring_charges(
        db.session, org_id=org.id, through_date=dt.date(2026, 3, 31)
    )
    db.session.commit()

    assert first.cycles_billed == 1
    assert second.cycles_billed == 0
    assert db.session.query(Invoice).count() == 1


def test_catch_up_bills_every_missed_cycle(db, org, scope, accounts, lease_record, rent_code):
    """A job that has not run for three months must not lose three months of rent."""
    lease_record.start_date = dt.date(2026, 1, 1)
    lease_record.end_date = dt.date(2026, 12, 31)
    _charge(db, org, lease_record, rent_code, "1000.00", start=dt.date(2026, 1, 1))

    run = generate_recurring_charges(db.session, org_id=org.id, through_date=dt.date(2026, 3, 31))
    db.session.commit()

    assert run.cycles_billed == 3
    assert sum(i.total for i in run.invoices) == Decimal("3000.0000")


def test_billing_stops_at_the_lease_end(db, org, scope, accounts, lease_record, rent_code):
    lease_record.start_date = dt.date(2026, 1, 1)
    lease_record.end_date = dt.date(2026, 2, 14)
    _charge(db, org, lease_record, rent_code, "3100.00", start=dt.date(2026, 1, 1))

    run = generate_recurring_charges(db.session, org_id=org.id, through_date=dt.date(2026, 6, 30))
    db.session.commit()

    assert run.cycles_billed == 2
    # The final stub month is billed only for the days occupied.
    assert run.invoices[-1].total == (Decimal("3100") * 14 / 28).quantize(Decimal("0.0001"))


def test_multiple_charges_land_on_one_invoice(db, org, scope, accounts, lease_record, rent_code):
    """A resident gets one bill, not one per charge line."""
    lease_record.start_date = dt.date(2026, 3, 1)
    lease_record.end_date = dt.date(2027, 2, 28)
    _charge(db, org, lease_record, rent_code, "3100.00", start=dt.date(2026, 3, 1))

    parking = ChargeCode(
        org_id=org.id,
        code="PARK",
        name="Parking",
        gl_account_id=accounts[AccountCode.OTHER_INCOME].id,
    )
    db.session.add(parking)
    db.session.commit()
    _charge(db, org, lease_record, parking, "150.00", start=dt.date(2026, 3, 1))

    run = generate_recurring_charges(db.session, org_id=org.id, through_date=dt.date(2026, 3, 31))
    db.session.commit()

    assert run.cycles_billed == 1
    assert run.invoices[0].total == Decimal("3250.0000")
    assert len(run.invoices[0].lines) == 2


def test_inactive_charges_are_skipped(db, org, scope, accounts, lease_record, rent_code):
    charge = _charge(db, org, lease_record, rent_code, "3100.00")
    charge.is_active = False
    db.session.commit()

    run = generate_recurring_charges(db.session, org_id=org.id, through_date=dt.date(2027, 1, 1))
    db.session.commit()
    assert run.cycles_billed == 0


# ------------------------------------------------------------ delinquency


def _overdue_invoice(db, org, accounts, lease, days_past, amount="1000.00"):
    from app.services.accounting.receivables import ChargeInput, issue_invoice

    today = dt.date.today()
    due = today - dt.timedelta(days=days_past)
    return issue_invoice(
        db.session,
        org_id=org.id,
        charges=[
            ChargeInput(
                description="Rent",
                amount=Decimal(amount),
                account_id=accounts[AccountCode.RENTAL_INCOME].id,
            )
        ],
        issue_date=due,
        due_date=due,
        lease=lease,
        property_id=lease.property_id,
    )


def test_the_grace_period_is_honoured(db, org, scope, accounts, lease_record):
    lease_record.late_fee_grace_days = 5
    _overdue_invoice(db, org, accounts, lease_record, days_past=3)
    db.session.commit()

    run = sweep_delinquency(db.session, org_id=org.id)
    db.session.commit()

    assert run.escalated == 0
    assert db.session.query(Notice).count() == 0


def test_past_the_grace_period_escalates_and_assesses_a_fee(db, org, scope, accounts, lease_record):
    lease_record.late_fee_grace_days = 5
    lease_record.late_fee_amount = Decimal("75.00")
    invoice = _overdue_invoice(db, org, accounts, lease_record, days_past=8)
    db.session.commit()

    run = sweep_delinquency(db.session, org_id=org.id)
    db.session.commit()

    assert run.escalated == 1
    assert invoice.delinquency_stage == 1
    assert len(run.late_fees) == 1
    assert run.late_fees[0].total == Decimal("75.0000")
    assert run.notices[0].kind == NoticeKind.LATE_RENT
    assert run.notices[0].respond_by is not None


def test_the_sweep_is_idempotent(db, org, scope, accounts, lease_record):
    """A second run must not assess a second late fee."""
    lease_record.late_fee_grace_days = 0
    lease_record.late_fee_amount = Decimal("75.00")
    _overdue_invoice(db, org, accounts, lease_record, days_past=3)
    db.session.commit()

    first = sweep_delinquency(db.session, org_id=org.id)
    db.session.commit()
    second = sweep_delinquency(db.session, org_id=org.id)
    db.session.commit()

    assert first.escalated == 1
    assert second.escalated == 0
    assert len(second.late_fees) == 0
    assert db.session.query(Notice).count() == 1


def test_further_delinquency_escalates_to_pay_or_quit(db, org, scope, accounts, lease_record):
    lease_record.late_fee_grace_days = 0
    lease_record.late_fee_amount = Decimal("75.00")
    invoice = _overdue_invoice(db, org, accounts, lease_record, days_past=45)
    db.session.commit()

    run = sweep_delinquency(db.session, org_id=org.id)
    db.session.commit()

    assert invoice.delinquency_stage == 3
    kinds = {notice.kind for notice in run.notices}
    assert NoticeKind.PAY_OR_QUIT in kinds
    # The intermediate stages are not skipped: each one is recorded.
    assert len(run.notices) == 3
    # Only stage one carries a fee, so a single escalation cannot charge thrice.
    assert len(run.late_fees) == 1


def test_paid_invoices_are_left_alone(db, org, scope, accounts, lease_record):
    from app.models.accounting import PaymentMethod
    from app.services.accounting.receivables import record_payment

    lease_record.late_fee_grace_days = 0
    invoice = _overdue_invoice(db, org, accounts, lease_record, days_past=20)
    db.session.commit()
    record_payment(
        db.session,
        org_id=org.id,
        amount=invoice.total,
        method=PaymentMethod.ACH,
        received_date=dt.date.today(),
        lease_id=lease_record.id,
    )
    db.session.commit()

    run = sweep_delinquency(db.session, org_id=org.id)
    db.session.commit()

    assert invoice.status == InvoiceStatus.PAID
    assert run.escalated == 0

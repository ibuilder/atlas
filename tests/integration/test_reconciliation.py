"""Bank reconciliation.

The three acceptance cases: an overlapping re-import creates no duplicates,
suggestions are ranked, and a reconciliation cannot complete while it does not
actually agree.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.errors import BusinessRuleViolation, ValidationFailed
from app.models.accounting import (
    BankAccount,
    BankAccountType,
    BankTransaction,
    ReconciliationStatus,
)
from app.services.accounting.chart import AccountCode
from app.services.accounting.ledger import LineInput, post_journal_entry
from app.services.accounting.reconciliation import (
    MatchStatus,
    StatementLine,
    auto_match,
    complete_reconciliation,
    import_statement,
    match_transaction,
    open_reconciliation,
    parse_statement_csv,
    raise_exception,
    refresh_totals,
    reopen_reconciliation,
    resolve_exception,
    suggest_matches,
    unmatch_transaction,
)

pytestmark = pytest.mark.integration

MARCH = dt.date(2026, 3, 10)
OPERATOR = "019fea00-0000-7000-8000-0000000000b1"


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


def _line(amount="1200.00", on=MARCH, description="ACH DEPOSIT LARKSPUR", **kw):
    return StatementLine(posted_date=on, amount=Decimal(amount), description=description, **kw)


def _ledger_receipt(
    db, org, accounts, amount="1200.00", on=MARCH, description="Rent from Larkspur"
):
    """A deposit in the ledger: debit the bank, credit income."""
    entry = post_journal_entry(
        db.session,
        org_id=org.id,
        entry_date=on,
        description=description,
        lines=[
            LineInput(account_id=accounts[AccountCode.CASH_OPERATING].id, debit=Decimal(amount)),
            LineInput(account_id=accounts[AccountCode.RENTAL_INCOME].id, credit=Decimal(amount)),
        ],
    )
    db.session.commit()
    return next(line for line in entry.lines if line.debit > 0)


# ------------------------------------------------------------------ import


def test_a_statement_imports(db, org, scope, bank):
    result = import_statement(
        db.session,
        org_id=org.id,
        bank_account_id=bank.id,
        lines=[_line(), _line("-450.00", description="CHECK 1041")],
    )
    db.session.commit()

    assert result.count == 2
    assert result.duplicates == 0
    assert {t.amount for t in result.imported} == {Decimal("1200.0000"), Decimal("-450.0000")}


def test_reimporting_an_overlapping_window_creates_no_duplicates(db, org, scope, bank):
    """The acceptance case. Somebody always downloads March after downloading 1-15."""
    first_half = [_line(on=dt.date(2026, 3, 1)), _line("-450.00", on=dt.date(2026, 3, 5))]
    whole_month = first_half + [_line("2000.00", on=dt.date(2026, 3, 20))]

    import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=first_half)
    db.session.commit()
    second = import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=whole_month)
    db.session.commit()

    assert second.count == 1
    assert second.duplicates == 2
    assert db.session.query(BankTransaction).count() == 3


def test_two_genuinely_identical_transactions_both_survive(db, org, scope, bank):
    """Two identical $50 fees on one day are two fees, not one duplicated."""
    lines = [_line("-50.00", description="MONTHLY FEE") for _ in range(2)]
    result = import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=lines)
    db.session.commit()
    assert result.count == 2

    # ...and re-importing the same file still adds nothing.
    again = import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=lines)
    db.session.commit()
    assert again.count == 0
    assert again.duplicates == 2


def test_the_bank_reference_wins_when_there_is_one(db, org, scope, bank):
    lines = [_line(external_id="TXN-9001"), _line(external_id="TXN-9002")]
    import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=lines)
    db.session.commit()

    # Same reference, different description: still the same transaction.
    changed = [_line(external_id="TXN-9001", description="ACH DEPOSIT (corrected)")]
    result = import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=changed)
    db.session.commit()
    assert result.duplicates == 1


def test_an_unusable_line_is_rejected_not_imported(db, org, scope, bank):
    result = import_statement(
        db.session,
        org_id=org.id,
        bank_account_id=bank.id,
        lines=[StatementLine(posted_date=MARCH, amount=Decimal("10"), description="")],
    )
    assert result.count == 0
    assert result.rejected == 1


# --------------------------------------------------------------------- CSV


def test_a_csv_export_parses():
    text = "date,amount,description,reference,id\n2026-03-10,1200.00,ACH DEPOSIT,INV-1,TXN-1\n"
    lines = parse_statement_csv(text)
    assert len(lines) == 1
    assert lines[0].amount == Decimal("1200.00")
    assert lines[0].external_id == "TXN-1"


def test_accountants_parentheses_are_negative():
    text = 'date,amount,description\n2026-03-10,"(1,250.00)",CHECK 1041\n'
    lines = parse_statement_csv(text)
    assert lines[0].amount == Decimal("-1250.00")


def test_an_unparseable_amount_is_refused_loudly():
    """A zero in a reconciliation is an afternoon somebody spends hunting."""
    text = "date,amount,description\n2026-03-10,not a number,CHECK 1041\n"
    with pytest.raises(ValidationFailed):
        parse_statement_csv(text)


def test_an_unparseable_date_is_refused():
    text = "date,amount,description\n10/03/2026,100.00,CHECK\n"
    with pytest.raises(ValidationFailed):
        parse_statement_csv(text)


# ---------------------------------------------------------------- matching


def test_an_exact_same_day_match_scores_highest(db, org, scope, accounts, bank):
    _ledger_receipt(db, org, accounts)
    result = import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=[_line()])
    db.session.commit()

    candidates = suggest_matches(db.session, transaction=result.imported[0])
    assert candidates
    assert candidates[0].confidence >= 80
    assert any("amount matches" in reason for reason in candidates[0].reasons)


def test_suggestions_are_ranked_and_explained(db, org, scope, accounts, bank):
    """The acceptance case: an unexplained ranking is one nobody trusts."""
    _ledger_receipt(db, org, accounts, on=MARCH, description="Rent from Larkspur")
    _ledger_receipt(db, org, accounts, on=MARCH + dt.timedelta(days=4), description="Other")
    result = import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=[_line()])
    db.session.commit()

    candidates = suggest_matches(db.session, transaction=result.imported[0])
    assert len(candidates) == 2
    assert candidates[0].confidence > candidates[1].confidence
    assert all(candidate.reasons for candidate in candidates)


def test_a_different_amount_is_never_a_candidate(db, org, scope, accounts, bank):
    _ledger_receipt(db, org, accounts, amount="999.00")
    result = import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=[_line()])
    db.session.commit()
    assert suggest_matches(db.session, transaction=result.imported[0]) == []


def test_an_entry_outside_the_window_is_never_a_candidate(db, org, scope, accounts, bank):
    _ledger_receipt(db, org, accounts, on=MARCH + dt.timedelta(days=30))
    result = import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=[_line()])
    db.session.commit()
    assert suggest_matches(db.session, transaction=result.imported[0]) == []


def test_auto_match_takes_the_confident_unambiguous_one(db, org, scope, accounts, bank):
    _ledger_receipt(db, org, accounts)
    import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=[_line()])
    db.session.commit()

    matched = auto_match(db.session, org_id=org.id, bank_account_id=bank.id)
    db.session.commit()

    assert len(matched) == 1
    assert matched[0].match_status == MatchStatus.MATCHED
    assert matched[0].match_confidence >= 90


def test_auto_match_refuses_a_tie(db, org, scope, accounts, bank):
    """Guessing between two identical payments is how reconciliation stops meaning anything."""
    _ledger_receipt(db, org, accounts, description="Rent")
    _ledger_receipt(db, org, accounts, description="Rent")
    import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=[_line()])
    db.session.commit()

    matched = auto_match(db.session, org_id=org.id, bank_account_id=bank.id)
    db.session.commit()

    assert matched == []
    transaction = db.session.query(BankTransaction).one()
    assert transaction.match_status == MatchStatus.SUGGESTED


def test_a_ledger_line_settles_once(db, org, scope, accounts, bank):
    line = _ledger_receipt(db, org, accounts)
    result = import_statement(
        db.session, org_id=org.id, bank_account_id=bank.id, lines=[_line(), _line()]
    )
    db.session.commit()

    match_transaction(db.session, transaction=result.imported[0], journal_line=line)
    db.session.commit()
    with pytest.raises(BusinessRuleViolation):
        match_transaction(db.session, transaction=result.imported[1], journal_line=line)


def test_unmatching_frees_the_ledger_line(db, org, scope, accounts, bank):
    line = _ledger_receipt(db, org, accounts)
    result = import_statement(
        db.session, org_id=org.id, bank_account_id=bank.id, lines=[_line(), _line()]
    )
    db.session.commit()

    match_transaction(db.session, transaction=result.imported[0], journal_line=line)
    unmatch_transaction(db.session, transaction=result.imported[0])
    db.session.commit()

    match_transaction(db.session, transaction=result.imported[1], journal_line=line)
    db.session.commit()
    assert result.imported[1].match_status == MatchStatus.MATCHED


# ----------------------------------------------------------- the session


def _reconciliation(db, org, bank, closing="1200.00"):
    return open_reconciliation(
        db.session,
        org_id=org.id,
        bank_account_id=bank.id,
        statement_start=dt.date(2026, 3, 1),
        statement_end=dt.date(2026, 3, 31),
        opening_balance=Decimal("0.00"),
        closing_balance=Decimal(closing),
    )


def test_opening_claims_the_statement_transactions(db, org, scope, accounts, bank):
    import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=[_line()])
    db.session.commit()

    reconciliation = _reconciliation(db, org, bank)
    db.session.commit()
    assert db.session.query(BankTransaction).one().reconciliation_id == reconciliation.id


def test_overlapping_reconciliations_are_refused(db, org, scope, bank):
    _reconciliation(db, org, bank)
    db.session.commit()
    with pytest.raises(BusinessRuleViolation):
        open_reconciliation(
            db.session,
            org_id=org.id,
            bank_account_id=bank.id,
            statement_start=dt.date(2026, 3, 15),
            statement_end=dt.date(2026, 4, 15),
            opening_balance=Decimal("0"),
            closing_balance=Decimal("0"),
        )


def test_a_reversed_period_is_refused(db, org, scope, bank):
    with pytest.raises(ValidationFailed):
        open_reconciliation(
            db.session,
            org_id=org.id,
            bank_account_id=bank.id,
            statement_start=dt.date(2026, 3, 31),
            statement_end=dt.date(2026, 3, 1),
            opening_balance=Decimal("0"),
            closing_balance=Decimal("0"),
        )


def test_it_cannot_complete_with_a_difference(db, org, scope, accounts, bank):
    """The acceptance case, and the whole point of the exercise."""
    import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=[_line()])
    db.session.commit()
    reconciliation = _reconciliation(db, org, bank, closing="1200.00")
    db.session.commit()

    with pytest.raises(BusinessRuleViolation) as exc:
        complete_reconciliation(db.session, reconciliation=reconciliation, completed_by_id=OPERATOR)
    assert "out by" in str(exc.value)


def test_it_completes_once_everything_agrees(db, org, scope, accounts, bank):
    _ledger_receipt(db, org, accounts)
    import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=[_line()])
    db.session.commit()
    auto_match(db.session, org_id=org.id, bank_account_id=bank.id)
    db.session.commit()

    reconciliation = _reconciliation(db, org, bank, closing="1200.00")
    complete_reconciliation(db.session, reconciliation=reconciliation, completed_by_id=OPERATOR)
    db.session.commit()

    assert reconciliation.status == ReconciliationStatus.COMPLETED
    assert reconciliation.difference == Decimal("0.0000")
    assert reconciliation.cleared_balance == Decimal("1200.0000")
    assert reconciliation.completed_by_id == OPERATOR


def test_the_cleared_balance_survives_a_reload(db, org, scope, accounts, bank):
    """It has to be a column, not an attribute set on the way past.

    Asserting on the object the service just returned passes either way. The
    figure the difference was derived from is only actually retained if it
    comes back from the database.
    """
    from app.models.accounting import Reconciliation

    _ledger_receipt(db, org, accounts)
    import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=[_line()])
    db.session.commit()
    auto_match(db.session, org_id=org.id, bank_account_id=bank.id)
    db.session.commit()

    reconciliation = _reconciliation(db, org, bank, closing="1200.00")
    complete_reconciliation(db.session, reconciliation=reconciliation, completed_by_id=OPERATOR)
    db.session.commit()

    reconciliation_id = reconciliation.id
    db.session.expunge_all()

    reloaded = db.session.get(Reconciliation, reconciliation_id)
    assert reloaded is not None
    assert reloaded.cleared_balance == Decimal("1200.0000")


def test_it_cannot_complete_with_an_unresolved_exception(db, org, scope, accounts, bank):
    _ledger_receipt(db, org, accounts)
    import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=[_line()])
    db.session.commit()
    auto_match(db.session, org_id=org.id, bank_account_id=bank.id)
    reconciliation = _reconciliation(db, org, bank, closing="1200.00")
    raise_exception(
        db.session,
        reconciliation=reconciliation,
        kind="missing_deposit",
        description="A deposit slip has no matching entry.",
    )
    db.session.commit()

    with pytest.raises(BusinessRuleViolation) as exc:
        complete_reconciliation(db.session, reconciliation=reconciliation, completed_by_id=OPERATOR)
    assert "unresolved" in str(exc.value)


def test_a_resolved_exception_unblocks_completion(db, org, scope, accounts, bank):
    _ledger_receipt(db, org, accounts)
    import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=[_line()])
    db.session.commit()
    auto_match(db.session, org_id=org.id, bank_account_id=bank.id)
    reconciliation = _reconciliation(db, org, bank, closing="1200.00")
    exception = raise_exception(
        db.session,
        reconciliation=reconciliation,
        kind="timing",
        description="Deposit in transit.",
    )
    resolve_exception(
        db.session,
        exception=exception,
        resolved_by_id=OPERATOR,
        note="Cleared on 2 April; carried forward.",
    )
    db.session.commit()

    complete_reconciliation(db.session, reconciliation=reconciliation, completed_by_id=OPERATOR)
    db.session.commit()
    assert reconciliation.status == ReconciliationStatus.COMPLETED


def test_resolving_without_a_note_is_refused(db, org, scope, bank):
    reconciliation = _reconciliation(db, org, bank)
    exception = raise_exception(
        db.session, reconciliation=reconciliation, kind="timing", description="Something."
    )
    db.session.commit()
    with pytest.raises(ValidationFailed):
        resolve_exception(db.session, exception=exception, resolved_by_id=OPERATOR, note="  ")


def test_an_unmatched_transaction_blocks_completion(db, org, scope, accounts, bank):
    """Even when the arithmetic happens to work out."""
    reconciliation = _reconciliation(db, org, bank, closing="0.00")
    import_statement(
        db.session,
        org_id=org.id,
        bank_account_id=bank.id,
        lines=[_line("500.00"), _line("-500.00", description="REVERSAL")],
    )
    db.session.commit()
    for transaction in db.session.query(BankTransaction).all():
        transaction.reconciliation_id = reconciliation.id
    refresh_totals(db.session, reconciliation=reconciliation)
    db.session.commit()

    assert reconciliation.difference == Decimal("0.0000")
    with pytest.raises(BusinessRuleViolation) as exc:
        complete_reconciliation(db.session, reconciliation=reconciliation, completed_by_id=OPERATOR)
    assert "neither matched nor" in str(exc.value)


def test_a_completed_reconciliation_locks_its_transactions(db, org, scope, accounts, bank):
    _ledger_receipt(db, org, accounts)
    import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=[_line()])
    db.session.commit()
    auto_match(db.session, org_id=org.id, bank_account_id=bank.id)
    reconciliation = _reconciliation(db, org, bank, closing="1200.00")
    complete_reconciliation(db.session, reconciliation=reconciliation, completed_by_id=OPERATOR)
    db.session.commit()

    transaction = db.session.query(BankTransaction).one()
    with pytest.raises(BusinessRuleViolation):
        unmatch_transaction(db.session, transaction=transaction)


def test_reopening_requires_a_reason_and_is_audited(db, org, scope, accounts, bank):
    from app.models.audit import AuditAction, AuditEvent

    _ledger_receipt(db, org, accounts)
    import_statement(db.session, org_id=org.id, bank_account_id=bank.id, lines=[_line()])
    db.session.commit()
    auto_match(db.session, org_id=org.id, bank_account_id=bank.id)
    reconciliation = _reconciliation(db, org, bank, closing="1200.00")
    complete_reconciliation(db.session, reconciliation=reconciliation, completed_by_id=OPERATOR)
    db.session.commit()

    with pytest.raises(ValidationFailed):
        reopen_reconciliation(
            db.session, reconciliation=reconciliation, actor_id=OPERATOR, reason=""
        )

    reopen_reconciliation(
        db.session,
        reconciliation=reconciliation,
        actor_id=OPERATOR,
        reason="A transaction was matched to the wrong entry.",
    )
    db.session.commit()

    assert reconciliation.status == ReconciliationStatus.IN_REVIEW
    critical = [
        event
        for event in db.session.query(AuditEvent)
        .filter(AuditEvent.action == AuditAction.RECONCILIATION_COMPLETED)
        .all()
        if event.payload.get("reopened")
    ]
    assert len(critical) == 1
    assert critical[0].severity.value == "critical"


# ------------------------------------------------------------- isolation


def test_statements_do_not_cross_organizations(db, org, other_org, scope, bank):
    from app.errors import NotFound

    with pytest.raises(NotFound):
        import_statement(db.session, org_id=other_org.id, bank_account_id=bank.id, lines=[_line()])


def test_a_matching_reference_raises_the_score(db, org, scope, accounts, bank):
    """The branch that reads the ledger entry for a reference.

    Untested until the demo seed ran through it and it raised AttributeError on
    a field that does not exist - a scoring path that crashed rather than
    scored, on every transaction the bank supplied a reference for.
    """
    line = _ledger_receipt(db, org, accounts)
    entry_number = db.session.get(type(line.entry), line.journal_entry_id).entry_number

    with_reference = import_statement(
        db.session,
        org_id=org.id,
        bank_account_id=bank.id,
        lines=[_line(reference=entry_number)],
    ).imported[0]

    candidates = suggest_matches(db.session, transaction=with_reference)
    assert candidates
    assert any("reference appears" in reason for reason in candidates[0].reasons)


def test_an_unmatched_reference_does_not_raise_the_score(db, org, scope, accounts, bank):
    _ledger_receipt(db, org, accounts)
    transaction = import_statement(
        db.session,
        org_id=org.id,
        bank_account_id=bank.id,
        lines=[_line(reference="SOMETHING-ELSE")],
    ).imported[0]

    candidates = suggest_matches(db.session, transaction=transaction)
    assert candidates
    assert not any("reference appears" in reason for reason in candidates[0].reasons)

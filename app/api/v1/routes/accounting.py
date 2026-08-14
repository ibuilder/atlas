"""Ledger, invoices, and payments.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from flask import Response, request
from flask_login import current_user
from sqlalchemy import select

from app.api.helpers import (
    paginate,
    parse_body,
    parse_query,
    respond,
    respond_collection,
    respond_created,
)
from app.api.v1 import api_v1_bp
from app.errors import NotFound
from app.extensions import current_session, db
from app.middleware import require_org_scope
from app.models.accounting import (
    BankTransaction,
    Bill,
    BillPayment,
    BillStatus,
    DepositMovement,
    Invoice,
    InvoiceStatus,
    JournalEntry,
    JournalLine,
    Payment,
    Reconciliation,
    ReconciliationException,
)
from app.models.leasing import Lease
from app.models.types import utcnow
from app.schemas.operations import (
    AutoMatchRequest,
    BankTransactionOut,
    BillApproval,
    BillCreate,
    BillLineOut,
    BillListQuery,
    BillOut,
    BillPaymentCreate,
    BillPaymentOut,
    DepositBalanceOut,
    DepositCollect,
    DepositMovementListQuery,
    DepositMovementOut,
    DepositRelease,
    ExceptionRaise,
    ExceptionResolve,
    InvoiceListQuery,
    InvoiceOut,
    JournalEntryCreate,
    JournalEntryOut,
    PaymentCreate,
    PaymentOut,
    ReconciliationComplete,
    ReconciliationExceptionOut,
    ReconciliationListQuery,
    ReconciliationOpen,
    ReconciliationOut,
    StatementImport,
    TransactionMatch,
)
from app.security.permissions import Perm
from app.security.policies import filter_permitted, require
from app.services.accounting import deposits, ledger, payables, receivables, reconciliation
from app.services.common.unit_of_work import transaction

__all__ = []


@api_v1_bp.get("/invoices", endpoint="invoices_list")
def list_invoices() -> Response:
    require(Perm.INVOICE_READ)
    query = parse_query(InvoiceListQuery)
    org_id = require_org_scope()

    stmt = select(Invoice).where(Invoice.org_id == org_id)
    if query.status:
        stmt = stmt.where(Invoice.status == query.status)
    if query.lease_id:
        stmt = stmt.where(Invoice.lease_id == query.lease_id)
    if query.property_id:
        stmt = stmt.where(Invoice.property_id == query.property_id)
    if query.overdue:
        stmt = stmt.where(
            Invoice.due_date < utcnow().date(),
            Invoice.status.in_([InvoiceStatus.OPEN, InvoiceStatus.PARTIALLY_PAID]),
        )

    page = paginate(current_session(), stmt, Invoice, limit=query.limit, cursor=query.cursor)
    page.items = filter_permitted(Perm.INVOICE_READ, page.items)
    return respond_collection(page, InvoiceOut)


@api_v1_bp.get("/invoices/<id:invoice_id>", endpoint="invoices_get")
def get_invoice(invoice_id: str) -> Response:
    record = db.session.get(Invoice, invoice_id)
    if record is None:
        raise NotFound("That invoice was not found.")
    require(Perm.INVOICE_READ, record)
    return respond(InvoiceOut.model_validate(record, from_attributes=True))


@api_v1_bp.get("/payments", endpoint="payments_list")
def list_payments() -> Response:
    require(Perm.PAYMENT_READ)
    query = parse_query(InvoiceListQuery)
    org_id = require_org_scope()

    stmt = select(Payment).where(Payment.org_id == org_id)
    if query.lease_id:
        stmt = stmt.where(Payment.lease_id == query.lease_id)

    page = paginate(current_session(), stmt, Payment, limit=query.limit, cursor=query.cursor)
    page.items = filter_permitted(Perm.PAYMENT_READ, page.items)
    return respond_collection(page, PaymentOut)


@api_v1_bp.post("/payments", endpoint="payments_create")
def create_payment() -> Response:
    """Record a payment.

    Supply ``Idempotency-Key`` on this endpoint. A retried payment without one
    is a second charge, and no downstream consumer can tell the difference.
    """
    require(Perm.PAYMENT_RECORD)
    payload = parse_body(PaymentCreate)
    org_id = require_org_scope()

    allocations = [
        (item["invoice_id"], item["amount"])
        for item in payload.applications
        if item.get("invoice_id") and item.get("amount")
    ] or None

    with transaction() as session:
        record = receivables.record_payment(
            session,
            org_id=org_id,
            amount=payload.amount,
            method=payload.method,
            received_date=payload.received_date,
            lease_id=payload.lease_id,
            resident_id=payload.resident_id or getattr(current_user, "resident_id", None),
            bank_account_id=payload.bank_account_id,
            reference=payload.reference,
            memo=payload.memo,
            allocations=allocations,
            actor_id=current_user.id,
        )

    return respond_created(
        PaymentOut.model_validate(record, from_attributes=True),
        location=f"/api/v1/payments/{record.id}",
    )


@api_v1_bp.get("/ledger/entries", endpoint="ledger_entries_list")
def list_journal_entries() -> Response:
    require(Perm.LEDGER_READ)
    query = parse_query(InvoiceListQuery)
    org_id = require_org_scope()

    stmt = select(JournalEntry).where(JournalEntry.org_id == org_id)
    if query.property_id:
        stmt = stmt.where(JournalEntry.property_id == query.property_id)

    page = paginate(current_session(), stmt, JournalEntry, limit=query.limit, cursor=query.cursor)
    return respond_collection(page, JournalEntryOut)


@api_v1_bp.post("/ledger/entries", endpoint="ledger_entries_create")
def create_journal_entry() -> Response:
    """Post a manual journal entry.

    The payload schema already rejects an unbalanced entry, and the service
    rejects it again. Two checks on purpose: the schema gives the client a
    useful message, the service guarantees the invariant for every other caller.
    """
    require(Perm.LEDGER_POST)
    payload = parse_body(JournalEntryCreate)
    org_id = require_org_scope()

    lines = [
        ledger.LineInput(
            account_id=line.account_id,
            debit=line.debit,
            credit=line.credit,
            memo=line.memo,
            property_id=line.property_id,
            unit_id=line.unit_id,
            lease_id=line.lease_id,
        )
        for line in payload.lines
    ]

    with transaction() as session:
        entry = ledger.post_journal_entry(
            session,
            org_id=org_id,
            entry_date=payload.entry_date,
            description=payload.description,
            lines=lines,
            memo=payload.memo,
            property_id=payload.property_id,
            source_type="manual",
            post=payload.post,
            actor_id=current_user.id,
        )

    return respond_created(
        JournalEntryOut.model_validate(entry, from_attributes=True),
        location=f"/api/v1/ledger/entries/{entry.id}",
    )


@api_v1_bp.get("/ledger/trial-balance", endpoint="ledger_trial_balance")
def get_trial_balance() -> Response:
    """Per-account totals. Debits and credits must agree."""
    require(Perm.LEDGER_READ)
    org_id = require_org_scope()

    rows = ledger.trial_balance(current_session(), org_id=org_id)
    total_debit = sum(row["debit"] for row in rows)
    total_credit = sum(row["credit"] for row in rows)

    return respond(
        {
            "data": [
                {
                    **row,
                    "debit": str(row["debit"]),
                    "credit": str(row["credit"]),
                    "balance": str(row["balance"]),
                }
                for row in rows
            ],
            "totals": {
                "debit": str(total_debit),
                "credit": str(total_credit),
                "balanced": total_debit == total_credit,
            },
        }
    )


# ------------------------------------------------------------------ deposits
#
# The subledger that says whose money is in the trust. Collecting and releasing
# are separate permissions on purpose: taking a deposit in is routine, and
# letting one out is money leaving an account the operator does not own.


@api_v1_bp.get("/deposits", endpoint="deposits_list")
def list_deposit_movements() -> Response:
    """Movements, newest first. Filterable by lease, account, and date."""
    require(Perm.DEPOSIT_READ)
    query = parse_query(DepositMovementListQuery)
    org_id = require_org_scope()

    stmt = select(DepositMovement).where(DepositMovement.org_id == org_id)
    if query.lease_id:
        stmt = stmt.where(DepositMovement.lease_id == query.lease_id)
    if query.bank_account_id:
        stmt = stmt.where(DepositMovement.bank_account_id == query.bank_account_id)
    if query.as_of:
        stmt = stmt.where(DepositMovement.effective_date <= query.as_of)

    page = paginate(
        current_session(), stmt, DepositMovement, limit=query.limit, cursor=query.cursor
    )
    return respond_collection(page, DepositMovementOut)


@api_v1_bp.get("/leases/<id:lease_id>/deposit", endpoint="deposits_balance")
def get_deposit_balance(lease_id: str) -> Response:
    """What is held for one lease, optionally in one account, at a date.

    ``as_of`` is answered from the movements rather than a stored balance, so
    "what did we hold when they moved out" is a question this can answer months
    afterwards - which is when it gets asked.
    """
    require(Perm.DEPOSIT_READ)
    query = parse_query(DepositMovementListQuery)
    org_id = require_org_scope()

    lease = db.session.get(Lease, lease_id)
    if lease is None or lease.org_id != org_id:
        raise NotFound("That lease was not found.")

    as_of = query.as_of or utcnow().date()
    held = deposits.deposit_balance(
        current_session(),
        org_id=org_id,
        lease_id=lease_id,
        bank_account_id=query.bank_account_id,
        as_of=as_of,
    )
    return respond(
        DepositBalanceOut(
            lease_id=lease_id,
            bank_account_id=query.bank_account_id,
            as_of=as_of,
            held=held,
        ).model_dump(mode="json")
    )


@api_v1_bp.post("/deposits/collect", endpoint="deposits_collect")
def collect_deposit() -> Response:
    """Take a deposit into trust.

    Supply ``Idempotency-Key``. A retried collection without one is a second
    deposit the resident never paid, and the trust will reconcile to it.
    """
    require(Perm.DEPOSIT_COLLECT)
    payload = parse_body(DepositCollect)
    org_id = require_org_scope()

    with transaction() as session:
        movement = deposits.collect_deposit(
            session,
            org_id=org_id,
            lease_id=payload.lease_id,
            bank_account_id=payload.bank_account_id,
            amount=payload.amount,
            effective_date=payload.effective_date,
            reason=payload.reason,
            actor_id=current_user.id,
        )

    return respond_created(
        DepositMovementOut.model_validate(movement, from_attributes=True),
        location=f"/api/v1/deposits/{movement.id}",
    )


@api_v1_bp.post("/deposits/release", endpoint="deposits_release")
def release_deposit() -> Response:
    """Let a deposit, or part of one, back out of trust.

    Separate permission from collecting, and a sensitive one: this is money
    leaving an account that holds somebody else's funds.
    """
    require(Perm.DEPOSIT_RELEASE)
    payload = parse_body(DepositRelease)
    org_id = require_org_scope()

    with transaction() as session:
        movement = deposits.release_deposit(
            session,
            org_id=org_id,
            lease_id=payload.lease_id,
            bank_account_id=payload.bank_account_id,
            amount=payload.amount,
            kind=payload.kind,
            effective_date=payload.effective_date,
            reason=payload.reason,
            actor_id=current_user.id,
        )

    return respond_created(
        DepositMovementOut.model_validate(movement, from_attributes=True),
        location=f"/api/v1/deposits/{movement.id}",
    )


# ----------------------------------------------------------------- payables
#
# Money going out. Two rules here are the whole point of the module and both
# are enforced by the service rather than by these routes:
#
# Whoever recorded a bill cannot approve it, however senior they are.
# Separation of duties is by identity, not by role — fake-vendor fraud needs
# one person able to do both halves, and a role check does not stop that.
#
# An approval authorises an *amount*, not a row. The approved total is
# snapshotted, and a bill that has moved since is no longer covered by it.


def _bill_or_404(bill_id: str, org_id: str) -> Bill:
    record = db.session.get(Bill, bill_id)
    if record is None or record.org_id != org_id:
        raise NotFound("That bill was not found.")
    return record


@api_v1_bp.get("/bills", endpoint="bills_list")
def list_bills() -> Response:
    require(Perm.BILL_READ)
    query = parse_query(BillListQuery)
    org_id = require_org_scope()

    stmt = select(Bill).where(Bill.org_id == org_id)
    if query.status:
        stmt = stmt.where(Bill.status == query.status)
    if query.vendor_id:
        stmt = stmt.where(Bill.vendor_id == query.vendor_id)
    if query.property_id:
        stmt = stmt.where(Bill.property_id == query.property_id)
    if query.due:
        # Approved, still owing, and past due: what a payment run today is for.
        stmt = stmt.where(
            Bill.status.in_([BillStatus.APPROVED, BillStatus.PARTIALLY_PAID]),
            Bill.balance > 0,
            Bill.due_date <= utcnow().date(),
        )

    page = paginate(current_session(), stmt, Bill, limit=query.limit, cursor=query.cursor)
    return respond_collection(page, BillOut)


@api_v1_bp.get("/bills/<id:bill_id>", endpoint="bills_get")
def get_bill(bill_id: str) -> Response:
    """One bill with its coded lines and what has been paid against it."""
    require(Perm.BILL_READ)
    org_id = require_org_scope()

    record = _bill_or_404(bill_id, org_id)
    payments = list(
        db.session.execute(
            select(BillPayment)
            .where(BillPayment.org_id == org_id, BillPayment.bill_id == bill_id)
            .order_by(BillPayment.paid_date)
        ).scalars()
    )
    return respond(
        {
            **BillOut.model_validate(record, from_attributes=True).model_dump(mode="json"),
            "lines": [
                BillLineOut.model_validate(line, from_attributes=True).model_dump(mode="json")
                for line in record.lines
            ],
            "payments": [
                BillPaymentOut.model_validate(p, from_attributes=True).model_dump(mode="json")
                for p in payments
            ],
            #: Whether this one needs a second person. An unset threshold means
            #: everything does — a control that vanishes when configuration is
            #: missing is not a control.
            "requires_approval": payables.requires_approval(
                current_session(), org_id=org_id, total=record.total
            ),
        }
    )


@api_v1_bp.post("/bills", endpoint="bills_create")
def create_bill() -> Response:
    """Record a vendor invoice and post its ledger impact."""
    require(Perm.BILL_MANAGE)
    payload = parse_body(BillCreate)
    org_id = require_org_scope()

    with transaction() as session:
        record = payables.record_bill(
            session,
            org_id=org_id,
            vendor_id=payload.vendor_id,
            bill_date=payload.bill_date,
            due_date=payload.due_date,
            lines=[
                payables.BillLineInput(
                    description=line.description,
                    amount=line.amount,
                    account_id=line.account_id,
                    property_id=line.property_id,
                    unit_id=line.unit_id,
                    quantity=line.quantity,
                    is_owner_billable=line.is_owner_billable,
                )
                for line in payload.lines
            ],
            vendor_invoice_number=payload.vendor_invoice_number,
            property_id=payload.property_id,
            work_order_id=payload.work_order_id,
            memo=payload.memo,
            actor_id=current_user.id,
        )

    return respond_created(
        BillOut.model_validate(record, from_attributes=True),
        location=f"/api/v1/bills/{record.id}",
    )


@api_v1_bp.post("/bills/<id:bill_id>/approve", endpoint="bills_approve")
def approve_bill(bill_id: str) -> Response:
    """Authorise a bill for payment.

    Refused if the caller is the person who recorded it. That check is by
    identity rather than by role: seniority does not make one person into two.
    """
    require(Perm.BILL_APPROVE)
    payload = parse_body(BillApproval)
    org_id = require_org_scope()

    with transaction() as session:
        bill = _bill_or_404(bill_id, org_id)
        record = payables.approve_bill(
            session, bill=bill, approver_id=current_user.id, note=payload.note
        )

    return respond(BillOut.model_validate(record, from_attributes=True))


@api_v1_bp.post("/bills/<id:bill_id>/payments", endpoint="bill_payments_create")
def pay_bill(bill_id: str) -> Response:
    """Disburse against an approved bill."""
    require(Perm.BILL_PAY)
    payload = parse_body(BillPaymentCreate)
    org_id = require_org_scope()

    with transaction() as session:
        bill = _bill_or_404(bill_id, org_id)
        payment = payables.pay_bill(
            session,
            bill=bill,
            bank_account_id=payload.bank_account_id,
            amount=payload.amount,
            paid_date=payload.paid_date,
            method=payload.method,
            check_number=payload.check_number,
            actor_id=current_user.id,
        )

    return respond_created(
        BillPaymentOut.model_validate(payment, from_attributes=True),
        location=f"/api/v1/bills/{bill_id}",
    )


@api_v1_bp.get("/payables/outstanding", endpoint="payables_outstanding")
def outstanding_payable() -> Response:
    """What is owed, in total or to one vendor."""
    require(Perm.BILL_READ)
    org_id = require_org_scope()
    vendor_id = request.args.get("vendor_id")

    return respond(
        {
            "vendor_id": vendor_id,
            "outstanding": str(
                payables.outstanding_payable(current_session(), org_id=org_id, vendor_id=vendor_id)
            ),
        }
    )


# ---------------------------------------------------------- reconciliation
#
# The workspace behind a monthly tie-out. Two things here are the point:
#
# Auto-matching only takes what is both confident *and* unambiguous. A second
# candidate scoring near the first is exactly the case a person has to look at,
# and a machine that resolves it silently is a machine that hides the one
# transaction worth finding.
#
# Sign-off refuses anything that does not actually agree — a non-zero
# difference, an unresolved exception, or a transaction that is neither matched
# nor deliberately ignored. A reconciliation that can be signed while out is not
# a reconciliation.


def _reconciliation_or_404(reconciliation_id: str, org_id: str) -> Reconciliation:
    record = db.session.get(Reconciliation, reconciliation_id)
    if record is None or record.org_id != org_id:
        raise NotFound("That reconciliation was not found.")
    return record


def _transaction_or_404(transaction_id: str, org_id: str) -> BankTransaction:
    record = db.session.get(BankTransaction, transaction_id)
    if record is None or record.org_id != org_id:
        raise NotFound("That bank transaction was not found.")
    return record


@api_v1_bp.get("/reconciliations", endpoint="reconciliations_list")
def list_reconciliations() -> Response:
    require(Perm.RECONCILIATION_READ)
    query = parse_query(ReconciliationListQuery)
    org_id = require_org_scope()

    stmt = select(Reconciliation).where(Reconciliation.org_id == org_id)
    if query.bank_account_id:
        stmt = stmt.where(Reconciliation.bank_account_id == query.bank_account_id)
    if query.status:
        stmt = stmt.where(Reconciliation.status == query.status)

    page = paginate(current_session(), stmt, Reconciliation, limit=query.limit, cursor=query.cursor)
    return respond_collection(page, ReconciliationOut)


@api_v1_bp.get("/reconciliations/<id:reconciliation_id>", endpoint="reconciliations_get")
def get_reconciliation(reconciliation_id: str) -> Response:
    """One reconciliation, its transactions, and what is still unresolved."""
    require(Perm.RECONCILIATION_READ)
    org_id = require_org_scope()

    record = _reconciliation_or_404(reconciliation_id, org_id)
    transactions = list(
        db.session.execute(
            select(BankTransaction)
            .where(
                BankTransaction.org_id == org_id,
                BankTransaction.reconciliation_id == reconciliation_id,
            )
            .order_by(BankTransaction.posted_date)
        ).scalars()
    )
    return respond(
        {
            **ReconciliationOut.model_validate(record, from_attributes=True).model_dump(
                mode="json"
            ),
            "transactions": [
                BankTransactionOut.model_validate(t, from_attributes=True).model_dump(mode="json")
                for t in transactions
            ],
            "exceptions": [
                ReconciliationExceptionOut.model_validate(e, from_attributes=True).model_dump(
                    mode="json"
                )
                for e in record.exceptions
            ],
        }
    )


@api_v1_bp.post("/reconciliations", endpoint="reconciliations_open")
def open_reconciliation() -> Response:
    """Start a reconciliation over a statement window."""
    require(Perm.RECONCILIATION_MANAGE)
    payload = parse_body(ReconciliationOpen)
    org_id = require_org_scope()

    with transaction() as session:
        record = reconciliation.open_reconciliation(
            session,
            org_id=org_id,
            bank_account_id=payload.bank_account_id,
            statement_start=payload.statement_start,
            statement_end=payload.statement_end,
            opening_balance=payload.opening_balance,
            closing_balance=payload.closing_balance,
            actor_id=current_user.id,
        )

    return respond_created(
        ReconciliationOut.model_validate(record, from_attributes=True),
        location=f"/api/v1/reconciliations/{record.id}",
    )


@api_v1_bp.post("/bank-statements", endpoint="bank_statements_import")
def import_statement() -> Response:
    """Load a bank CSV export.

    Re-importing the same file inserts nothing: each line carries a stable
    fingerprint that includes an occurrence index, so two genuinely identical
    transactions on one day both survive while a repeat import of either does
    not.
    """
    require(Perm.RECONCILIATION_MANAGE)
    payload = parse_body(StatementImport)
    org_id = require_org_scope()

    with transaction() as session:
        lines = reconciliation.parse_statement_csv(
            payload.csv,
            date_column=payload.date_column,
            amount_column=payload.amount_column,
            description_column=payload.description_column,
            reference_column=payload.reference_column,
            external_id_column=payload.external_id_column,
            date_format=payload.date_format,
        )
        result = reconciliation.import_statement(
            session,
            org_id=org_id,
            bank_account_id=payload.bank_account_id,
            lines=lines,
            actor_id=current_user.id,
        )
        body = {
            "imported": result.count,
            "duplicates": result.duplicates,
            "rejected": result.rejected,
            "transactions": [
                BankTransactionOut.model_validate(t, from_attributes=True).model_dump(mode="json")
                for t in result.imported
            ],
        }

    return respond(body, status=201)


@api_v1_bp.get(
    "/bank-transactions/<id:transaction_id>/matches", endpoint="bank_transactions_suggest"
)
def suggest_matches(transaction_id: str) -> Response:
    """Ranked ledger lines that might be this bank line, with their reasons.

    The reasons are returned because a suggestion nobody can interrogate is a
    suggestion nobody should accept.
    """
    require(Perm.RECONCILIATION_READ)
    org_id = require_org_scope()

    record = _transaction_or_404(transaction_id, org_id)
    candidates = reconciliation.suggest_matches(current_session(), transaction=record)
    return respond(
        {
            "data": [
                {
                    "journal_line_id": candidate.journal_line.id,
                    "journal_entry_id": candidate.journal_line.journal_entry_id,
                    "confidence": candidate.confidence,
                    "amount": str(candidate.journal_line.debit or candidate.journal_line.credit),
                    "memo": candidate.journal_line.memo,
                    "reasons": list(candidate.reasons),
                }
                for candidate in candidates
            ]
        }
    )


@api_v1_bp.post("/bank-transactions/<id:transaction_id>/match", endpoint="bank_transactions_match")
def match_transaction(transaction_id: str) -> Response:
    """Bind a bank line to a ledger line. One ledger line settles once."""
    require(Perm.RECONCILIATION_MANAGE)
    payload = parse_body(TransactionMatch)
    org_id = require_org_scope()

    with transaction() as session:
        record = _transaction_or_404(transaction_id, org_id)
        line = session.get(JournalLine, payload.journal_line_id)
        if line is None or line.org_id != org_id:
            raise NotFound("That ledger line was not found.")
        matched = reconciliation.match_transaction(
            session,
            transaction=record,
            journal_line=line,
            confidence=payload.confidence,
            actor_id=current_user.id,
        )

    return respond(BankTransactionOut.model_validate(matched, from_attributes=True))


@api_v1_bp.post(
    "/bank-transactions/<id:transaction_id>/unmatch", endpoint="bank_transactions_unmatch"
)
def unmatch_transaction(transaction_id: str) -> Response:
    require(Perm.RECONCILIATION_MANAGE)
    org_id = require_org_scope()

    with transaction() as session:
        record = reconciliation.unmatch_transaction(
            session, transaction=_transaction_or_404(transaction_id, org_id)
        )

    return respond(BankTransactionOut.model_validate(record, from_attributes=True))


@api_v1_bp.post(
    "/reconciliations/<id:reconciliation_id>/auto-match", endpoint="reconciliations_auto_match"
)
def auto_match(reconciliation_id: str) -> Response:
    """Match only what is both confident and unambiguous.

    A near-tie is left alone deliberately: that is exactly the transaction a
    person needs to look at, and resolving it silently hides the one thing
    worth finding.
    """
    require(Perm.RECONCILIATION_MANAGE)
    payload = parse_body(AutoMatchRequest)
    org_id = require_org_scope()

    with transaction() as session:
        record = _reconciliation_or_404(reconciliation_id, org_id)
        matched = reconciliation.auto_match(
            session,
            org_id=org_id,
            bank_account_id=record.bank_account_id,
            threshold=payload.threshold,
            actor_id=current_user.id,
        )
        matched_ids = [t.id for t in matched]

    return respond({"matched": len(matched_ids), "transaction_ids": matched_ids})


@api_v1_bp.post(
    "/reconciliations/<id:reconciliation_id>/exceptions", endpoint="reconciliation_exceptions_raise"
)
def raise_exception(reconciliation_id: str) -> Response:
    """Record something that does not agree and needs a person."""
    require(Perm.RECONCILIATION_MANAGE)
    payload = parse_body(ExceptionRaise)
    org_id = require_org_scope()

    with transaction() as session:
        record = _reconciliation_or_404(reconciliation_id, org_id)
        raised = reconciliation.raise_exception(
            session,
            reconciliation=record,
            kind=payload.kind,
            description=payload.description,
            amount=payload.amount,
            bank_transaction_id=payload.bank_transaction_id,
        )

    return respond_created(
        ReconciliationExceptionOut.model_validate(raised, from_attributes=True),
        location=f"/api/v1/reconciliations/{reconciliation_id}",
    )


@api_v1_bp.post(
    "/reconciliation-exceptions/<id:exception_id>/resolve",
    endpoint="reconciliation_exceptions_resolve",
)
def resolve_exception(exception_id: str) -> Response:
    """Close an exception, with the note that makes it auditable."""
    require(Perm.RECONCILIATION_MANAGE)
    payload = parse_body(ExceptionResolve)
    org_id = require_org_scope()

    with transaction() as session:
        record = session.get(ReconciliationException, exception_id)
        if record is None or record.org_id != org_id:
            raise NotFound("That exception was not found.")
        resolved = reconciliation.resolve_exception(
            session, exception=record, resolved_by_id=current_user.id, note=payload.note
        )

    return respond(ReconciliationExceptionOut.model_validate(resolved, from_attributes=True))


@api_v1_bp.post(
    "/reconciliations/<id:reconciliation_id>/complete", endpoint="reconciliations_complete"
)
def complete_reconciliation(reconciliation_id: str) -> Response:
    """Sign off. Refuses anything that does not actually agree."""
    require(Perm.RECONCILIATION_MANAGE)
    payload = parse_body(ReconciliationComplete)
    org_id = require_org_scope()

    with transaction() as session:
        record = reconciliation.complete_reconciliation(
            session,
            reconciliation=_reconciliation_or_404(reconciliation_id, org_id),
            completed_by_id=current_user.id,
            notes=payload.notes,
        )

    return respond(ReconciliationOut.model_validate(record, from_attributes=True))

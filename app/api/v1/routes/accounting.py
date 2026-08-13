"""Ledger, invoices, and payments.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from flask import Response
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
    DepositMovement,
    Invoice,
    InvoiceStatus,
    JournalEntry,
    Payment,
)
from app.models.leasing import Lease
from app.models.types import utcnow
from app.schemas.operations import (
    DepositBalanceOut,
    DepositCollect,
    DepositMovementListQuery,
    DepositMovementOut,
    DepositRelease,
    InvoiceListQuery,
    InvoiceOut,
    JournalEntryCreate,
    JournalEntryOut,
    PaymentCreate,
    PaymentOut,
)
from app.security.permissions import Perm
from app.security.policies import filter_permitted, require
from app.services.accounting import deposits, ledger, receivables
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

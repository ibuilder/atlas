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
from app.extensions import db
from app.middleware import require_org_scope
from app.models.accounting import Invoice, InvoiceStatus, JournalEntry, Payment
from app.models.types import utcnow
from app.schemas.operations import (
    InvoiceListQuery,
    InvoiceOut,
    JournalEntryCreate,
    JournalEntryOut,
    PaymentCreate,
    PaymentOut,
)
from app.security.permissions import Perm
from app.security.policies import filter_permitted, require
from app.services.accounting import ledger, receivables
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

    page = paginate(db.session, stmt, Invoice, limit=query.limit, cursor=query.cursor)
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

    page = paginate(db.session, stmt, Payment, limit=query.limit, cursor=query.cursor)
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

    page = paginate(db.session, stmt, JournalEntry, limit=query.limit, cursor=query.cursor)
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

    rows = ledger.trial_balance(db.session, org_id=org_id)
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

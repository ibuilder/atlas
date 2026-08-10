"""Allocation of human-facing document numbers.

Every number comes from a locked counter row. The lock is held for the rest of
the transaction, which means a rolled-back transaction also rolls back the
allocation - so the sequence stays gap-free, which is what an auditor expects
from an invoice series.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.sequences import NumberSequence, SequenceKey
from app.services.common.unit_of_work import lock_row

__all__ = ["DEFAULT_PREFIXES", "next_number", "peek_number"]

#: Prefixes chosen so a number is self-describing when a resident reads it out
#: over the phone.
DEFAULT_PREFIXES: dict[str, str] = {
    SequenceKey.JOURNAL_ENTRY: "JE-",
    SequenceKey.INVOICE: "INV-",
    SequenceKey.BILL: "BILL-",
    SequenceKey.PAYMENT: "PMT-",
    SequenceKey.WORK_ORDER: "WO-",
    SequenceKey.MAINTENANCE_REQUEST: "REQ-",
    SequenceKey.LEASE: "LSE-",
    SequenceKey.APPLICATION: "APP-",
    SequenceKey.INSPECTION: "INS-",
    SequenceKey.OWNER_STATEMENT: "STMT-",
    SequenceKey.DISTRIBUTION: "DIST-",
    SequenceKey.PURCHASE_ORDER: "PO-",
}


def next_number(
    session: Session,
    key: str,
    *,
    org_id: str,
    period: str = "",
    prefix: str | None = None,
    padding: int = 6,
) -> str:
    """Allocate and format the next number in a series.

    The counter row is created on first use. ``period`` partitions a series that
    restarts - pass a fiscal year for counters that should reset annually.
    """
    counter = lock_row(
        session,
        NumberSequence,
        NumberSequence.org_id == org_id,
        NumberSequence.key == key,
        NumberSequence.period == period,
    )

    if counter is None:
        counter = NumberSequence(
            org_id=org_id,
            key=key,
            period=period,
            prefix=prefix if prefix is not None else DEFAULT_PREFIXES.get(key, ""),
            padding=padding,
            next_value=1,
        )
        session.add(counter)
        # Materialise immediately so a concurrent transaction hits the unique
        # constraint instead of creating a second counter for the same series.
        session.flush()

    value = counter.next_value
    counter.next_value = value + 1
    session.flush()
    return counter.format(value)


def peek_number(session: Session, key: str, *, org_id: str, period: str = "") -> str | None:
    """The number that *would* be allocated next, without consuming it.

    For previews only. Two callers can see the same value, so nothing may be
    persisted based on it.
    """
    counter = (
        session.query(NumberSequence)
        .filter(
            NumberSequence.org_id == org_id,
            NumberSequence.key == key,
            NumberSequence.period == period,
        )
        .one_or_none()
    )
    return counter.format(counter.next_value) if counter else None

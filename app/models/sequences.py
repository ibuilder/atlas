"""Per-organization, gap-free document numbering.

Invoice numbers, journal entry numbers, and work-order numbers are referenced by
residents, owners, auditors, and vendors. They have to be stable, human-readable,
and allocated without collisions under concurrency.

A dedicated counter row locked with ``SELECT ... FOR UPDATE`` is the boring,
correct answer. ``MAX(number) + 1`` races; a database sequence cannot be
partitioned per tenant without creating one sequence per tenant per document
type, and leaves visible gaps that make an auditor ask questions.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TenantModel

__all__ = ["NumberSequence", "SequenceKey"]


class SequenceKey:
    """Well-known counter names."""

    JOURNAL_ENTRY = "journal_entry"
    INVOICE = "invoice"
    BILL = "bill"
    PAYMENT = "payment"
    WORK_ORDER = "work_order"
    MAINTENANCE_REQUEST = "maintenance_request"
    LEASE = "lease"
    APPLICATION = "application"
    INSPECTION = "inspection"
    OWNER_STATEMENT = "owner_statement"
    DISTRIBUTION = "distribution"
    PURCHASE_ORDER = "purchase_order"


class NumberSequence(TenantModel):
    """A monotonic counter scoped to one organization and one document type."""

    __tablename__ = "number_sequences"
    __table_args__ = (
        UniqueConstraint("org_id", "key", "period", name="uq_number_sequences_org_key_period"),
        Index("ix_number_sequences_org_created", "org_id", "created_at"),
    )

    key: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    #: Optional partition, e.g. ``"2026"`` for counters that restart annually.
    period: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    prefix: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    next_value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    padding: Mapped[int] = mapped_column(Integer, nullable=False, default=6)

    def format(self, value: int) -> str:
        return f"{self.prefix}{value:0{self.padding}d}"

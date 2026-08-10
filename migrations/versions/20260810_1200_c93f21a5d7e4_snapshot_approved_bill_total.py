"""Snapshot the total a bill was approved at.

Approving "this bill" is not the same as approving whatever this bill later
becomes. Payment compares the current total against the approved one and
refuses if they differ, so a bill edited from $4,200 to $42,000 after approval
cannot be paid on the strength of the old decision.

Backfill note: existing approved bills get their current total, which is the
only defensible reading - we cannot know what they looked like at approval, and
leaving the column NULL disables the check for them, which is worse.

Revision ID: c93f21a5d7e4
Revises: b1c4e77a91d2
Create Date: 2026-08-10

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models.types import Money

revision = "c93f21a5d7e4"
down_revision = "b1c4e77a91d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bills", sa.Column("approved_total", Money(), nullable=True))
    op.execute(
        "UPDATE bills SET approved_total = total "
        "WHERE approved_at IS NOT NULL AND approved_total IS NULL"
    )


def downgrade() -> None:
    op.drop_column("bills", "approved_total")

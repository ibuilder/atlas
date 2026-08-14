"""a bearer credential for the directory to present

An identity provider calling SCIM authenticates as itself, not as a person, so
it needs a credential of its own. The token is stored hashed for the same
reason a password is: a leaked database should not hand somebody the ability to
deactivate every account in the tenant.

Revision ID: a4c1e2d9b7f3
Revises: 9c56e71f2f93
Created: 2026-08-14 15:40:00.000000+00:00

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.models.types

revision: str = "a4c1e2d9b7f3"
down_revision: str | None = "9c56e71f2f93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("identity_providers", schema=None) as batch_op:
        batch_op.add_column(sa.Column("scim_token_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("scim_token_fingerprint", sa.String(length=16), nullable=True)
        )
        batch_op.add_column(
            sa.Column("scim_token_issued_at", app.models.types.UTCDateTime(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("scim_last_seen_at", app.models.types.UTCDateTime(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("identity_providers", schema=None) as batch_op:
        batch_op.drop_column("scim_last_seen_at")
        batch_op.drop_column("scim_token_issued_at")
        batch_op.drop_column("scim_token_fingerprint")
        batch_op.drop_column("scim_token_hash")

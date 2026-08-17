"""embeddable enquiry forms

A key an operator pastes into their own marketing site. The public key is
unique globally rather than per organization, because a public request presents
it before any organization is known - it has to resolve to exactly one tenant
on its own.

``apply_tenant_policies`` runs at the end for the usual reason: a new tenant
table without the row-level policy is protected by the ORM guard and by nothing
else, and this particular table is reachable from an unauthenticated request.

Revision ID: e3b9f04c7a18
Revises: a4c1e2d9b7f3
Created: 2026-08-16 20:15:00.000000+00:00

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from migrations.support.rls import apply_tenant_policies
import app.models.types

revision: str = "e3b9f04c7a18"
down_revision: str | None = "a4c1e2d9b7f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "embed_forms",
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("public_key", sa.String(length=64), nullable=False),
        sa.Column("property_id", app.models.types.GUID(), nullable=True),
        sa.Column("allowed_origins", app.models.types.JSONType(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revoked_at", app.models.types.UTCDateTime(), nullable=True),
        sa.Column("submission_count", sa.Integer(), nullable=False),
        sa.Column("last_submission_at", app.models.types.UTCDateTime(), nullable=True),
        sa.Column("org_id", app.models.types.GUID(), nullable=False),
        sa.Column("id", app.models.types.GUID(), nullable=False),
        sa.Column("created_at", app.models.types.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.models.types.UTCDateTime(), nullable=False),
        sa.Column("created_by_id", app.models.types.GUID(), nullable=True),
        sa.Column("updated_by_id", app.models.types.GUID(), nullable=True),
        sa.Column("deleted_at", app.models.types.UTCDateTime(), nullable=True),
        sa.Column("deleted_by_id", app.models.types.GUID(), nullable=True),
        sa.Column("delete_reason", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_embed_forms_org_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["properties.id"],
            name=op.f("fk_embed_forms_property_id_properties"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embed_forms")),
        # Global, not scoped to org_id. The uniqueness is what lets an
        # unauthenticated request resolve to one tenant and no other.
        sa.UniqueConstraint("public_key", name="uq_embed_forms_public_key"),
    )
    with op.batch_alter_table("embed_forms", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_embed_forms_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_embed_forms_deleted_at"), ["deleted_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_embed_forms_enabled"), ["enabled"], unique=False)
        batch_op.create_index("ix_embed_forms_org_enabled", ["org_id", "enabled"], unique=False)
        batch_op.create_index(batch_op.f("ix_embed_forms_org_id"), ["org_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_embed_forms_property_id"), ["property_id"], unique=False
        )
        # The lookup every public request makes, once, before anything else.
        batch_op.create_index(
            batch_op.f("ix_embed_forms_public_key"), ["public_key"], unique=False
        )

    apply_tenant_policies()


def downgrade() -> None:
    with op.batch_alter_table("embed_forms", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_embed_forms_public_key"))
        batch_op.drop_index(batch_op.f("ix_embed_forms_property_id"))
        batch_op.drop_index(batch_op.f("ix_embed_forms_org_id"))
        batch_op.drop_index("ix_embed_forms_org_enabled")
        batch_op.drop_index(batch_op.f("ix_embed_forms_enabled"))
        batch_op.drop_index(batch_op.f("ix_embed_forms_deleted_at"))
        batch_op.drop_index(batch_op.f("ix_embed_forms_created_at"))

    op.drop_table("embed_forms")

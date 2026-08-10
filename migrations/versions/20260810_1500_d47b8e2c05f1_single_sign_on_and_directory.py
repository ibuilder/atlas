"""Single sign-on and directory provisioning.

Adds the identity-provider configuration, the single-use OIDC state row, and
the SAML assertion replay guard - plus the three columns on ``users`` that
record which directory, if any, owns an account.

``users.is_directory_managed`` defaults to false, which is correct for every
existing account: they were created here, so this remains their source of truth
until a directory explicitly claims them.

Revision ID: d47b8e2c05f1
Revises: c93f21a5d7e4
Create Date: 2026-08-10

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.models.types import GUID, EncryptedText, JSONType, UTCDateTime
from migrations.support.rls import apply_tenant_policies

revision = "d47b8e2c05f1"
down_revision = "c93f21a5d7e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "identity_providers",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("org_id", GUID(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("created_by_id", GUID(), nullable=True),
        sa.Column("updated_by_id", GUID(), nullable=True),
        sa.Column("deleted_at", UTCDateTime(), nullable=True),
        sa.Column("deleted_by_id", GUID(), nullable=True),
        sa.Column("code", sa.String(60), nullable=False),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("protocol", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("issuer", sa.String(255), nullable=True),
        sa.Column("client_id", sa.String(255), nullable=True),
        sa.Column("client_secret", EncryptedText(), nullable=True),
        sa.Column("discovery_url", sa.String(500), nullable=True),
        sa.Column("authorization_endpoint", sa.String(500), nullable=True),
        sa.Column("token_endpoint", sa.String(500), nullable=True),
        sa.Column("jwks_uri", sa.String(500), nullable=True),
        sa.Column("userinfo_endpoint", sa.String(500), nullable=True),
        sa.Column("scopes", JSONType(), nullable=False),
        sa.Column("jwks_cache", JSONType(), nullable=False),
        sa.Column("jwks_fetched_at", UTCDateTime(), nullable=True),
        sa.Column("entity_id", sa.String(255), nullable=True),
        sa.Column("sso_url", sa.String(500), nullable=True),
        sa.Column("slo_url", sa.String(500), nullable=True),
        sa.Column("signing_certificate", sa.Text(), nullable=True),
        sa.Column("email_claim", sa.String(80), nullable=False),
        sa.Column("name_claim", sa.String(80), nullable=False),
        sa.Column("groups_claim", sa.String(80), nullable=True),
        sa.Column("attribute_map", JSONType(), nullable=False),
        sa.Column("group_role_map", JSONType(), nullable=False),
        sa.Column("allowed_email_domains", JSONType(), nullable=False),
        sa.Column("jit_provisioning", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("default_role_code", sa.String(60), nullable=True),
        sa.Column(
            "require_signed_assertions", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
        sa.Column("scim_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_login_at", UTCDateTime(), nullable=True),
        sa.Column("login_count", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "code", name="uq_identity_providers_org_code"),
    )
    op.create_index(
        "ix_identity_providers_org_active", "identity_providers", ["org_id", "is_active"]
    )
    op.create_index(
        "ix_identity_providers_org_created", "identity_providers", ["org_id", "created_at"]
    )

    op.create_table(
        "sso_login_states",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("org_id", GUID(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("created_by_id", GUID(), nullable=True),
        sa.Column("updated_by_id", GUID(), nullable=True),
        sa.Column("provider_id", GUID(), nullable=False),
        sa.Column("state", sa.String(128), nullable=False),
        sa.Column("nonce", sa.String(128), nullable=True),
        sa.Column("code_verifier", sa.String(128), nullable=True),
        sa.Column("redirect_to", sa.String(500), nullable=True),
        sa.Column("relay_state", sa.String(255), nullable=True),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("consumed_at", UTCDateTime(), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_id"], ["identity_providers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("state", name="uq_sso_login_states_state"),
    )
    op.create_index("ix_sso_login_states_provider_id", "sso_login_states", ["provider_id"])
    op.create_index("ix_sso_login_states_expiry", "sso_login_states", ["expires_at"])
    op.create_index("ix_sso_login_states_org_created", "sso_login_states", ["org_id", "created_at"])

    op.create_table(
        "sso_replay_guards",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("org_id", GUID(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.Column("updated_at", UTCDateTime(), nullable=False),
        sa.Column("created_by_id", GUID(), nullable=True),
        sa.Column("updated_by_id", GUID(), nullable=True),
        sa.Column("provider_id", GUID(), nullable=True),
        sa.Column("assertion_id", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(255), nullable=True),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_id"], ["identity_providers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "assertion_id", name="uq_sso_replay_guards_assertion"),
    )
    op.create_index("ix_sso_replay_guards_provider_id", "sso_replay_guards", ["provider_id"])
    op.create_index("ix_sso_replay_guards_expiry", "sso_replay_guards", ["expires_at"])
    op.create_index(
        "ix_sso_replay_guards_org_created", "sso_replay_guards", ["org_id", "created_at"]
    )

    op.add_column("users", sa.Column("external_id", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("identity_provider_id", GUID(), nullable=True))
    op.add_column(
        "users",
        sa.Column("is_directory_managed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_users_external_id", "users", ["external_id"])
    op.create_index("ix_users_identity_provider_id", "users", ["identity_provider_id"])

    # Three new tenant tables. The original RLS migration only saw the tables
    # that existed when it ran, so without this they would be outside the
    # isolation boundary while looking entirely correct.
    apply_tenant_policies()


def downgrade() -> None:
    op.drop_index("ix_users_identity_provider_id", table_name="users")
    op.drop_index("ix_users_external_id", table_name="users")
    op.drop_column("users", "is_directory_managed")
    op.drop_column("users", "identity_provider_id")
    op.drop_column("users", "external_id")
    op.drop_table("sso_replay_guards")
    op.drop_table("sso_login_states")
    op.drop_table("identity_providers")

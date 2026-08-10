"""Single sign-on and directory provisioning.

Adds the identity-provider configuration, the single-use OIDC state row, and
the SAML assertion replay guard - plus the three columns on ``users`` that
record which directory, if any, owns an account.

``users.is_directory_managed`` defaults to false, which is correct for every
existing account: they were created here, so this remains their source of truth
until a directory explicitly claims them.

Revision ID: 9ef691b1a0aa
Revises: c93f21a5d7e4
Created: 2026-08-10 21:09:09.446756+00:00

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from collections.abc import Sequence

import app.models.types
import sqlalchemy as sa
from alembic import op

from migrations.support.rls import apply_tenant_policies

revision: str = "9ef691b1a0aa"
down_revision: str | None = "c93f21a5d7e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "identity_providers",
        sa.Column("code", sa.String(length=60), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column(
            "protocol",
            sa.Enum("oidc", "saml", name="ssoprotocol_enum", native_enum=False, length=64),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("issuer", sa.String(length=255), nullable=True),
        sa.Column("client_id", sa.String(length=255), nullable=True),
        sa.Column("client_secret", app.models.types.EncryptedText(), nullable=True),
        sa.Column("discovery_url", sa.String(length=500), nullable=True),
        sa.Column("authorization_endpoint", sa.String(length=500), nullable=True),
        sa.Column("token_endpoint", sa.String(length=500), nullable=True),
        sa.Column("jwks_uri", sa.String(length=500), nullable=True),
        sa.Column("userinfo_endpoint", sa.String(length=500), nullable=True),
        sa.Column("scopes", app.models.types.JSONType(), nullable=False),
        sa.Column("jwks_cache", app.models.types.JSONType(), nullable=False),
        sa.Column("jwks_fetched_at", app.models.types.UTCDateTime(), nullable=True),
        sa.Column("entity_id", sa.String(length=255), nullable=True),
        sa.Column("sso_url", sa.String(length=500), nullable=True),
        sa.Column("slo_url", sa.String(length=500), nullable=True),
        sa.Column("signing_certificate", sa.Text(), nullable=True),
        sa.Column("email_claim", sa.String(length=80), nullable=False),
        sa.Column("name_claim", sa.String(length=80), nullable=False),
        sa.Column("groups_claim", sa.String(length=80), nullable=True),
        sa.Column("attribute_map", app.models.types.JSONType(), nullable=False),
        sa.Column("group_role_map", app.models.types.JSONType(), nullable=False),
        sa.Column("allowed_email_domains", app.models.types.JSONType(), nullable=False),
        sa.Column("jit_provisioning", sa.Boolean(), nullable=False),
        sa.Column("default_role_code", sa.String(length=60), nullable=True),
        sa.Column("require_signed_assertions", sa.Boolean(), nullable=False),
        sa.Column("scim_enabled", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", app.models.types.UTCDateTime(), nullable=True),
        sa.Column("login_count", sa.Integer(), nullable=False),
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
            name=op.f("fk_identity_providers_org_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_identity_providers")),
        sa.UniqueConstraint("org_id", "code", name="uq_identity_providers_org_code"),
    )
    with op.batch_alter_table("identity_providers", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_identity_providers_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_identity_providers_deleted_at"), ["deleted_at"], unique=False
        )
        batch_op.create_index(
            "ix_identity_providers_org_active", ["org_id", "is_active"], unique=False
        )
        batch_op.create_index(
            "ix_identity_providers_org_created", ["org_id", "created_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_identity_providers_org_id"), ["org_id"], unique=False)

    op.create_table(
        "sso_login_states",
        sa.Column("provider_id", app.models.types.GUID(), nullable=False),
        sa.Column("state", sa.String(length=128), nullable=False),
        sa.Column("nonce", sa.String(length=128), nullable=True),
        sa.Column("code_verifier", sa.String(length=128), nullable=True),
        sa.Column("redirect_to", sa.String(length=500), nullable=True),
        sa.Column("relay_state", sa.String(length=255), nullable=True),
        sa.Column("expires_at", app.models.types.UTCDateTime(), nullable=False),
        sa.Column("consumed_at", app.models.types.UTCDateTime(), nullable=True),
        sa.Column("org_id", app.models.types.GUID(), nullable=False),
        sa.Column("id", app.models.types.GUID(), nullable=False),
        sa.Column("created_at", app.models.types.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.models.types.UTCDateTime(), nullable=False),
        sa.Column("created_by_id", app.models.types.GUID(), nullable=True),
        sa.Column("updated_by_id", app.models.types.GUID(), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_sso_login_states_org_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["identity_providers.id"],
            name=op.f("fk_sso_login_states_provider_id_identity_providers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sso_login_states")),
        sa.UniqueConstraint("state", name="uq_sso_login_states_state"),
    )
    with op.batch_alter_table("sso_login_states", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_sso_login_states_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index("ix_sso_login_states_expiry", ["expires_at"], unique=False)
        batch_op.create_index(
            "ix_sso_login_states_org_created", ["org_id", "created_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_sso_login_states_org_id"), ["org_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_sso_login_states_provider_id"), ["provider_id"], unique=False
        )

    op.create_table(
        "sso_replay_guards",
        sa.Column("provider_id", app.models.types.GUID(), nullable=True),
        sa.Column("assertion_id", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=True),
        sa.Column("expires_at", app.models.types.UTCDateTime(), nullable=False),
        sa.Column("org_id", app.models.types.GUID(), nullable=False),
        sa.Column("id", app.models.types.GUID(), nullable=False),
        sa.Column("created_at", app.models.types.UTCDateTime(), nullable=False),
        sa.Column("updated_at", app.models.types.UTCDateTime(), nullable=False),
        sa.Column("created_by_id", app.models.types.GUID(), nullable=True),
        sa.Column("updated_by_id", app.models.types.GUID(), nullable=True),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name=op.f("fk_sso_replay_guards_org_id_organizations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["identity_providers.id"],
            name=op.f("fk_sso_replay_guards_provider_id_identity_providers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sso_replay_guards")),
        sa.UniqueConstraint("org_id", "assertion_id", name="uq_sso_replay_guards_assertion"),
    )
    with op.batch_alter_table("sso_replay_guards", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_sso_replay_guards_created_at"), ["created_at"], unique=False
        )
        batch_op.create_index("ix_sso_replay_guards_expiry", ["expires_at"], unique=False)
        batch_op.create_index(
            "ix_sso_replay_guards_org_created", ["org_id", "created_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_sso_replay_guards_org_id"), ["org_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_sso_replay_guards_provider_id"), ["provider_id"], unique=False
        )

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("external_id", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column("identity_provider_id", app.models.types.GUID(), nullable=True)
        )
        batch_op.add_column(sa.Column("is_directory_managed", sa.Boolean(), nullable=False))
        batch_op.create_index(batch_op.f("ix_users_external_id"), ["external_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_users_identity_provider_id"), ["identity_provider_id"], unique=False
        )

    # Three new tenant tables. The original RLS migration only walked the
    # tables that existed when it ran, so without this they would sit outside
    # the isolation boundary while looking entirely correct.
    apply_tenant_policies()


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_identity_provider_id"))
        batch_op.drop_index(batch_op.f("ix_users_external_id"))
        batch_op.drop_column("is_directory_managed")
        batch_op.drop_column("identity_provider_id")
        batch_op.drop_column("external_id")

    with op.batch_alter_table("sso_replay_guards", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sso_replay_guards_provider_id"))
        batch_op.drop_index(batch_op.f("ix_sso_replay_guards_org_id"))
        batch_op.drop_index("ix_sso_replay_guards_org_created")
        batch_op.drop_index("ix_sso_replay_guards_expiry")
        batch_op.drop_index(batch_op.f("ix_sso_replay_guards_created_at"))

    op.drop_table("sso_replay_guards")
    with op.batch_alter_table("sso_login_states", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_sso_login_states_provider_id"))
        batch_op.drop_index(batch_op.f("ix_sso_login_states_org_id"))
        batch_op.drop_index("ix_sso_login_states_org_created")
        batch_op.drop_index("ix_sso_login_states_expiry")
        batch_op.drop_index(batch_op.f("ix_sso_login_states_created_at"))

    op.drop_table("sso_login_states")
    with op.batch_alter_table("identity_providers", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_identity_providers_org_id"))
        batch_op.drop_index("ix_identity_providers_org_created")
        batch_op.drop_index("ix_identity_providers_org_active")
        batch_op.drop_index(batch_op.f("ix_identity_providers_deleted_at"))
        batch_op.drop_index(batch_op.f("ix_identity_providers_created_at"))

    op.drop_table("identity_providers")

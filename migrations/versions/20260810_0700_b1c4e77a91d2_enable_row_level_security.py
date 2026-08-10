"""Enable row-level security on every tenant-scoped table

Revision ID: b1c4e77a91d2
Revises: 6a76de779278
Created: 2026-08-10

The third isolation layer. Layers one and two are application code and share a
failure mode: a query path that forgets. This moves the rule into the database,
where a raw ``text()`` statement or an ORM bug cannot route around it.

Applied by iterating the catalogue rather than a frozen table list, so the whole
schema is covered exactly as it stands at this revision. Tables added *later*
must enrol themselves — ``tests/security/test_row_level_security.py`` fails the
build if one does not, because a table silently outside the policy looks
completely correct in review.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b1c4e77a91d2"
down_revision: str | None = "6a76de779278"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tables carrying ``org_id`` that must stay readable without a tenant scope.
#: Mirrors ``app.models.rls.RLS_EXEMPT_TABLES``; duplicated here on purpose, so
#: this migration keeps meaning what it meant even if that constant changes.
EXEMPT = (
    "organizations",
    "permissions",
    "login_attempts",
    "audit_chain_heads",
    "alembic_version",
)

_PREDICATE = (
    "nullif(current_setting(''atlas.bypass_rls'', true), '''') = ''on'' "
    "OR org_id = nullif(current_setting(''atlas.current_org'', true), '''')::uuid"
)

_ENABLE = f"""
DO $$
DECLARE
    target text;
BEGIN
    FOR target IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN information_schema.columns col
          ON col.table_name = c.relname AND col.column_name = 'org_id'
        WHERE n.nspname = current_schema()
          AND c.relkind = 'r'
          AND c.relname NOT IN ({", ".join(f"'{name}'" for name in EXEMPT)})
    LOOP
        EXECUTE 'ALTER TABLE ' || quote_ident(target) || ' ENABLE ROW LEVEL SECURITY';
        -- FORCE matters: without it the policy is skipped for the table owner,
        -- and applications are very often connected as the owner. RLS would be
        -- enabled, look correct, and do nothing.
        EXECUTE 'ALTER TABLE ' || quote_ident(target) || ' FORCE ROW LEVEL SECURITY';
        EXECUTE 'DROP POLICY IF EXISTS atlas_tenant_isolation ON ' || quote_ident(target);
        EXECUTE 'CREATE POLICY atlas_tenant_isolation ON ' || quote_ident(target)
             || ' USING ({_PREDICATE})'
             || ' WITH CHECK ({_PREDICATE})';
    END LOOP;
END
$$;
"""

_DISABLE = f"""
DO $$
DECLARE
    target text;
BEGIN
    FOR target IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN information_schema.columns col
          ON col.table_name = c.relname AND col.column_name = 'org_id'
        WHERE n.nspname = current_schema()
          AND c.relkind = 'r'
          AND c.relname NOT IN ({", ".join(f"'{name}'" for name in EXEMPT)})
    LOOP
        EXECUTE 'DROP POLICY IF EXISTS atlas_tenant_isolation ON ' || quote_ident(target);
        EXECUTE 'ALTER TABLE ' || quote_ident(target) || ' NO FORCE ROW LEVEL SECURITY';
        EXECUTE 'ALTER TABLE ' || quote_ident(target) || ' DISABLE ROW LEVEL SECURITY';
    END LOOP;
END
$$;
"""


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        # SQLite has no row-level security. The application-level layers still
        # apply, which is why the portable test suite remains meaningful.
        return
    op.execute(_ENABLE)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(_DISABLE)

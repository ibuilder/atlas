"""Re-applying tenant row-level security after new tables appear.

The original RLS migration walks every table carrying ``org_id`` at the moment
it runs. Any table created afterwards therefore has no policy - it is enabled
for the tables that existed, and silently absent for the ones that did not.

Every migration that adds a tenant-scoped table must call
:func:`apply_tenant_policies`. The statement is idempotent (``DROP POLICY IF
EXISTS`` then create), so re-running it costs nothing and missing it costs a
table outside the isolation boundary.

``app.models.registry`` fails the test suite when a tenant table has no policy,
which is what turns "somebody forgot" into a red build rather than a breach.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from alembic import op

__all__ = ["EXEMPT", "apply_tenant_policies"]

#: Tables carrying ``org_id`` that must stay readable without a tenant scope.
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


def apply_tenant_policies() -> None:
    """Enable and force the isolation policy on every tenant table.

    A no-op outside PostgreSQL: SQLite has no row-level security, which is one
    of the reasons it is supported for development and testing only.
    """
    if op.get_bind().dialect.name != "postgresql":
        return

    # The only interpolated value is EXEMPT, a module-level tuple of literal
    # table names. Nothing here is caller-supplied, and the identifiers inside
    # the DO block go through quote_ident.
    exempt = ", ".join(f"'{name}'" for name in EXEMPT)
    statement = f"""
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
          AND c.relname NOT IN ({exempt})
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
"""  # noqa: S608 - see the note above; nothing here is caller-supplied
    op.execute(statement)

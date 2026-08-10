"""PostgreSQL row-level security: the third isolation layer.

Layers one and two live in the application — service scoping and the ORM guard.
Both are defeated by the same class of mistake: a query path that forgets. RLS
moves the rule inside the database, where a raw ``text()`` statement, a reporting
query written in a hurry, or an ORM bug cannot route around it.

**How the tenant is communicated.** Each transaction sets a session variable:

.. code-block:: sql

    SET LOCAL atlas.current_org = '019f...';

``SET LOCAL`` scopes it to the transaction, so a pooled connection cannot leak
one request's tenant into the next — the single most dangerous failure mode for
connection-level state.

**What RLS does and does not protect against.** It contains *our* bugs. It does
not contain an attacker who already has database credentials and can set the
variable themselves — that is what the audit chain is for. Stating the threat
model matters: RLS described as protection against a hostile DBA is
security theatre.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.context import current_org_id
from app.logging import get_logger

__all__ = [
    "BYPASS_SETTING",
    "ORG_SETTING",
    "RLS_EXEMPT_TABLES",
    "install_rls_session_binding",
    "policy_sql_for",
]

log = get_logger("models.rls")

#: Session variable carrying the active organization.
ORG_SETTING = "atlas.current_org"
#: Session variable that suspends the policy for legitimately global work.
BYPASS_SETTING = "atlas.bypass_rls"

#: Tables that carry ``org_id`` but must stay readable without a tenant scope.
#: Kept in step with ``app.models.registry.TENANT_COLUMN_EXEMPTIONS``.
RLS_EXEMPT_TABLES: frozenset[str] = frozenset(
    {
        "organizations",  # the tenant itself, not tenant data
        "permissions",  # global catalogue
        "login_attempts",  # written before a tenant is known
        "audit_chain_heads",  # chain metadata, guarded by the audit service
        "alembic_version",
    }
)

_installed = False


def install_rls_session_binding() -> None:
    """Emit ``SET LOCAL`` at the start of every PostgreSQL transaction.

    Idempotent. A no-op on any other dialect, so the same code runs against the
    SQLite test database without special-casing at every call site.
    """
    global _installed
    if _installed:
        return

    @event.listens_for(Session, "after_begin")
    def _bind_tenant(session: Session, transaction: Any, connection: Any) -> None:
        if connection.dialect.name != "postgresql":
            return

        from app.models.base import is_unscoped

        org_id = current_org_id()

        # No tenant scope, or an explicit unscoped block: suspend the policy.
        # Layer two already refuses unscoped tenant queries in strict mode, so
        # this does not widen what the application can reach - it keeps
        # provisioning, migrations, and the audit verifier working.
        if org_id is None or is_unscoped():
            connection.exec_driver_sql(f"SET LOCAL {BYPASS_SETTING} = 'on'")
            connection.exec_driver_sql(f"SET LOCAL {ORG_SETTING} = ''")
            return

        connection.exec_driver_sql(f"SET LOCAL {BYPASS_SETTING} = 'off'")
        # Parameterised: the value reaches the database as a bind, never as
        # interpolated SQL.
        connection.execute(
            text(f"SELECT set_config('{ORG_SETTING}', :org_id, true)"), {"org_id": org_id}
        )

    _installed = True


def policy_sql_for(table: str) -> list[str]:
    """The statements that enrol one table in tenant isolation.

    ``FORCE ROW LEVEL SECURITY`` matters: without it the policy is skipped for
    the table's owner, and applications are very often connected as the owner -
    which would leave RLS enabled, looking correct, and doing nothing.
    """
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS atlas_tenant_isolation ON {table}",
        f"""
        CREATE POLICY atlas_tenant_isolation ON {table}
            USING (
                nullif(current_setting('{BYPASS_SETTING}', true), '') = 'on'
                OR org_id = nullif(current_setting('{ORG_SETTING}', true), '')::uuid
            )
            WITH CHECK (
                nullif(current_setting('{BYPASS_SETTING}', true), '') = 'on'
                OR org_id = nullif(current_setting('{ORG_SETTING}', true), '')::uuid
            )
        """,
    ]


def revoke_policy_sql_for(table: str) -> list[str]:
    return [
        f"DROP POLICY IF EXISTS atlas_tenant_isolation ON {table}",
        f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY",
    ]


def tables_missing_policies(connection: Any) -> list[str]:
    """Tenant tables that exist without an isolation policy.

    Drift detection. A table added later without RLS looks perfectly correct in
    review and silently drops out of the third layer, so this is asserted in the
    test suite rather than left to discipline.
    """
    if connection.dialect.name != "postgresql":
        return []

    rows = connection.execute(
        text(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN information_schema.columns col
              ON col.table_name = c.relname AND col.column_name = 'org_id'
            WHERE n.nspname = current_schema()
              AND c.relkind = 'r'
              AND (
                    NOT c.relrowsecurity
                 OR NOT EXISTS (
                        SELECT 1 FROM pg_policy p
                        WHERE p.polrelid = c.oid AND p.polname = 'atlas_tenant_isolation'
                    )
              )
            """
        )
    )
    return sorted({row[0] for row in rows} - RLS_EXEMPT_TABLES)

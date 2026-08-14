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
        _apply(connection, current_org_id(), should_bypass())

    _installed = True


def should_bypass() -> bool:
    """Whether the policy should be suspended for the current unit of work.

    Only two things earn a bypass, and both are deliberate acts:

    * an explicit :func:`~app.models.base.unscoped` block, and
    * a **system context** - provisioning, seeding, migrations, scheduled jobs -
      created through :func:`app.context.system_context`.

    Notably *not* "no organization is bound". An earlier version granted the
    bypass whenever context was merely absent, which meant a raw ``text()``
    query in a job that forgot to bind a tenant read every tenant's rows - the
    exact failure this layer exists to contain. Absence of context is now a
    denial: the policy evaluates against an empty organization and matches
    nothing.
    """
    from app.context import current_context
    from app.models.base import is_unscoped

    if is_unscoped():
        return True

    ctx = current_context()
    if ctx is None:
        return False
    # A system context earns the bypass only while it has no tenant bound. Once
    # a scheduled job binds an organization to work on, it is held to it - the
    # bypass is for the phase that legitimately spans tenants, not for the whole
    # job.
    return ctx.actor_type == "system" and ctx.org_id is None


def _apply(connection: Any, org_id: str | None, bypass: bool) -> None:
    connection.exec_driver_sql(f"SET LOCAL {BYPASS_SETTING} = '{'on' if bypass else 'off'}'")
    # Parameterised: the value reaches the database as a bind, never as
    # interpolated SQL.
    connection.execute(
        text(f"SELECT set_config('{ORG_SETTING}', :org_id, true)"), {"org_id": org_id or ""}
    )


def refresh_session_bindings(session: Session) -> None:
    """Re-issue the settings for a transaction that is already open.

    ``after_begin`` fires once, so entering an ``unscoped`` block part-way
    through a transaction would otherwise change layer two's behaviour and leave
    layer three still enforcing. Called on entry to and exit from those blocks
    so all three layers agree at every moment.
    """
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not session.in_transaction():
        return
    _apply(session.connection(), current_org_id(), should_bypass())


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
        text("""
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN information_schema.columns col
              ON col.table_name = c.relname
             AND col.table_schema = n.nspname
             AND col.column_name = 'org_id'
            WHERE n.nspname = current_schema()
              AND c.relkind = 'r'
              AND (
                    NOT c.relrowsecurity
                 OR NOT EXISTS (
                        SELECT 1 FROM pg_policy p
                        WHERE p.polrelid = c.oid AND p.polname = 'atlas_tenant_isolation'
                    )
              )
            """)
    )
    return sorted({row[0] for row in rows} - RLS_EXEMPT_TABLES)

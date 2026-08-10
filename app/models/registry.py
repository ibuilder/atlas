"""Model registry and structural invariants.

Importing this module imports every mapped class, which is what Alembic
autogeneration and ``db.create_all()`` need in order to see the full schema. A
model that is never imported is a table that silently never gets a migration.

It also holds the structural checks that keep the isolation guarantees honest.
:func:`assert_tenant_coverage` fails the test suite if someone adds a table with
an ``org_id`` column without subclassing :class:`~app.models.base.TenantModel` -
the exact mistake that would leave a table outside automatic query scoping and
outside the row-level-security migration, while looking completely correct in
review.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from sqlalchemy import Table

from app.models import (  # noqa: F401  - imported for side effects
    accounting,
    asset_graph,
    audit,
    automation,
    documents,
    iam,
    integration,
    leasing,
    maintenance,
    org,
    reporting,
    resident,
    sequences,
    vendor,
)
from app.models.base import Base, BaseModel, TenantModel

__all__ = [
    "all_models",
    "assert_tenant_coverage",
    "assert_timestamp_coverage",
    "tenant_tables",
    "validate_schema",
]

#: Tables that legitimately hold an ``org_id`` without being tenant-scoped
#: models, and why.
TENANT_COLUMN_EXEMPTIONS: dict[str, str] = {
    # Written before authentication resolves an organization; keyed by email
    # digest, and deliberately not tenant-filtered so lockout works pre-login.
    "login_attempts": "pre-authentication, no tenant context exists yet",
    # One row per organization by construction; the chain head is metadata about
    # the tenant rather than tenant data.
    "audit_chain_heads": "chain metadata, guarded by the audit service",
}


def all_models() -> list[type[BaseModel]]:
    """Every concrete mapped model."""
    return sorted(
        (mapper.class_ for mapper in Base.registry.mappers),
        key=lambda cls: cls.__name__,
    )


def tenant_tables() -> list[Table]:
    """Tables enrolled in tenant scoping and row-level security."""
    return sorted(
        (
            mapper.local_table
            for mapper in Base.registry.mappers
            if issubclass(mapper.class_, TenantModel) and mapper.local_table is not None
        ),
        key=lambda table: table.name,
    )


def assert_tenant_coverage() -> None:
    """Every table with ``org_id`` must be a :class:`TenantModel`.

    This is the invariant the entire isolation design rests on. Enforced as a
    test rather than a convention because it is invisible in review: a model
    with ``org_id: Mapped[str]`` looks exactly as correct as one that inherits
    it, and behaves completely differently.
    """
    offenders: list[str] = []
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if table is None or "org_id" not in table.columns:
            continue
        if issubclass(mapper.class_, TenantModel):
            continue
        if table.name in TENANT_COLUMN_EXEMPTIONS:
            continue
        offenders.append(f"{mapper.class_.__name__} ({table.name})")

    if offenders:
        raise AssertionError(
            "Tables carry org_id without subclassing TenantModel, so they are outside "
            "automatic query scoping and row-level security: " + ", ".join(sorted(offenders))
        )


def assert_timestamp_coverage() -> None:
    """Every table carries creation and update timestamps plus actor columns."""
    required = {"created_at", "updated_at", "created_by_id", "updated_by_id"}
    offenders: list[str] = []
    for mapper in Base.registry.mappers:
        table = mapper.local_table
        if table is None:
            continue
        missing = required - set(table.columns.keys())
        if missing:
            offenders.append(f"{table.name} missing {sorted(missing)}")
    if offenders:
        raise AssertionError("Tables missing audit columns: " + "; ".join(sorted(offenders)))


def assert_indexed_foreign_keys() -> None:
    """Foreign keys should be indexed.

    An unindexed foreign key makes the child-side lookup a sequential scan and
    makes ``ON DELETE``/``ON UPDATE`` checks on the parent scan the whole child
    table - which is exactly how a delete on a small table locks a large one.
    """
    offenders: list[str] = []
    for table in Base.metadata.sorted_tables:
        indexed: set[str] = set()
        for index in table.indexes:
            columns = list(index.columns)
            if columns:
                indexed.add(columns[0].name)
        for column in table.columns:
            if column.foreign_keys and not (
                column.index or column.primary_key or column.name in indexed
            ):
                offenders.append(f"{table.name}.{column.name}")
    if offenders:
        raise AssertionError("Unindexed foreign keys: " + ", ".join(sorted(offenders)))


def validate_schema() -> None:
    """Run every structural invariant. Called by the test suite and by CI."""
    assert_tenant_coverage()
    assert_timestamp_coverage()
    assert_indexed_foreign_keys()

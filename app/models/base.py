"""Declarative base, shared mixins, and the tenant isolation guard.

Isolation layer 2 of 3 lives here. The full stack:

1. **Services** resolve and pass an explicit organization scope.
2. **This module** injects ``org_id = <current org>`` into every ORM query that
   touches a tenant-scoped entity, and - in strict mode - refuses to run such a
   query when no organization context is bound at all.
3. **PostgreSQL row-level security** policies (see the RLS migration) enforce
   the same rule inside the database, so a raw ``psql`` session or a bug in an
   unmapped query is still contained.

Any one layer can be defeated by a mistake. Getting past all three requires a
deliberate act.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
import re
from contextvars import ContextVar, Token
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import Index, MetaData, String, event, inspect
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    declared_attr,
    mapped_column,
    with_loader_criteria,
)

from app.context import current_actor_id, current_org_id
from app.models.types import (
    GUID,
    JSONType,
    Money,
    UTCDateTime,
    utcnow,
    uuid7_str,
)

__all__ = [
    "Base",
    "BaseModel",
    "SoftDeleteMixin",
    "TenantModel",
    "include_deleted",
    "install_tenancy_guard",
    "is_including_deleted",
    "is_unscoped",
    "set_strict_tenancy",
    "unscoped",
]

#: Deterministic constraint names so Alembic can autogenerate reversible
#: migrations. Without this, dropping an unnamed constraint on PostgreSQL means
#: guessing what the server called it.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: Execution options that opt a query out of automatic scoping.
OPT_UNSCOPED = "atlas_unscoped"
OPT_INCLUDE_DELETED = "atlas_include_deleted"

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


class Base(DeclarativeBase):
    """Declarative base for every Atlas model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map: ClassVar[dict[Any, Any]] = {
        str: String,
        dt.datetime: UTCDateTime,
        Decimal: Money,
        dict[str, Any]: JSONType,
        list[Any]: JSONType,
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging affordance
        identity = getattr(self, "id", None)
        return f"<{type(self).__name__} {identity}>"


def _derive_table_name(class_name: str) -> str:
    """``WorkOrderLine`` -> ``work_order_lines``."""
    snake = _CAMEL_BOUNDARY.sub("_", class_name).lower()
    if snake.endswith(("s", "x", "z", "ch", "sh")):
        return snake + "es"
    if snake.endswith("y") and snake[-2:-1] not in "aeiou":
        return snake[:-1] + "ies"
    return snake + "s"


class BaseModel(Base):
    """Identity, timestamps, and actor attribution for every persisted row.

    ``created_by_id``/``updated_by_id`` are intentionally *not* foreign keys.
    Attribution has to survive a user row being erased in response to a
    data-subject request; a foreign key would force either a cascade that
    destroys history or a null that destroys attribution.
    """

    __abstract__ = True

    @declared_attr.directive
    def __tablename__(cls) -> str:  # noqa: N805
        return _derive_table_name(cls.__name__)

    id: Mapped[str] = mapped_column(GUID, primary_key=True, default=uuid7_str)

    created_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    created_by_id: Mapped[str | None] = mapped_column(GUID, nullable=True)
    updated_by_id: Mapped[str | None] = mapped_column(GUID, nullable=True)

    def touch(self, actor_id: str | None = None) -> None:
        self.updated_at = utcnow()
        self.updated_by_id = actor_id or current_actor_id()


class SoftDeleteMixin:
    """Retention-friendly deletion.

    Nothing operational is ever hard-deleted: financial records, audit context,
    and lease history must remain reconstructible. Purging happens later, by
    retention policy, as a deliberate administrative act.
    """

    deleted_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    deleted_by_id: Mapped[str | None] = mapped_column(GUID, nullable=True)
    delete_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def soft_delete(self, actor_id: str | None = None, reason: str | None = None) -> None:
        self.deleted_at = utcnow()
        self.deleted_by_id = actor_id or current_actor_id()
        self.delete_reason = reason

    def restore(self) -> None:
        self.deleted_at = None
        self.deleted_by_id = None
        self.delete_reason = None


class TenantModel(BaseModel):
    """Base for every row that belongs to exactly one organization.

    Subclassing this is what enrols a table in automatic query scoping and in
    the row-level-security migration. A tenant-scoped table that forgets to
    subclass it is the bug this design exists to make impossible to write
    accidentally - :func:`app.models.registry.assert_tenant_coverage` fails the
    build if a table with an ``org_id`` column is not a ``TenantModel``.
    """

    __abstract__ = True

    @declared_attr
    def org_id(cls) -> Mapped[str]:  # noqa: N805
        from sqlalchemy import ForeignKey

        return mapped_column(
            GUID,
            ForeignKey("organizations.id", ondelete="RESTRICT"),
            nullable=False,
            index=True,
        )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Any, ...]:  # noqa: N805
        # Composite (org_id, created_at) index: virtually every tenant-facing
        # listing filters by org and orders by recency.
        return (Index(f"ix_{cls.__tablename__}_org_created", "org_id", "created_at"),)


# ---------------------------------------------------------------------------
# Automatic stamping
# ---------------------------------------------------------------------------


@event.listens_for(Session, "before_flush")
def _stamp_actor(session: Session, flush_context: Any, instances: Any) -> None:
    """Attribute writes to the current actor without every service remembering."""
    actor_id = current_actor_id()
    now = utcnow()
    for obj in session.new:
        if isinstance(obj, BaseModel):
            if obj.created_by_id is None:
                obj.created_by_id = actor_id
            if obj.updated_by_id is None:
                obj.updated_by_id = actor_id
    for obj in session.dirty:
        if isinstance(obj, BaseModel) and session.is_modified(obj, include_collections=False):
            obj.updated_at = now
            obj.updated_by_id = actor_id


# ---------------------------------------------------------------------------
# Tenancy guard
# ---------------------------------------------------------------------------

_strict_tenancy = False


def set_strict_tenancy(enabled: bool) -> None:
    """Toggle hard failure on unscoped tenant queries.

    Enabled in tests and deployed environments. Disabled during migrations and
    bootstrap, where there is legitimately no tenant yet.
    """
    global _strict_tenancy
    _strict_tenancy = enabled


def is_strict_tenancy() -> bool:
    return _strict_tenancy


#: Depth counters rather than booleans, so nested escapes unwind correctly.
#: Context variables rather than session execution options because
#: ``Session.get()`` and lazy loads do not reliably carry session-level options
#: into the hook below - and a scoping escape that silently fails to apply is
#: far worse than one that is slightly more verbose.
_unscoped_depth: ContextVar[int] = ContextVar("atlas_unscoped_depth", default=0)
_include_deleted_depth: ContextVar[int] = ContextVar("atlas_include_deleted_depth", default=0)


class _DepthScope:
    """Re-entrant context manager toggling a scoping escape."""

    __slots__ = ("_session", "_var", "_token")

    def __init__(self, session: Session | None, var: ContextVar[int]) -> None:
        self._session = session
        self._var = var
        self._token: Token[int] | None = None

    def __enter__(self) -> Session | None:
        self._token = self._var.set(self._var.get() + 1)
        return self._session

    def __exit__(self, *exc: Any) -> None:
        if self._token is not None:
            self._var.reset(self._token)
            self._token = None


def unscoped(session: Session | None = None) -> _DepthScope:
    """Run a block without automatic organization scoping.

    Legitimate uses are narrow and auditable: authenticating by email before an
    organization is known, platform-operator reporting, retention jobs, and the
    audit chain verifier. Every call site should be obvious about why.
    """
    return _DepthScope(session, _unscoped_depth)


def include_deleted(session: Session | None = None) -> _DepthScope:
    """Run a block that also sees soft-deleted rows."""
    return _DepthScope(session, _include_deleted_depth)


def is_unscoped() -> bool:
    return _unscoped_depth.get() > 0


def is_including_deleted() -> bool:
    return _include_deleted_depth.get() > 0


def install_tenancy_guard() -> None:
    """Register the ORM execution hook. Idempotent."""
    if getattr(install_tenancy_guard, "_installed", False):
        return

    @event.listens_for(Session, "do_orm_execute")
    def _scope_orm_execution(execute_state: Any) -> None:
        if not execute_state.is_select:
            return
        # Column loads and relationship loads inherit the criteria of the
        # statement that spawned them; re-applying would double-filter.
        if execute_state.is_column_load or execute_state.is_relationship_load:
            return

        options = execute_state.execution_options
        entities = _tenant_entities(execute_state)

        if not (options.get(OPT_INCLUDE_DELETED) or is_including_deleted()):
            execute_state.statement = execute_state.statement.options(
                with_loader_criteria(
                    SoftDeleteMixin,
                    lambda cls: cls.deleted_at.is_(None),
                    include_aliases=True,
                )
            )

        if options.get(OPT_UNSCOPED) or is_unscoped():
            return

        # A bare aggregate registers no ORM entity, so entity detection alone
        # would wave `SELECT count(*) FROM units` straight through. The tables
        # in the FROM clause are checked as well.
        aggregate_tables = (
            []
            if _selects_an_entity(execute_state.statement)
            else _tenant_tables_in(execute_state.statement)
        )
        if not entities and not aggregate_tables:
            return

        org_id = current_org_id()
        if org_id is None:
            if _strict_tenancy:
                from app.errors import TenantIsolationViolation

                names = ", ".join(
                    sorted(
                        [entity.__name__ for entity in entities]
                        or [table.name for table in aggregate_tables]
                    )
                )
                raise TenantIsolationViolation(
                    f"Query against tenant-scoped entities ({names}) with no organization "
                    "context. Bind a context or use app.models.base.unscoped()."
                )
            return

        # Applied per concrete entity rather than against ``TenantModel``
        # itself: ``org_id`` is a ``declared_attr`` there, so on the abstract
        # class it is an unnamed Column that cannot compile. Building the
        # predicate from each mapped class also keeps ``org_id`` a bound
        # parameter instead of baking a tenant identifier into a cached
        # statement - which would be the worst possible caching bug to have.
        for entity in entities:
            execute_state.statement = execute_state.statement.options(
                with_loader_criteria(
                    entity,
                    entity.org_id == org_id,
                    include_aliases=True,
                )
            )

        # `with_loader_criteria` only reaches statements that actually load an
        # ORM entity. Dashboards are built almost entirely from bare aggregates,
        # which makes this the single most likely place for a scoping hole to
        # hide - so those get an explicit predicate instead.
        for table in aggregate_tables:
            execute_state.statement = execute_state.statement.where(table.c["org_id"] == org_id)

    install_tenancy_guard._installed = True  # type: ignore[attr-defined]


def _selects_an_entity(statement: Any) -> bool:
    """Whether the statement loads an ORM entity that loader criteria can reach."""
    try:
        descriptions = statement.column_descriptions
    except (AttributeError, NotImplementedError):  # pragma: no cover - defensive
        return True
    return any(description.get("entity") is not None for description in descriptions)


def _tenant_tables_in(statement: Any) -> list[Any]:
    """Tenant-scoped tables appearing in a statement's FROM clause."""
    try:
        froms = statement.get_final_froms()
    except Exception:  # pragma: no cover - unusual statement shapes
        return []

    known = {table.name: table for table in _tenant_table_registry()}
    found: list[Any] = []
    for from_clause in froms:
        table = getattr(from_clause, "element", from_clause)
        name = getattr(table, "name", None)
        columns = getattr(table, "c", None)
        if name in known and columns is not None and "org_id" in columns:
            found.append(table)
    return found


def _tenant_table_registry() -> list[Any]:
    """Tables belonging to tenant-scoped models, resolved once."""
    cached = getattr(_tenant_table_registry, "_cache", None)
    if cached is None:
        cached = [
            mapper.local_table
            for mapper in Base.registry.mappers
            if issubclass(mapper.class_, TenantModel) and mapper.local_table is not None
        ]
        _tenant_table_registry._cache = cached  # type: ignore[attr-defined]
    return cached


def _tenant_entities(execute_state: Any) -> list[type[TenantModel]]:
    """Tenant-scoped mapped classes referenced by this statement."""
    found: list[type[TenantModel]] = []
    for description in execute_state.all_mappers:
        cls = description.class_
        if isinstance(cls, type) and issubclass(cls, TenantModel):
            found.append(cls)
    return found


def model_to_dict(instance: BaseModel, *, exclude: set[str] | None = None) -> dict[str, Any]:
    """Column values as a plain dict - used for audit payloads and diffs."""
    exclude = exclude or set()
    mapper = inspect(type(instance))
    result: dict[str, Any] = {}
    for column in mapper.columns:
        key = column.key
        if key in exclude:
            continue
        value = getattr(instance, key, None)
        if isinstance(value, dt.datetime) or isinstance(value, dt.date):
            result[key] = value.isoformat()
        elif isinstance(value, Decimal):
            result[key] = str(value)
        else:
            result[key] = value
    return result

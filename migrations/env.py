"""Alembic environment.

Two things here are load-bearing.

**Custom types render as themselves.** Atlas columns use type decorators that
resolve differently per dialect - ``GUID`` is a native ``UUID`` on PostgreSQL
and ``CHAR(36)`` elsewhere, ``Money`` is ``NUMERIC`` or a scaled integer.
Autogenerate would otherwise bake whichever dialect it was run against into the
migration, producing a file that is wrong everywhere else. ``render_item``
emits the decorator instead, so one migration is correct on both.

**Batch mode on SQLite.** SQLite cannot ``ALTER`` most things; batch mode
rebuilds the table around the change.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import create_app
from app.models import registry  # noqa: F401  - imports every model
from app.models.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

log = logging.getLogger("alembic.env")

target_metadata = Base.metadata

#: Type decorators that must be rendered by name, with an import, rather than
#: as whatever concrete type the current dialect happens to resolve them to.
_ATLAS_TYPES = {
    "GUID",
    "JSONType",
    "Money",
    "UTCDateTime",
    "EncryptedText",
}


def _database_url() -> str:
    """Resolve the URL from application settings, not from alembic.ini."""
    configured = config.get_main_option("sqlalchemy.url", None)
    if configured:
        return configured
    application = create_app()
    return application.config["SQLALCHEMY_DATABASE_URI"]


def render_item(type_, obj, autogen_context):  # noqa: ANN001, ANN201
    """Render Atlas type decorators with an explicit import."""
    if type_ == "type" and type(obj).__name__ in _ATLAS_TYPES:
        autogen_context.imports.add("import app.models.types")
        return f"app.models.types.{type(obj).__name__}()"
    return False


def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001, ANN201
    """Skip objects Alembic should not manage."""
    return not (type_ == "table" and name in {"alembic_version"})


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting - for reviewing a change."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=_database_url().startswith("sqlite"),
        render_item=render_item,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live connection."""
    url = _database_url()
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = url

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            render_as_batch=connection.dialect.name == "sqlite",
            render_item=render_item,
            include_object=include_object,
            # Transactional DDL on PostgreSQL: a failed migration rolls back
            # whole rather than leaving the schema half-applied.
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

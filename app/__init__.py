"""Atlas PMOS - an enterprise property management operating system.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

__version__ = "0.5.0"

__all__ = ["__version__", "create_app"]


def create_app(config_name: str | None = None, **overrides: object):  # noqa: ANN201
    """Application factory.

    Imported lazily so that ``import app`` stays cheap for tooling (Alembic,
    Celery bootstraps, CLI entry points) that only needs the version.
    """
    from app.factory import create_app as _create_app

    return _create_app(config_name, **overrides)

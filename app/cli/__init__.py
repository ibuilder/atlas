"""Operational CLI commands.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from flask import Flask

__all__ = ["register_cli"]


def register_cli(app: Flask) -> None:
    from app.cli.admin import admin_cli
    from app.cli.seed import seed_cli
    from app.cli.seed_scale import register_scale_commands

    register_scale_commands(seed_cli)

    app.cli.add_command(seed_cli)
    app.cli.add_command(admin_cli)

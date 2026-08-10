"""Staging configuration.

Production controls, production-shaped data volumes, and verbose logging.
Everything the base class enforces for deployed environments applies here -
staging is where a misconfiguration should surface, not production.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from app.config.base import Settings


class StagingSettings(Settings):
    env: str = "staging"  # type: ignore[assignment]
    debug: bool = False
    log_level: str = "DEBUG"  # type: ignore[assignment]
    log_format: str = "json"  # type: ignore[assignment]
    tracing_enabled: bool = True
    feature_ai_copilot: bool = False

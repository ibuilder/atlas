"""Environment-aware configuration loading.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import os
from typing import Any

from app.config.base import ConfigError, EnvName, Settings
from app.config.dev import DevelopmentSettings
from app.config.prod import ProductionSettings
from app.config.staging import StagingSettings
from app.config.test import TestSettings

__all__ = [
    "ConfigError",
    "DevelopmentSettings",
    "EnvName",
    "ProductionSettings",
    "Settings",
    "StagingSettings",
    "TestSettings",
    "load_settings",
    "resolve_env",
]

_SETTINGS_BY_ENV: dict[str, type[Settings]] = {
    "development": DevelopmentSettings,
    "dev": DevelopmentSettings,
    "testing": TestSettings,
    "test": TestSettings,
    "staging": StagingSettings,
    "stage": StagingSettings,
    "production": ProductionSettings,
    "prod": ProductionSettings,
}


def resolve_env(config_name: str | None = None) -> str:
    """Resolve the effective environment name.

    Explicit argument wins, then ``ATLAS_ENV``/``APP_ENV``/``FLASK_ENV``, then
    development. An unrecognised name is an error rather than a silent fallback:
    a typo in a deployment manifest must not quietly downgrade hardening.
    """
    raw = (
        (
            config_name
            or os.getenv("ATLAS_ENV")
            or os.getenv("APP_ENV")
            or os.getenv("FLASK_ENV")
            or "development"
        )
        .strip()
        .lower()
    )
    if raw not in _SETTINGS_BY_ENV:
        valid = ", ".join(sorted(set(_SETTINGS_BY_ENV)))
        raise ConfigError(f"Unknown environment {raw!r}. Expected one of: {valid}.")
    return raw


def load_settings(config_name: str | None = None, **overrides: Any) -> Settings:
    """Instantiate the settings class for the requested environment."""
    settings_cls = _SETTINGS_BY_ENV[resolve_env(config_name)]
    return settings_cls(**overrides)

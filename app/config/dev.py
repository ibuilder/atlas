"""Development configuration.

Convenience is allowed here and nowhere else: auto-generated secrets, a local
SQLite file so the app runs with no services, relaxed cookie flags for plain
HTTP, and human-readable logs.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from pydantic import model_validator

from app.config.base import Settings, generate_dev_secret


class DevelopmentSettings(Settings):
    env: str = "development"  # type: ignore[assignment]
    debug: bool = True
    force_https: bool = False
    session_cookie_secure: bool = False
    log_format: str = "console"  # type: ignore[assignment]
    log_level: str = "DEBUG"  # type: ignore[assignment]
    db_enable_rls: bool = False
    malware_scan_required: bool = False
    metrics_enabled: bool = True

    @model_validator(mode="after")
    def _fill_dev_secrets(self) -> DevelopmentSettings:
        if not self.secret_key.get_secret_value():
            self.secret_key = generate_dev_secret("dev-secret")
        if not self.field_encryption_key.get_secret_value():
            from app.security.crypto import generate_encryption_key

            self.field_encryption_key = generate_encryption_key()
        if not self.webhook_signing_secret.get_secret_value():
            self.webhook_signing_secret = generate_dev_secret("dev-webhook")
        return self

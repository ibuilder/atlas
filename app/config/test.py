"""Test configuration.

An isolated in-memory database by default so the suite runs anywhere with no
services. Point ``DATABASE_URL`` at PostgreSQL to run the identical suite
against the production dialect, including the row-level-security tests.

Password hashing is deliberately weakened here: Argon2 at production cost turns
a 900-test suite into a coffee break. Every other control stays on, because the
security tests are the ones that matter most.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import os

from pydantic import model_validator

from app.config.base import Settings, generate_dev_secret


class TestSettings(Settings):
    env: str = "testing"  # type: ignore[assignment]
    testing: bool = True
    debug: bool = False
    # `or` rather than a getenv default: CI matrices routinely set the variable
    # to an empty string for the branch that does not want it, and an empty
    # string is not a valid DSN.
    database_url: str = os.getenv("DATABASE_URL") or "sqlite+pysqlite:///:memory:"
    force_https: bool = False
    session_cookie_secure: bool = False
    csrf_enabled: bool = False  # exercised explicitly in tests/security
    ratelimit_enabled: bool = False
    celery_task_always_eager: bool = True
    log_level: str = "WARNING"  # type: ignore[assignment]
    log_format: str = "console"  # type: ignore[assignment]
    # Enabled so the row-level-security tests exercise the real session binding.
    # The hook is a no-op on SQLite, so the portable path is unaffected.
    db_enable_rls: bool = True
    malware_scan_required: bool = False
    metrics_enabled: bool = False
    mail_backend: str = "memory"  # type: ignore[assignment]
    storage_backend: str = "local"  # type: ignore[assignment]

    # Fast KDF parameters - tests assert on behaviour, not on work factor.
    argon2_time_cost: int = 1
    argon2_memory_cost_kib: int = 8_192
    argon2_parallelism: int = 1

    @model_validator(mode="after")
    def _fill_test_secrets(self) -> TestSettings:
        if not self.secret_key.get_secret_value():
            self.secret_key = generate_dev_secret("test-secret")
        if not self.field_encryption_key.get_secret_value():
            from app.security.crypto import generate_encryption_key

            self.field_encryption_key = generate_encryption_key()
        if not self.webhook_signing_secret.get_secret_value():
            self.webhook_signing_secret = generate_dev_secret("test-webhook")
        return self

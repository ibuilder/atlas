"""Production configuration.

The strictest profile. All hardening enforced by :class:`Settings` for deployed
environments applies, plus production-only requirements: a Postgres DSN, a real
Redis instance rather than a local cache, and a signing secret for outbound
webhooks.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from pydantic import model_validator

from app.config.base import ConfigError, Settings


class ProductionSettings(Settings):
    env: str = "production"  # type: ignore[assignment]
    debug: bool = False
    testing: bool = False
    log_level: str = "INFO"  # type: ignore[assignment]
    log_format: str = "json"  # type: ignore[assignment]
    force_https: bool = True
    session_cookie_secure: bool = True
    tracing_enabled: bool = True
    db_enable_rls: bool = True
    malware_scan_required: bool = True

    @model_validator(mode="after")
    def _validate_production(self) -> ProductionSettings:
        if not self.is_postgres:
            raise ConfigError("Production requires a PostgreSQL DATABASE_URL.")
        if not self.redis_url or self.redis_url.startswith("memory"):
            raise ConfigError("Production requires a real Redis instance.")
        self._require_strong_secret("WEBHOOK_SIGNING_SECRET", self.webhook_signing_secret)
        if self.storage_backend == "local":
            raise ConfigError(
                "Production requires object storage (STORAGE_BACKEND=s3); "
                "local disk is not durable across replicas."
            )
        if not self.mfa_required_for_privileged:
            raise ConfigError("MFA cannot be disabled for privileged roles in production.")
        if self.mail_backend == "console":
            raise ConfigError("Production requires a real mail backend.")
        return self

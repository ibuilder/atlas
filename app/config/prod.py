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

#: Reserved by RFC 2606 and RFC 6761 so that they can never resolve to a real
#: host. A sender at any of them cannot pass SPF or DKIM at the receiving end,
#: and that rejection happens after Atlas has already recorded the send.
RESERVED_MAIL_DOMAINS = frozenset({"example.com", "example.net", "example.org"})
RESERVED_MAIL_SUFFIXES = (".example", ".invalid", ".test", ".localhost")


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
        if self.mail_backend == "smtp" and not self.smtp_host.strip():
            raise ConfigError(
                "MAIL_BACKEND=smtp requires SMTP_HOST. Left empty, every send "
                "fails at connect time inside a Celery worker, where the only "
                "trace is a task traceback nobody is watching."
            )
        self._require_routable_sender()
        return self

    def _require_routable_sender(self) -> None:
        """MAIL_FROM has to name a domain the operator actually controls.

        Left at its placeholder the application boots, sends, and records
        success. The rejection happens at the *receiving* mail server, which has
        no way to tell Atlas about it, so nothing here ever learns. The first
        symptom is a resident saying they never got a notice Atlas has filed as
        delivered — and for a delinquency notice that gap is a legal one.

        The reference compose file demands both this and SMTP_HOST, but that
        only covers deployers who use it. Kubernetes, systemd, and bare-metal
        deployments reach this code with nothing else in the way.
        """
        address = self.mail_from.strip().rstrip(">")
        _, _, domain = address.rpartition("@")
        domain = domain.strip().lower()

        if not domain or "." not in domain:
            raise ConfigError(
                f"MAIL_FROM is not a usable address ({self.mail_from!r}); set it "
                "to a mailbox on a domain this deployment controls."
            )
        if domain in RESERVED_MAIL_DOMAINS or domain.endswith(RESERVED_MAIL_SUFFIXES):
            raise ConfigError(
                f"MAIL_FROM still uses the reserved domain {domain!r}, which can "
                "never authenticate mail. Set it to a domain this deployment "
                "controls, or every notice Atlas records as sent is dropped by "
                "the recipient's server."
            )

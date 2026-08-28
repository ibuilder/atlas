"""Base settings shared by every environment.

Configuration is validated at import time by pydantic-settings. Environment
subclasses tighten the rules; production refuses to boot on a weak secret, a
non-Postgres database, or debug mode left enabled. Failing closed at startup is
cheaper than failing open in production.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import os
import secrets
from datetime import timedelta
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

EnvName = Literal["development", "testing", "staging", "production"]

#: Placeholder that must never survive into a deployed environment.
INSECURE_SECRET_SENTINELS = frozenset(
    {
        "",
        "change-me",
        "changeme",
        "secret",
        "dev",
        "development",
        "test",
        "atlas",
        "please-change-me",
    }
)

MIN_SECRET_LENGTH = 32


class ConfigError(RuntimeError):
    """Raised when configuration is invalid for the selected environment."""


def _normalize_database_url(url: str) -> str:
    """Normalize database URLs onto explicit, supported drivers.

    Accepts the ``postgres://`` form emitted by several managed providers and
    rewrites it to the psycopg 3 driver Atlas actually uses.
    """
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("sqlite://") and not url.startswith("sqlite+"):
        return "sqlite+pysqlite://" + url[len("sqlite://") :]
    return url


class Settings(BaseSettings):
    """Validated application settings.

    Every value is overridable by an environment variable of the same name
    (case-insensitive). ``ATLAS_SECRETS_DIR`` enables file-based secrets for
    Docker/Kubernetes secret mounts.
    """

    model_config = SettingsConfigDict(
        env_file=(".env",),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        secrets_dir=os.getenv("ATLAS_SECRETS_DIR") or None,
    )

    # ---------------------------------------------------------------- core
    env: EnvName = Field(
        default="development",
        validation_alias=AliasChoices("ATLAS_ENV", "APP_ENV", "FLASK_ENV", "env"),
    )
    debug: bool = False
    testing: bool = False
    app_name: str = "Atlas PMOS"
    app_url: str = "http://localhost:5000"
    secret_key: SecretStr = SecretStr("")

    #: Encrypts sensitive columns (bank detail fragments, MFA seeds, tokens).
    #: A urlsafe base64 32-byte Fernet key. Auto-generated outside production.
    field_encryption_key: SecretStr = SecretStr("")

    # ------------------------------------------------------------ database
    database_url: str = Field(
        default="sqlite+pysqlite:///instance/atlas.db",
        validation_alias=AliasChoices("DATABASE_URL", "SQLALCHEMY_DATABASE_URI", "database_url"),
    )
    db_pool_size: int = Field(default=10, ge=1, le=200)
    db_max_overflow: int = Field(default=20, ge=0, le=200)
    db_pool_recycle: int = Field(default=1800, ge=60)
    db_pool_timeout: int = Field(default=30, ge=1)
    db_statement_timeout_ms: int = Field(default=30_000, ge=0)
    db_echo: bool = False
    #: Enforce Postgres row-level security policies for tenant tables.
    db_enable_rls: bool = True

    # --------------------------------------------------------------- redis
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = ""
    celery_result_backend: str = ""
    #: Run Celery tasks inline (no broker). Only ever true in dev/test.
    celery_task_always_eager: bool = False

    # ------------------------------------------------------------- session
    session_cookie_name: str = "atlas_session"
    session_cookie_secure: bool = True
    session_cookie_httponly: bool = True
    session_cookie_samesite: Literal["Lax", "Strict", "None"] = "Lax"
    session_lifetime_minutes: int = Field(default=720, ge=5, le=10_080)
    #: Sliding idle timeout; a session untouched for this long is rejected.
    session_idle_timeout_minutes: int = Field(default=60, ge=5, le=1_440)
    #: Privileged roles must re-assert MFA after this long.
    session_privileged_reauth_minutes: int = Field(default=240, ge=5, le=1_440)

    # -------------------------------------------------------------- security
    force_https: bool = True
    hsts_max_age: int = Field(default=31_536_000, ge=0)
    csrf_enabled: bool = True
    csrf_time_limit_seconds: int = Field(default=7_200, ge=300)
    #: Number of proxies in front of the app; 0 disables header trust entirely.
    trusted_proxy_count: int = Field(default=0, ge=0, le=8)
    #: ``NoDecode`` matters more than it looks. Without it, pydantic-settings
    #: treats a ``list`` field as complex and runs ``json.loads`` on the
    #: environment value *before* any validator sees it — so the comma-separated
    #: form the validator below exists to accept could never reach it, and
    #: ``CORS_ALLOWED_ORIGINS=https://example.com`` failed the application's
    #: startup with a parse error rather than being split. Found by deploying it.
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    password_min_length: int = Field(default=12, ge=8, le=256)
    password_max_length: int = Field(default=1_024, ge=64)
    password_history_depth: int = Field(default=5, ge=0, le=50)
    password_reset_ttl_minutes: int = Field(default=30, ge=5, le=1_440)
    login_max_attempts: int = Field(default=5, ge=1, le=100)
    login_lockout_minutes: int = Field(default=15, ge=1, le=1_440)

    argon2_time_cost: int = Field(default=3, ge=1, le=32)
    argon2_memory_cost_kib: int = Field(default=65_536, ge=8_192)
    argon2_parallelism: int = Field(default=2, ge=1, le=16)

    mfa_issuer: str = "Atlas PMOS"
    mfa_required_for_privileged: bool = True
    mfa_recovery_code_count: int = Field(default=10, ge=4, le=32)
    #: Accepted TOTP drift, in 30-second steps, either side of now.
    mfa_totp_window: int = Field(default=1, ge=0, le=4)

    api_token_ttl_days: int = Field(default=90, ge=1, le=730)
    signed_url_ttl_seconds: int = Field(default=900, ge=30, le=86_400)
    webhook_signing_secret: SecretStr = SecretStr("")
    webhook_timeout_seconds: int = Field(default=10, ge=1, le=120)
    webhook_max_attempts: int = Field(default=8, ge=1, le=25)
    #: Rejects webhook replays and inbound requests skewed beyond this window.
    webhook_tolerance_seconds: int = Field(default=300, ge=30, le=3_600)

    # ---------------------------------------------------------- rate limits
    ratelimit_enabled: bool = True
    ratelimit_storage_url: str = ""
    ratelimit_default: str = "600 per minute"
    ratelimit_auth: str = "10 per minute"
    ratelimit_api: str = "300 per minute"
    ratelimit_expensive: str = "20 per minute"

    # ------------------------------------------------------------- storage
    storage_backend: Literal["local", "s3"] = "local"
    storage_local_path: str = "instance/storage"
    storage_bucket: str = ""
    storage_region: str = ""
    storage_endpoint_url: str = ""
    upload_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    #: Uploads stay quarantined until the malware scan pipeline clears them.
    malware_scan_required: bool = True

    # --------------------------------------------------------- observability
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    log_sql_queries: bool = False
    metrics_enabled: bool = True
    #: Guards /metrics with a bearer token when set.
    metrics_token: SecretStr = SecretStr("")
    tracing_enabled: bool = False
    otel_exporter_endpoint: str = ""
    sentry_dsn: str = ""
    slow_request_ms: int = Field(default=1_000, ge=1)
    slow_query_ms: int = Field(default=300, ge=1)

    # ------------------------------------------------------------- features
    feature_ai_copilot: bool = False
    feature_automation_engine: bool = True
    feature_owner_portal: bool = True
    feature_vendor_portal: bool = True
    feature_openapi_ui: bool = True

    # ------------------------------------------------------------ integrations
    mail_backend: Literal["console", "smtp", "memory"] = "console"
    mail_from: str = "no-reply@atlas.example"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_use_tls: bool = True
    esign_backend: Literal["mock", "http"] = "mock"

    # Uploads are quarantined until scanned either way; this decides what does
    # the scanning. ``structural`` performs checks it can do correctly - EICAR,
    # active content - and is explicit that it is not a virus scanner, so a
    # green result from it is never mistaken for one. A deployment handling
    # resident uploads should run ``clamav``.
    malware_scanner: Literal["structural", "clamav"] = "structural"
    clamav_host: str = "127.0.0.1"
    clamav_port: int = 3310
    clamav_timeout_seconds: int = 30

    # ------------------------------------------------------------ validators
    @field_validator("database_url")
    @classmethod
    def _normalize_db(cls, value: str) -> str:
        return _normalize_database_url(value)

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: Any) -> Any:
        """Accept the comma form and the JSON one.

        ``NoDecode`` on the field stops pydantic-settings JSON-parsing the
        environment value before this runs, which is what lets the comma form
        work at all. But somebody following pydantic-settings' own
        documentation writes a JSON array, and splitting that on commas yields
        origins like ``["https://a.example.com"`` — wrong, and wrong quietly.
        Both forms are reasonable to write, so both are read.
        """
        if not isinstance(value, str):
            return value

        text = value.strip()
        if text.startswith("["):
            import json

            try:
                decoded = json.loads(text)
            except ValueError as exc:
                raise ValueError(
                    f"{text!r} starts like a JSON array and is not one. Give a "
                    "comma-separated list, or valid JSON."
                ) from exc
            if not isinstance(decoded, list):
                raise ValueError("A JSON value for allowed origins must be an array.")
            return [str(item).strip() for item in decoded if str(item).strip()]

        return [item.strip() for item in text.split(",") if item.strip()]

    @model_validator(mode="after")
    def _derive_defaults(self) -> Settings:
        if not self.celery_broker_url:
            self.celery_broker_url = self.redis_url
        if not self.celery_result_backend:
            self.celery_result_backend = self.redis_url
        if not self.ratelimit_storage_url:
            self.ratelimit_storage_url = self.redis_url
        return self

    @model_validator(mode="after")
    def _validate_for_environment(self) -> Settings:
        """Environment-specific hardening. Subclasses extend, never relax."""
        if self.is_deployed:
            self._require_strong_secret("SECRET_KEY", self.secret_key)
            self._require_strong_secret("FIELD_ENCRYPTION_KEY", self.field_encryption_key)
            if self.debug:
                raise ConfigError("DEBUG must be disabled outside development.")
            if self.testing:
                raise ConfigError("TESTING must be disabled outside the test environment.")
            if self.database_url.startswith("sqlite"):
                raise ConfigError(
                    "SQLite is not supported in staging or production; "
                    "set DATABASE_URL to a PostgreSQL DSN."
                )
            if not self.session_cookie_secure:
                raise ConfigError("SESSION_COOKIE_SECURE must remain enabled when deployed.")
            if not self.csrf_enabled:
                raise ConfigError("CSRF protection must remain enabled when deployed.")
            if self.celery_task_always_eager:
                raise ConfigError("CELERY_TASK_ALWAYS_EAGER must be disabled when deployed.")
            if "*" in self.cors_allowed_origins:
                raise ConfigError("Wildcard CORS origins are not permitted when deployed.")
        return self

    @staticmethod
    def _require_strong_secret(name: str, value: SecretStr) -> None:
        raw = value.get_secret_value()
        if raw.strip().lower() in INSECURE_SECRET_SENTINELS:
            raise ConfigError(f"{name} is unset or uses a well-known placeholder value.")
        if len(raw) < MIN_SECRET_LENGTH:
            raise ConfigError(f"{name} must be at least {MIN_SECRET_LENGTH} characters.")

    # -------------------------------------------------------------- helpers
    @property
    def is_deployed(self) -> bool:
        """True for staging and production - the environments that face users."""
        return self.env in ("staging", "production")

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    def engine_options(self) -> dict[str, Any]:
        """SQLAlchemy engine options appropriate to the configured dialect."""
        if self.is_sqlite:
            # SQLite has no server-side pool to tune; the connect args below
            # keep the in-memory test database usable across threads.
            options: dict[str, Any] = {
                "echo": self.db_echo,
                "future": True,
                "connect_args": {"check_same_thread": False},
            }
            if ":memory:" in self.database_url or "mode=memory" in self.database_url:
                from sqlalchemy.pool import StaticPool

                options["poolclass"] = StaticPool
            return options

        connect_args: dict[str, Any] = {"application_name": self.app_name}
        if self.db_statement_timeout_ms:
            connect_args["options"] = f"-c statement_timeout={self.db_statement_timeout_ms}"
        return {
            "echo": self.db_echo,
            "future": True,
            "pool_pre_ping": True,
            "pool_size": self.db_pool_size,
            "max_overflow": self.db_max_overflow,
            "pool_recycle": self.db_pool_recycle,
            "pool_timeout": self.db_pool_timeout,
            "connect_args": connect_args,
        }

    def to_flask_config(self) -> dict[str, Any]:
        """Project settings onto the Flask config keys extensions expect."""
        return {
            "ENV": self.env,
            "DEBUG": self.debug,
            "TESTING": self.testing,
            "SECRET_KEY": self.secret_key.get_secret_value(),
            "APP_NAME": self.app_name,
            "APP_URL": self.app_url,
            "VERSION": _package_version(),
            # Database
            "SQLALCHEMY_DATABASE_URI": self.database_url,
            "SQLALCHEMY_ENGINE_OPTIONS": self.engine_options(),
            "SQLALCHEMY_TRACK_MODIFICATIONS": False,
            # Sessions
            "SESSION_COOKIE_NAME": self.session_cookie_name,
            "SESSION_COOKIE_SECURE": self.session_cookie_secure,
            "SESSION_COOKIE_HTTPONLY": self.session_cookie_httponly,
            "SESSION_COOKIE_SAMESITE": self.session_cookie_samesite,
            "PERMANENT_SESSION_LIFETIME": timedelta(minutes=self.session_lifetime_minutes),
            "SESSION_REFRESH_EACH_REQUEST": False,
            # CSRF
            "WTF_CSRF_ENABLED": self.csrf_enabled,
            "WTF_CSRF_TIME_LIMIT": self.csrf_time_limit_seconds,
            "WTF_CSRF_SSL_STRICT": self.force_https,
            # Uploads
            "MAX_CONTENT_LENGTH": self.upload_max_bytes,
            # Rate limiting
            "RATELIMIT_ENABLED": self.ratelimit_enabled,
            "RATELIMIT_STORAGE_URI": self.ratelimit_storage_url,
            "RATELIMIT_HEADERS_ENABLED": True,
            "RATELIMIT_STRATEGY": "moving-window",
            # Caching
            "CACHE_TYPE": "RedisCache" if not self.is_local_cache else "SimpleCache",
            "CACHE_REDIS_URL": self.redis_url,
            "CACHE_DEFAULT_TIMEOUT": 300,
            # JSON / templating
            "JSON_SORT_KEYS": False,
            "TEMPLATES_AUTO_RELOAD": self.debug,
            "EXPLAIN_TEMPLATE_LOADING": False,
            "PREFERRED_URL_SCHEME": "https" if self.force_https else "http",
            # Escape hatch for code that only has `current_app`
            "SETTINGS": self,
        }

    @property
    def is_local_cache(self) -> bool:
        """Use an in-process cache when Redis is not part of the deployment."""
        return self.testing or self.env == "development"


def _package_version() -> str:
    from app import __version__

    return __version__


def generate_dev_secret(seed: str) -> SecretStr:
    """Deterministic-length random secret for non-deployed environments.

    Never used when ``env`` is staging or production - those require an
    explicitly provided secret and refuse to start without one.
    """
    return SecretStr(f"{seed}-{secrets.token_urlsafe(48)}")

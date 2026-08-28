"""Configuration fails closed, and the schema keeps its structural promises.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pytest

from app.config import ConfigError, load_settings
from app.config.base import Settings
from app.models.registry import (
    assert_indexed_foreign_keys,
    assert_tenant_coverage,
    assert_timestamp_coverage,
    tenant_tables,
)

pytestmark = pytest.mark.unit

STRONG_SECRET = "x" * 48
STRONG_KEY = "y" * 48
POSTGRES_DSN = "postgresql+psycopg://atlas:atlas@db:5432/atlas"


def _production(**overrides):
    from app.config.prod import ProductionSettings

    base = {
        "secret_key": STRONG_SECRET,
        "field_encryption_key": STRONG_KEY,
        "webhook_signing_secret": STRONG_SECRET,
        "database_url": POSTGRES_DSN,
        "redis_url": "redis://redis:6379/0",
        "storage_backend": "s3",
        "mail_backend": "smtp",
        # An SMTP backend is only half a mail configuration: production also
        # requires somewhere to connect and a sender on a domain that can pass
        # SPF. See tests/unit/test_production_refusals.py for why each is a
        # boot refusal rather than a runtime surprise.
        "smtp_host": "smtp.example.com",
        "mail_from": "no-reply@atlas-pmos.io",
        # Builds the password-reset link. Production refuses the development
        # default, because a reset mail pointing at localhost reaches somebody
        # who is already locked out.
        "app_url": "https://atlas.example-realty.com",
    }
    base.update(overrides)
    return ProductionSettings(**base)


# ----------------------------------------------------------------- config


def test_unknown_environment_is_an_error():
    """A typo in a deployment manifest must not silently downgrade hardening."""
    with pytest.raises(ConfigError):
        load_settings("prodution")


def test_development_generates_its_own_secrets():
    settings = load_settings("development")
    assert settings.secret_key.get_secret_value()
    assert settings.field_encryption_key.get_secret_value()
    assert not settings.is_deployed


def test_production_accepts_a_correct_configuration():
    settings = _production()
    assert settings.is_production
    assert settings.is_postgres


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"secret_key": ""}, "SECRET_KEY"),
        ({"secret_key": "change-me"}, "SECRET_KEY"),
        ({"secret_key": "tooshort"}, "SECRET_KEY"),
        ({"field_encryption_key": ""}, "FIELD_ENCRYPTION_KEY"),
        ({"debug": True}, "DEBUG"),
        ({"database_url": "sqlite+pysqlite:///atlas.db"}, "SQLite"),
        ({"session_cookie_secure": False}, "SESSION_COOKIE_SECURE"),
        ({"csrf_enabled": False}, "CSRF"),
        ({"celery_task_always_eager": True}, "EAGER"),
        ({"cors_allowed_origins": ["*"]}, "Wildcard"),
        ({"storage_backend": "local"}, "object storage"),
        ({"mfa_required_for_privileged": False}, "MFA"),
        ({"mail_backend": "console"}, "mail backend"),
    ],
)
def test_production_refuses_unsafe_configuration(overrides, fragment):
    with pytest.raises(ConfigError) as exc:
        _production(**overrides)
    assert fragment.lower() in str(exc.value).lower()


def test_database_urls_are_normalised_onto_explicit_drivers():
    settings = Settings(database_url="postgres://user:pw@host/db")
    assert settings.database_url.startswith("postgresql+psycopg://")


def test_dsn_credentials_are_redacted_for_logging():
    from app.factory import _redact_dsn

    redacted = _redact_dsn("postgresql+psycopg://atlas:supersecret@db:5432/atlas")
    assert "supersecret" not in redacted
    assert redacted.endswith("db:5432/atlas")


def test_sqlite_engine_options_avoid_pool_settings():
    settings = Settings(database_url="sqlite+pysqlite:///:memory:")
    options = settings.engine_options()
    assert "pool_size" not in options
    assert options["connect_args"]["check_same_thread"] is False


# ----------------------------------------------------------------- schema


def test_every_table_with_org_id_is_a_tenant_model():
    """The invariant the whole isolation design rests on."""
    assert_tenant_coverage()


def test_every_table_carries_audit_columns():
    assert_timestamp_coverage()


def test_foreign_keys_are_indexed():
    assert_indexed_foreign_keys()


def test_the_expected_domains_are_tenant_scoped():
    names = {table.name for table in tenant_tables()}
    for expected in (
        "properties",
        "units",
        "leases",
        "residents",
        "invoices",
        "payments",
        "journal_entries",
        "journal_lines",
        "work_orders",
        "documents",
        "audit_events",
        "users",
    ):
        assert expected in names, f"{expected} is not tenant-scoped"


def test_permission_catalog_is_consistent():
    from app.security.permissions import validate_catalog

    validate_catalog()

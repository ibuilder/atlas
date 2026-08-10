"""Application factory.

One function assembles the application in a fixed order, and the order matters:
configuration is validated before anything reads it, the field cipher exists
before any model can decrypt a column, and the tenancy guard is installed before
the first query can possibly run.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify

from app.config import Settings, load_settings
from app.errors import register_error_handlers
from app.logging import configure_logging, get_logger
from app.middleware import init_middleware
from app.observability import init_observability

__all__ = ["create_app"]

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent

log = get_logger("factory")


def create_app(config_name: str | None = None, **overrides: Any) -> Flask:
    """Build a fully configured Atlas application."""
    settings = load_settings(config_name, **overrides)

    configure_logging(
        level=settings.log_level,
        fmt=settings.log_format,
        environment=settings.env,
        service="atlas-pmos",
    )

    app = Flask(
        "atlas",
        template_folder=str(BASE_DIR / "templates"),
        static_folder=str(BASE_DIR / "static"),
        static_url_path="/static",
        instance_path=str(PROJECT_ROOT / "instance"),
    )
    app.config.update(settings.to_flask_config())

    _prepare_filesystem(app, settings)
    _init_crypto(app, settings)
    _init_persistence(app, settings)
    _init_security(app, settings)

    init_middleware(app)
    register_error_handlers(app)
    init_observability(app)

    _register_blueprints(app, settings)
    _register_cli(app)
    _register_shell_context(app)
    _register_template_globals(app, settings)

    log.info(
        "application ready",
        extra={
            "event": "app.startup",
            "environment": settings.env,
            "database": _redact_dsn(settings.database_url),
            "strict_tenancy": settings.env != "development",
            "features": {
                "automation": settings.feature_automation_engine,
                "owner_portal": settings.feature_owner_portal,
                "vendor_portal": settings.feature_vendor_portal,
                "ai_copilot": settings.feature_ai_copilot,
            },
        },
    )
    return app


# ---------------------------------------------------------------------------
# Assembly steps
# ---------------------------------------------------------------------------


def _prepare_filesystem(app: Flask, settings: Settings) -> None:
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    if settings.storage_backend == "local":
        Path(settings.storage_local_path).mkdir(parents=True, exist_ok=True)
        (Path(settings.storage_local_path) / "quarantine").mkdir(parents=True, exist_ok=True)
    if settings.is_sqlite and "/instance/" in settings.database_url.replace("\\", "/"):
        Path(app.instance_path).mkdir(parents=True, exist_ok=True)


def _init_crypto(app: Flask, settings: Settings) -> None:
    from app.security.crypto import FieldCipher
    from app.security.keyring import set_field_cipher

    cipher = FieldCipher(settings.field_encryption_key)
    app.extensions["atlas_field_cipher"] = cipher
    # Also install process-wide so Celery workers and Alembic - which run
    # without an application context - can still decrypt.
    set_field_cipher(cipher)


def _init_persistence(app: Flask, settings: Settings) -> None:
    from app.extensions import db, migrate
    from app.models import registry  # noqa: F401  (imports every model)
    from app.models.base import install_tenancy_guard, set_strict_tenancy

    install_tenancy_guard()
    # Development keeps the guard permissive so a `flask shell` session is
    # usable; every other environment fails closed on an unscoped query.
    set_strict_tenancy(settings.env != "development")

    db.init_app(app)
    migrate.init_app(
        app,
        db,
        directory=str(PROJECT_ROOT / "migrations"),
        render_as_batch=settings.is_sqlite,
        compare_type=True,
        compare_server_default=True,
    )

    if settings.is_sqlite:
        _enable_sqlite_pragmas(db)


def _enable_sqlite_pragmas(db: Any) -> None:
    """SQLite defaults are wrong for anything resembling an application.

    Foreign keys are off by default, which would silently disable every
    referential guarantee the schema relies on during tests.
    """
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    @event.listens_for(Engine, "connect")
    def _set_pragmas(dbapi_connection: Any, connection_record: Any) -> None:  # noqa: ARG001
        if dbapi_connection.__class__.__module__.startswith("sqlite3"):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()


def _init_security(app: Flask, settings: Settings) -> None:
    from app.extensions import cache, csrf, init_login_manager, limiter, talisman

    init_login_manager(app)
    csrf.init_app(app)
    cache.init_app(app)

    if settings.ratelimit_enabled:
        limiter.init_app(app)
        limiter.default_limits = [settings.ratelimit_default]

    talisman.init_app(
        app,
        force_https=settings.force_https,
        force_https_permanent=True,
        strict_transport_security=settings.force_https,
        strict_transport_security_max_age=settings.hsts_max_age,
        strict_transport_security_include_subdomains=True,
        session_cookie_secure=settings.session_cookie_secure,
        session_cookie_http_only=settings.session_cookie_httponly,
        frame_options="DENY",
        referrer_policy="strict-origin-when-cross-origin",
        content_security_policy=_content_security_policy(),
        content_security_policy_nonce_in=["script-src", "style-src"],
    )


def _content_security_policy() -> dict[str, Any]:
    """Strict CSP. No inline script or style without a per-response nonce.

    'unsafe-inline' is absent deliberately: the admin UI uses HTMX with
    nonce-tagged blocks rather than inline handlers, so the policy stays
    meaningful instead of decorative.
    """
    return {
        "default-src": "'self'",
        "script-src": ["'self'"],
        "style-src": ["'self'"],
        "img-src": ["'self'", "data:", "blob:"],
        "font-src": ["'self'", "data:"],
        "connect-src": "'self'",
        "media-src": "'self'",
        "object-src": "'none'",
        "base-uri": "'self'",
        "form-action": "'self'",
        "frame-ancestors": "'none'",
        "worker-src": ["'self'", "blob:"],
        "manifest-src": "'self'",
    }


def _register_blueprints(app: Flask, settings: Settings) -> None:
    from app.api.health import health_bp

    app.register_blueprint(health_bp)

    from app.api.v1 import api_v1_bp

    app.register_blueprint(api_v1_bp, url_prefix="/api/v1")

    from app.web import register_web_blueprints

    register_web_blueprints(app, settings)

    if settings.feature_openapi_ui:
        from app.api.openapi import openapi_bp

        app.register_blueprint(openapi_bp)


def _register_cli(app: Flask) -> None:
    from app.cli import register_cli

    register_cli(app)


def _register_shell_context(app: Flask) -> None:
    @app.shell_context_processor
    def _shell_context() -> dict[str, Any]:
        from app import models
        from app.extensions import db

        exported: dict[str, Any] = {"db": db}
        for name in models.__all__:
            exported[name] = getattr(models, name)
        return exported


def _register_template_globals(app: Flask, settings: Settings) -> None:
    from app.web.helpers import register_template_helpers

    register_template_helpers(app, settings)

    @app.route("/healthz/ping")
    def _ping() -> Response:
        return jsonify({"status": "ok"})


def _redact_dsn(dsn: str) -> str:
    """Strip credentials before a DSN goes anywhere near a log line."""
    if "@" not in dsn:
        return dsn
    scheme, _, remainder = dsn.partition("://")
    _, _, host = remainder.rpartition("@")
    return f"{scheme}://***@{host}"

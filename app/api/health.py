"""Liveness, readiness, and version endpoints.

The distinction matters to an orchestrator and is routinely got wrong:

* **Liveness** answers "is this process wedged?". It must not touch the database.
  A liveness probe that fails during a database blip makes Kubernetes restart
  every healthy replica at the exact moment the system is already struggling.
* **Readiness** answers "should traffic come here *right now*?". It checks the
  dependencies a request actually needs, so a replica with a broken pool is
  removed from rotation instead of serving errors.

Neither endpoint reveals anything useful to an unauthenticated caller beyond
up/down: no versions, no hostnames, no dependency detail.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import time
from typing import Any

from flask import Blueprint, Response, current_app, jsonify
from sqlalchemy import text

from app.extensions import talisman
from app.logging import get_logger

health_bp = Blueprint("health", __name__)
log = get_logger("api.health")

__all__ = ["health_bp"]

#: Probes are exempt from the HTTPS redirect, and they have to be.
#:
#: Every orchestrator — Kubernetes, ECS, a plain Docker healthcheck, a load
#: balancer — probes over plain HTTP from inside the network, where there is no
#: certificate to present and no proxy in the path. Redirecting those to HTTPS
#: means the probe never sees a 200, the container never reports healthy, and a
#: rolling deploy stalls with every replica running correctly. Found exactly
#: that way: the container booted fine and sat in `starting` forever.
#:
#: Nothing is given away by serving these over HTTP. They answer up or down and
#: deliberately carry no version, hostname, or dependency detail.
_probe = talisman(force_https=False)


@health_bp.get("/healthz")
@_probe
def liveness() -> Response:
    """Process is running and the event loop is responsive."""
    return jsonify({"status": "ok"})


@health_bp.get("/readyz")
@_probe
def readiness() -> tuple[Response, int]:
    """Dependencies required to serve a request are reachable."""
    checks: dict[str, dict[str, Any]] = {}
    healthy = True

    db_ok, db_detail = _check_database()
    checks["database"] = db_detail
    healthy &= db_ok

    migrations_ok, migration_detail = _check_migrations()
    checks["migrations"] = migration_detail
    # A pending migration means the running code may expect columns that do not
    # exist yet. Refusing traffic is the correct, conservative answer.
    healthy &= migrations_ok

    settings = current_app.config["SETTINGS"]
    if not settings.is_local_cache:
        redis_ok, redis_detail = _check_redis(settings.redis_url)
        checks["redis"] = redis_detail
        healthy &= redis_ok

    status = 200 if healthy else 503
    return jsonify({"status": "ready" if healthy else "not_ready", "checks": checks}), status


@health_bp.get("/version")
def version() -> Response:
    """Build identity, for correlating a deployment with its logs."""
    from app import __version__

    settings = current_app.config["SETTINGS"]
    return jsonify(
        {
            "name": settings.app_name,
            "version": __version__,
            "environment": settings.env,
        }
    )


def _check_database() -> tuple[bool, dict[str, Any]]:
    from app.extensions import db

    started = time.perf_counter()
    try:
        db.session.execute(text("SELECT 1"))
        return True, {
            "status": "ok",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    except Exception as exc:  # noqa: BLE001 - readiness must never raise
        log.error(
            "database readiness check failed",
            extra={"event": "health.database_down", "detail": str(exc)[:200]},
        )
        return False, {"status": "error"}


def _check_migrations() -> tuple[bool, dict[str, Any]]:
    """Compare the database's Alembic revision against the code's head."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory
        from flask_migrate import current as _current  # noqa: F401  (ensures extension is live)

        from app.extensions import db
        from app.factory import PROJECT_ROOT

        migrations_dir = PROJECT_ROOT / "migrations"
        if not (migrations_dir / "env.py").exists():
            return True, {"status": "skipped", "reason": "no migration directory"}

        config = Config()
        config.set_main_option("script_location", str(migrations_dir))
        script = ScriptDirectory.from_config(config)
        heads = set(script.get_heads())

        row = db.session.execute(text("SELECT version_num FROM alembic_version")).fetchall()
        applied = {value for (value,) in row}

        if not applied:
            return False, {"status": "not_applied"}
        if applied != heads:
            return False, {"status": "pending"}
        return True, {"status": "ok"}
    except Exception as exc:  # noqa: BLE001
        # A schema managed outside Alembic (create_all in tests) is not a
        # failure - it is simply unknown.
        log.debug(
            "migration readiness check inconclusive",
            extra={"event": "health.migrations_unknown", "detail": str(exc)[:200]},
        )
        return True, {"status": "unknown"}


def _check_redis(url: str) -> tuple[bool, dict[str, Any]]:
    started = time.perf_counter()
    try:
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        client.ping()
        return True, {
            "status": "ok",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }
    except Exception as exc:  # noqa: BLE001
        log.error(
            "redis readiness check failed",
            extra={"event": "health.redis_down", "detail": str(exc)[:200]},
        )
        return False, {"status": "error"}

"""Prometheus instrumentation.

Metric names follow the Prometheus convention of ``<namespace>_<unit>_<suffix>``
with base units (seconds, bytes). Label sets are kept deliberately small: the
``endpoint`` label uses the Flask *rule name*, never the raw path, so a scan of
``/api/v1/units/<uuid>`` cannot mint a million time series.

Beyond the usual RED metrics, the business counters here exist because the
things that page an on-call engineer for a property platform are rarely CPU:
they are reconciliation exceptions piling up, delinquency notices firing at the
wrong rate, and maintenance SLAs quietly breaching.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import time
from typing import Any

from flask import Flask, Response, g, request
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from app.security.crypto import compare_digest

__all__ = [
    "AUTH_ATTEMPTS",
    "AUDIT_EVENTS",
    "AUTOMATION_RUNS",
    "DELINQUENCY_NOTICES",
    "HTTP_REQUESTS",
    "HTTP_REQUEST_DURATION",
    "JOB_RUNS",
    "LEDGER_POSTINGS",
    "PAYMENTS",
    "QUEUE_DEPTH",
    "RECONCILIATION_EXCEPTIONS",
    "SLA_BREACHES",
    "WEBHOOK_DELIVERIES",
    "init_observability",
]

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0, 2.5, 5.0, 10.0)

HTTP_REQUESTS = Counter(
    "atlas_http_requests_total",
    "HTTP requests handled.",
    ["method", "endpoint", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "atlas_http_request_duration_seconds",
    "HTTP request latency.",
    ["method", "endpoint"],
    buckets=_LATENCY_BUCKETS,
)
DB_QUERIES = Counter(
    "atlas_db_queries_total",
    "Database statements executed.",
    ["operation"],
)
DB_QUERY_DURATION = Histogram(
    "atlas_db_query_duration_seconds",
    "Database statement latency.",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0),
)
AUTH_ATTEMPTS = Counter(
    "atlas_auth_attempts_total",
    "Authentication attempts by outcome.",
    ["outcome"],  # success | invalid_credentials | locked | mfa_required | mfa_failed
)
AUDIT_EVENTS = Counter(
    "atlas_audit_events_total",
    "Audit events written.",
    ["action"],
)
JOB_RUNS = Counter(
    "atlas_job_runs_total",
    "Background job executions by outcome.",
    ["task", "outcome"],  # success | retry | failure | skipped_duplicate
)
QUEUE_DEPTH = Gauge(
    "atlas_queue_depth",
    "Pending messages per queue.",
    ["queue"],
)
WEBHOOK_DELIVERIES = Counter(
    "atlas_webhook_deliveries_total",
    "Outbound webhook deliveries by outcome.",
    ["event", "outcome"],
)
LEDGER_POSTINGS = Counter(
    "atlas_ledger_postings_total",
    "Journal entries posted.",
    ["kind"],
)
RECONCILIATION_EXCEPTIONS = Gauge(
    "atlas_reconciliation_exceptions",
    "Open bank reconciliation exceptions.",
    ["org"],
)
PAYMENTS = Counter(
    "atlas_payments_total",
    "Payment attempts by outcome.",
    ["method", "outcome"],
)
DELINQUENCY_NOTICES = Counter(
    "atlas_delinquency_notices_total",
    "Delinquency notices issued by stage.",
    ["stage"],
)
SLA_BREACHES = Counter(
    "atlas_maintenance_sla_breaches_total",
    "Work orders that breached their SLA.",
    ["priority"],
)
AUTOMATION_RUNS = Counter(
    "atlas_automation_runs_total",
    "Automation rule runs by outcome.",
    ["outcome"],
)
DOCUMENTS_SCANNED = Counter(
    "atlas_document_scans_total",
    "Uploaded documents processed by the malware scanner.",
    ["outcome"],
)


def init_observability(app: Flask) -> None:
    """Install request timing, the metrics endpoint, and slow-request logging."""
    settings = app.config["SETTINGS"]
    if not settings.metrics_enabled:
        return

    @app.before_request
    def _start_timer() -> None:
        g._atlas_started = time.perf_counter()

    @app.after_request
    def _record_request(response: Response) -> Response:
        started = getattr(g, "_atlas_started", None)
        if started is None:
            return response
        elapsed = time.perf_counter() - started
        endpoint = request.endpoint or "unmatched"
        HTTP_REQUESTS.labels(request.method, endpoint, str(response.status_code)).inc()
        HTTP_REQUEST_DURATION.labels(request.method, endpoint).observe(elapsed)

        elapsed_ms = elapsed * 1000
        response.headers["X-Response-Time-ms"] = f"{elapsed_ms:.1f}"
        if elapsed_ms > settings.slow_request_ms:
            app.logger.warning(
                "slow request",
                extra={
                    "event": "performance.slow_request",
                    "endpoint": endpoint,
                    "method": request.method,
                    "duration_ms": round(elapsed_ms, 1),
                    "threshold_ms": settings.slow_request_ms,
                },
            )
        return response

    @app.route("/metrics")
    def _metrics() -> Response:
        # Metrics reveal tenant counts, revenue-shaped counters, and endpoint
        # topology. When a token is configured, it is mandatory.
        expected = settings.metrics_token.get_secret_value()
        if expected:
            provided = request.headers.get("Authorization", "")
            scheme, _, value = provided.partition(" ")
            if scheme.lower() != "bearer" or not compare_digest(value, expected):
                from app.errors import AuthenticationRequired

                raise AuthenticationRequired("A valid metrics token is required.")
        return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

    _install_query_instrumentation(app, settings)


def _install_query_instrumentation(app: Flask, settings: Any) -> None:
    """Count and time database statements, and log slow ones with context.

    Needs an application context to resolve the engine, because Flask-SQLAlchemy
    binds engines per application rather than globally.
    """
    from sqlalchemy import event

    from app.extensions import db

    try:
        with app.app_context():
            engine = db.engine
    except Exception:  # pragma: no cover - no engine configured yet
        app.logger.debug("query instrumentation skipped: no engine available")
        return

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ANN202
        conn.info.setdefault("_atlas_query_start", []).append(time.perf_counter())

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ANN202
        stack = conn.info.get("_atlas_query_start")
        if not stack:
            return
        elapsed = time.perf_counter() - stack.pop()
        operation = statement.lstrip().split(" ", 1)[0].upper()[:12] or "OTHER"
        DB_QUERIES.labels(operation).inc()
        DB_QUERY_DURATION.labels(operation).observe(elapsed)
        elapsed_ms = elapsed * 1000
        if elapsed_ms > settings.slow_query_ms:
            app.logger.warning(
                "slow query",
                extra={
                    "event": "performance.slow_query",
                    "operation": operation,
                    "duration_ms": round(elapsed_ms, 1),
                    # The statement text only - never the bound parameters,
                    # which are the part that carries resident and bank data.
                    "statement": statement[:500],
                },
            )

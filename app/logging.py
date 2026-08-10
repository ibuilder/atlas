"""Structured logging with correlation IDs and PII redaction.

Three rules this module exists to enforce:

1. **Logs are structured.** JSON in every deployed environment, so a log line is
   queryable rather than greppable.
2. **Logs carry correlation.** Every line emitted during a request, task, or
   webhook delivery carries the same ``correlation_id``, so one incident is one
   query.
3. **Logs are not a PII exfiltration channel.** A redaction filter runs on every
   record - including third-party libraries' records - before anything reaches a
   handler. Redaction is applied by key *and* by value pattern, because the leak
   you did not anticipate is the one that matters.

The audit trail is deliberately *not* here. Audit events are durable, ordered,
hash-chained rows in the database (:mod:`app.models.audit`); application logs
are best-effort operational telemetry. Conflating the two produces an audit
trail that a log-shipper outage can silently truncate.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import json
import logging
import logging.config
import re
import sys
import time
from typing import Any

from app.context import current_context

__all__ = [
    "RedactionFilter",
    "audit_logger",
    "configure_logging",
    "get_logger",
    "redact_value",
]

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

REDACTED = "[REDACTED]"

#: Substring match against the *key*: any structured field whose name contains
#: one of these is replaced wholesale. Substring rather than exact match catches
#: `new_password`, `password_confirm`, `smtp_password`, and friends.
SENSITIVE_KEY_PARTS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "auth_header",
        "cookie",
        "session_id",
        "csrf",
        "private_key",
        "credential",
        "mfa_secret",
        "totp",
        "recovery_code",
        "ssn",
        "social_security",
        "tax_id",
        "ein",
        "account_number",
        "routing_number",
        "iban",
        "card_number",
        "cvv",
        "cvc",
        "pan",
        "date_of_birth",
        "dob",
        "signature",
        "encryption_key",
    }
)

#: Keys that are masked rather than removed, because a partially visible value
#: is genuinely useful for support and not meaningfully identifying on its own.
MASKED_KEY_PARTS: frozenset[str] = frozenset({"email", "phone", "mobile", "telephone"})

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}")
_ATLAS_TOKEN_RE = re.compile(r"\batlas_[a-z]+_[A-Za-z0-9._\-]{16,}")
_MAX_REDACT_DEPTH = 6


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _is_masked_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in MASKED_KEY_PARTS)


def _mask_partial(value: str) -> str:
    """Keep just enough of a value to be recognisable in a support conversation."""
    if "@" in value:
        return _EMAIL_RE.sub(lambda m: f"{m.group(1)}***{m.group(2)}", value)
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 4:
        return f"***{digits[-4:]}"
    return REDACTED


def _scrub_text(text: str) -> str:
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    text = _ATLAS_TOKEN_RE.sub(REDACTED, text)
    text = _SSN_RE.sub(REDACTED, text)
    text = _EMAIL_RE.sub(lambda m: f"{m.group(1)}***{m.group(2)}", text)
    return _CARD_RE.sub(lambda m: _redact_if_card_like(m.group(0)), text)


def _redact_if_card_like(candidate: str) -> str:
    """Only redact digit runs that could plausibly be a card number.

    Naively redacting every 13-19 digit run destroys legitimate identifiers
    (amounts in minor units, sequence numbers). A Luhn check keeps the false
    positive rate low while still catching real PANs.
    """
    digits = re.sub(r"\D", "", candidate)
    if not 13 <= len(digits) <= 19 or not _luhn_valid(digits):
        return candidate
    return f"{REDACTED}{digits[-4:]}"


def _luhn_valid(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        value = ord(char) - 48
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def redact_value(value: Any, *, key: str | None = None, _depth: int = 0) -> Any:
    """Recursively redact a value for logging."""
    if _depth > _MAX_REDACT_DEPTH:
        return "[TRUNCATED]"
    if key is not None:
        if _is_sensitive_key(key):
            return REDACTED
        if _is_masked_key(key) and isinstance(value, str):
            return _mask_partial(value)
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, dict):
        return {k: redact_value(v, key=str(k), _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact_value(item, _depth=_depth + 1) for item in value]
    return value


class RedactionFilter(logging.Filter):
    """Scrubs PII and credentials from every record before it is formatted.

    Applied at handler level so it covers third-party loggers too - SQLAlchemy
    echoing a parameterised query, or an HTTP client logging a request header,
    are exactly the paths that leak.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _scrub_text(record.msg)
        elif isinstance(record.msg, (dict, list)):
            record.msg = redact_value(record.msg)

        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: redact_value(v, key=str(k)) for k, v in record.args.items()}
            else:
                record.args = tuple(redact_value(arg) for arg in record.args)

        for key, value in list(record.__dict__.items()):
            if key in _RESERVED_RECORD_KEYS:
                continue
            record.__dict__[key] = redact_value(value, key=key)
        return True


class ContextFilter(logging.Filter):
    """Attaches the ambient request/task context to each record."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = current_context()
        if ctx is not None:
            for key, value in ctx.as_log_fields().items():
                if not hasattr(record, key):
                    setattr(record, key, value)
        return True


# Attributes LogRecord defines itself; everything else on __dict__ is a caller
# supplied `extra` and must go through redaction.
_RESERVED_RECORD_KEYS: frozenset[str] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with a stable base schema."""

    converter = time.gmtime

    def __init__(self, service: str = "atlas-pmos", environment: str = "development") -> None:
        super().__init__()
        self.service = service
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S")
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service,
            "environment": self.environment,
        }
        if record.exc_info:
            payload["exception"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "stacktrace": self.formatException(record.exc_info),
            }
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_KEYS or key in payload:
                continue
            payload[key] = value

        try:
            return json.dumps(payload, default=_json_default, separators=(",", ":"))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return json.dumps(
                {
                    "timestamp": payload["timestamp"],
                    "level": payload["level"],
                    "logger": payload["logger"],
                    "message": payload["message"],
                    "serialization_error": True,
                }
            )


def _json_default(value: Any) -> str:
    return str(value)


class ConsoleFormatter(logging.Formatter):
    """Readable output for local development."""

    _COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[1;31m",
    }
    _RESET = "\033[0m"

    def __init__(self, use_color: bool = True) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%H:%M:%S")
        self.use_color = use_color and sys.stderr.isatty()

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        ctx = getattr(record, "correlation_id", None)
        if ctx:
            base = f"{base}  [cid={str(ctx)[:8]}]"
        if self.use_color:
            color = self._COLORS.get(record.levelname, "")
            return f"{color}{base}{self._RESET}"
        return base


def configure_logging(
    *,
    level: str = "INFO",
    fmt: str = "json",
    environment: str = "development",
    service: str = "atlas-pmos",
) -> None:
    """Install handlers, filters, and library log levels.

    Idempotent: calling it twice (factory plus Celery bootstrap) will not stack
    duplicate handlers.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JsonFormatter(service=service, environment=environment)
        if fmt == "json"
        else ConsoleFormatter()
    )
    handler.addFilter(ContextFilter())
    handler.addFilter(RedactionFilter())

    root.addHandler(handler)
    root.setLevel(level)

    # Library noise control. SQLAlchemy's engine logger in particular will emit
    # bound parameters at INFO, which is precisely the PII path we care about.
    for noisy, noisy_level in {
        "werkzeug": "WARNING",
        "sqlalchemy.engine": "WARNING",
        "sqlalchemy.pool": "WARNING",
        "alembic": "INFO",
        "celery": "INFO",
        "urllib3": "WARNING",
        "botocore": "WARNING",
        "asyncio": "WARNING",
    }.items():
        logging.getLogger(noisy).setLevel(noisy_level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"atlas.{name}" if not name.startswith("atlas") else name)


def audit_logger() -> logging.Logger:
    """Operational mirror of the audit trail.

    Shipped to a separate sink from application logs. The database remains the
    system of record; this exists so a SIEM can alert in near-real time.
    """
    return logging.getLogger("atlas.audit")

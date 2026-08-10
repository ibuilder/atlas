"""Idempotency-Key support for unsafe requests.

External systems retry. Networks lie about what was delivered. Without this, a
retried ``POST /payments`` charges a resident twice and the second charge looks
completely legitimate to every downstream consumer.

The contract:

* Same key, same body -> the original response is replayed verbatim.
* Same key, *different* body -> ``409``. That is a client bug, and answering it
  quietly with the first response would hide a real defect.
* Key in flight -> ``409``, so two concurrent retries cannot both execute.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

from flask import Response, current_app, g, jsonify, request

from app.errors import IdempotencyConflict, ValidationFailed
from app.logging import get_logger
from app.models.integration import IdempotencyRecord
from app.models.types import utcnow
from app.security.crypto import sha256_hex

__all__ = ["IDEMPOTENCY_HEADER", "begin_idempotent_request", "finish_idempotent_request"]

log = get_logger("api.idempotency")

IDEMPOTENCY_HEADER = "Idempotency-Key"
RETENTION_HOURS = 24
MAX_KEY_LENGTH = 255
#: How long an in-flight record may sit before a retry is allowed to proceed.
#: Covers the case where the original request's process died mid-write.
STALE_LOCK_MINUTES = 5

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def begin_idempotent_request(org_id: str) -> Response | None:
    """Claim the key, or return the stored response for a completed retry."""
    key = request.headers.get(IDEMPOTENCY_HEADER)
    if not key or request.method not in _UNSAFE_METHODS:
        return None

    key = key.strip()
    if len(key) > MAX_KEY_LENGTH:
        raise ValidationFailed(f"{IDEMPOTENCY_HEADER} must be at most {MAX_KEY_LENGTH} characters.")

    from app.extensions import db

    body_hash = sha256_hex(request.get_data(cache=True) or b"")
    now = utcnow()

    record = (
        db.session.query(IdempotencyRecord)
        .filter(
            IdempotencyRecord.org_id == org_id,
            IdempotencyRecord.idempotency_key == key,
        )
        .one_or_none()
    )

    if record is not None:
        if record.request_hash != body_hash:
            raise IdempotencyConflict(
                "This idempotency key was already used with a different request body."
            )
        if record.is_complete:
            log.info(
                "replaying idempotent response",
                extra={"event": "api.idempotent_replay", "path": request.path},
            )
            response = jsonify(record.response_body or {})
            response.status_code = record.response_status or 200
            response.headers["Idempotent-Replay"] = "true"
            return response

        locked_for = (now - record.locked_at).total_seconds() / 60 if record.locked_at else 0
        if locked_for < STALE_LOCK_MINUTES:
            raise IdempotencyConflict(
                "A request with this idempotency key is still being processed."
            )
        # The original attempt died without completing; let this one take over.
        record.locked_at = now
        db.session.flush()
        g._atlas_idempotency = record
        return None

    record = IdempotencyRecord(
        org_id=org_id,
        idempotency_key=key,
        method=request.method,
        path=request.path[:500],
        request_hash=body_hash,
        locked_at=now,
        expires_at=now + dt.timedelta(hours=RETENTION_HOURS),
    )
    db.session.add(record)
    db.session.flush()
    g._atlas_idempotency = record
    return None


def finish_idempotent_request(response: Response) -> Response:
    """Persist the outcome so a later retry can replay it."""
    record = getattr(g, "_atlas_idempotency", None)
    if record is None:
        return response

    # Only successful outcomes are recorded. Replaying a 500 would make a
    # transient failure permanent for that key.
    if not (200 <= response.status_code < 300):
        _release(record)
        return response

    from app.extensions import db

    try:
        record.response_status = response.status_code
        record.response_body = response.get_json(silent=True)
        record.completed_at = utcnow()
        db.session.commit()
    except Exception:  # pragma: no cover - never fail a good response over this
        log.exception("failed to persist idempotency record")
        db.session.rollback()
    finally:
        g._atlas_idempotency = None
    return response


def _release(record: IdempotencyRecord) -> None:
    """Drop the claim so the caller can retry with the same key."""
    from app.extensions import db

    try:
        db.session.delete(record)
        db.session.commit()
    except Exception:  # pragma: no cover
        db.session.rollback()
    finally:
        g._atlas_idempotency = None


def purge_expired(session: Any = None) -> int:
    """Remove records past their retention window."""
    from app.extensions import db

    session = session or db.session
    deleted = (
        session.query(IdempotencyRecord)
        .filter(IdempotencyRecord.expires_at < utcnow())
        .delete(synchronize_session=False)
    )
    session.commit()
    return int(deleted or 0)


def idempotent(view: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator form, for handlers outside the blanket blueprint hook."""
    from functools import wraps

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        from app.middleware import require_org_scope

        replay = begin_idempotent_request(require_org_scope())
        if replay is not None:
            return replay
        result = view(*args, **kwargs)
        if isinstance(result, Response):
            return finish_idempotent_request(result)
        return result

    return wrapper


def cleanup_interval_hours() -> int:
    return int(current_app.config.get("IDEMPOTENCY_RETENTION_HOURS", RETENTION_HOURS))

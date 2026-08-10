"""Shared REST plumbing: validation, pagination, ETags, response shaping.

Two decisions worth stating.

**Cursor pagination, not offset.** ``LIMIT 50 OFFSET 10000`` makes the database
walk ten thousand rows it will discard, and rows shifting under a paging client
cause silent duplicates and omissions. A keyset cursor over
``(created_at, id)`` is stable under concurrent writes and costs the same on
page one and page two hundred.

**Envelopes on collections only.** A single resource is returned bare, so a
client can address ``response.name``. Collections carry ``data`` plus
``page_info``, because pagination state has to live somewhere.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from flask import Response, jsonify, request
from pydantic import BaseModel, ValidationError
from sqlalchemy import Select, and_, or_
from sqlalchemy.orm import Session

from app.errors import PreconditionFailed, ValidationFailed

__all__ = [
    "Page",
    "apply_cursor",
    "decode_cursor",
    "encode_cursor",
    "etag_for",
    "paginate",
    "parse_body",
    "parse_query",
    "require_if_match",
    "respond",
    "respond_collection",
    "respond_created",
]

T = TypeVar("T", bound=BaseModel)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


# ---------------------------------------------------------------------------
# Request parsing
# ---------------------------------------------------------------------------


def parse_body(schema: type[T]) -> T:
    """Validate the JSON body against a pydantic model.

    Field-level errors are returned in the standard envelope's ``details`` so a
    client can bind them to form fields, rather than being flattened into one
    unhelpful sentence.
    """
    if not request.is_json:
        raise ValidationFailed(
            "Request body must be JSON.",
            details=[{"field": "content-type", "message": "Expected application/json."}],
        )
    try:
        payload = request.get_json(silent=False)
    except Exception as exc:  # noqa: BLE001 - werkzeug raises a variety of parse errors
        raise ValidationFailed("Request body is not valid JSON.") from exc

    if not isinstance(payload, dict):
        raise ValidationFailed("Request body must be a JSON object.")

    try:
        return schema.model_validate(payload)
    except ValidationError as exc:
        raise ValidationFailed(
            "The submitted data failed validation.", details=_pydantic_details(exc)
        ) from exc


def parse_query(schema: type[T]) -> T:
    """Validate query-string parameters against a pydantic model."""
    try:
        return schema.model_validate(dict(request.args))
    except ValidationError as exc:
        raise ValidationFailed(
            "One or more query parameters are invalid.", details=_pydantic_details(exc)
        ) from exc


def _pydantic_details(exc: ValidationError) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "body"
        details.append({"field": location, "message": error["msg"], "code": error["type"]})
    return details


# ---------------------------------------------------------------------------
# Cursor pagination
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cursor:
    created_at: dt.datetime
    id: str


def encode_cursor(created_at: dt.datetime, row_id: str) -> str:
    raw = json.dumps({"t": created_at.astimezone(dt.UTC).isoformat(), "i": row_id})
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def decode_cursor(value: str) -> Cursor:
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return Cursor(created_at=dt.datetime.fromisoformat(payload["t"]), id=payload["i"])
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise ValidationFailed(
            "The pagination cursor is not valid.",
            details=[{"field": "cursor", "message": "Cursor is malformed or truncated."}],
        ) from exc


def apply_cursor(
    stmt: Select, model: Any, cursor: str | None, *, descending: bool = True
) -> Select:
    """Add keyset predicates and ordering to a select.

    The tie-break on ``id`` matters: without it, rows sharing a millisecond
    timestamp can be skipped or repeated across page boundaries.
    """
    order = (
        (model.created_at.desc(), model.id.desc())
        if descending
        else (model.created_at.asc(), model.id.asc())
    )
    stmt = stmt.order_by(*order)

    if cursor:
        point = decode_cursor(cursor)
        if descending:
            stmt = stmt.where(
                or_(
                    model.created_at < point.created_at,
                    and_(model.created_at == point.created_at, model.id < point.id),
                )
            )
        else:
            stmt = stmt.where(
                or_(
                    model.created_at > point.created_at,
                    and_(model.created_at == point.created_at, model.id > point.id),
                )
            )
    return stmt


@dataclass
class Page:
    items: list[Any]
    next_cursor: str | None
    has_more: bool
    limit: int


def paginate(
    session: Session,
    stmt: Select,
    model: Any,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    descending: bool = True,
) -> Page:
    """Execute a keyset-paginated query."""
    effective = min(max(limit or DEFAULT_PAGE_SIZE, 1), MAX_PAGE_SIZE)
    stmt = apply_cursor(stmt, model, cursor, descending=descending)
    # Fetch one extra row to determine whether another page exists, without a
    # second COUNT query over the whole filtered set.
    rows = list(session.execute(stmt.limit(effective + 1)).scalars())

    has_more = len(rows) > effective
    items = rows[:effective]
    next_cursor = encode_cursor(items[-1].created_at, items[-1].id) if has_more and items else None
    return Page(items=items, next_cursor=next_cursor, has_more=has_more, limit=effective)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def respond(payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    body = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    response = jsonify(body)
    response.status_code = status
    for key, value in (headers or {}).items():
        response.headers[key] = value
    return response


def respond_created(payload: Any, location: str | None = None) -> Response:
    headers = {"Location": location} if location else {}
    return respond(payload, status=201, headers=headers)


def respond_collection(
    page: Page, serializer: Any, *, extra: dict[str, Any] | None = None
) -> Response:
    body: dict[str, Any] = {
        "data": [_dump(serializer, item) for item in page.items],
        "page_info": {
            "has_more": page.has_more,
            "next_cursor": page.next_cursor,
            "limit": page.limit,
        },
    }
    if extra:
        body.update(extra)
    return respond(body)


def _dump(serializer: Any, item: Any) -> Any:
    if serializer is None:
        return item
    if isinstance(serializer, type) and issubclass(serializer, BaseModel):
        return serializer.model_validate(item, from_attributes=True).model_dump(mode="json")
    return serializer(item)


def respond_no_content() -> Response:
    return Response(status=204)


# ---------------------------------------------------------------------------
# Concurrency control
# ---------------------------------------------------------------------------


def etag_for(resource: Any) -> str:
    """A weak ETag derived from identity and last-modified time.

    Cheap and sufficient: any write updates ``updated_at``, so the tag changes
    exactly when the representation does.
    """
    updated = getattr(resource, "updated_at", None)
    identity = getattr(resource, "id", "")
    stamp = updated.isoformat() if isinstance(updated, dt.datetime) else str(updated)
    digest = hashlib.sha256(f"{identity}:{stamp}".encode()).hexdigest()[:32]
    return f'W/"{digest}"'


def require_if_match(resource: Any) -> None:
    """Enforce optimistic concurrency on unsafe methods.

    When a client sends ``If-Match``, a stale tag is refused rather than
    silently overwriting a change someone else made in between. Absent the
    header the write proceeds - mandating it would break every simple client for
    a guarantee they never asked for.
    """
    provided = request.headers.get("If-Match")
    if not provided:
        return
    current = etag_for(resource)
    candidates = {value.strip() for value in provided.split(",")}
    if "*" in candidates or current in candidates:
        return
    raise PreconditionFailed(
        "The resource has changed since you last read it.",
        details=[{"field": "if-match", "message": "Re-read the resource and retry."}],
    )


def add_etag(response: Response, resource: Any) -> Response:
    response.headers["ETag"] = etag_for(resource)
    return response


def conditional_get(resource: Any) -> Response | None:
    """Return a 304 when the client's cached copy is still current."""
    tag = etag_for(resource)
    if_none_match = request.headers.get("If-None-Match", "")
    if tag in {value.strip() for value in if_none_match.split(",") if value.strip()}:
        response = Response(status=304)
        response.headers["ETag"] = tag
        return response
    return None


def sanitize_text(value: str | None, *, max_length: int = 10_000) -> str | None:
    """Strip HTML from free-text input.

    Applied at the boundary so stored text is plain. Escaping only at render
    time works right up until the day one template forgets, or the value is
    exported into a context with different rules.
    """
    if value is None:
        return None
    import bleach

    cleaned = bleach.clean(value, tags=[], attributes={}, strip=True)
    return cleaned[:max_length].strip()


def _sequence_or_empty(value: Sequence[Any] | None) -> list[Any]:
    return list(value) if value else []

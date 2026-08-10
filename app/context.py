"""Request/job-scoped ambient context.

Context variables rather than Flask's ``g`` because the same values must follow
work into Celery tasks, webhook deliveries, and background threads - none of
which have a request context. Middleware populates these; logging, auditing, and
the tenancy guard read them.

Nothing here is a security boundary on its own. :func:`current_org_id` tells you
what tenant the caller *claims*; the policy engine and the tenancy guard decide
whether that claim is honoured.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "RequestContext",
    "bind_context",
    "clear_context",
    "current_actor_id",
    "current_context",
    "current_org_id",
    "get_correlation_id",
    "new_correlation_id",
    "set_correlation_id",
    "use_context",
]


@dataclass(slots=True)
class RequestContext:
    """Everything ambient about the unit of work currently executing."""

    correlation_id: str
    request_id: str | None = None
    org_id: str | None = None
    actor_id: str | None = None
    actor_type: str = "anonymous"
    session_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    source: str = "http"  # http | task | cli | webhook | system
    extra: dict[str, Any] = field(default_factory=dict)

    def as_log_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {"correlation_id": self.correlation_id, "source": self.source}
        for key in ("request_id", "org_id", "actor_id", "session_id"):
            value = getattr(self, key)
            if value:
                fields[key] = value
        if self.actor_type != "anonymous":
            fields["actor_type"] = self.actor_type
        return fields


_context: ContextVar[RequestContext | None] = ContextVar("atlas_request_context", default=None)


def new_correlation_id() -> str:
    """Correlation IDs are UUID4 hex - opaque, unguessable, log-safe."""
    return uuid.uuid4().hex


def current_context() -> RequestContext | None:
    return _context.get()


def bind_context(ctx: RequestContext) -> Token[RequestContext | None]:
    return _context.set(ctx)


def clear_context(token: Token[RequestContext | None] | None = None) -> None:
    if token is not None:
        _context.reset(token)
    else:
        _context.set(None)


@contextlib.contextmanager
def use_context(ctx: RequestContext) -> Iterator[RequestContext]:
    """Bind a context for the duration of a block, then restore the previous one."""
    token = bind_context(ctx)
    try:
        yield ctx
    finally:
        clear_context(token)


def get_correlation_id() -> str | None:
    ctx = _context.get()
    return ctx.correlation_id if ctx else None


def set_correlation_id(correlation_id: str) -> None:
    ctx = _context.get()
    if ctx is None:
        bind_context(RequestContext(correlation_id=correlation_id))
    else:
        ctx.correlation_id = correlation_id


def current_org_id() -> str | None:
    ctx = _context.get()
    return ctx.org_id if ctx else None


def current_actor_id() -> str | None:
    ctx = _context.get()
    return ctx.actor_id if ctx else None


def system_context(source: str = "system", org_id: str | None = None) -> RequestContext:
    """Context for work with no human actor: schedulers, migrations, seeds."""
    return RequestContext(
        correlation_id=new_correlation_id(),
        org_id=org_id,
        actor_type="system",
        source=source,
    )

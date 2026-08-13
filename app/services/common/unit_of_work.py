"""Transaction boundaries and row locking.

Services own transactions; route handlers do not. A handler that commits partway
through a workflow leaves the ledger and the operational record in states that
disagree with each other, and no amount of downstream validation recovers from
that.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.errors import Conflict
from app.logging import get_logger

__all__ = ["lock_row", "supports_row_locking", "transaction"]

log = get_logger("services.uow")

T = TypeVar("T")


@contextmanager
def transaction(session: Session | None = None, *, commit: bool = True) -> Iterator[Session]:
    """Run a block as one transaction.

    Commits on clean exit, rolls back on any exception. ``commit=False`` runs
    the block inside a caller-owned transaction, which is how nested service
    calls compose without each one committing partial work.
    """
    if session is None:
        from app.extensions import current_session

        session = current_session()

    if not commit:
        yield session
        return

    try:
        yield session
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        log.warning(
            "integrity error rolled back transaction",
            extra={"event": "db.integrity_error", "detail": str(exc.orig)[:300]},
        )
        raise Conflict("The operation conflicts with an existing record.") from exc
    except Exception:
        session.rollback()
        raise


def supports_row_locking(session: Session) -> bool:
    """Whether the bound dialect implements ``SELECT ... FOR UPDATE``.

    SQLite does not, and does not need to: it serialises writers with a database
    level lock. Detecting this rather than assuming it keeps the same service
    code correct on both dialects.
    """
    bind = session.get_bind()
    return bind.dialect.name not in ("sqlite",)


def lock_row(session: Session, model: type[T], *criteria: Any, nowait: bool = False) -> T | None:
    """Fetch a single row with a write lock held for the rest of the transaction.

    Used wherever a read-modify-write must not interleave: sequence allocation,
    audit chain extension, payment application against an invoice balance.
    """
    stmt = select(model).where(*criteria).limit(1)
    if supports_row_locking(session):
        stmt = stmt.with_for_update(nowait=nowait)
    try:
        return session.execute(stmt).scalar_one_or_none()
    except OperationalError as exc:
        if nowait:
            raise Conflict("The record is being modified by another request.") from exc
        raise

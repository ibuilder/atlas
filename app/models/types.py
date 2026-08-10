"""Portable column types.

PostgreSQL is the system of record. SQLite exists so the test suite and a
zero-dependency dev environment work anywhere. These type decorators make the
two dialects behave *identically at the Python boundary*, which is the only
thing that keeps a portable test suite honest.

The interesting one is :class:`Money`. On PostgreSQL it is a real ``NUMERIC``.
On SQLite it is stored as a scaled integer, because SQLite has no decimal type
and SQLAlchemy's fallback round-trips through ``float`` - which means
``0.10 + 0.20`` sums to ``0.30000000000000004`` in a ledger test. Scaling to
integer minor units keeps ``SUM``, ordering, and comparison exact on both
dialects, and the result processor hands back a ``Decimal`` either way.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
import enum
import secrets
import time
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import CHAR, BigInteger, DateTime, Dialect, Text, TypeDecorator
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.types import JSON, Numeric

__all__ = [
    "MONEY_SCALE",
    "EncryptedText",
    "GUID",
    "JSONType",
    "Money",
    "UTCDateTime",
    "enum_column",
    "quantize_money",
    "utcnow",
    "uuid7",
    "uuid7_str",
]

MONEY_SCALE = 4
MONEY_PRECISION = 20
_MONEY_QUANT = Decimal(1).scaleb(-MONEY_SCALE)
_MONEY_FACTOR = 10**MONEY_SCALE


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


def uuid7() -> uuid.UUID:
    """Generate an RFC 9562 version 7 (time-ordered) UUID.

    Time-ordered rather than random because these are primary keys: a v4 key
    scatters inserts across the whole B-tree, which on a busy ledger table costs
    real write amplification. v7 keeps inserts at the right edge of the index
    while remaining globally unique and non-sequential enough not to leak
    row counts.

    Layout: 48-bit millisecond timestamp | version 7 | 12 random | variant |
    62 random.
    """
    unix_ms = int(time.time() * 1000) & 0xFFFF_FFFF_FFFF
    value = unix_ms << 80
    value |= 0x7 << 76  # version
    value |= secrets.randbits(12) << 64  # rand_a
    value |= 0b10 << 62  # RFC 4122 variant
    value |= secrets.randbits(62)  # rand_b
    return uuid.UUID(int=value)


def uuid7_str() -> str:
    return str(uuid7())


class GUID(TypeDecorator[str]):
    """UUID column that is native on PostgreSQL and ``CHAR(36)`` elsewhere.

    Always presents as ``str`` in Python. Uniform string identifiers avoid an
    entire family of bugs where a ``UUID`` object and its string form fail to
    compare equal across a cache, a context variable, or a JSON boundary.
    """

    impl = CHAR(36)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=False))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return str(value)
        # Round-tripping through UUID() validates the input, so a malformed
        # identifier fails at the boundary instead of becoming a silent no-match.
        return str(uuid.UUID(str(value)))

    def process_result_value(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return str(value)


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def utcnow() -> dt.datetime:
    """Timezone-aware current time. The only sanctioned source of 'now'."""
    return dt.datetime.now(dt.UTC)


class UTCDateTime(TypeDecorator[dt.datetime]):
    """Timezone-aware datetime, normalised to UTC on the way in and out.

    Naive datetimes are rejected rather than assumed to be UTC. An assumption is
    how a lease ends a day early in one timezone and a day late in another.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> dt.datetime | None:
        if value is None:
            return None
        if not isinstance(value, dt.datetime):
            raise TypeError(f"Expected datetime, received {type(value).__name__}")
        if value.tzinfo is None:
            raise ValueError(
                "Naive datetime rejected; use app.models.types.utcnow() or attach a tzinfo."
            )
        return value.astimezone(dt.UTC)

    def process_result_value(self, value: Any, dialect: Dialect) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # SQLite discards offsets; everything we store is UTC by construction.
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------


def quantize_money(value: Decimal | int | float | str) -> Decimal:
    """Coerce to the canonical money precision using banker-safe rounding.

    ``ROUND_HALF_UP`` rather than Python's default ``ROUND_HALF_EVEN``: it is
    what accounting conventions and every tenant's calculator expect.
    """
    if isinstance(value, Decimal):
        dec = value
    else:
        dec = Decimal(str(value))
    return dec.quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)


class Money(TypeDecorator[Decimal]):
    """Exact decimal money on PostgreSQL, exact scaled integers elsewhere."""

    impl = Numeric(MONEY_PRECISION, MONEY_SCALE)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Numeric(MONEY_PRECISION, MONEY_SCALE, asdecimal=True))
        return dialect.type_descriptor(BigInteger())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        dec = quantize_money(value)
        if dialect.name == "postgresql":
            return dec
        return int(dec.scaleb(MONEY_SCALE))

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return quantize_money(value)
        # SUM()/AVG() over the scaled integer column arrives here too, which is
        # exactly why the scaling has to be reversible without rounding error.
        return quantize_money(Decimal(int(value)) / _MONEY_FACTOR)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


class JSONType(TypeDecorator[Any]):
    """``JSONB`` on PostgreSQL (indexable, binary) and ``JSON`` elsewhere."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB(astext_type=Text()))
        return dialect.type_descriptor(JSON())


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------


class EncryptedText(TypeDecorator[str]):
    """Application-level encryption for a single column.

    Used for the handful of fields that are sensitive even to a database
    operator: MFA seeds, bank account fragments, integration credentials.
    Ciphertext is opaque, so these columns cannot be searched or indexed by
    value - that trade-off is the point, and it is why this is applied
    selectively rather than everywhere.
    """

    impl = Text
    cache_ok = False  # cipher identity is resolved per app, not per statement

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        from app.security.keyring import get_field_cipher

        return get_field_cipher().encrypt(str(value))

    def process_result_value(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        from app.security.keyring import get_field_cipher

        return get_field_cipher().decrypt(value)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


def enum_column(enum_cls: type[enum.Enum], *, name: str | None = None) -> SAEnum:
    """Portable enum column.

    ``native_enum=False`` deliberately: PostgreSQL native enums require a
    migration with an exclusive lock to add a value, which turns "add a work
    order status" into a maintenance window. A ``VARCHAR`` plus a check
    constraint gives the same integrity with an online migration path.
    """
    return SAEnum(
        enum_cls,
        name=name or f"{enum_cls.__name__.lower()}_enum",
        native_enum=False,
        length=64,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )

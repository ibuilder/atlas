"""Shared schema base classes and primitives.

Request models set ``extra="forbid"``. A misspelled field is then a loud 422
rather than a silently ignored key - which is the difference between a client
discovering their bug in five minutes and discovering it in production when
someone notices the discount was never applied.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

__all__ = [
    "AtlasRequest",
    "AtlasResponse",
    "Cursor",
    "Email",
    "ListQuery",
    "Money",
    "PageInfo",
    "Paginated",
    "ShortText",
    "Slug",
    "Text",
    "UuidStr",
]

T = TypeVar("T")

UuidStr = Annotated[str, StringConstraints(min_length=32, max_length=36)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=20_000)]
Email = Annotated[
    str, StringConstraints(strip_whitespace=True, to_lower=True, min_length=3, max_length=320)
]
Slug = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, to_lower=True, min_length=2, max_length=63, pattern=r"^[a-z0-9-]+$"
    ),
]
Cursor = Annotated[str, StringConstraints(max_length=512)]
#: Money crosses the API boundary as a JSON string, never a float. A float
#: cannot represent 0.1 exactly, and an amount that survives four hops and one
#: rounding is not an amount you can reconcile.
Money = Decimal


class AtlasRequest(BaseModel):
    """Base for inbound payloads: strict, trimmed, no surprise fields."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
        populate_by_name=True,
    )


class AtlasResponse(BaseModel):
    """Base for outbound payloads, built directly from ORM instances."""

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        ser_json_timedelta="iso8601",
    )


class PageInfo(AtlasResponse):
    has_more: bool = False
    next_cursor: str | None = None
    limit: int = 50


class Paginated(AtlasResponse, Generic[T]):
    data: list[T] = Field(default_factory=list)
    page_info: PageInfo = Field(default_factory=PageInfo)


class ListQuery(AtlasRequest):
    """Common collection parameters."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    cursor: Cursor | None = None
    limit: int = Field(default=50, ge=1, le=200)
    q: Annotated[str, StringConstraints(max_length=200)] | None = None
    sort: Annotated[str, StringConstraints(max_length=60)] | None = None

    @field_validator("limit", mode="before")
    @classmethod
    def _coerce_limit(cls, value: Any) -> Any:
        if value in ("", None):
            return 50
        return value


class TimestampFields(AtlasResponse):
    id: str
    created_at: dt.datetime
    updated_at: dt.datetime


class ErrorDetail(AtlasResponse):
    field: str | None = None
    message: str
    code: str | None = None


class ErrorBody(AtlasResponse):
    code: str
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)
    correlation_id: str | None = None


class ErrorEnvelope(AtlasResponse):
    """The shape every error response takes. Part of the public contract."""

    error: ErrorBody


class AddressFields(AtlasRequest):
    address_line1: ShortText | None = None
    address_line2: ShortText | None = None
    city: ShortText | None = None
    region: ShortText | None = None
    postal_code: Annotated[str, StringConstraints(max_length=20)] | None = None
    country: Annotated[str, StringConstraints(min_length=2, max_length=2, to_upper=True)] = "US"

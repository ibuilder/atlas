"""Organization, portfolio, property, unit, and owner contracts.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.models.org import OrganizationStatus, OwnerType, PropertyStatus, PropertyType, UnitStatus
from app.schemas.common import (
    AddressFields,
    AtlasRequest,
    AtlasResponse,
    Email,
    ListQuery,
    ShortText,
    Text,
)

__all__ = [
    "OrganizationOut",
    "OwnerCreate",
    "OwnerOut",
    "PortfolioCreate",
    "PortfolioOut",
    "PropertyCreate",
    "PropertyListQuery",
    "PropertyOut",
    "PropertyUpdate",
    "UnitCreate",
    "UnitListQuery",
    "UnitOut",
    "UnitUpdate",
]

Code = Annotated[
    str,
    StringConstraints(strip_whitespace=True, to_upper=True, min_length=1, max_length=40),
]


class OrganizationOut(AtlasResponse):
    id: str
    name: str
    legal_name: str | None = None
    slug: str
    status: OrganizationStatus
    timezone: str
    currency: str
    fiscal_year_start_month: int
    created_at: dt.datetime


class PortfolioCreate(AtlasRequest):
    name: ShortText
    code: Code
    description: Text | None = None


class PortfolioOut(AtlasResponse):
    id: str
    name: str
    code: str
    description: str | None = None
    is_active: bool
    created_at: dt.datetime


class PropertyCreate(AddressFields):
    name: ShortText
    code: Code
    property_type: PropertyType = PropertyType.RESIDENTIAL_MULTI
    portfolio_id: str | None = None
    address_line1: ShortText
    city: ShortText
    region: ShortText
    postal_code: Annotated[str, StringConstraints(min_length=1, max_length=20)]
    year_built: int | None = Field(default=None, ge=1600, le=2200)
    total_square_feet: int | None = Field(default=None, ge=0)
    acquisition_date: dt.date | None = None
    acquisition_price: Decimal | None = Field(default=None, ge=0)
    tax_parcel_id: Annotated[str, StringConstraints(max_length=64)] | None = None
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def _coordinates_come_as_a_pair(self) -> PropertyCreate:
        # One coordinate without the other is not a location, it is a data
        # entry error that will silently place the property on the equator.
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be provided together")
        return self


class PropertyUpdate(AtlasRequest):
    name: ShortText | None = None
    portfolio_id: str | None = None
    property_type: PropertyType | None = None
    status: PropertyStatus | None = None
    address_line1: ShortText | None = None
    address_line2: ShortText | None = None
    city: ShortText | None = None
    region: ShortText | None = None
    postal_code: Annotated[str, StringConstraints(max_length=20)] | None = None
    year_built: int | None = Field(default=None, ge=1600, le=2200)
    total_square_feet: int | None = Field(default=None, ge=0)


class PropertyOut(AtlasResponse):
    id: str
    name: str
    code: str
    property_type: PropertyType
    status: PropertyStatus
    portfolio_id: str | None = None
    address_line1: str
    address_line2: str | None = None
    city: str
    region: str
    postal_code: str
    country: str
    year_built: int | None = None
    total_units: int
    created_at: dt.datetime
    updated_at: dt.datetime


class PropertyListQuery(ListQuery):
    status: PropertyStatus | None = None
    property_type: PropertyType | None = None
    portfolio_id: str | None = None


class UnitCreate(AtlasRequest):
    property_id: str
    unit_number: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)
    ]
    building_id: str | None = None
    unit_type: ShortText | None = None
    floor: int | None = Field(default=None, ge=-10, le=200)
    bedrooms: int | None = Field(default=None, ge=0, le=20)
    bathrooms: Decimal | None = Field(default=None, ge=0, le=20)
    square_feet: int | None = Field(default=None, ge=0)
    market_rent: Decimal | None = Field(default=None, ge=0)
    deposit_amount: Decimal | None = Field(default=None, ge=0)
    status: UnitStatus = UnitStatus.VACANT_NOT_READY
    amenities: list[str] = Field(default_factory=list)

    @field_validator("amenities")
    @classmethod
    def _cap_amenities(cls, value: list[str]) -> list[str]:
        if len(value) > 50:
            raise ValueError("at most 50 amenities")
        return [item.strip()[:60] for item in value if item.strip()]


class UnitUpdate(AtlasRequest):
    unit_type: ShortText | None = None
    floor: int | None = Field(default=None, ge=-10, le=200)
    bedrooms: int | None = Field(default=None, ge=0, le=20)
    bathrooms: Decimal | None = Field(default=None, ge=0, le=20)
    square_feet: int | None = Field(default=None, ge=0)
    market_rent: Decimal | None = Field(default=None, ge=0)
    deposit_amount: Decimal | None = Field(default=None, ge=0)
    status: UnitStatus | None = None
    is_listed: bool | None = None
    available_from: dt.date | None = None


class UnitOut(AtlasResponse):
    id: str
    property_id: str
    building_id: str | None = None
    unit_number: str
    unit_type: str | None = None
    floor: int | None = None
    bedrooms: int | None = None
    bathrooms: Decimal | None = None
    square_feet: int | None = None
    status: UnitStatus
    market_rent: Decimal | None = None
    is_listed: bool
    available_from: dt.date | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class UnitListQuery(ListQuery):
    property_id: str | None = None
    status: UnitStatus | None = None
    is_listed: bool | None = None
    bedrooms: int | None = None


class OwnerCreate(AddressFields):
    name: ShortText
    code: Code
    owner_type: OwnerType = OwnerType.INDIVIDUAL
    email: Email | None = None
    phone: Annotated[str, StringConstraints(max_length=40)] | None = None
    is_1099_required: bool = True
    reserve_amount: Decimal = Field(default=Decimal("0"), ge=0)
    notes: Text | None = None


class OwnerOut(AtlasResponse):
    id: str
    code: str
    name: str
    owner_type: OwnerType
    email: str | None = None
    phone: str | None = None
    is_1099_required: bool
    reserve_amount: Decimal
    portal_enabled: bool
    created_at: dt.datetime

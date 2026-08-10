"""Organizations, portfolios, properties, buildings, units, and ownership.

The spine of the canonical model. Everything else in Atlas hangs off this
hierarchy:

``Organization -> Portfolio -> Property -> Building -> Unit``

Ownership is modelled as a *temporal stake* rather than a column on the
property, because properties change hands, are held in fractions, and an owner
statement for March must reflect who owned the asset in March - not who owns it
today.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import builtins
import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, SoftDeleteMixin, TenantModel
from app.models.types import GUID, EncryptedText, JSONType, Money, enum_column

if TYPE_CHECKING:
    from app.models.accounting import BankAccount
    from app.models.leasing import Lease

__all__ = [
    "Building",
    "Organization",
    "OrganizationStatus",
    "OwnerEntity",
    "OwnerType",
    "OwnershipStake",
    "Portfolio",
    "Property",
    "PropertyStatus",
    "PropertyType",
    "Unit",
    "UnitStatus",
]


class OrganizationStatus(StrEnum):
    ONBOARDING = "onboarding"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class PropertyType(StrEnum):
    RESIDENTIAL_SINGLE = "residential_single"
    RESIDENTIAL_MULTI = "residential_multi"
    CONDOMINIUM = "condominium"
    HOA = "hoa"
    COMMERCIAL_OFFICE = "commercial_office"
    COMMERCIAL_RETAIL = "commercial_retail"
    INDUSTRIAL = "industrial"
    MIXED_USE = "mixed_use"
    STORAGE = "storage"
    LAND = "land"


class PropertyStatus(StrEnum):
    ACTIVE = "active"
    ONBOARDING = "onboarding"
    INACTIVE = "inactive"
    DISPOSED = "disposed"


class UnitStatus(StrEnum):
    VACANT_READY = "vacant_ready"
    VACANT_NOT_READY = "vacant_not_ready"
    OCCUPIED = "occupied"
    NOTICE = "notice"
    TURN = "turn"
    DOWN = "down"
    OFF_MARKET = "off_market"


class OwnerType(StrEnum):
    INDIVIDUAL = "individual"
    COMPANY = "company"
    TRUST = "trust"
    PARTNERSHIP = "partnership"
    ASSOCIATION = "association"


class Organization(BaseModel, SoftDeleteMixin):
    """A tenant. The isolation boundary for every other row in the system."""

    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_organizations_slug"),
        CheckConstraint(
            "fiscal_year_start_month BETWEEN 1 AND 12",
            name="fiscal_year_start_month_range",
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    legal_name: Mapped[str | None] = mapped_column(String(255))
    #: URL- and subdomain-safe identifier. Immutable once issued: it appears in
    #: object-storage prefixes and external integration configuration.
    slug: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    status: Mapped[OrganizationStatus] = mapped_column(
        enum_column(OrganizationStatus), nullable=False, default=OrganizationStatus.ONBOARDING
    )

    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/New_York")
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="en_US")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    fiscal_year_start_month: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    contact_email: Mapped[str | None] = mapped_column(String(320))
    contact_phone: Mapped[str | None] = mapped_column(String(40))
    website: Mapped[str | None] = mapped_column(String(255))

    address_line1: Mapped[str | None] = mapped_column(String(255))
    address_line2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    region: Mapped[str | None] = mapped_column(String(120))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="US")

    tax_id: Mapped[str | None] = mapped_column(EncryptedText)

    #: Where this tenant's data must live. Drives storage bucket selection and,
    #: for single-tenant deployments, which cluster serves them.
    data_region: Mapped[str] = mapped_column(String(32), nullable=False, default="us-east-1")
    #: Per-tenant object-storage prefix, so one tenant's documents can never be
    #: addressed by another's key space.
    storage_prefix: Mapped[str | None] = mapped_column(String(128))

    #: Tenant-level configuration: approval thresholds, notice periods, branding.
    settings: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    #: Feature flags evaluated per tenant, so a capability can be piloted safely.
    feature_flags: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    portfolios: Mapped[list[Portfolio]] = relationship(
        back_populates="organization", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def is_active(self) -> bool:
        return self.status == OrganizationStatus.ACTIVE and not self.is_deleted

    def setting(self, key: str, default: Any = None) -> Any:
        return (self.settings or {}).get(key, default)

    def feature_enabled(self, name: str, default: bool = False) -> bool:
        return bool((self.feature_flags or {}).get(name, default))


class Portfolio(TenantModel, SoftDeleteMixin):
    """A grouping of properties, and the unit of delegated authority.

    Role assignments are scoped to a portfolio, which is how a regional manager
    gets full authority over their twelve properties and none anywhere else.
    """

    __tablename__ = "portfolios"
    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_portfolios_org_code"),
        Index("ix_portfolios_org_created", "org_id", "created_at"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    organization: Mapped[Organization] = relationship(back_populates="portfolios")
    properties: Mapped[list[Property]] = relationship(
        back_populates="portfolio", passive_deletes=True
    )


class Property(TenantModel, SoftDeleteMixin):
    """A physical asset under management."""

    __tablename__ = "properties"
    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_properties_org_code"),
        CheckConstraint(
            "year_built IS NULL OR year_built BETWEEN 1600 AND 2200", name="year_built_range"
        ),
        Index("ix_properties_org_status", "org_id", "status"),
        Index("ix_properties_org_created", "org_id", "created_at"),
    )

    portfolio_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("portfolios.id", ondelete="SET NULL"), index=True
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    property_type: Mapped[PropertyType] = mapped_column(
        enum_column(PropertyType), nullable=False, default=PropertyType.RESIDENTIAL_MULTI
    )
    status: Mapped[PropertyStatus] = mapped_column(
        enum_column(PropertyStatus), nullable=False, default=PropertyStatus.ACTIVE, index=True
    )

    address_line1: Mapped[str] = mapped_column(String(255), nullable=False)
    address_line2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str] = mapped_column(String(120), nullable=False)
    region: Mapped[str] = mapped_column(String(120), nullable=False)
    postal_code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="US")
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))

    year_built: Mapped[int | None] = mapped_column(Integer)
    total_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_square_feet: Mapped[int | None] = mapped_column(Integer)
    acquisition_date: Mapped[dt.date | None] = mapped_column(Date)
    acquisition_price: Mapped[Decimal | None] = mapped_column(Money)
    tax_parcel_id: Mapped[str | None] = mapped_column(String(64))

    #: Operating and trust bank accounts. Trust-account rules make these
    #: deliberately property-level rather than organization-level.
    #:
    #: Intentionally *not* database foreign keys. ``bank_accounts.property_id``
    #: already points the other way, and a constraint in both directions makes
    #: the two tables mutually dependent - which leaves no valid creation order
    #: for the schema and forces every migration touching either table into
    #: deferred-constraint territory. Referential integrity here is enforced by
    #: the service layer, which has to validate trust/operating classification
    #: anyway.
    operating_bank_account_id: Mapped[str | None] = mapped_column(GUID, index=True)
    trust_bank_account_id: Mapped[str | None] = mapped_column(GUID, index=True)

    settings: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    portfolio: Mapped[Portfolio | None] = relationship(back_populates="properties")
    buildings: Mapped[list[Building]] = relationship(
        back_populates="property", cascade="all, delete-orphan", passive_deletes=True
    )
    units: Mapped[list[Unit]] = relationship(back_populates="property", passive_deletes=True)
    ownership_stakes: Mapped[list[OwnershipStake]] = relationship(
        back_populates="property", cascade="all, delete-orphan", passive_deletes=True
    )
    # Joins declared explicitly because the columns above carry no database
    # foreign key. `viewonly` keeps the ORM from trying to manage the far side.
    operating_bank_account: Mapped[BankAccount | None] = relationship(
        primaryjoin="Property.operating_bank_account_id == BankAccount.id",
        foreign_keys=lambda: [Property.operating_bank_account_id],
        viewonly=True,
    )
    trust_bank_account: Mapped[BankAccount | None] = relationship(
        primaryjoin="Property.trust_bank_account_id == BankAccount.id",
        foreign_keys=lambda: [Property.trust_bank_account_id],
        viewonly=True,
    )

    @property
    def display_address(self) -> str:
        parts = [
            self.address_line1,
            self.address_line2,
            f"{self.city}, {self.region} {self.postal_code}",
        ]
        return ", ".join(part for part in parts if part)


class Building(TenantModel, SoftDeleteMixin):
    """A structure within a property. Multi-building sites are the norm, not the exception."""

    __tablename__ = "buildings"
    __table_args__ = (
        UniqueConstraint("property_id", "code", name="uq_buildings_property_code"),
        Index("ix_buildings_org_created", "org_id", "created_at"),
    )

    property_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    floors: Mapped[int | None] = mapped_column(Integer)
    year_built: Mapped[int | None] = mapped_column(Integer)
    square_feet: Mapped[int | None] = mapped_column(Integer)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    property: Mapped[Property] = relationship(back_populates="buildings")
    units: Mapped[list[Unit]] = relationship(back_populates="building", passive_deletes=True)


class Unit(TenantModel, SoftDeleteMixin):
    """A leasable space. The atom of occupancy, revenue, and maintenance."""

    __tablename__ = "units"
    __table_args__ = (
        UniqueConstraint("property_id", "unit_number", name="uq_units_property_number"),
        CheckConstraint("bedrooms IS NULL OR bedrooms >= 0", name="bedrooms_non_negative"),
        CheckConstraint("market_rent IS NULL OR market_rent >= 0", name="market_rent_non_negative"),
        Index("ix_units_org_status", "org_id", "status"),
        Index("ix_units_org_created", "org_id", "created_at"),
    )

    property_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="CASCADE"), nullable=False, index=True
    )
    building_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("buildings.id", ondelete="SET NULL"), index=True
    )

    unit_number: Mapped[str] = mapped_column(String(40), nullable=False)
    unit_type: Mapped[str | None] = mapped_column(String(60))
    floor: Mapped[int | None] = mapped_column(Integer)
    bedrooms: Mapped[int | None] = mapped_column(Integer)
    bathrooms: Mapped[Decimal | None] = mapped_column(Numeric(4, 1))
    square_feet: Mapped[int | None] = mapped_column(Integer)

    status: Mapped[UnitStatus] = mapped_column(
        enum_column(UnitStatus), nullable=False, default=UnitStatus.VACANT_NOT_READY, index=True
    )
    market_rent: Mapped[Decimal | None] = mapped_column(Money)
    deposit_amount: Mapped[Decimal | None] = mapped_column(Money)
    available_from: Mapped[dt.date | None] = mapped_column(Date)
    is_listed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    amenities: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    property: Mapped[Property] = relationship(back_populates="units")
    building: Mapped[Building | None] = relationship(back_populates="units")
    leases: Mapped[list[Lease]] = relationship(back_populates="unit", passive_deletes=True)

    # `property` above shadows the builtin for the rest of this class body, so
    # computed attributes here must reference it explicitly. Renaming the
    # relationship would read worse everywhere it is used (`unit.property`).
    @builtins.property
    def is_occupied(self) -> bool:
        return self.status in (UnitStatus.OCCUPIED, UnitStatus.NOTICE)


class OwnerEntity(TenantModel, SoftDeleteMixin):
    """An owner, investor, or association that holds economic interest."""

    __tablename__ = "owner_entities"
    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_owner_entities_org_code"),
        Index("ix_owner_entities_org_created", "org_id", "created_at"),
    )

    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_type: Mapped[OwnerType] = mapped_column(
        enum_column(OwnerType), nullable=False, default=OwnerType.INDIVIDUAL
    )

    email: Mapped[str | None] = mapped_column(String(320), index=True)
    phone: Mapped[str | None] = mapped_column(String(40))
    #: Encrypted: an SSN or EIN is exactly the field a database operator should
    #: not be able to read casually.
    tax_id: Mapped[str | None] = mapped_column(EncryptedText)
    tax_id_last4: Mapped[str | None] = mapped_column(String(4))

    address_line1: Mapped[str | None] = mapped_column(String(255))
    address_line2: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    region: Mapped[str | None] = mapped_column(String(120))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    country: Mapped[str] = mapped_column(String(2), nullable=False, default="US")

    is_1099_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    distribution_method: Mapped[str] = mapped_column(String(20), nullable=False, default="check")
    #: Reserve the owner must keep on hand before distributions are calculated.
    reserve_amount: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    statement_delivery: Mapped[str] = mapped_column(String(20), nullable=False, default="email")
    portal_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(Text)

    stakes: Mapped[list[OwnershipStake]] = relationship(
        back_populates="owner", cascade="all, delete-orphan", passive_deletes=True
    )


class OwnershipStake(TenantModel):
    """A time-bounded ownership percentage in a property.

    Kept append-only in spirit: transferring ownership closes the outgoing stake
    with an ``effective_to`` and opens a new one, so a statement generated for
    any historical period resolves the owners who actually held the asset then.
    """

    __tablename__ = "ownership_stakes"
    __table_args__ = (
        CheckConstraint("percentage > 0 AND percentage <= 100", name="percentage_range"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to >= effective_from", name="effective_range"
        ),
        Index("ix_ownership_stakes_property_effective", "property_id", "effective_from"),
        Index("ix_ownership_stakes_org_created", "org_id", "created_at"),
    )

    property_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Named ``owner_entity_id`` to match every other reference to an owner in
    # the schema. Consistent foreign key naming is what lets the policy engine
    # resolve a resource's owner scope by convention rather than by a lookup
    # table of special cases.
    owner_entity_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("owner_entities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    percentage: Mapped[Decimal] = mapped_column(Numeric(7, 4), nullable=False)
    effective_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[dt.date | None] = mapped_column(Date)
    is_primary_contact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    property: Mapped[Property] = relationship(back_populates="ownership_stakes")
    owner: Mapped[OwnerEntity] = relationship(back_populates="stakes")

    def covers(self, on_date: dt.date) -> bool:
        if on_date < self.effective_from:
            return False
        return self.effective_to is None or on_date <= self.effective_to

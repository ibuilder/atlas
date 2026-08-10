"""Asset intelligence: spaces, equipment, service history, warranties.

The strategic differentiator, and the one that only pays off if the data model
is right from the beginning. A boiler is not a text field on a unit. It has a
manufacturer, a serial number, a warranty window, a service history, an expected
life, and a replacement cost - and when it fails at 2am, the useful question is
"is this still under warranty and who serviced it last?", not "which unit is it
in?".

:class:`Space` exists to give geometry somewhere to attach later. Rooms, risers,
and mechanical areas modelled now mean a future BIM or scan import has anchors
to bind to, instead of requiring a migration of every asset record.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
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

from app.models.base import SoftDeleteMixin, TenantModel
from app.models.types import GUID, JSONType, Money, enum_column, utcnow

__all__ = [
    "Asset",
    "AssetCategory",
    "AssetCriticality",
    "AssetServiceEvent",
    "AssetStatus",
    "ServiceEventType",
    "Space",
    "SpaceKind",
    "Warranty",
]


class AssetCategory(StrEnum):
    HVAC = "hvac"
    PLUMBING = "plumbing"
    ELECTRICAL = "electrical"
    APPLIANCE = "appliance"
    ROOF = "roof"
    STRUCTURE = "structure"
    ELEVATOR = "elevator"
    FIRE_SAFETY = "fire_safety"
    SECURITY = "security"
    LANDSCAPE = "landscape"
    POOL = "pool"
    LAUNDRY = "laundry"
    OTHER = "other"


class AssetStatus(StrEnum):
    ACTIVE = "active"
    NEEDS_REPAIR = "needs_repair"
    OUT_OF_SERVICE = "out_of_service"
    SCHEDULED_REPLACEMENT = "scheduled_replacement"
    RETIRED = "retired"


class AssetCriticality(StrEnum):
    """How badly a failure hurts. Drives PM frequency and dispatch priority."""

    CRITICAL = "critical"  # habitability or life safety
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ServiceEventType(StrEnum):
    INSTALLATION = "installation"
    INSPECTION = "inspection"
    PREVENTIVE = "preventive"
    REPAIR = "repair"
    REPLACEMENT = "replacement"
    METER_READING = "meter_reading"
    DECOMMISSION = "decommission"


class SpaceKind(StrEnum):
    ROOM = "room"
    COMMON_AREA = "common_area"
    MECHANICAL = "mechanical"
    CIRCULATION = "circulation"
    EXTERIOR = "exterior"
    PARKING = "parking"
    STORAGE = "storage"


class Space(TenantModel, SoftDeleteMixin):
    """A physical space, nestable to arbitrary depth."""

    __tablename__ = "spaces"
    __table_args__ = (
        UniqueConstraint("property_id", "code", name="uq_spaces_property_code"),
        Index("ix_spaces_org_created", "org_id", "created_at"),
    )

    property_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="CASCADE"), nullable=False, index=True
    )
    building_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("buildings.id", ondelete="CASCADE"), index=True
    )
    unit_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("units.id", ondelete="CASCADE"), index=True
    )
    parent_space_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("spaces.id", ondelete="CASCADE"), index=True
    )

    code: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[SpaceKind] = mapped_column(
        enum_column(SpaceKind), nullable=False, default=SpaceKind.ROOM
    )
    level: Mapped[int | None] = mapped_column(Integer)
    area_sqft: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))

    #: Reserved for future geometry integration - an IFC GUID, a room GUID from
    #: a scan, or a coordinate reference. Deliberately opaque for now.
    external_reference: Mapped[str | None] = mapped_column(String(120), index=True)
    geometry_ref: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    parent: Mapped[Space | None] = relationship(remote_side="Space.id")


class Asset(TenantModel, SoftDeleteMixin):
    """A piece of equipment or a building component."""

    __tablename__ = "assets"
    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_assets_org_code"),
        Index("ix_assets_org_category", "org_id", "category"),
        Index("ix_assets_property", "org_id", "property_id", "status"),
        Index("ix_assets_replacement", "org_id", "expected_replacement_on"),
        Index("ix_assets_org_created", "org_id", "created_at"),
    )

    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    category: Mapped[AssetCategory] = mapped_column(
        enum_column(AssetCategory), nullable=False, default=AssetCategory.OTHER, index=True
    )
    status: Mapped[AssetStatus] = mapped_column(
        enum_column(AssetStatus), nullable=False, default=AssetStatus.ACTIVE, index=True
    )
    criticality: Mapped[AssetCriticality] = mapped_column(
        enum_column(AssetCriticality), nullable=False, default=AssetCriticality.MEDIUM
    )

    property_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="CASCADE"), nullable=False, index=True
    )
    building_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("buildings.id", ondelete="SET NULL"), index=True
    )
    unit_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("units.id", ondelete="SET NULL"), index=True
    )
    space_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("spaces.id", ondelete="SET NULL"), index=True
    )
    #: Component hierarchy: a compressor belongs to a rooftop unit.
    parent_asset_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("assets.id", ondelete="SET NULL"), index=True
    )

    manufacturer: Mapped[str | None] = mapped_column(String(120))
    model_number: Mapped[str | None] = mapped_column(String(120))
    serial_number: Mapped[str | None] = mapped_column(String(120), index=True)
    location_note: Mapped[str | None] = mapped_column(String(255))

    installed_on: Mapped[dt.date | None] = mapped_column(Date)
    purchase_price: Mapped[Decimal | None] = mapped_column(Money)
    expected_life_years: Mapped[int | None] = mapped_column(Integer)
    #: Derived from install date plus expected life, but stored so capital
    #: planning can query a horizon without recomputing across the portfolio.
    expected_replacement_on: Mapped[dt.date | None] = mapped_column(Date, index=True)
    replacement_cost: Mapped[Decimal | None] = mapped_column(Money)
    condition_score: Mapped[int | None] = mapped_column(Integer)

    last_serviced_on: Mapped[dt.date | None] = mapped_column(Date)
    service_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lifetime_service_cost: Mapped[Decimal] = mapped_column(
        Money, nullable=False, default=Decimal("0")
    )

    barcode: Mapped[str | None] = mapped_column(String(80), index=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(Text)

    service_events: Mapped[list[AssetServiceEvent]] = relationship(
        back_populates="asset",
        cascade="all, delete-orphan",
        order_by="AssetServiceEvent.performed_on",
        passive_deletes=True,
    )
    warranties: Mapped[list[Warranty]] = relationship(
        back_populates="asset", cascade="all, delete-orphan", passive_deletes=True
    )

    def compute_replacement_date(self) -> dt.date | None:
        if self.installed_on is None or not self.expected_life_years:
            return None
        try:
            return self.installed_on.replace(year=self.installed_on.year + self.expected_life_years)
        except ValueError:  # 29 February on a non-leap target year
            return self.installed_on.replace(
                year=self.installed_on.year + self.expected_life_years, day=28
            )

    def active_warranty(self, on_date: dt.date | None = None) -> Warranty | None:
        """The warranty covering ``on_date``, if any.

        Checked before dispatching a paid repair - the single highest-value
        question the asset registry answers.
        """
        reference = on_date or utcnow().date()
        for warranty in self.warranties:
            if warranty.covers(reference):
                return warranty
        return None

    @property
    def is_past_expected_life(self) -> bool:
        target = self.expected_replacement_on or self.compute_replacement_date()
        return target is not None and target <= utcnow().date()


class AssetServiceEvent(TenantModel):
    """Something that happened to an asset."""

    __tablename__ = "asset_service_events"
    __table_args__ = (
        Index("ix_asset_service_events_asset_date", "asset_id", "performed_on"),
        Index("ix_asset_service_events_org_created", "org_id", "created_at"),
    )

    asset_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[ServiceEventType] = mapped_column(
        enum_column(ServiceEventType), nullable=False
    )
    performed_on: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)

    work_order_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("work_orders.id", ondelete="SET NULL"), index=True
    )
    vendor_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="SET NULL"), index=True
    )
    performed_by_id: Mapped[str | None] = mapped_column(GUID)

    cost: Mapped[Decimal] = mapped_column(Money, nullable=False, default=Decimal("0"))
    was_under_warranty: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: For metered equipment: run hours, gallons, cycles.
    meter_reading: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    meter_unit: Mapped[str | None] = mapped_column(String(20))
    condition_after: Mapped[int | None] = mapped_column(Integer)
    parts_replaced: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    notes: Mapped[str | None] = mapped_column(Text)

    asset: Mapped[Asset] = relationship(back_populates="service_events")


class Warranty(TenantModel):
    """Warranty coverage for an asset."""

    __tablename__ = "warranties"
    __table_args__ = (
        Index("ix_warranties_expiry", "org_id", "expires_on"),
        Index("ix_warranties_org_created", "org_id", "created_at"),
    )

    asset_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(150), nullable=False)
    policy_number: Mapped[str | None] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(40), nullable=False, default="manufacturer")

    starts_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    expires_on: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    coverage_summary: Mapped[str | None] = mapped_column(Text)
    covers_parts: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    covers_labor: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deductible: Mapped[Decimal | None] = mapped_column(Money)
    claim_phone: Mapped[str | None] = mapped_column(String(40))
    document_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("documents.id", ondelete="SET NULL"), index=True
    )
    is_transferable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    asset: Mapped[Asset] = relationship(back_populates="warranties")

    def covers(self, on_date: dt.date) -> bool:
        return self.starts_on <= on_date <= self.expires_on

"""Residents, tenancies, communications, and notices.

A *resident* is a person. A *tenancy* is that person's relationship to a lease,
with a role: primary, co-resident, occupant, or guarantor. Keeping them separate
is what allows a person to move between units, hold two leases at once, or be a
guarantor on one and a resident on another - all of which are ordinary, and all
of which break a model that puts the person on the lease row.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import SoftDeleteMixin, TenantModel
from app.models.types import GUID, EncryptedText, JSONType, UTCDateTime, enum_column, utcnow

if TYPE_CHECKING:
    from app.models.leasing import Lease

__all__ = [
    "CommunicationChannel",
    "Message",
    "MessageDirection",
    "MessageThread",
    "Notice",
    "NoticeKind",
    "Resident",
    "ResidentStatus",
    "Tenancy",
    "TenancyRole",
]


class ResidentStatus(StrEnum):
    PROSPECT = "prospect"
    APPLICANT = "applicant"
    FUTURE = "future"
    CURRENT = "current"
    FORMER = "former"
    EVICTED = "evicted"


class TenancyRole(StrEnum):
    PRIMARY = "primary"
    CO_RESIDENT = "co_resident"
    OCCUPANT = "occupant"
    GUARANTOR = "guarantor"
    MINOR = "minor"


class CommunicationChannel(StrEnum):
    PORTAL = "portal"
    EMAIL = "email"
    SMS = "sms"
    PHONE = "phone"
    IN_PERSON = "in_person"
    MAIL = "mail"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    INTERNAL = "internal"


class NoticeKind(StrEnum):
    LATE_RENT = "late_rent"
    PAY_OR_QUIT = "pay_or_quit"
    LEASE_VIOLATION = "lease_violation"
    ENTRY = "entry"
    RENEWAL_OFFER = "renewal_offer"
    NON_RENEWAL = "non_renewal"
    TERMINATION = "termination"
    RENT_CHANGE = "rent_change"
    GENERAL = "general"


class Resident(TenantModel, SoftDeleteMixin):
    """A person who lives in, or has lived in, a managed unit."""

    __tablename__ = "residents"
    __table_args__ = (
        Index("ix_residents_org_status", "org_id", "status"),
        Index("ix_residents_org_email", "org_id", "email"),
        Index("ix_residents_name", "org_id", "last_name", "first_name"),
        Index("ix_residents_org_created", "org_id", "created_at"),
    )

    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)
    middle_name: Mapped[str | None] = mapped_column(String(100))
    preferred_name: Mapped[str | None] = mapped_column(String(100))

    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(40))
    alternate_phone: Mapped[str | None] = mapped_column(String(40))

    #: Encrypted. Date of birth and the SSN tail are the two fields that turn a
    #: leaked resident table into an identity-theft incident.
    date_of_birth: Mapped[str | None] = mapped_column(EncryptedText)
    ssn_last4: Mapped[str | None] = mapped_column(EncryptedText)
    government_id_type: Mapped[str | None] = mapped_column(String(40))
    government_id_number: Mapped[str | None] = mapped_column(EncryptedText)

    status: Mapped[ResidentStatus] = mapped_column(
        enum_column(ResidentStatus), nullable=False, default=ResidentStatus.PROSPECT, index=True
    )
    first_move_in: Mapped[dt.date | None] = mapped_column(Date)
    final_move_out: Mapped[dt.date | None] = mapped_column(Date)

    emergency_contact_name: Mapped[str | None] = mapped_column(String(150))
    emergency_contact_phone: Mapped[str | None] = mapped_column(String(40))
    emergency_contact_relation: Mapped[str | None] = mapped_column(String(60))

    preferred_channel: Mapped[CommunicationChannel] = mapped_column(
        enum_column(CommunicationChannel), nullable=False, default=CommunicationChannel.EMAIL
    )
    #: Honoured by every automated notice path. A do-not-contact flag that some
    #: code paths ignore is worse than none at all.
    do_not_contact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    portal_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    #: Mailing address once they have moved out, for deposit disposition.
    forwarding_address: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    notes: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    tenancies: Mapped[list[Tenancy]] = relationship(
        back_populates="resident", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def display_name(self) -> str:
        return self.preferred_name or self.full_name

    @property
    def is_current(self) -> bool:
        return self.status == ResidentStatus.CURRENT


class Tenancy(TenantModel):
    """The relationship between one resident and one lease."""

    __tablename__ = "tenancies"
    __table_args__ = (
        UniqueConstraint("lease_id", "resident_id", name="uq_tenancies_lease_resident"),
        Index("ix_tenancies_resident", "resident_id", "ended_at"),
        Index("ix_tenancies_org_created", "org_id", "created_at"),
    )

    lease_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("leases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resident_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("residents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[TenancyRole] = mapped_column(
        enum_column(TenancyRole), nullable=False, default=TenancyRole.PRIMARY
    )
    #: Financial responsibility. Occupants and minors live there; they do not owe
    #: rent, and collections must never chase them.
    is_financially_responsible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    started_at: Mapped[dt.date] = mapped_column(Date, nullable=False)
    ended_at: Mapped[dt.date | None] = mapped_column(Date)

    resident: Mapped[Resident] = relationship(back_populates="tenancies")
    lease: Mapped[Lease] = relationship(back_populates="tenancies")

    @property
    def is_active(self) -> bool:
        return self.ended_at is None


class MessageThread(TenantModel, SoftDeleteMixin):
    """A conversation, anchored to whatever it is about."""

    __tablename__ = "message_threads"
    __table_args__ = (
        Index("ix_message_threads_org_updated", "org_id", "last_message_at"),
        Index("ix_message_threads_subject_ref", "org_id", "subject_type", "subject_id"),
        Index("ix_message_threads_org_created", "org_id", "created_at"),
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Polymorphic anchor: ``lease``, ``work_order``, ``application``, ``owner``.
    subject_type: Mapped[str | None] = mapped_column(String(40), index=True)
    subject_id: Mapped[str | None] = mapped_column(GUID, index=True)

    property_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("properties.id", ondelete="SET NULL"), index=True
    )
    unit_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("units.id", ondelete="SET NULL"), index=True
    )
    resident_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("residents.id", ondelete="SET NULL"), index=True
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    #: Internal threads are never visible in a resident or owner portal.
    is_internal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_message_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)
    assigned_to_id: Mapped[str | None] = mapped_column(GUID, index=True)

    messages: Mapped[list[Message]] = relationship(
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
        passive_deletes=True,
    )


class Message(TenantModel):
    """One message in a thread."""

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_thread_created", "thread_id", "created_at"),
        Index("ix_messages_org_created", "org_id", "created_at"),
    )

    thread_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("message_threads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    direction: Mapped[MessageDirection] = mapped_column(
        enum_column(MessageDirection), nullable=False, default=MessageDirection.OUTBOUND
    )
    channel: Mapped[CommunicationChannel] = mapped_column(
        enum_column(CommunicationChannel), nullable=False, default=CommunicationChannel.PORTAL
    )

    sender_user_id: Mapped[str | None] = mapped_column(GUID, index=True)
    sender_label: Mapped[str] = mapped_column(String(150), nullable=False, default="System")
    recipient_label: Mapped[str | None] = mapped_column(String(150))

    #: Stored as text and sanitised on render. Storing pre-rendered HTML makes
    #: every future output context depend on a sanitiser decision made once,
    #: years ago, by someone who has left.
    body: Mapped[str] = mapped_column(Text, nullable=False)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    read_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    delivery_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    delivery_error: Mapped[str | None] = mapped_column(String(255))
    external_id: Mapped[str | None] = mapped_column(String(120), index=True)

    thread: Mapped[MessageThread] = relationship(back_populates="messages")


class Notice(TenantModel):
    """A formal notice served on a resident.

    Notices carry legal weight and deadlines, so delivery evidence - method,
    timestamp, and the rendered document - is retained rather than derived.
    """

    __tablename__ = "notices"
    __table_args__ = (
        Index("ix_notices_org_kind_status", "org_id", "kind", "status"),
        Index("ix_notices_org_created", "org_id", "created_at"),
    )

    kind: Mapped[NoticeKind] = mapped_column(enum_column(NoticeKind), nullable=False, index=True)
    lease_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("leases.id", ondelete="SET NULL"), index=True
    )
    resident_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("residents.id", ondelete="SET NULL"), index=True
    )
    unit_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("units.id", ondelete="SET NULL"), index=True
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    effective_date: Mapped[dt.date | None] = mapped_column(Date)
    #: When the resident's clock runs out. Automations escalate from here.
    respond_by: Mapped[dt.date | None] = mapped_column(Date, index=True)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    delivery_method: Mapped[CommunicationChannel | None] = mapped_column(
        enum_column(CommunicationChannel)
    )
    delivery_evidence: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    document_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("documents.id", ondelete="SET NULL"), index=True
    )
    issued_by_id: Mapped[str | None] = mapped_column(GUID)
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    def mark_delivered(self, method: CommunicationChannel, evidence: dict[str, Any]) -> None:
        self.delivered_at = utcnow()
        self.delivery_method = method
        self.delivery_evidence = evidence
        self.status = "delivered"

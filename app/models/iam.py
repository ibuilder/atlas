"""Identity and access: users, roles, assignments, sessions, tokens, MFA.

One identity table serves staff, residents, owners, and vendors, discriminated
by :class:`UserType` and linked to the corresponding domain record. Four parallel
authentication systems is how portals end up with four different session
timeouts and three different password reset flows, one of which is broken.

Role assignments are *scoped* - to the organization, a portfolio, or a single
property - which is what lets a regional manager have full authority over their
twelve properties and none anywhere else, without inventing a role per region.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel, SoftDeleteMixin, TenantModel
from app.models.types import GUID, EncryptedText, JSONType, UTCDateTime, enum_column, utcnow

if TYPE_CHECKING:
    from app.models.org import Organization

__all__ = [
    "ApiToken",
    "LoginAttempt",
    "LoginOutcome",
    "MfaRecoveryCode",
    "PasswordHistory",
    "PasswordResetToken",
    "Permission",
    "Role",
    "RoleAssignment",
    "RolePermission",
    "ScopeType",
    "User",
    "UserSession",
    "UserStatus",
    "UserType",
]


class UserType(StrEnum):
    STAFF = "staff"
    RESIDENT = "resident"
    OWNER = "owner"
    VENDOR = "vendor"
    SERVICE = "service"


class UserStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    LOCKED = "locked"
    DEACTIVATED = "deactivated"


class ScopeType(StrEnum):
    """How far a role assignment reaches."""

    ORGANIZATION = "organization"
    PORTFOLIO = "portfolio"
    PROPERTY = "property"


class LoginOutcome(StrEnum):
    SUCCESS = "success"
    INVALID_CREDENTIALS = "invalid_credentials"
    UNKNOWN_USER = "unknown_user"
    LOCKED = "locked"
    DISABLED = "disabled"
    MFA_REQUIRED = "mfa_required"
    MFA_FAILED = "mfa_failed"


class Permission(BaseModel):
    """The catalogue of things that can be permitted.

    Global rather than tenant-scoped: the vocabulary of actions is a property of
    the software, not of a customer. Tenants compose roles from it; they do not
    invent new verbs the code has never heard of.
    """

    __tablename__ = "permissions"
    __table_args__ = (UniqueConstraint("code", name="uq_permissions_code"),)

    code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(60), nullable=False, default="general")
    #: Permissions that move money or change who can move money. The policy
    #: engine demands a fresh MFA assertion for these regardless of role.
    is_sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Role(TenantModel, SoftDeleteMixin):
    """A named bundle of permissions within one organization."""

    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_roles_org_code"),
        Index("ix_roles_org_created", "org_id", "created_at"),
    )

    code: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: System roles are provisioned with every organization and cannot be
    #: deleted - only copied and adjusted.
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Applied automatically to new users of the matching type.
    default_for_user_type: Mapped[UserType | None] = mapped_column(enum_column(UserType))
    #: Roles that grant permission over money movement require MFA to hold.
    requires_mfa: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    permissions: Mapped[list[RolePermission]] = relationship(
        back_populates="role", cascade="all, delete-orphan", passive_deletes=True
    )
    assignments: Mapped[list[RoleAssignment]] = relationship(
        back_populates="role", passive_deletes=True
    )

    @property
    def permission_codes(self) -> set[str]:
        return {link.permission_code for link in self.permissions}


class RolePermission(TenantModel):
    """Association between a role and a permission code."""

    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "permission_code", name="uq_role_permissions_role_code"),
        Index("ix_role_permissions_org_created", "org_id", "created_at"),
    )

    role_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    permission_code: Mapped[str] = mapped_column(
        String(100), ForeignKey("permissions.code", ondelete="CASCADE"), nullable=False, index=True
    )

    role: Mapped[Role] = relationship(back_populates="permissions")


class User(TenantModel, SoftDeleteMixin):
    """An identity that can authenticate."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        Index("ix_users_org_status", "org_id", "status"),
        Index("ix_users_org_type", "org_id", "user_type"),
        Index("ix_users_org_created", "org_id", "created_at"),
    )

    # -- identity ---------------------------------------------------------
    #: Globally unique and always stored lower-cased. Global uniqueness means
    #: authentication can resolve an account before an organization is known,
    #: which is what makes a single sign-in page possible across all portals.
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    #: Set when the account came from, or is kept in step with, an external
    #: directory. A directory-managed account is read-only here: a local edit
    #: would either be reverted on the next sync or silently override an
    #: offboarding, and neither is a defensible outcome.
    external_id: Mapped[str | None] = mapped_column(String(255), index=True)
    identity_provider_id: Mapped[str | None] = mapped_column(GUID, index=True)
    is_directory_managed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    email_verified_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(40))
    phone_verified_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    user_type: Mapped[UserType] = mapped_column(
        enum_column(UserType), nullable=False, default=UserType.STAFF, index=True
    )
    status: Mapped[UserStatus] = mapped_column(
        enum_column(UserStatus), nullable=False, default=UserStatus.INVITED, index=True
    )
    #: Platform operators (Atlas staff), not customer administrators. Carries
    #: cross-organization read access for support and is always audited.
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # -- credentials ------------------------------------------------------
    password_hash: Mapped[str | None] = mapped_column(String(255))
    password_changed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Bumped on password change and on "sign out everywhere", invalidating every
    #: issued remember-me cookie at once.
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # -- MFA --------------------------------------------------------------
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Encrypted at rest: a TOTP seed is a bearer credential.
    mfa_secret: Mapped[str | None] = mapped_column(EncryptedText)
    mfa_confirmed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    #: Rejects replay of a code within its own 30-second step.
    mfa_last_counter: Mapped[int | None] = mapped_column(Integer)

    # -- federation -------------------------------------------------------
    idp_issuer: Mapped[str | None] = mapped_column(String(255))
    idp_subject: Mapped[str | None] = mapped_column(String(255), index=True)

    # -- lockout ----------------------------------------------------------
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    last_login_ip: Mapped[str | None] = mapped_column(String(45))

    # -- preferences and links -------------------------------------------
    timezone: Mapped[str | None] = mapped_column(String(64))
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="en_US")
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    accepted_terms_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    resident_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("residents.id", ondelete="SET NULL"), index=True
    )
    owner_entity_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("owner_entities.id", ondelete="SET NULL"), index=True
    )
    vendor_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("vendors.id", ondelete="SET NULL"), index=True
    )

    organization: Mapped[Organization] = relationship(foreign_keys="User.org_id")
    role_assignments: Mapped[list[RoleAssignment]] = relationship(
        back_populates="user",
        foreign_keys="RoleAssignment.user_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    # -- Flask-Login contract --------------------------------------------
    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def is_anonymous(self) -> bool:
        return False

    @property
    def is_active(self) -> bool:
        """Whether this identity may hold a session right now."""
        if self.status != UserStatus.ACTIVE or self.is_deleted:
            return False
        return not self.is_locked

    def get_id(self) -> str:
        # Includes the credential version so Flask-Login's remember cookie is
        # invalidated automatically when credentials change.
        return f"{self.id}:{self.credential_version}"

    @property
    def actor_type(self) -> str:
        return str(self.user_type)

    # -- domain helpers ---------------------------------------------------
    @property
    def is_locked(self) -> bool:
        return self.locked_until is not None and self.locked_until > utcnow()

    @property
    def has_usable_password(self) -> bool:
        return bool(self.password_hash)

    @property
    def label(self) -> str:
        return self.display_name or self.full_name or self.email

    def can_access_org(self, org_id: str) -> bool:
        """Whether this user may act within ``org_id``.

        Home organization always; anywhere for a platform operator; otherwise
        only where an explicit, live role assignment exists. The lookup runs
        unscoped by necessity - it is the question "which tenants may I see?",
        which cannot itself be tenant-scoped.
        """
        if org_id == self.org_id:
            return True
        if self.is_platform_admin:
            return True

        from app.extensions import current_session, db
        from app.models.base import unscoped

        with unscoped(current_session()):
            now = utcnow()
            stmt = (
                select(RoleAssignment.id)
                .where(
                    RoleAssignment.user_id == self.id,
                    RoleAssignment.org_id == org_id,
                    RoleAssignment.revoked_at.is_(None),
                )
                .limit(1)
            )
            assignment_id = db.session.execute(stmt).scalar_one_or_none()
            if assignment_id is None:
                return False
            expires = db.session.get(RoleAssignment, assignment_id)
            return expires is None or expires.expires_at is None or expires.expires_at > now

    def record_failed_login(self, max_attempts: int, lockout_minutes: int) -> bool:
        """Increment the failure counter and lock out if the threshold is hit.

        Returns whether the account is now locked.
        """
        self.failed_login_count += 1
        if self.failed_login_count >= max_attempts:
            self.locked_until = utcnow() + dt.timedelta(minutes=lockout_minutes)
            return True
        return False

    def clear_lockout(self) -> None:
        self.failed_login_count = 0
        self.locked_until = None


class RoleAssignment(TenantModel):
    """Grants a role to a user within a scope."""

    __tablename__ = "role_assignments"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "role_id", "scope_type", "scope_id", name="uq_role_assignments_unique"
        ),
        Index("ix_role_assignments_user", "user_id", "revoked_at"),
        Index("ix_role_assignments_org_created", "org_id", "created_at"),
    )

    user_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scope_type: Mapped[ScopeType] = mapped_column(
        enum_column(ScopeType), nullable=False, default=ScopeType.ORGANIZATION
    )
    #: Null when the scope is the organization itself.
    scope_id: Mapped[str | None] = mapped_column(GUID, index=True)

    granted_by_id: Mapped[str | None] = mapped_column(GUID)
    granted_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    #: Time-boxed access for contractors, auditors, and temporary cover.
    expires_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    revoked_by_id: Mapped[str | None] = mapped_column(GUID)
    reason: Mapped[str | None] = mapped_column(String(255))

    user: Mapped[User] = relationship(back_populates="role_assignments", foreign_keys=[user_id])
    role: Mapped[Role] = relationship(back_populates="assignments")

    @property
    def is_live(self) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > utcnow()


class UserSession(TenantModel):
    """A device session, individually revocable.

    Stored server-side rather than trusted from the cookie, so "sign out on that
    laptop I left at the office" is a real operation instead of a suggestion.
    """

    __tablename__ = "user_sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_user_sessions_token_hash"),
        Index("ix_user_sessions_user_active", "user_id", "revoked_at"),
        Index("ix_user_sessions_org_created", "org_id", "created_at"),
    )

    user_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Only the digest is stored; the session token itself lives in the cookie.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    device_label: Mapped[str | None] = mapped_column(String(120))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(512))

    issued_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    revoked_reason: Mapped[str | None] = mapped_column(String(120))

    #: When MFA was last satisfied on this session. Sensitive actions require
    #: this to be recent, which is what stops a stolen session cookie from being
    #: enough to move money.
    mfa_verified_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    user: Mapped[User] = relationship(back_populates="sessions")

    def is_valid(self, idle_timeout_minutes: int) -> bool:
        now = utcnow()
        if self.revoked_at is not None or self.expires_at <= now:
            return False
        idle_deadline = self.last_seen_at + dt.timedelta(minutes=idle_timeout_minutes)
        return idle_deadline > now

    def revoke(self, reason: str = "user_request") -> None:
        if self.revoked_at is None:
            self.revoked_at = utcnow()
            self.revoked_reason = reason


class ApiToken(TenantModel):
    """A machine credential for the REST API."""

    __tablename__ = "api_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_api_tokens_token_hash"),
        Index("ix_api_tokens_org_created", "org_id", "created_at"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: Displayable prefix so a token can be identified in a UI or a log without
    #: ever storing the secret.
    prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    user_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    #: Subset of the owning user's permissions. A token can never exceed the
    #: authority of the identity that minted it.
    scopes: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    #: Optional CIDR allowlist, evaluated before the token is even hashed.
    allowed_ips: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)

    expires_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    last_used_ip: Mapped[str | None] = mapped_column(String(45))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    user: Mapped[User | None] = relationship(foreign_keys=[user_id])

    @property
    def is_live(self) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > utcnow()


class PasswordHistory(TenantModel):
    """Previous password hashes, to stop trivial reuse on rotation."""

    __tablename__ = "password_histories"
    __table_args__ = (Index("ix_password_histories_user", "user_id", "created_at"),)

    user_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)


class PasswordResetToken(TenantModel):
    """A single-use, short-lived password reset grant."""

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_password_reset_tokens_hash"),
        Index("ix_password_reset_tokens_user", "user_id", "used_at"),
    )

    user_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    expires_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    used_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    requested_ip: Mapped[str | None] = mapped_column(String(45))
    #: Invalidated wholesale when a newer request supersedes this one.
    superseded_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    @property
    def is_usable(self) -> bool:
        return self.used_at is None and self.superseded_at is None and self.expires_at > utcnow()


class MfaRecoveryCode(TenantModel):
    """One-time recovery code for a lost authenticator."""

    __tablename__ = "mfa_recovery_codes"
    __table_args__ = (Index("ix_mfa_recovery_codes_user", "user_id", "used_at"),)

    user_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    used_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    used_ip: Mapped[str | None] = mapped_column(String(45))


class LoginAttempt(BaseModel):
    """Authentication attempt log, used for lockout and anomaly detection.

    Not tenant-scoped and keyed by an email *digest* rather than the address:
    the table has to be writable before an organization - or even a valid user -
    is known, and it should not itself become a harvestable list of customer
    email addresses.
    """

    __tablename__ = "login_attempts"
    __table_args__ = (
        Index("ix_login_attempts_email_time", "email_hash", "created_at"),
        Index("ix_login_attempts_ip_time", "ip_address", "created_at"),
    )

    email_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(GUID, index=True)
    org_id: Mapped[str | None] = mapped_column(GUID, index=True)
    outcome: Mapped[LoginOutcome] = mapped_column(enum_column(LoginOutcome), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    correlation_id: Mapped[str | None] = mapped_column(String(128))

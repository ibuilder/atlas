"""Provisioning: permission catalogue, system roles, organizations, users.

Idempotent throughout. These functions run on every deploy (to sync the
permission catalogue), on every new tenant, and repeatedly during seeding and
testing - so running them twice must be indistinguishable from running them
once.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import Conflict, ValidationFailed
from app.logging import get_logger
from app.models.audit import AuditAction
from app.models.base import unscoped
from app.models.iam import (
    Permission,
    Role,
    RoleAssignment,
    RolePermission,
    ScopeType,
    User,
    UserStatus,
    UserType,
)
from app.models.org import Organization, OrganizationStatus
from app.models.types import utcnow
from app.security.passwords import hash_password, validate_password
from app.security.permissions import PERMISSION_CATALOG, SYSTEM_ROLES
from app.services.audit.recorder import record_audit_event

__all__ = [
    "assign_role",
    "create_organization",
    "create_user",
    "ensure_system_roles",
    "sync_permission_catalog",
]

log = get_logger("services.iam.provisioning")


def sync_permission_catalog(session: Session) -> int:
    """Upsert the permission catalogue from code into the database.

    Permissions removed from code are left in place rather than deleted:
    dropping one would cascade away every role that references it, turning a
    refactor into a silent, organization-wide loss of access.
    """
    with unscoped(session):
        existing = {
            permission.code: permission
            for permission in session.execute(select(Permission)).scalars()
        }

    changed = 0
    for definition in PERMISSION_CATALOG:
        current = existing.get(definition.code)
        if current is None:
            session.add(
                Permission(
                    code=definition.code,
                    name=definition.name,
                    description=definition.description,
                    category=definition.category,
                    is_sensitive=definition.sensitive,
                )
            )
            changed += 1
            continue

        if (
            current.name != definition.name
            or current.category != definition.category
            or current.is_sensitive != definition.sensitive
            or (current.description or "") != definition.description
        ):
            current.name = definition.name
            current.category = definition.category
            current.is_sensitive = definition.sensitive
            current.description = definition.description
            changed += 1

    session.flush()
    if changed:
        log.info(
            "permission catalog synchronised",
            extra={"event": "provisioning.permissions_synced", "changed": changed},
        )
    return changed


def ensure_system_roles(session: Session, org_id: str) -> dict[str, Role]:
    """Create or refresh the built-in roles for one organization.

    Permission sets are reconciled on every call, so a release that adds a
    permission grants it to the roles that should have it without an operator
    editing eighty tenants by hand.
    """
    with unscoped(session):
        existing = {
            role.code: role
            for role in session.execute(select(Role).where(Role.org_id == org_id)).scalars()
        }

    roles: dict[str, Role] = {}
    for definition in SYSTEM_ROLES:
        role = existing.get(definition.code)
        if role is None:
            role = Role(
                org_id=org_id,
                code=definition.code,
                name=definition.name,
                description=definition.description,
                is_system=True,
                requires_mfa=definition.requires_mfa,
                default_for_user_type=definition.default_for,
            )
            session.add(role)
            session.flush()
        else:
            role.name = definition.name
            role.description = definition.description
            role.requires_mfa = definition.requires_mfa
            role.default_for_user_type = definition.default_for

        _reconcile_role_permissions(session, role, definition.permissions)
        roles[definition.code] = role

    session.flush()
    return roles


def _reconcile_role_permissions(session: Session, role: Role, expected: frozenset[str]) -> None:
    with unscoped(session):
        current_links = list(
            session.execute(
                select(RolePermission).where(RolePermission.role_id == role.id)
            ).scalars()
        )

    current = {link.permission_code: link for link in current_links}

    for code in expected - set(current):
        session.add(RolePermission(org_id=role.org_id, role_id=role.id, permission_code=code))
    for code in set(current) - expected:
        session.delete(current[code])


def create_organization(
    session: Session,
    *,
    name: str,
    slug: str,
    legal_name: str | None = None,
    timezone: str = "America/New_York",
    currency: str = "USD",
    status: OrganizationStatus = OrganizationStatus.ACTIVE,
    **extra: object,
) -> Organization:
    """Create a tenant and provision its roles."""
    normalized_slug = slug.strip().lower()
    if not normalized_slug.replace("-", "").isalnum():
        raise ValidationFailed("Organization slug must be alphanumeric with hyphens.")

    with unscoped(session):
        clash = session.execute(
            select(Organization.id).where(Organization.slug == normalized_slug)
        ).scalar_one_or_none()
    if clash:
        raise Conflict(f"An organization with slug {normalized_slug!r} already exists.")

    organization = Organization(
        name=name,
        legal_name=legal_name or name,
        slug=normalized_slug,
        status=status,
        timezone=timezone,
        currency=currency,
        # Tenant-scoped storage prefix, so one tenant's object keys can never
        # address another's.
        storage_prefix=f"org/{normalized_slug}",
        **extra,
    )
    session.add(organization)
    session.flush()

    sync_permission_catalog(session)
    ensure_system_roles(session, organization.id)

    record_audit_event(
        action=AuditAction.ORG_CREATED,
        resource_type="Organization",
        resource_id=organization.id,
        resource_label=organization.name,
        payload={"slug": normalized_slug},
        org_id=organization.id,
    )
    session.flush()
    return organization


def create_user(
    session: Session,
    *,
    org_id: str,
    email: str,
    full_name: str,
    password: str | None = None,
    user_type: UserType = UserType.STAFF,
    status: UserStatus = UserStatus.ACTIVE,
    role_codes: list[str] | None = None,
    is_platform_admin: bool = False,
    must_change_password: bool = False,
    resident_id: str | None = None,
    owner_entity_id: str | None = None,
    vendor_id: str | None = None,
) -> User:
    """Create a user and grant their roles."""
    from flask import current_app

    normalized_email = (email or "").strip().lower()
    if "@" not in normalized_email:
        raise ValidationFailed("A valid email address is required.")

    with unscoped(session):
        clash = session.execute(
            select(User.id).where(User.email == normalized_email)
        ).scalar_one_or_none()
    if clash:
        raise Conflict("An account with that email address already exists.")

    password_hash = None
    if password:
        settings = current_app.config["SETTINGS"] if current_app else None
        strength = validate_password(
            password,
            min_length=settings.password_min_length if settings else 12,
            user_terms=[normalized_email.split("@")[0], full_name],
        )
        if not strength.acceptable:
            raise ValidationFailed(
                "The password does not meet policy.",
                details=[{"field": "password", "message": r} for r in strength.reasons],
            )
        password_hash = hash_password(
            password,
            time_cost=settings.argon2_time_cost if settings else 3,
            memory_cost_kib=settings.argon2_memory_cost_kib if settings else 65_536,
            parallelism=settings.argon2_parallelism if settings else 2,
        )

    user = User(
        org_id=org_id,
        email=normalized_email,
        full_name=full_name,
        user_type=user_type,
        status=status,
        password_hash=password_hash,
        password_changed_at=utcnow() if password_hash else None,
        must_change_password=must_change_password,
        is_platform_admin=is_platform_admin,
        resident_id=resident_id,
        owner_entity_id=owner_entity_id,
        vendor_id=vendor_id,
    )
    session.add(user)
    session.flush()

    codes = list(role_codes or [])
    if not codes:
        # Portal users get their portal role automatically; staff get nothing
        # until someone grants it, because staff authority should be a decision.
        codes = [
            definition.code for definition in SYSTEM_ROLES if definition.default_for == user_type
        ]

    for code in codes:
        assign_role(session, user=user, role_code=code)

    record_audit_event(
        action=AuditAction.USER_CREATED,
        resource_type="User",
        resource_id=user.id,
        resource_label=user.label,
        payload={"user_type": str(user_type), "roles": codes},
        org_id=org_id,
    )
    session.flush()
    return user


def assign_role(
    session: Session,
    *,
    user: User,
    role_code: str,
    scope_type: ScopeType = ScopeType.ORGANIZATION,
    scope_id: str | None = None,
    granted_by_id: str | None = None,
    expires_at: object | None = None,
) -> RoleAssignment:
    """Grant a role at a scope. Idempotent for an identical grant."""
    with unscoped(session):
        role = session.execute(
            select(Role).where(Role.org_id == user.org_id, Role.code == role_code)
        ).scalar_one_or_none()

    if role is None:
        raise ValidationFailed(f"Role {role_code!r} does not exist in this organization.")

    if scope_type == ScopeType.ORGANIZATION and scope_id is not None:
        raise ValidationFailed("Organization-scoped assignments must not carry a scope id.")
    if scope_type != ScopeType.ORGANIZATION and scope_id is None:
        raise ValidationFailed(f"A {scope_type} assignment requires a scope id.")

    with unscoped(session):
        existing = session.execute(
            select(RoleAssignment).where(
                RoleAssignment.user_id == user.id,
                RoleAssignment.role_id == role.id,
                RoleAssignment.scope_type == scope_type,
                RoleAssignment.scope_id == scope_id,
            )
        ).scalar_one_or_none()

    if existing is not None:
        if existing.revoked_at is not None:
            existing.revoked_at = None
            existing.revoked_by_id = None
            existing.granted_at = utcnow()
            session.flush()
        return existing

    assignment = RoleAssignment(
        org_id=user.org_id,
        user_id=user.id,
        role_id=role.id,
        scope_type=scope_type,
        scope_id=scope_id,
        granted_by_id=granted_by_id,
        expires_at=expires_at,  # type: ignore[arg-type]
    )
    session.add(assignment)
    session.flush()

    record_audit_event(
        action=AuditAction.ROLE_ASSIGNED,
        resource_type="User",
        resource_id=user.id,
        resource_label=user.label,
        payload={"role": role_code, "scope_type": str(scope_type), "scope_id": scope_id},
        org_id=user.org_id,
    )
    return assignment


def revoke_role(
    session: Session, *, assignment: RoleAssignment, revoked_by_id: str | None = None
) -> None:
    assignment.revoked_at = utcnow()
    assignment.revoked_by_id = revoked_by_id
    session.flush()
    record_audit_event(
        action=AuditAction.ROLE_REVOKED,
        resource_type="User",
        resource_id=assignment.user_id,
        payload={"assignment_id": assignment.id},
        org_id=assignment.org_id,
    )


__all__ += ["revoke_role"]

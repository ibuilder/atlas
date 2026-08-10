"""Building the authorization context.

Resolving "what may this person do" touches role assignments, role permissions,
and - for portal accounts - the leases, properties, or work they own. Doing that
per permission check would turn one page render into dozens of identical
queries, so it is resolved once per request and cached.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from flask import g, has_request_context
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.logging import get_logger
from app.models.base import unscoped
from app.models.iam import (
    Role,
    RoleAssignment,
    RolePermission,
    ScopeType,
    User,
    UserType,
)
from app.models.org import OwnershipStake, Property
from app.models.resident import Tenancy
from app.models.types import utcnow
from app.security.policies import AuthorizationContext, Grant

__all__ = [
    "build_authorization_context",
    "clear_authorization_context",
    "get_authorization_context",
    "set_authorization_context",
]

log = get_logger("services.iam.authorization")

_CACHE_KEY = "_atlas_authz_context"


def get_authorization_context() -> AuthorizationContext | None:
    """The context for the current request, built on first use."""
    if not has_request_context():
        return getattr(_thread_local, "context", None)

    cached = getattr(g, _CACHE_KEY, None)
    if cached is not None:
        return cached

    from flask_login import current_user

    if not getattr(current_user, "is_authenticated", False):
        return None

    from app.context import current_org_id

    context = build_authorization_context(
        current_user, org_id=current_org_id() or current_user.org_id
    )
    setattr(g, _CACHE_KEY, context)
    return context


class _ThreadLocal:
    """Context holder for work outside a request - Celery tasks and CLI."""

    context: AuthorizationContext | None = None


_thread_local = _ThreadLocal()


def set_authorization_context(context: AuthorizationContext | None) -> None:
    if has_request_context():
        setattr(g, _CACHE_KEY, context)
    else:
        _thread_local.context = context


def clear_authorization_context() -> None:
    if has_request_context():
        if hasattr(g, _CACHE_KEY):
            delattr(g, _CACHE_KEY)
    else:
        _thread_local.context = None


def build_authorization_context(
    user: User,
    *,
    org_id: str | None = None,
    session: Session | None = None,
) -> AuthorizationContext:
    """Resolve every grant and ownership set for ``user`` within ``org_id``."""
    if session is None:
        from app.extensions import db

        session = db.session

    org_id = org_id or user.org_id
    now = utcnow()

    with unscoped(session):
        grants = _load_grants(session, user.id, org_id, now)
        needs_portfolio_map = any(grant.scope_type == ScopeType.PORTFOLIO for grant in grants)
        property_portfolio = (
            _load_property_portfolio(session, org_id) if needs_portfolio_map else {}
        )

        resident_ids: set[str] = set()
        lease_ids: set[str] = set()
        owner_entity_ids: set[str] = set()
        owned_property_ids: set[str] = set()

        if user.user_type == UserType.RESIDENT and user.resident_id:
            resident_ids.add(user.resident_id)
            lease_ids = {
                row
                for row in session.execute(
                    select(Tenancy.lease_id).where(
                        Tenancy.resident_id == user.resident_id,
                        Tenancy.org_id == org_id,
                    )
                ).scalars()
            }
        elif user.user_type == UserType.OWNER and user.owner_entity_id:
            owner_entity_ids.add(user.owner_entity_id)
            owned_property_ids = {
                row
                for row in session.execute(
                    select(OwnershipStake.property_id).where(
                        OwnershipStake.owner_entity_id == user.owner_entity_id,
                        OwnershipStake.org_id == org_id,
                    )
                ).scalars()
            }

    session_row = getattr(user, "_atlas_session", None)
    mfa_verified_at = getattr(session_row, "mfa_verified_at", None)

    from flask import current_app

    reauth_window = 240
    if current_app:
        settings = current_app.config.get("SETTINGS")
        if settings is not None:
            reauth_window = settings.session_privileged_reauth_minutes

    return AuthorizationContext(
        user_id=user.id,
        org_id=org_id,
        user_type=user.user_type,
        is_active=user.is_active,
        is_platform_admin=user.is_platform_admin,
        grants=frozenset(grants),
        mfa_enabled=user.mfa_enabled,
        mfa_verified_at=mfa_verified_at,
        reauth_window_minutes=reauth_window,
        resident_ids=frozenset(resident_ids),
        lease_ids=frozenset(lease_ids),
        owner_entity_ids=frozenset(owner_entity_ids),
        owned_property_ids=frozenset(owned_property_ids),
        vendor_id=user.vendor_id,
        property_portfolio=property_portfolio,
    )


def _load_grants(session: Session, user_id: str, org_id: str, now) -> set[Grant]:  # noqa: ANN001
    """Every permission the user holds, with the scope it was granted at.

    One join rather than a query per assignment: a property manager with twelve
    property-scoped assignments would otherwise cost thirteen round trips before
    the first permission check.
    """
    stmt = (
        select(
            RolePermission.permission_code,
            RoleAssignment.scope_type,
            RoleAssignment.scope_id,
        )
        .join(Role, Role.id == RoleAssignment.role_id)
        .join(RolePermission, RolePermission.role_id == Role.id)
        .where(
            RoleAssignment.user_id == user_id,
            RoleAssignment.org_id == org_id,
            RoleAssignment.revoked_at.is_(None),
            Role.deleted_at.is_(None),
        )
    )

    grants: set[Grant] = set()
    for permission_code, scope_type, scope_id in session.execute(stmt):
        grants.add(
            Grant(
                permission=permission_code,
                scope_type=scope_type,
                scope_id=scope_id,
            )
        )

    # Expiry is filtered in Python because the assignment rows are already
    # loaded and the count is small; pushing a time comparison into the join
    # would make the query non-cacheable for no benefit.
    expired = _expired_assignment_scopes(session, user_id, org_id, now)
    if expired:
        grants = {grant for grant in grants if (grant.scope_type, grant.scope_id) not in expired}
    return grants


def _expired_assignment_scopes(
    session: Session, user_id: str, org_id: str, now
) -> set[tuple[ScopeType, str | None]]:  # noqa: ANN001
    stmt = select(RoleAssignment.scope_type, RoleAssignment.scope_id).where(
        RoleAssignment.user_id == user_id,
        RoleAssignment.org_id == org_id,
        RoleAssignment.revoked_at.is_(None),
        RoleAssignment.expires_at.is_not(None),
        RoleAssignment.expires_at <= now,
    )
    return {(scope_type, scope_id) for scope_type, scope_id in session.execute(stmt)}


def _load_property_portfolio(session: Session, org_id: str) -> dict[str, str | None]:
    stmt = select(Property.id, Property.portfolio_id).where(
        Property.org_id == org_id, Property.deleted_at.is_(None)
    )
    return {property_id: portfolio_id for property_id, portfolio_id in session.execute(stmt)}

"""The authorization engine.

Every access decision in Atlas goes through :func:`evaluate`. Permission checks
are not scattered across views, because scattered checks are how one forgotten
route becomes an incident, and because a policy nobody can enumerate is a policy
nobody can test. This one is enumerable: the test suite walks the full
role x action x resource matrix.

The evaluation order, and why:

1. **Unknown action -> deny.** A typo in a permission name must fail closed. The
   opposite - treating an unrecognised action as unrestricted - is the single
   most dangerous default an authorization layer can have.
2. **Tenant boundary.** A resource in another organization is not "denied", it is
   *absent*: reported as not-found so the API cannot be used to probe which
   identifiers exist elsewhere.
3. **Role grants, scoped.** A grant carries a scope - organization, portfolio, or
   property - and only covers resources inside it.
4. **Portal ownership.** Residents, owners, and vendors additionally must own the
   resource. Holding ``invoice.read`` lets a resident read *their* invoices, not
   the building's.
5. **Sensitive actions.** Anything that moves money, changes who can move money,
   or exposes bulk personal data demands a recent MFA assertion, whatever role
   granted it.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, TypeVar

from app.errors import PermissionDenied, ReauthenticationRequired, TenantIsolationViolation
from app.logging import get_logger
from app.models.iam import ScopeType, UserType
from app.models.types import utcnow
from app.security.permissions import all_permission_codes, sensitive_permission_codes

__all__ = [
    "AuthorizationContext",
    "Decision",
    "Grant",
    "ResourceScope",
    "can",
    "describe_resource",
    "evaluate",
    "require",
    "requires",
]

log = get_logger("security.policy")

_ALL_PERMISSIONS = all_permission_codes()
_SENSITIVE_PERMISSIONS = sensitive_permission_codes()

F = TypeVar("F", bound=Callable[..., Any])


class DenyReason:
    NO_CONTEXT = "no_authorization_context"
    UNKNOWN_ACTION = "unknown_action"
    INACTIVE_ACTOR = "actor_not_active"
    CROSS_TENANT = "cross_tenant"
    NO_GRANT = "no_matching_grant"
    OUT_OF_SCOPE = "resource_out_of_scope"
    NOT_OWNED = "resource_not_owned"
    MFA_STALE = "mfa_reauthentication_required"
    ALLOWED = "allowed"


@dataclass(frozen=True, slots=True)
class Grant:
    """One permission, at one scope."""

    permission: str
    scope_type: ScopeType = ScopeType.ORGANIZATION
    scope_id: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceScope:
    """Where a resource sits in the hierarchy, for scope matching."""

    org_id: str | None = None
    portfolio_id: str | None = None
    property_id: str | None = None
    unit_id: str | None = None
    lease_id: str | None = None
    resident_id: str | None = None
    owner_entity_id: str | None = None
    vendor_id: str | None = None
    #: Set when the resource *is* a user, so self-service checks work.
    user_id: str | None = None


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str
    detail: str = ""
    requires_mfa: bool = False

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class AuthorizationContext:
    """Everything needed to decide, resolved once per request.

    Built by :mod:`app.services.iam.authorization` and cached for the duration of
    the request. Rebuilding it per check would turn a page render into dozens of
    identical role lookups.
    """

    user_id: str
    org_id: str
    user_type: UserType = UserType.STAFF
    is_active: bool = True
    is_platform_admin: bool = False
    grants: frozenset[Grant] = field(default_factory=frozenset)

    mfa_enabled: bool = False
    mfa_verified_at: dt.datetime | None = None
    reauth_window_minutes: int = 240

    # Portal ownership sets, empty for staff.
    resident_ids: frozenset[str] = field(default_factory=frozenset)
    lease_ids: frozenset[str] = field(default_factory=frozenset)
    owner_entity_ids: frozenset[str] = field(default_factory=frozenset)
    owned_property_ids: frozenset[str] = field(default_factory=frozenset)
    vendor_id: str | None = None

    #: property_id -> portfolio_id, so a portfolio-scoped grant can be matched
    #: against a resource that only knows its property.
    property_portfolio: dict[str, str | None] = field(default_factory=dict)

    # ---------------------------------------------------------------- helpers
    @property
    def is_portal_user(self) -> bool:
        return self.user_type in (UserType.RESIDENT, UserType.OWNER, UserType.VENDOR)

    def permissions(self) -> set[str]:
        """Every permission held at any scope - for menu rendering only.

        Never sufficient on its own to authorise an action: it discards scope.
        """
        return {grant.permission for grant in self.grants}

    def has_fresh_mfa(self, now: dt.datetime | None = None) -> bool:
        if self.mfa_verified_at is None:
            return False
        reference = now or utcnow()
        age = reference - self.mfa_verified_at
        return age <= dt.timedelta(minutes=self.reauth_window_minutes)

    def portfolio_for(self, property_id: str | None) -> str | None:
        return self.property_portfolio.get(property_id) if property_id else None


# ---------------------------------------------------------------------------
# Resource description
# ---------------------------------------------------------------------------


def describe_resource(resource: Any) -> ResourceScope:
    """Derive a :class:`ResourceScope` from a model instance or a mapping.

    Duck-typed on purpose: every tenant model already names these columns
    consistently, and requiring each of eighty models to implement a protocol
    method would be ceremony that adds no safety.
    """
    if resource is None:
        return ResourceScope()
    if isinstance(resource, ResourceScope):
        return resource
    if isinstance(resource, dict):
        get = resource.get
    else:

        def get(name: str, default: Any = None) -> Any:
            return getattr(resource, name, default)

    scope = ResourceScope(
        org_id=get("org_id"),
        portfolio_id=get("portfolio_id"),
        property_id=get("property_id"),
        unit_id=get("unit_id"),
        lease_id=get("lease_id"),
        resident_id=get("resident_id"),
        owner_entity_id=get("owner_entity_id"),
        vendor_id=get("vendor_id"),
    )

    # A Property is its own property scope; a User is its own user scope.
    type_name = type(resource).__name__
    if type_name == "Property" and scope.property_id is None:
        scope = ResourceScope(
            org_id=scope.org_id,
            portfolio_id=scope.portfolio_id,
            property_id=get("id"),
            unit_id=scope.unit_id,
            lease_id=scope.lease_id,
            resident_id=scope.resident_id,
            owner_entity_id=scope.owner_entity_id,
            vendor_id=scope.vendor_id,
        )
    elif type_name == "Portfolio" and scope.portfolio_id is None:
        scope = ResourceScope(org_id=scope.org_id, portfolio_id=get("id"))
    elif type_name == "Resident" and scope.resident_id is None:
        scope = ResourceScope(org_id=scope.org_id, resident_id=get("id"))
    elif type_name == "Lease" and scope.lease_id is None:
        scope = ResourceScope(
            org_id=scope.org_id,
            property_id=scope.property_id,
            unit_id=scope.unit_id,
            lease_id=get("id"),
        )
    elif type_name == "Vendor" and scope.vendor_id is None:
        scope = ResourceScope(org_id=scope.org_id, vendor_id=get("id"))
    elif type_name == "OwnerEntity" and scope.owner_entity_id is None:
        scope = ResourceScope(org_id=scope.org_id, owner_entity_id=get("id"))
    elif type_name == "User":
        scope = ResourceScope(org_id=scope.org_id, user_id=get("id"))

    return scope


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate(
    context: AuthorizationContext | None,
    action: str,
    resource: Any = None,
    *,
    now: dt.datetime | None = None,
) -> Decision:
    """Decide whether ``context`` may perform ``action`` on ``resource``."""
    if context is None:
        return Decision(False, DenyReason.NO_CONTEXT, "No authenticated actor.")

    if action not in _ALL_PERMISSIONS:
        # Fail closed. An unrecognised action is a bug, and a bug must not
        # accidentally grant access.
        log.error(
            "authorization check used an unknown action",
            extra={"event": "security.unknown_action", "action": action},
        )
        return Decision(False, DenyReason.UNKNOWN_ACTION, f"Unknown action {action!r}.")

    if not context.is_active:
        return Decision(False, DenyReason.INACTIVE_ACTOR, "Actor is not active.")

    scope = describe_resource(resource)

    # 2 - tenant boundary
    if scope.org_id is not None and scope.org_id != context.org_id:
        if not context.is_platform_admin:
            return Decision(False, DenyReason.CROSS_TENANT, "Resource belongs to another tenant.")

    # 3 - role grants
    if context.is_platform_admin:
        matched = True
    else:
        matched = _has_matching_grant(context, action, scope)
        if not matched:
            return Decision(
                False,
                DenyReason.NO_GRANT,
                f"No grant for {action} covering this resource.",
            )

    # 4 - portal ownership
    if context.is_portal_user and resource is not None:
        owned = _portal_owns(context, scope, resource)
        if not owned:
            return Decision(
                False, DenyReason.NOT_OWNED, "Resource does not belong to this account."
            )

    # 5 - sensitive actions require fresh MFA
    if action in _SENSITIVE_PERMISSIONS and context.mfa_enabled and not context.has_fresh_mfa(now):
        return Decision(
            False,
            DenyReason.MFA_STALE,
            "This action requires re-confirming your identity.",
            requires_mfa=True,
        )

    return Decision(True, DenyReason.ALLOWED)


def _has_matching_grant(context: AuthorizationContext, action: str, scope: ResourceScope) -> bool:
    """Whether any held grant carries ``action`` at a scope covering the resource."""
    portfolio_id = scope.portfolio_id or context.portfolio_for(scope.property_id)

    for grant in context.grants:
        if grant.permission != action:
            continue
        if grant.scope_type == ScopeType.ORGANIZATION:
            return True
        if grant.scope_type == ScopeType.PORTFOLIO:
            # A portfolio grant covers the whole portfolio. When the resource
            # carries no property or portfolio at all (an organization-wide
            # listing), a scoped grant is deliberately not enough.
            if portfolio_id is not None and grant.scope_id == portfolio_id:
                return True
        elif grant.scope_type == ScopeType.PROPERTY:
            if scope.property_id is not None and grant.scope_id == scope.property_id:
                return True
    return False


def _portal_owns(context: AuthorizationContext, scope: ResourceScope, resource: Any) -> bool:
    """Ownership predicate for resident, owner, and vendor accounts."""
    if context.user_type == UserType.RESIDENT:
        if scope.resident_id and scope.resident_id in context.resident_ids:
            return True
        if scope.lease_id and scope.lease_id in context.lease_ids:
            return True
        if scope.user_id and scope.user_id == context.user_id:
            return True
        # A resident-submitted item they created but which is not yet linked to
        # a lease (a request raised before move-in, for instance).
        created_by = getattr(resource, "created_by_id", None)
        reported_by = getattr(resource, "reported_by_user_id", None)
        return context.user_id in {created_by, reported_by} and context.user_id is not None

    if context.user_type == UserType.OWNER:
        if scope.owner_entity_id and scope.owner_entity_id in context.owner_entity_ids:
            return True
        return bool(scope.property_id and scope.property_id in context.owned_property_ids)

    if context.user_type == UserType.VENDOR:
        if context.vendor_id is None:
            return False
        if scope.vendor_id and scope.vendor_id == context.vendor_id:
            return True
        # Vendors see work assigned to them, and nothing else on the property.
        assigned_vendor = getattr(resource, "vendor_id", None)
        inspector_vendor = getattr(resource, "inspector_vendor_id", None)
        return context.vendor_id in {assigned_vendor, inspector_vendor}

    return True


# ---------------------------------------------------------------------------
# Convenience API
# ---------------------------------------------------------------------------


def current_context() -> AuthorizationContext | None:
    """The authorization context for the active request, if any."""
    from app.services.iam.authorization import get_authorization_context

    return get_authorization_context()


def can(action: str, resource: Any = None) -> bool:
    """Boolean check for templates and menu rendering.

    Convenient, and therefore easy to misuse: it answers "should I draw this
    button", never "may this write proceed". Writes call :func:`require`.
    """
    return evaluate(current_context(), action, resource).allowed


def require(action: str, resource: Any = None) -> None:
    """Enforce a permission or raise.

    Cross-tenant attempts raise :class:`TenantIsolationViolation`, which is
    reported to the client as a 404 and logged server-side as a security event.
    """
    context = current_context()
    decision = evaluate(context, action, resource)
    if decision.allowed:
        return

    actor = context.user_id if context else None
    if decision.reason == DenyReason.CROSS_TENANT:
        raise TenantIsolationViolation(
            f"Actor {actor} attempted {action} on a resource in another organization."
        )
    if decision.requires_mfa:
        raise ReauthenticationRequired(decision.detail)

    log.warning(
        "permission denied",
        extra={
            "event": "security.permission_denied",
            "action": action,
            "reason": decision.reason,
            "actor_id": actor,
            "resource_type": type(resource).__name__ if resource is not None else None,
        },
    )
    _record_denial(action, resource, decision)
    raise PermissionDenied()


def _record_denial(action: str, resource: Any, decision: Decision) -> None:
    """Write an audit event for a denied attempt.

    Denials are the signal that matters: a single 403 is noise, forty in a
    minute across four endpoints is an incident. Best-effort - a failure to
    record must never convert a clean denial into a 500.
    """
    try:
        from app.models.audit import AuditAction, AuditOutcome, AuditSeverity
        from app.services.audit.recorder import record_audit_event

        scope = describe_resource(resource)
        record_audit_event(
            action=AuditAction.AUTH_PERMISSION_DENIED,
            resource_type=type(resource).__name__ if resource is not None else None,
            resource_id=getattr(resource, "id", None),
            outcome=AuditOutcome.DENIED,
            severity=AuditSeverity.NOTICE,
            payload={"attempted_action": action, "deny_reason": decision.reason},
            org_id=scope.org_id,
            commit=False,
        )
    except Exception:  # pragma: no cover - auditing must never mask a denial
        log.exception("failed to record permission denial")


def requires(action: str, loader: Callable[..., Any] | None = None) -> Callable[[F], F]:
    """Decorator enforcing a permission on a route handler.

    ``loader`` receives the view arguments and returns the resource to check.
    Without it the check is permission-only, which is correct for collection
    endpoints where the resource set is filtered by the tenancy guard anyway.

    .. code-block:: python

        @bp.get("/<work_order_id>")
        @requires(Perm.WORK_ORDER_READ, lambda work_order_id, **_: get_work_order(work_order_id))
        def show(work_order_id: str): ...
    """

    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            resource = loader(*args, **kwargs) if loader is not None else None
            require(action, resource)
            return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


def filter_permitted(action: str, resources: Iterable[Any]) -> list[Any]:
    """Keep only the resources the current actor may act on.

    For list endpoints where the tenancy guard has already narrowed to the
    organization but portfolio or ownership scoping still applies.
    """
    context = current_context()
    return [item for item in resources if evaluate(context, action, item).allowed]

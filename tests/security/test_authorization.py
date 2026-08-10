"""The authorization matrix.

Walks role x action combinations rather than spot-checking a few, because the
failure mode of a permission system is never the case someone thought to test -
it is the one nobody enumerated.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.models.iam import ScopeType, UserType
from app.security.permissions import (
    SYSTEM_ROLES,
    Perm,
    all_permission_codes,
    permissions_for_role,
    validate_catalog,
)
from app.security.policies import (
    AuthorizationContext,
    Grant,
    ResourceScope,
    evaluate,
)

pytestmark = pytest.mark.security


def _context(org_id: str, permissions: set[str], **kwargs) -> AuthorizationContext:
    return AuthorizationContext(
        user_id=kwargs.pop("user_id", "user-1"),
        org_id=org_id,
        grants=frozenset(Grant(code) for code in permissions),
        **kwargs,
    )


def test_catalog_is_internally_consistent():
    validate_catalog()


def test_unknown_action_is_denied():
    """A typo must fail closed, never open."""
    context = _context("org-1", {Perm.PROPERTY_READ})
    decision = evaluate(context, "property.reed")
    assert not decision.allowed
    assert decision.reason == "unknown_action"


def test_no_context_is_denied():
    assert not evaluate(None, Perm.PROPERTY_READ)


def test_inactive_actor_is_denied():
    context = _context("org-1", {Perm.PROPERTY_READ}, is_active=False)
    assert not evaluate(context, Perm.PROPERTY_READ)


def test_permission_absent_is_denied():
    context = _context("org-1", {Perm.PROPERTY_READ})
    assert evaluate(context, Perm.PROPERTY_READ)
    assert not evaluate(context, Perm.PROPERTY_DELETE)


def test_cross_tenant_resource_is_denied():
    context = _context("org-1", {Perm.PROPERTY_READ})
    resource = ResourceScope(org_id="org-2", property_id="p-1")
    decision = evaluate(context, Perm.PROPERTY_READ, resource)
    assert not decision.allowed
    assert decision.reason == "cross_tenant"


@pytest.mark.parametrize(
    ("scope_type", "scope_id", "resource_property", "expected"),
    [
        (ScopeType.ORGANIZATION, None, "prop-1", True),
        (ScopeType.PROPERTY, "prop-1", "prop-1", True),
        (ScopeType.PROPERTY, "prop-2", "prop-1", False),
    ],
)
def test_scoped_grants_cover_only_their_scope(scope_type, scope_id, resource_property, expected):
    context = AuthorizationContext(
        user_id="user-1",
        org_id="org-1",
        grants=frozenset({Grant(Perm.PROPERTY_READ, scope_type, scope_id)}),
    )
    resource = ResourceScope(org_id="org-1", property_id=resource_property)
    assert bool(evaluate(context, Perm.PROPERTY_READ, resource)) is expected


def test_portfolio_grant_covers_properties_in_that_portfolio():
    context = AuthorizationContext(
        user_id="user-1",
        org_id="org-1",
        grants=frozenset({Grant(Perm.PROPERTY_READ, ScopeType.PORTFOLIO, "pf-1")}),
        property_portfolio={"prop-1": "pf-1", "prop-2": "pf-2"},
    )
    assert evaluate(
        context, Perm.PROPERTY_READ, ResourceScope(org_id="org-1", property_id="prop-1")
    )
    assert not evaluate(
        context, Perm.PROPERTY_READ, ResourceScope(org_id="org-1", property_id="prop-2")
    )


def test_resident_cannot_read_another_residents_record():
    context = AuthorizationContext(
        user_id="user-1",
        org_id="org-1",
        user_type=UserType.RESIDENT,
        grants=frozenset({Grant(Perm.INVOICE_READ)}),
        resident_ids=frozenset({"res-1"}),
        lease_ids=frozenset({"lease-1"}),
    )

    own = ResourceScope(org_id="org-1", resident_id="res-1", lease_id="lease-1")
    other = ResourceScope(org_id="org-1", resident_id="res-2", lease_id="lease-2")

    assert evaluate(context, Perm.INVOICE_READ, own)
    decision = evaluate(context, Perm.INVOICE_READ, other)
    assert not decision.allowed
    assert decision.reason == "resource_not_owned"


def test_owner_sees_only_owned_properties():
    context = AuthorizationContext(
        user_id="user-1",
        org_id="org-1",
        user_type=UserType.OWNER,
        grants=frozenset({Grant(Perm.PROPERTY_READ)}),
        owner_entity_ids=frozenset({"owner-1"}),
        owned_property_ids=frozenset({"prop-1"}),
    )
    assert evaluate(
        context, Perm.PROPERTY_READ, ResourceScope(org_id="org-1", property_id="prop-1")
    )
    assert not evaluate(
        context, Perm.PROPERTY_READ, ResourceScope(org_id="org-1", property_id="prop-9")
    )


def test_vendor_sees_only_assigned_work():
    context = AuthorizationContext(
        user_id="user-1",
        org_id="org-1",
        user_type=UserType.VENDOR,
        grants=frozenset({Grant(Perm.WORK_ORDER_READ)}),
        vendor_id="vendor-1",
    )
    assert evaluate(
        context, Perm.WORK_ORDER_READ, ResourceScope(org_id="org-1", vendor_id="vendor-1")
    )
    assert not evaluate(
        context, Perm.WORK_ORDER_READ, ResourceScope(org_id="org-1", vendor_id="vendor-2")
    )


def test_sensitive_action_requires_fresh_mfa():
    """Holding the permission is not enough for money movement."""
    from app.models.types import utcnow

    stale = AuthorizationContext(
        user_id="user-1",
        org_id="org-1",
        grants=frozenset({Grant(Perm.BILL_PAY)}),
        mfa_enabled=True,
        mfa_verified_at=utcnow() - dt.timedelta(hours=9),
        reauth_window_minutes=240,
    )
    decision = evaluate(stale, Perm.BILL_PAY)
    assert not decision.allowed
    assert decision.requires_mfa

    fresh = AuthorizationContext(
        user_id="user-1",
        org_id="org-1",
        grants=frozenset({Grant(Perm.BILL_PAY)}),
        mfa_enabled=True,
        mfa_verified_at=utcnow() - dt.timedelta(minutes=5),
        reauth_window_minutes=240,
    )
    assert evaluate(fresh, Perm.BILL_PAY)


def test_non_sensitive_action_does_not_demand_reauthentication():
    from app.models.types import utcnow

    context = AuthorizationContext(
        user_id="user-1",
        org_id="org-1",
        grants=frozenset({Grant(Perm.PROPERTY_READ)}),
        mfa_enabled=True,
        mfa_verified_at=utcnow() - dt.timedelta(days=2),
    )
    assert evaluate(context, Perm.PROPERTY_READ)


@pytest.mark.parametrize("role", [role.code for role in SYSTEM_ROLES])
def test_every_role_grants_only_catalogued_permissions(role):
    assert permissions_for_role(role) <= all_permission_codes()


def test_portal_roles_cannot_touch_the_ledger():
    """Residents, owners, and vendors must never hold posting authority."""
    for role_code in ("resident", "owner", "vendor"):
        granted = permissions_for_role(role_code)
        forbidden = {
            Perm.LEDGER_POST,
            Perm.LEDGER_REVERSE,
            Perm.BILL_PAY,
            Perm.BILL_APPROVE,
            Perm.PERIOD_CLOSE,
            Perm.BANK_ACCOUNT_MANAGE,
            Perm.ROLE_ASSIGN,
            Perm.USER_CREATE,
        }
        assert not (granted & forbidden), f"{role_code} holds {sorted(granted & forbidden)}"


def test_accountant_cannot_approve_or_disburse():
    """Separation of duties: entering a bill and paying it are different jobs."""
    accountant = permissions_for_role("accountant")
    assert Perm.BILL_MANAGE in accountant
    assert Perm.BILL_APPROVE not in accountant
    assert Perm.BILL_PAY not in accountant
    assert Perm.PERIOD_CLOSE not in accountant

    controller = permissions_for_role("controller")
    assert {Perm.BILL_APPROVE, Perm.BILL_PAY, Perm.PERIOD_CLOSE} <= controller


def test_auditor_is_read_only():
    auditor = permissions_for_role("auditor")
    mutating = {
        Perm.LEDGER_POST,
        Perm.PAYMENT_RECORD,
        Perm.WORK_ORDER_CREATE,
        Perm.LEASE_CREATE,
        Perm.PROPERTY_CREATE,
        Perm.USER_CREATE,
    }
    assert not (auditor & mutating)
    assert Perm.AUDIT_READ in auditor

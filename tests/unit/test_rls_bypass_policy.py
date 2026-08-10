"""When the row-level-security policy may be suspended.

Dialect-independent, so it runs everywhere rather than only on the PostgreSQL
job. The decision itself is pure Python; the SQL it drives is covered by
tests/security/test_row_level_security.py.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pytest

from app.context import (
    RequestContext,
    bind_context,
    clear_context,
    new_correlation_id,
    system_context,
)
from app.models.base import unscoped
from app.models.rls import should_bypass

pytestmark = pytest.mark.unit


def _bind(ctx: RequestContext | None):
    return bind_context(ctx) if ctx is not None else None


def test_no_context_does_not_bypass():
    """The regression this exists for.

    An earlier version granted the bypass whenever no organization was bound,
    which meant a raw text() query in a job that forgot to bind a tenant read
    every tenant's rows - precisely the failure this layer exists to contain.
    """
    clear_context()
    assert should_bypass() is False


def test_request_context_without_a_tenant_does_not_bypass():
    token = _bind(RequestContext(correlation_id=new_correlation_id(), source="http"))
    try:
        assert should_bypass() is False
    finally:
        clear_context(token)


def test_explicit_unscoped_block_bypasses():
    clear_context()
    with unscoped():
        assert should_bypass() is True
    assert should_bypass() is False


def test_system_context_without_a_tenant_bypasses():
    """Provisioning and seeding legitimately span tenants before one exists."""
    token = _bind(system_context("seed"))
    try:
        assert should_bypass() is True
    finally:
        clear_context(token)


def test_system_context_with_a_tenant_is_held_to_it():
    """A job that has chosen an organization is enforced against it.

    The bypass covers the phase that legitimately spans tenants, not the whole
    job - otherwise every scheduled task would run unconstrained.
    """
    token = _bind(system_context("task", org_id="019fea00-0000-7000-8000-00000000000c"))
    try:
        assert should_bypass() is False
    finally:
        clear_context(token)


def test_unscoped_still_wins_inside_a_bound_system_context():
    token = _bind(system_context("task", org_id="019fea00-0000-7000-8000-00000000000d"))
    try:
        with unscoped():
            assert should_bypass() is True
        assert should_bypass() is False
    finally:
        clear_context(token)

"""Audit chain integrity.

The claim is that tampering is *detectable*. These tests do the tampering and
check that it is caught.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from app.models.audit import GENESIS_HASH, AuditEvent, AuditOutcome, canonical_json
from app.services.audit.recorder import (
    diff_payload,
    record_audit_event,
    validate_action,
    verify_chain,
)

pytestmark = pytest.mark.unit


def test_action_naming_is_enforced():
    validate_action("lease.created")
    for bad in ("LeaseCreated", "lease", "lease.Created", "lease..created", ""):
        with pytest.raises(ValueError):
            validate_action(bad)


def test_canonical_json_is_order_independent():
    """The hash is only meaningful if serialisation is deterministic."""
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


@pytest.fixture()
def virgin_org(db):
    """An organization with no audit history.

    Created directly rather than through provisioning, which itself writes
    audit events - so this is the only way to observe a genesis link.
    """
    from app.models.org import Organization, OrganizationStatus

    organization = Organization(name="Genesis Co", slug="genesis", status=OrganizationStatus.ACTIVE)
    db.session.add(organization)
    db.session.commit()
    return organization


def test_first_event_links_to_genesis(db, virgin_org):
    event = record_audit_event(action="test.created", org_id=virgin_org.id)
    db.session.commit()

    assert event is not None
    assert event.sequence == 1
    assert event.previous_hash == GENESIS_HASH
    assert event.is_intact


def test_events_chain_to_their_predecessor(db, org, scope):
    first = record_audit_event(action="test.one", org_id=org.id)
    second = record_audit_event(action="test.two", org_id=org.id)
    third = record_audit_event(action="test.three", org_id=org.id)
    db.session.commit()

    # Relative, because provisioning the organization already wrote events.
    assert second.sequence == first.sequence + 1
    assert third.sequence == second.sequence + 1
    assert second.previous_hash == first.entry_hash
    assert third.previous_hash == second.entry_hash
    assert verify_chain(db.session, org_id=org.id)["intact"]


def test_modifying_a_row_breaks_the_chain(db, org, scope):
    """Tampering behind the ORM is still detected on the next verification."""
    record_audit_event(action="test.one", org_id=org.id)
    target = record_audit_event(action="test.two", org_id=org.id)
    record_audit_event(action="test.three", org_id=org.id)
    db.session.commit()

    # Go around the ORM guard the way a database operator with write access
    # would - the exact scenario the chain exists for.
    db.session.execute(
        update(AuditEvent).where(AuditEvent.id == target.id).values(action="test.innocuous")
    )
    db.session.commit()
    db.session.expire_all()

    result = verify_chain(db.session, org_id=org.id)
    assert result["intact"] is False
    assert result["failure"] == "content_modified"
    assert result["at_sequence"] == target.sequence


def test_deleting_a_row_breaks_the_chain(db, org, scope):
    record_audit_event(action="test.one", org_id=org.id)
    victim = record_audit_event(action="test.two", org_id=org.id)
    record_audit_event(action="test.three", org_id=org.id)
    db.session.commit()

    db.session.execute(AuditEvent.__table__.delete().where(AuditEvent.id == victim.id))
    db.session.commit()
    db.session.expire_all()

    result = verify_chain(db.session, org_id=org.id)
    assert result["intact"] is False
    assert result["failure"] == "sequence_gap"


def test_orm_refuses_to_update_an_audit_event(db, org, scope):
    from app.models.audit import AuditImmutabilityError

    event = record_audit_event(action="test.locked", org_id=org.id)
    db.session.commit()

    event.action = "test.rewritten"
    with pytest.raises(AuditImmutabilityError):
        db.session.flush()
    db.session.rollback()


def test_orm_refuses_to_delete_an_audit_event(db, org, scope):
    from app.models.audit import AuditImmutabilityError

    event = record_audit_event(action="test.permanent", org_id=org.id)
    db.session.commit()

    db.session.delete(event)
    with pytest.raises(AuditImmutabilityError):
        db.session.flush()
    db.session.rollback()


def test_payload_is_redacted_before_storage(db, org, scope):
    """The audit trail must not become the widest PII surface in the system."""
    event = record_audit_event(
        action="test.redaction",
        org_id=org.id,
        payload={
            "password": "hunter2",
            "ssn": "123-45-6789",
            "api_token": "atlas_api_abcdefghijklmnopqrstuvwxyz",
            "note": "Contact resident at jane.doe@example.com",
            "amount": "100.00",
        },
    )
    db.session.commit()

    payload = event.payload
    assert payload["password"] == "[REDACTED]"
    assert payload["ssn"] == "[REDACTED]"
    assert payload["api_token"] == "[REDACTED]"
    assert "jane.doe@example.com" not in payload["note"]
    # Business data survives - redaction must not make the trail useless.
    assert payload["amount"] == "100.00"


def test_diff_payload_records_only_changes():
    before = {"rent": "2400", "status": "draft", "updated_at": "t1"}
    after = {"rent": "2500", "status": "draft", "updated_at": "t2"}

    changes = diff_payload(before, after)["changes"]
    assert changes == {"rent": {"from": "2400", "to": "2500"}}


def test_no_organization_means_no_event(db):
    """Pre-authentication failures have no tenant to attribute to."""
    assert record_audit_event(action="test.orphan") is None


def test_denied_outcomes_are_recorded(db, org, scope):
    event = record_audit_event(
        action="auth.permission_denied", org_id=org.id, outcome=AuditOutcome.DENIED
    )
    db.session.commit()

    stored = (
        db.session.execute(select(AuditEvent).where(AuditEvent.action == "auth.permission_denied"))
        .scalars()
        .one()
    )
    assert stored.outcome == AuditOutcome.DENIED
    assert stored.id == event.id

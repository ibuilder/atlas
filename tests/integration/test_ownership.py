"""Ownership transfer, and the invariant it exists to protect.

The failure worth catching is not a crash. It is a transfer that quietly moves
ninety-six percent where a hundred was held, after which every owner statement
under-distributes and nobody notices for a year.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.errors import BusinessRuleViolation, NotFound, ValidationFailed
from app.services.portfolio.ownership import (
    assert_fully_allocated,
    ownership_on,
    ownership_timeline,
    record_initial_stake,
    total_allocated,
    transfer_ownership,
)

pytestmark = pytest.mark.integration

JANUARY = dt.date(2026, 1, 1)
MARCH = dt.date(2026, 3, 14)
ACTOR = "019fea00-0000-7000-8000-0000000000f1"


@pytest.fixture()
def owners(db, org, scope):
    from app.models.org import OwnerEntity, OwnerType

    created = []
    for code, name in (("ALPHA", "Alpha Holdings"), ("BETA", "Beta Trust"), ("GAMMA", "Gamma LP")):
        entity = OwnerEntity(
            org_id=org.id,
            code=code,
            name=name,
            owner_type=OwnerType.COMPANY,
        )
        db.session.add(entity)
        created.append(entity)
    db.session.commit()
    return created


@pytest.fixture()
def owned(db, org, scope, property_record, owners):
    """A property wholly owned by Alpha from January."""
    record_initial_stake(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        owner_entity_id=owners[0].id,
        percentage=Decimal("100"),
        effective_from=JANUARY,
        is_primary_contact=True,
        actor_id=ACTOR,
    )
    db.session.commit()
    return property_record


# ---------------------------------------------------------------------------
# Establishing ownership
# ---------------------------------------------------------------------------


def test_a_stake_can_be_recorded(db, org, scope, owned, owners):
    assert total_allocated(
        db.session, org_id=org.id, property_id=owned.id, on_date=JANUARY
    ) == Decimal("100.0000")


def test_stakes_can_be_entered_one_at_a_time(db, org, scope, property_record, owners):
    """Refusing the first of three would make it impossible to enter any."""
    for owner, share in zip(owners, ("50", "30", "20"), strict=True):
        record_initial_stake(
            db.session,
            org_id=org.id,
            property_id=property_record.id,
            owner_entity_id=owner.id,
            percentage=Decimal(share),
            effective_from=JANUARY,
        )
    db.session.commit()

    assert_fully_allocated(
        db.session, org_id=org.id, property_id=property_record.id, on_date=JANUARY
    )


def test_over_allocating_is_refused(db, org, scope, owned, owners):
    with pytest.raises(BusinessRuleViolation) as exc:
        record_initial_stake(
            db.session,
            org_id=org.id,
            property_id=owned.id,
            owner_entity_id=owners[1].id,
            percentage=Decimal("1"),
            effective_from=JANUARY,
        )
    assert "already allocated" in str(exc.value)


def test_a_partial_allocation_is_reported(db, org, scope, property_record, owners):
    record_initial_stake(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        owner_entity_id=owners[0].id,
        percentage=Decimal("60"),
        effective_from=JANUARY,
    )
    db.session.commit()

    with pytest.raises(BusinessRuleViolation) as exc:
        assert_fully_allocated(
            db.session, org_id=org.id, property_id=property_record.id, on_date=JANUARY
        )
    assert "not 100%" in str(exc.value)


def test_an_unowned_property_is_not_an_error(db, org, scope, property_record):
    """A managed property with no equity record on file is ordinary."""
    assert_fully_allocated(
        db.session, org_id=org.id, property_id=property_record.id, on_date=JANUARY
    )


# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------


def test_a_whole_transfer_closes_one_stake_and_opens_another(db, org, scope, owned, owners):
    transfer = transfer_ownership(
        db.session,
        org_id=org.id,
        property_id=owned.id,
        from_owner_entity_id=owners[0].id,
        to_owner_entity_id=owners[1].id,
        effective_from=MARCH,
        actor_id=ACTOR,
    )
    db.session.commit()

    assert transfer.percentage == Decimal("100.0000")
    assert transfer.retained_stake_id is None

    before = ownership_on(
        db.session, org_id=org.id, property_id=owned.id, on_date=MARCH - dt.timedelta(days=1)
    )
    after = ownership_on(db.session, org_id=org.id, property_id=owned.id, on_date=MARCH)

    assert [stake.owner_entity_id for stake in before] == [owners[0].id]
    assert [stake.owner_entity_id for stake in after] == [owners[1].id]


def test_the_two_stakes_never_both_cover_the_transfer_date(db, org, scope, owned, owners):
    """Otherwise the day-weighted share counts that day twice."""
    transfer_ownership(
        db.session,
        org_id=org.id,
        property_id=owned.id,
        from_owner_entity_id=owners[0].id,
        to_owner_entity_id=owners[1].id,
        effective_from=MARCH,
    )
    db.session.commit()

    assert total_allocated(
        db.session, org_id=org.id, property_id=owned.id, on_date=MARCH
    ) == Decimal("100.0000")


def test_a_partial_transfer_leaves_the_seller_with_the_rest(db, org, scope, owned, owners):
    transfer = transfer_ownership(
        db.session,
        org_id=org.id,
        property_id=owned.id,
        from_owner_entity_id=owners[0].id,
        to_owner_entity_id=owners[1].id,
        effective_from=MARCH,
        percentage=Decimal("35"),
    )
    db.session.commit()

    assert transfer.retained_stake_id is not None
    after = {
        stake.owner_entity_id: Decimal(stake.percentage)
        for stake in ownership_on(db.session, org_id=org.id, property_id=owned.id, on_date=MARCH)
    }
    assert after == {owners[0].id: Decimal("65.0000"), owners[1].id: Decimal("35.0000")}


def test_the_total_is_preserved_across_a_transfer(db, org, scope, owned, owners):
    """The invariant. A transfer that loses four percent must not succeed."""
    transfer_ownership(
        db.session,
        org_id=org.id,
        property_id=owned.id,
        from_owner_entity_id=owners[0].id,
        to_owner_entity_id=owners[1].id,
        effective_from=MARCH,
        percentage=Decimal("40"),
    )
    db.session.commit()

    for day in (MARCH - dt.timedelta(days=1), MARCH, MARCH + dt.timedelta(days=200)):
        assert_fully_allocated(db.session, org_id=org.id, property_id=owned.id, on_date=day)


def test_transferring_more_than_is_held_is_refused(db, org, scope, owned, owners):
    with pytest.raises(BusinessRuleViolation) as exc:
        transfer_ownership(
            db.session,
            org_id=org.id,
            property_id=owned.id,
            from_owner_entity_id=owners[0].id,
            to_owner_entity_id=owners[1].id,
            effective_from=MARCH,
            percentage=Decimal("120"),
        )
    assert "cannot be transferred" in str(exc.value)


def test_transferring_from_an_owner_with_no_stake_is_refused(db, org, scope, owned, owners):
    with pytest.raises(BusinessRuleViolation) as exc:
        transfer_ownership(
            db.session,
            org_id=org.id,
            property_id=owned.id,
            from_owner_entity_id=owners[2].id,
            to_owner_entity_id=owners[1].id,
            effective_from=MARCH,
        )
    assert "nothing to transfer" in str(exc.value)


def test_transferring_to_oneself_is_refused(db, org, scope, owned, owners):
    with pytest.raises(ValidationFailed):
        transfer_ownership(
            db.session,
            org_id=org.id,
            property_id=owned.id,
            from_owner_entity_id=owners[0].id,
            to_owner_entity_id=owners[0].id,
            effective_from=MARCH,
        )


def test_transferring_on_the_day_a_stake_begins_is_refused(db, org, scope, owned, owners):
    """Closing it the day before would end it before it started."""
    with pytest.raises(BusinessRuleViolation) as exc:
        transfer_ownership(
            db.session,
            org_id=org.id,
            property_id=owned.id,
            from_owner_entity_id=owners[0].id,
            to_owner_entity_id=owners[1].id,
            effective_from=JANUARY,
        )
    assert "cannot be closed" in str(exc.value)


def test_another_tenants_property_is_not_found(db, org, other_org, scope, owners):
    with pytest.raises(NotFound):
        transfer_ownership(
            db.session,
            org_id=org.id,
            property_id="019fea00-0000-7000-8000-0000000000ff",
            from_owner_entity_id=owners[0].id,
            to_owner_entity_id=owners[1].id,
            effective_from=MARCH,
        )


# ---------------------------------------------------------------------------
# History, which is the point of storing stakes this way
# ---------------------------------------------------------------------------


def test_the_history_keeps_what_was_true_at_the_time(db, org, scope, owned, owners):
    transfer_ownership(
        db.session,
        org_id=org.id,
        property_id=owned.id,
        from_owner_entity_id=owners[0].id,
        to_owner_entity_id=owners[1].id,
        effective_from=MARCH,
    )
    db.session.commit()

    timeline = ownership_timeline(db.session, org_id=org.id, property_id=owned.id)
    assert len(timeline) == 2
    assert timeline[0].owner_entity_id == owners[0].id
    assert timeline[0].effective_to == MARCH - dt.timedelta(days=1)
    assert not timeline[0].is_current
    assert timeline[1].owner_entity_id == owners[1].id
    assert timeline[1].effective_to is None


def test_a_statement_for_the_month_of_a_transfer_splits_it(db, org, scope, owned, owners):
    """The reason transfers close rather than overwrite.

    March has 31 days; the transfer lands on the 14th. Alpha holds 13 of them
    and Beta 18, and the day-weighted share reflects that without either
    party's stake having been edited.
    """
    from app.services.accounting.statements import ownership_share

    transfer_ownership(
        db.session,
        org_id=org.id,
        property_id=owned.id,
        from_owner_entity_id=owners[0].id,
        to_owner_entity_id=owners[1].id,
        effective_from=MARCH,
    )
    db.session.commit()

    start, end = dt.date(2026, 3, 1), dt.date(2026, 3, 31)
    alpha = ownership_share(
        db.session,
        property_id=owned.id,
        owner_entity_id=owners[0].id,
        period_start=start,
        period_end=end,
    )
    beta = ownership_share(
        db.session,
        property_id=owned.id,
        owner_entity_id=owners[1].id,
        period_start=start,
        period_end=end,
    )

    assert alpha == (Decimal(13) / Decimal(31)).quantize(Decimal("0.000001"))
    assert beta == (Decimal(18) / Decimal(31)).quantize(Decimal("0.000001"))
    assert alpha + beta == Decimal("1.000000")


def test_the_transfer_is_audited_as_a_warning(db, org, scope, owned, owners):
    """Ownership decides who receives money. A change to it is not routine."""
    from app.models.audit import AuditEvent, AuditSeverity

    transfer_ownership(
        db.session,
        org_id=org.id,
        property_id=owned.id,
        from_owner_entity_id=owners[0].id,
        to_owner_entity_id=owners[1].id,
        effective_from=MARCH,
        actor_id=ACTOR,
    )
    db.session.commit()

    events = [
        event
        for event in db.session.query(AuditEvent).all()
        if event.resource_type == "Property" and event.severity == AuditSeverity.WARNING
    ]
    assert len(events) == 1
    assert events[0].payload["to_owner_entity_id"] == owners[1].id

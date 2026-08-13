"""Message threads, and who is allowed to see which.

The tests that carry this module are the visibility ones. A messaging feature
that works is unremarkable; one that shows the office's internal notes to the
resident they are about is the reason the module puts that decision in a query
rather than a template.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.errors import BusinessRuleViolation, NotFound, ValidationFailed
from app.models.resident import CommunicationChannel, MessageDirection
from app.services.notifications.messaging import (
    MAX_BODY,
    Participant,
    assign_thread,
    mark_read,
    open_thread,
    post_message,
    reopen_thread,
    resolve_thread,
    set_status,
    thread_for_participant,
    threads_for_subject,
    visible_threads,
)

pytestmark = pytest.mark.integration

STAFF = "019fea00-0000-7000-8000-00000000a001"


@pytest.fixture()
def resident(db, org, scope, lease_record):
    """A resident with a tenancy on the fixture lease."""
    from app.models.resident import Resident, ResidentStatus, Tenancy, TenancyRole

    person = Resident(
        org_id=org.id,
        first_name="Dana",
        last_name="Okonjo",
        email="dana@test.local",
        status=ResidentStatus.CURRENT,
    )
    db.session.add(person)
    db.session.flush()
    db.session.add(
        Tenancy(
            org_id=org.id,
            lease_id=lease_record.id,
            resident_id=person.id,
            role=TenancyRole.PRIMARY,
            started_at=lease_record.start_date,
        )
    )
    db.session.commit()
    return person


@pytest.fixture()
def their_thread(db, org, scope, lease_record, resident):
    thread = open_thread(
        db.session,
        org_id=org.id,
        title="Kitchen tap is dripping",
        subject_type="lease",
        subject_id=lease_record.id,
        resident_id=resident.id,
        actor_id=STAFF,
    )
    db.session.commit()
    return thread


# ---------------------------------------------------------------------------
# Threads and messages
# ---------------------------------------------------------------------------


def test_a_thread_opens_and_takes_messages(db, org, scope, their_thread):
    post_message(
        db.session,
        thread=their_thread,
        body="We will send somebody on Thursday.",
        sender_label="Dana Whitfield",
        sender_user_id=STAFF,
    )
    db.session.commit()

    assert their_thread.status == "open"
    assert len(their_thread.messages) == 1
    assert their_thread.last_message_at is not None


def test_a_thread_needs_a_title(db, org, scope):
    with pytest.raises(ValidationFailed):
        open_thread(db.session, org_id=org.id, title="   ")


def test_an_unknown_subject_type_is_refused(db, org, scope):
    """A subject nobody recognises is a thread no view will ever find again."""
    with pytest.raises(ValidationFailed) as exc:
        open_thread(
            db.session,
            org_id=org.id,
            title="Anchored to nothing",
            subject_type="spaceship",
            subject_id="019fea00-0000-7000-8000-00000000a002",
        )
    assert "not a subject" in str(exc.value)


def test_a_subject_needs_both_halves(db, org, scope):
    with pytest.raises(ValidationFailed):
        open_thread(db.session, org_id=org.id, title="Half anchored", subject_type="lease")


def test_an_empty_message_is_refused(db, org, scope, their_thread):
    with pytest.raises(ValidationFailed):
        post_message(db.session, thread=their_thread, body="   ", sender_label="Staff")


def test_an_over_long_message_is_refused_not_truncated(db, org, scope, their_thread):
    """Losing the end of somebody's account of a leak is worse than a refusal."""
    with pytest.raises(ValidationFailed) as exc:
        post_message(
            db.session, thread=their_thread, body="x" * (MAX_BODY + 1), sender_label="Staff"
        )
    assert "refused rather than truncated" in str(exc.value)


def test_a_resolved_thread_refuses_new_messages(db, org, scope, their_thread):
    resolve_thread(db.session, thread=their_thread, actor_id=STAFF)
    db.session.commit()

    with pytest.raises(BusinessRuleViolation) as exc:
        post_message(db.session, thread=their_thread, body="One more thing", sender_label="Dana")
    assert "Reopen it" in str(exc.value)


def test_reopening_lets_the_conversation_continue(db, org, scope, their_thread):
    resolve_thread(db.session, thread=their_thread, actor_id=STAFF)
    reopen_thread(db.session, thread=their_thread, actor_id=STAFF)
    post_message(db.session, thread=their_thread, body="It is dripping again.", sender_label="Dana")
    db.session.commit()

    assert their_thread.status == "open"
    assert len(their_thread.messages) == 1


def test_an_internal_note_cannot_land_on_a_resident_visible_thread(db, org, scope, their_thread):
    """The mistake that discloses. Refused at the service, not the template."""
    with pytest.raises(BusinessRuleViolation) as exc:
        post_message(
            db.session,
            thread=their_thread,
            body="Third complaint this month; check the tenancy file.",
            sender_label="Dana Whitfield",
            direction=MessageDirection.INTERNAL,
        )
    assert "Open an internal thread" in str(exc.value)


def test_an_inbound_reply_takes_a_pending_thread_back_to_open(db, org, scope, their_thread):
    set_status(db.session, thread=their_thread, status="pending", actor_id=STAFF)
    post_message(
        db.session,
        thread=their_thread,
        body="Thursday works.",
        sender_label="Dana Okonjo",
        direction=MessageDirection.INBOUND,
    )
    db.session.commit()

    assert their_thread.status == "open"


def test_assigning_and_resolving_are_audited(db, org, scope, their_thread):
    from app.models.audit import AuditAction, AuditEvent

    assign_thread(db.session, thread=their_thread, assignee_id=STAFF, actor_id=STAFF)
    resolve_thread(db.session, thread=their_thread, actor_id=STAFF)
    db.session.commit()

    actions = {event.action for event in db.session.query(AuditEvent).all()}
    assert AuditAction.THREAD_OPENED in actions
    assert AuditAction.THREAD_ASSIGNED in actions
    assert AuditAction.THREAD_RESOLVED in actions


def test_resolving_twice_is_a_no_op(db, org, scope, their_thread):
    resolve_thread(db.session, thread=their_thread, actor_id=STAFF)
    resolve_thread(db.session, thread=their_thread, actor_id=STAFF)
    db.session.commit()
    assert their_thread.status == "resolved"


def test_marking_read_stamps_only_the_other_side(db, org, scope, their_thread):
    post_message(
        db.session, thread=their_thread, body="From the office", sender_label="Dana Whitfield"
    )
    post_message(
        db.session,
        thread=their_thread,
        body="From the resident",
        sender_label="Dana Okonjo",
        direction=MessageDirection.INBOUND,
    )
    db.session.commit()

    assert mark_read(db.session, thread=their_thread, reader_is_staff=True) == 1
    inbound = [m for m in their_thread.messages if m.direction == MessageDirection.INBOUND]
    outbound = [m for m in their_thread.messages if m.direction == MessageDirection.OUTBOUND]
    assert inbound[0].read_at is not None
    assert outbound[0].read_at is None


def test_a_portal_message_is_delivered_on_arrival(db, org, scope, their_thread):
    message = post_message(db.session, thread=their_thread, body="Noted.", sender_label="Staff")
    assert message.delivery_status == "delivered"

    emailed = post_message(
        db.session,
        thread=their_thread,
        body="Also by email.",
        sender_label="Staff",
        channel=CommunicationChannel.EMAIL,
    )
    assert emailed.delivery_status == "pending"


# ---------------------------------------------------------------------------
# Visibility: the part that matters
# ---------------------------------------------------------------------------


def test_a_resident_sees_their_own_thread(db, org, scope, their_thread, resident):
    threads = visible_threads(
        db.session, org_id=org.id, participant=Participant(resident_id=resident.id)
    )
    assert [thread.id for thread in threads] == [their_thread.id]


def test_a_resident_never_sees_an_internal_thread(db, org, scope, lease_record, resident):
    """Anchored to their own lease, and still invisible to them."""
    internal = open_thread(
        db.session,
        org_id=org.id,
        title="Escalation history for this tenancy",
        subject_type="lease",
        subject_id=lease_record.id,
        resident_id=resident.id,
        is_internal=True,
        actor_id=STAFF,
    )
    db.session.commit()

    threads = visible_threads(
        db.session, org_id=org.id, participant=Participant(resident_id=resident.id)
    )
    assert internal.id not in [thread.id for thread in threads]


def test_a_resident_never_sees_another_tenancys_thread(
    db, org, scope, property_record, unit_record, resident
):
    from app.models.leasing import Lease, LeaseStatus
    from app.models.sequences import SequenceKey
    from app.services.common.numbering import next_number

    other_lease = Lease(
        org_id=org.id,
        lease_number=next_number(db.session, SequenceKey.LEASE, org_id=org.id),
        property_id=property_record.id,
        unit_id=unit_record.id,
        status=LeaseStatus.ACTIVE,
        start_date=dt.date(2026, 1, 1),
        end_date=dt.date(2026, 12, 31),
        rent_amount=Decimal("2000.00"),
        security_deposit=Decimal("2000.00"),
    )
    db.session.add(other_lease)
    db.session.flush()
    somebody_elses = open_thread(
        db.session,
        org_id=org.id,
        title="Their boiler",
        subject_type="lease",
        subject_id=other_lease.id,
    )
    db.session.commit()

    threads = visible_threads(
        db.session, org_id=org.id, participant=Participant(resident_id=resident.id)
    )
    assert somebody_elses.id not in [thread.id for thread in threads]


def test_fetching_somebody_elses_thread_is_not_found(db, org, scope, resident):
    """404, not 403: a 403 confirms the thread exists."""
    stranger = open_thread(db.session, org_id=org.id, title="Unanchored office thread")
    db.session.commit()

    with pytest.raises(NotFound):
        thread_for_participant(
            db.session,
            org_id=org.id,
            thread_id=stranger.id,
            participant=Participant(resident_id=resident.id),
        )


def test_a_participant_with_nothing_sees_nothing(db, org, scope, their_thread):
    threads = visible_threads(
        db.session,
        org_id=org.id,
        participant=Participant(resident_id="019fea00-0000-7000-8000-00000000a009"),
    )
    assert threads == []


def test_a_vendor_sees_threads_on_their_own_jobs(db, org, scope, property_record, vendor_record):
    from app.models.maintenance import WorkOrder, WorkOrderStatus
    from app.models.sequences import SequenceKey
    from app.services.common.numbering import next_number

    order = WorkOrder(
        org_id=org.id,
        work_order_number=next_number(db.session, SequenceKey.WORK_ORDER, org_id=org.id),
        property_id=property_record.id,
        title="Replace the tap",
        description="The kitchen mixer tap needs replacing.",
        status=WorkOrderStatus.ASSIGNED,
        vendor_id=vendor_record.id,
    )
    db.session.add(order)
    db.session.flush()

    theirs = open_thread(
        db.session,
        org_id=org.id,
        title="Access arrangements",
        subject_type="work_order",
        subject_id=order.id,
    )
    db.session.commit()

    threads = visible_threads(
        db.session, org_id=org.id, participant=Participant(vendor_id=vendor_record.id)
    )
    assert [thread.id for thread in threads] == [theirs.id]


def test_an_owner_sees_threads_on_a_property_they_hold(db, org, scope, property_record):
    from app.models.org import OwnerEntity, OwnerType
    from app.services.portfolio.ownership import record_initial_stake

    owner = OwnerEntity(
        org_id=org.id, code="ALPHA", name="Alpha Holdings", owner_type=OwnerType.COMPANY
    )
    db.session.add(owner)
    db.session.flush()
    record_initial_stake(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        owner_entity_id=owner.id,
        percentage=Decimal("100"),
        effective_from=dt.date(2026, 1, 1),
    )
    theirs = open_thread(
        db.session,
        org_id=org.id,
        title="Roof quote",
        subject_type="property",
        subject_id=property_record.id,
    )
    db.session.commit()

    threads = visible_threads(
        db.session, org_id=org.id, participant=Participant(owner_entity_id=owner.id)
    )
    assert [thread.id for thread in threads] == [theirs.id]


def test_staff_see_internal_threads_on_a_subject(db, org, scope, lease_record):
    open_thread(
        db.session,
        org_id=org.id,
        title="Internal",
        subject_type="lease",
        subject_id=lease_record.id,
        is_internal=True,
    )
    open_thread(
        db.session,
        org_id=org.id,
        title="Shared",
        subject_type="lease",
        subject_id=lease_record.id,
    )
    db.session.commit()

    everything = threads_for_subject(
        db.session, org_id=org.id, subject_type="lease", subject_id=lease_record.id
    )
    shared_only = threads_for_subject(
        db.session,
        org_id=org.id,
        subject_type="lease",
        subject_id=lease_record.id,
        include_internal=False,
    )
    assert len(everything) == 2
    assert [thread.title for thread in shared_only] == ["Shared"]


def test_an_owner_sees_a_thread_addressed_to_them(db, org, scope, property_record):
    """Not every owner thread is about a property.

    An owner's own identity is a subject in its own right - a distribution
    query is about them, not about any one building. Comparing an owner id
    against the property ids they hold matches nothing, which fails closed but
    hides the thread the office addressed to them.
    """
    from app.models.org import OwnerEntity, OwnerType
    from app.services.portfolio.ownership import record_initial_stake

    owner = OwnerEntity(
        org_id=org.id, code="ALPHA", name="Alpha Holdings", owner_type=OwnerType.COMPANY
    )
    db.session.add(owner)
    db.session.flush()
    record_initial_stake(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        owner_entity_id=owner.id,
        percentage=Decimal("100"),
        effective_from=dt.date(2026, 1, 1),
    )
    addressed = open_thread(
        db.session,
        org_id=org.id,
        title="Your March distribution",
        subject_type="owner",
        subject_id=owner.id,
    )
    db.session.commit()

    threads = visible_threads(
        db.session, org_id=org.id, participant=Participant(owner_entity_id=owner.id)
    )
    assert addressed.id in [thread.id for thread in threads]


def test_an_owner_still_cannot_see_another_owners_thread(db, org, scope, property_record):
    """The subject-typed lookup must not have widened the boundary."""
    from app.models.org import OwnerEntity, OwnerType
    from app.services.portfolio.ownership import record_initial_stake

    mine = OwnerEntity(org_id=org.id, code="MINE", name="Mine", owner_type=OwnerType.COMPANY)
    theirs = OwnerEntity(org_id=org.id, code="THEIRS", name="Theirs", owner_type=OwnerType.COMPANY)
    db.session.add_all([mine, theirs])
    db.session.flush()
    record_initial_stake(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        owner_entity_id=mine.id,
        percentage=Decimal("100"),
        effective_from=dt.date(2026, 1, 1),
    )
    not_mine = open_thread(
        db.session,
        org_id=org.id,
        title="Their distribution",
        subject_type="owner",
        subject_id=theirs.id,
    )
    db.session.commit()

    threads = visible_threads(
        db.session, org_id=org.id, participant=Participant(owner_entity_id=mine.id)
    )
    assert not_mine.id not in [thread.id for thread in threads]


def test_a_resident_thread_survives_the_end_of_the_tenancy(db, org, scope, resident):
    """A deposit dispute outlives the lease it concerns.

    Threads addressed to the resident directly must not vanish the moment their
    tenancy rows go, or the conversation about their deposit disappears exactly
    when they need it.
    """
    from app.models.resident import Tenancy

    direct = open_thread(
        db.session,
        org_id=org.id,
        title="Your deposit disposition",
        resident_id=resident.id,
    )
    db.session.commit()

    for tenancy in db.session.query(Tenancy).all():
        db.session.delete(tenancy)
    db.session.commit()

    threads = visible_threads(
        db.session, org_id=org.id, participant=Participant(resident_id=resident.id)
    )
    assert [thread.id for thread in threads] == [direct.id]

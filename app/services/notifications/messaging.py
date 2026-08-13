"""Conversations between the office, residents, owners, and vendors.

A thread is anchored to whatever it is about - a lease, a work order, an
application - so that the conversation and the record it concerns are found
together rather than in two places by two people.

Two rules carry this module.

**Visibility is a property of the thread, not of the reader.** ``is_internal``
means the office is talking to itself; such a thread never appears in a portal,
for anyone, ever. The alternative - filtering per message at render time - puts
the decision in whichever template was written last, and one template that
forgets is a disclosure. :func:`visible_threads` is the only supported way for a
portal to obtain threads, and it refuses to return internal ones.

**A participant is derived, never supplied.** A resident sees the threads on
their own tenancies; an owner sees the threads on properties they hold a stake
in; a vendor sees the threads on work orders assigned to them. Nothing accepts a
thread id and trusts it - the caller's own records are re-derived on every
request, and anything outside that set is *not found* rather than forbidden,
because a 403 confirms the thread exists.

Messages are stored as text and escaped on render. Storing pre-rendered HTML
makes every future output context depend on a sanitiser decision taken once,
years ago, by someone who has left.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.errors import BusinessRuleViolation, NotFound, ValidationFailed
from app.logging import get_logger
from app.models.audit import AuditAction, AuditSeverity
from app.models.resident import (
    CommunicationChannel,
    Message,
    MessageDirection,
    MessageThread,
)
from app.models.types import utcnow
from app.services.audit.recorder import record_audit_event

__all__ = [
    "MAX_BODY",
    "Participant",
    "SUBJECT_TYPES",
    "ThreadStatus",
    "assign_thread",
    "mark_read",
    "open_thread",
    "post_message",
    "reopen_thread",
    "resolve_thread",
    "set_status",
    "thread_for_participant",
    "threads_for_subject",
    "visible_threads",
]

log = get_logger("services.notifications.messaging")

#: Long enough for a real complaint, short enough that nobody pastes a database
#: into it. Truncating silently would lose the end of somebody's account of a
#: leak, so anything longer is refused rather than trimmed.
MAX_BODY = 10_000
MAX_TITLE = 255

ThreadStatus = Literal["open", "pending", "resolved"]
_STATUSES: tuple[str, ...] = ("open", "pending", "resolved")

#: Anchors a thread may be attached to. A closed set, because a subject_type
#: nobody recognises is a thread no view will ever find again.
SUBJECT_TYPES: tuple[str, ...] = (
    "lease",
    "work_order",
    "maintenance_request",
    "application",
    "owner",
    "property",
    "unit",
    "vendor",
)


@dataclass(frozen=True)
class Participant:
    """Whose threads to return, and how their ownership is derived.

    Exactly one of the id fields is set. The caller does not choose which
    threads they see; this decides, from who they are.
    """

    resident_id: str | None = None
    owner_entity_id: str | None = None
    vendor_id: str | None = None


def _clean_body(body: str) -> str:
    text = (body or "").strip()
    if not text:
        raise ValidationFailed("A message needs something in it.")
    if len(text) > MAX_BODY:
        raise ValidationFailed(
            f"That message is {len(text)} characters; the limit is {MAX_BODY}. "
            "It is refused rather than truncated, because losing the end of an "
            "account of a problem is worse than being asked to shorten it."
        )
    return text


def open_thread(
    session: Session,
    *,
    org_id: str,
    title: str,
    subject_type: str | None = None,
    subject_id: str | None = None,
    property_id: str | None = None,
    unit_id: str | None = None,
    resident_id: str | None = None,
    is_internal: bool = False,
    assigned_to_id: str | None = None,
    actor_id: str | None = None,
) -> MessageThread:
    """Start a conversation, optionally anchored to what it is about."""
    clean_title = (title or "").strip()
    if not clean_title:
        raise ValidationFailed("A thread needs a title.")
    if subject_type is not None and subject_type not in SUBJECT_TYPES:
        raise ValidationFailed(
            f"{subject_type!r} is not a subject a thread can be anchored to. "
            f"Expected one of: {', '.join(SUBJECT_TYPES)}."
        )
    if (subject_type is None) != (subject_id is None):
        raise ValidationFailed("A subject needs both a type and an id, or neither.")

    thread = MessageThread(
        org_id=org_id,
        title=clean_title[:MAX_TITLE],
        subject_type=subject_type,
        subject_id=subject_id,
        property_id=property_id,
        unit_id=unit_id,
        resident_id=resident_id,
        is_internal=is_internal,
        assigned_to_id=assigned_to_id,
        status="open",
    )
    session.add(thread)
    session.flush()

    record_audit_event(
        action=AuditAction.THREAD_OPENED,
        resource_type="MessageThread",
        resource_id=thread.id,
        resource_label=thread.title,
        severity=AuditSeverity.INFO,
        payload={
            "subject_type": subject_type,
            "subject_id": subject_id,
            "is_internal": is_internal,
        },
        reason="Message thread opened.",
        org_id=org_id,
        actor_id=actor_id,
        session=session,
    )
    return thread


def post_message(
    session: Session,
    *,
    thread: MessageThread,
    body: str,
    sender_label: str,
    direction: MessageDirection = MessageDirection.OUTBOUND,
    channel: CommunicationChannel = CommunicationChannel.PORTAL,
    sender_user_id: str | None = None,
    recipient_label: str | None = None,
    external_id: str | None = None,
) -> Message:
    """Add a message and move the thread's clock.

    Not audited per message: the thread's lifecycle is what an audit is asked
    about, and one chain entry per reply would bury everything else in it. The
    messages themselves are the record.
    """
    if thread.status == "resolved":
        raise BusinessRuleViolation(
            "That conversation is resolved. Reopen it before adding to it, so "
            "the reopening is on the record."
        )
    if direction == MessageDirection.INTERNAL and not thread.is_internal:
        raise BusinessRuleViolation(
            "An internal note cannot be added to a thread a resident can read. "
            "Open an internal thread for it."
        )

    now = utcnow()
    message = Message(
        org_id=thread.org_id,
        # The relationship rather than the raw id, so an already-loaded
        # ``thread.messages`` sees the new message instead of going stale. A
        # caller that read the collection before posting would otherwise get
        # the count from before.
        thread=thread,
        direction=direction,
        channel=channel,
        sender_user_id=sender_user_id,
        sender_label=(sender_label or "System")[:150],
        recipient_label=recipient_label[:150] if recipient_label else None,
        body=_clean_body(body),
        external_id=external_id,
        # Portal messages are readable the moment they are written; anything
        # leaving the system is pending until its transport says otherwise.
        delivery_status="delivered" if channel == CommunicationChannel.PORTAL else "pending",
        delivered_at=now if channel == CommunicationChannel.PORTAL else None,
    )
    session.add(message)

    thread.last_message_at = now
    # An inbound reply on a resolved-then-reopened thread should read as open
    # again; a reply on a pending one means it is no longer waiting on us.
    if direction == MessageDirection.INBOUND and thread.status == "pending":
        thread.status = "open"
    session.flush()
    return message


def assign_thread(
    session: Session,
    *,
    thread: MessageThread,
    assignee_id: str | None,
    actor_id: str | None = None,
) -> MessageThread:
    """Put a name against a conversation, or take one off it."""
    thread.assigned_to_id = assignee_id
    session.flush()

    record_audit_event(
        action=AuditAction.THREAD_ASSIGNED,
        resource_type="MessageThread",
        resource_id=thread.id,
        resource_label=thread.title,
        severity=AuditSeverity.INFO,
        payload={"assignee_id": assignee_id},
        reason="Message thread assigned." if assignee_id else "Message thread unassigned.",
        org_id=thread.org_id,
        actor_id=actor_id,
        session=session,
    )
    return thread


def resolve_thread(
    session: Session,
    *,
    thread: MessageThread,
    actor_id: str | None = None,
    reason: str | None = None,
) -> MessageThread:
    """Close a conversation. Idempotent."""
    if thread.status == "resolved":
        return thread
    thread.status = "resolved"
    session.flush()

    record_audit_event(
        action=AuditAction.THREAD_RESOLVED,
        resource_type="MessageThread",
        resource_id=thread.id,
        resource_label=thread.title,
        severity=AuditSeverity.INFO,
        payload={"messages": len(thread.messages)},
        reason=reason or "Message thread resolved.",
        org_id=thread.org_id,
        actor_id=actor_id,
        session=session,
    )
    return thread


def reopen_thread(
    session: Session, *, thread: MessageThread, actor_id: str | None = None
) -> MessageThread:
    """Put a resolved conversation back in the queue."""
    if thread.status != "resolved":
        return thread
    thread.status = "open"
    session.flush()

    record_audit_event(
        action=AuditAction.THREAD_OPENED,
        resource_type="MessageThread",
        resource_id=thread.id,
        resource_label=thread.title,
        severity=AuditSeverity.INFO,
        payload={"reopened": True},
        reason="Message thread reopened.",
        org_id=thread.org_id,
        actor_id=actor_id,
        session=session,
    )
    return thread


def mark_read(session: Session, *, thread: MessageThread, reader_is_staff: bool) -> int:
    """Stamp the messages the reader did not write. Returns how many."""
    now = utcnow()
    written_by_them = (
        (MessageDirection.INBOUND,) if reader_is_staff else (MessageDirection.OUTBOUND,)
    )
    touched = 0
    for message in thread.messages:
        if message.read_at is None and message.direction in written_by_them:
            message.read_at = now
            touched += 1
    if touched:
        session.flush()
    return touched


# ---------------------------------------------------------------------------
# Reading, which is where the visibility rules actually bite
# ---------------------------------------------------------------------------


def threads_for_subject(
    session: Session,
    *,
    org_id: str,
    subject_type: str,
    subject_id: str,
    include_internal: bool = True,
) -> list[MessageThread]:
    """Every thread anchored to one record. Staff-side; internal included."""
    conditions = [
        MessageThread.org_id == org_id,
        MessageThread.subject_type == subject_type,
        MessageThread.subject_id == subject_id,
        MessageThread.deleted_at.is_(None),
    ]
    if not include_internal:
        conditions.append(MessageThread.is_internal.is_(False))

    return list(
        session.execute(
            select(MessageThread)
            .where(*conditions)
            .order_by(MessageThread.last_message_at.desc().nulls_last())
        )
        .scalars()
        .all()
    )


def _owned_subjects(
    session: Session, *, org_id: str, participant: Participant
) -> dict[str, list[str]]:
    """What this participant owns, keyed by the subject type it anchors.

    A mapping rather than a flat list, because the ids are not
    interchangeable: an owner's leases are property ids and their own identity
    is an owner id, and comparing one against the other silently matches
    nothing. That is not a security failure - it fails closed - but it hides
    the thread the office addressed to them, which they will describe as the
    portal being broken.

    Re-derived on every call. Nothing here is taken from the request.
    """
    if participant.resident_id:
        from app.models.resident import Tenancy

        leases = [
            row
            for row in session.execute(
                select(Tenancy.lease_id).where(
                    Tenancy.org_id == org_id,
                    Tenancy.resident_id == participant.resident_id,
                )
            ).scalars()
        ]
        return {"lease": leases}

    if participant.owner_entity_id:
        from app.models.org import OwnershipStake

        properties = [
            row
            for row in session.execute(
                select(OwnershipStake.property_id).where(
                    OwnershipStake.org_id == org_id,
                    OwnershipStake.owner_entity_id == participant.owner_entity_id,
                )
            ).scalars()
        ]
        # Their own identity is a subject in its own right: a thread about a
        # distribution is about the owner, not about any one property.
        return {"property": properties, "owner": [participant.owner_entity_id]}

    if participant.vendor_id:
        from app.models.maintenance import WorkOrder

        orders = [
            row
            for row in session.execute(
                select(WorkOrder.id).where(
                    WorkOrder.org_id == org_id,
                    WorkOrder.vendor_id == participant.vendor_id,
                )
            ).scalars()
        ]
        return {"work_order": orders, "vendor": [participant.vendor_id]}

    return {}


def visible_threads(
    session: Session, *, org_id: str, participant: Participant, limit: int = 50
) -> list[MessageThread]:
    """The threads a portal user may see. The only supported portal entry point.

    Internal threads are excluded here rather than at render time, because a
    filter in a template is one refactor away from being dropped, and the
    failure mode is the office's private notes appearing in a resident's portal.
    """
    owned = _owned_subjects(session, org_id=org_id, participant=participant)

    # One clause per subject type, so each id is compared against ids of its own
    # kind. A single flat list matches nothing across types and hides the thread.
    reachable: list[ColumnElement[bool]] = [
        (MessageThread.subject_type == subject_type) & MessageThread.subject_id.in_(ids)
        for subject_type, ids in owned.items()
        if ids
    ]

    if participant.resident_id:
        # A thread addressed to the resident directly, which survives the end
        # of their tenancy: a deposit dispute outlives the lease it concerns.
        reachable.append(MessageThread.resident_id == participant.resident_id)

    if not reachable:
        return []

    conditions = [
        MessageThread.org_id == org_id,
        MessageThread.deleted_at.is_(None),
        # Not negotiable, and not overridable by a caller.
        MessageThread.is_internal.is_(False),
        or_(*reachable),
    ]

    return list(
        session.execute(
            select(MessageThread)
            .where(*conditions)
            .order_by(MessageThread.last_message_at.desc().nulls_last())
            .limit(limit)
        )
        .scalars()
        .all()
    )


def thread_for_participant(
    session: Session, *, org_id: str, thread_id: str, participant: Participant
) -> MessageThread:
    """One thread, if it is theirs. Raises :class:`NotFound` otherwise.

    404 rather than 403: telling somebody a thread exists but is not theirs
    turns the portal into a way to enumerate the building's correspondence.
    """
    for thread in visible_threads(session, org_id=org_id, participant=participant, limit=500):
        if thread.id == thread_id:
            return thread
    raise NotFound("That conversation was not found.")


def set_status(
    session: Session, *, thread: MessageThread, status: str, actor_id: str | None = None
) -> MessageThread:
    """Move a thread through open / pending / resolved."""
    if status not in _STATUSES:
        raise ValidationFailed(f"{status!r} is not a thread status.")
    if status == "resolved":
        return resolve_thread(session, thread=thread, actor_id=actor_id)
    if thread.status == "resolved":
        reopen_thread(session, thread=thread, actor_id=actor_id)
    thread.status = status
    session.flush()
    return thread

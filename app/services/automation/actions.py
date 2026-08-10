"""What a rule is allowed to *do*, and the registry that decides.

Every action is two functions rather than one:

* ``describe`` builds a plain-English, machine-readable statement of intent. It
  reads, and never writes.
* ``apply`` performs the change and returns its result.

A dry run calls ``describe`` and stops. This is the whole reason for the split.
The alternative - one function that checks a ``dry_run`` flag - relies on every
author of every future action remembering to check it, and the day somebody
forgets, a rule marked "safe to test" sends real notices. Here, dry run cannot
mutate because in dry run the mutating function is never called at all.

Actions are looked up by name in an explicit registry. An unknown action is a
validation error at save time and a failed step at run time; it is never a
silent no-op, because a rule that quietly does nothing is worse than one that
loudly fails.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import Session

from app.errors import ValidationFailed
from app.logging import get_logger
from app.models.automation import Approval, ApprovalStatus
from app.models.maintenance import Priority, WorkOrder, WorkOrderStatus

__all__ = [
    "ACTIONS",
    "ActionContext",
    "ActionSpec",
    "apply_action",
    "describe_action",
    "known_actions",
    "validate_actions",
]

log = get_logger("services.automation.actions")

#: A single rule may not fan out further than this.
MAX_ACTIONS_PER_RULE = 20


@dataclass
class ActionContext:
    """Everything an action handler is allowed to know."""

    session: Session
    org_id: str
    #: The event payload the rule matched on.
    payload: dict[str, Any]
    subject_type: str | None = None
    subject_id: str | None = None
    actor_id: str | None = None
    rule_code: str | None = None
    run_id: str | None = None


@dataclass(frozen=True)
class ActionSpec:
    """One registered action."""

    name: str
    summary: str
    describe: Callable[[ActionContext, dict[str, Any]], dict[str, Any]]
    apply: Callable[[ActionContext, dict[str, Any]], dict[str, Any]]
    validate: Callable[[dict[str, Any]], None] | None = None
    #: Actions that can spend money or reach a resident are never run live
    #: without an approval when the rule asks for one.
    is_sensitive: bool = False


ACTIONS: dict[str, ActionSpec] = {}


def register(spec: ActionSpec) -> ActionSpec:
    if spec.name in ACTIONS:  # pragma: no cover - registration is static
        raise RuntimeError(f"Automation action {spec.name!r} is already registered.")
    ACTIONS[spec.name] = spec
    return spec


def known_actions() -> list[str]:
    return sorted(ACTIONS)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_actions(actions: Any) -> list[dict[str, Any]]:
    """Check an action list before it is stored."""
    if actions in (None, []):
        return []
    if not isinstance(actions, list):
        raise ValidationFailed("Actions must be a list.")
    if len(actions) > MAX_ACTIONS_PER_RULE:
        raise ValidationFailed(f"A rule may not have more than {MAX_ACTIONS_PER_RULE} actions.")

    for entry in actions:
        if not isinstance(entry, dict):
            raise ValidationFailed("Each action must be an object.")
        name = entry.get("type")
        spec = ACTIONS.get(name) if isinstance(name, str) else None
        if spec is None:
            raise ValidationFailed(
                f"Unknown automation action {name!r}. Supported: {', '.join(known_actions())}."
            )
        params = entry.get("params", {})
        if not isinstance(params, dict):
            raise ValidationFailed(f"Action '{name}' params must be an object.")
        if spec.validate is not None:
            spec.validate(params)
    return actions


def describe_action(context: ActionContext, entry: dict[str, Any]) -> dict[str, Any]:
    """What this action *would* do. Reads only."""
    spec = _spec_for(entry)
    return spec.describe(context, entry.get("params", {}) or {})


def apply_action(context: ActionContext, entry: dict[str, Any]) -> dict[str, Any]:
    """Perform the action."""
    spec = _spec_for(entry)
    return spec.apply(context, entry.get("params", {}) or {})


def _spec_for(entry: dict[str, Any]) -> ActionSpec:
    name = entry.get("type")
    spec = ACTIONS.get(name) if isinstance(name, str) else None
    if spec is None:
        raise ValidationFailed(f"Unknown automation action {name!r}.")
    return spec


# ---------------------------------------------------------------------------
# Subject resolution
# ---------------------------------------------------------------------------


def _work_order(context: ActionContext) -> WorkOrder:
    """The work order this run is about, or a clear failure."""
    if context.subject_type != "work_order" or not context.subject_id:
        raise ValidationFailed("This action only applies to a work-order event.")
    order = context.session.get(WorkOrder, context.subject_id)
    if order is None or order.org_id != context.org_id:
        raise ValidationFailed("The work order for this event no longer exists.")
    return order


def _required(params: dict[str, Any], key: str) -> Any:
    if key not in params or params[key] in (None, ""):
        raise ValidationFailed(f"This action needs a '{key}' parameter.")
    return params[key]


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationFailed("That amount is not a number.") from exc


# ---------------------------------------------------------------------------
# Work-order actions
# ---------------------------------------------------------------------------


def _validate_priority(params: dict[str, Any]) -> None:
    value = _required(params, "priority")
    if value not in {p.value for p in Priority}:
        raise ValidationFailed(
            f"Unknown priority {value!r}. Supported: {', '.join(p.value for p in Priority)}."
        )


def _describe_set_priority(context: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    order = _work_order(context)
    target = params.get("priority")
    return {
        "action": "set_work_order_priority",
        "work_order": order.work_order_number,
        "from": order.priority.value,
        "to": target,
        "would_change": order.priority.value != target,
    }


def _apply_set_priority(context: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    from app.services.maintenance.service import _append_event

    order = _work_order(context)
    previous = order.priority
    order.priority = Priority(params["priority"])
    if previous != order.priority:
        _append_event(
            context.session,
            order,
            event_type="priority_changed",
            actor_id=context.actor_id,
            actor_label=f"Automation: {context.rule_code}" if context.rule_code else "Automation",
            note=f"Priority raised from {previous.value} to {order.priority.value} by rule.",
        )
    context.session.flush()
    return {
        "action": "set_work_order_priority",
        "work_order": order.work_order_number,
        "from": previous.value,
        "to": order.priority.value,
        "changed": previous != order.priority,
    }


register(
    ActionSpec(
        name="set_work_order_priority",
        summary="Raise or lower a work order's priority.",
        describe=_describe_set_priority,
        apply=_apply_set_priority,
        validate=_validate_priority,
    )
)


def _validate_assign(params: dict[str, Any]) -> None:
    if not params.get("vendor_id") and not params.get("assigned_user_id"):
        raise ValidationFailed("Assignment needs either a 'vendor_id' or an 'assigned_user_id'.")


def _describe_assign(context: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    order = _work_order(context)
    return {
        "action": "assign_work_order",
        "work_order": order.work_order_number,
        "current_vendor_id": order.vendor_id,
        "current_assignee_id": order.assigned_user_id,
        "to_vendor_id": params.get("vendor_id"),
        "to_user_id": params.get("assigned_user_id"),
        # The compliance gate is not evaluated here: describing must not query
        # its way into a side effect, and a dry run reports intent, not outcome.
        "note": "Vendor insurance compliance is checked when the rule goes live.",
    }


def _apply_assign(context: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    from app.services.maintenance.service import transition_work_order

    order = _work_order(context)
    target = order.status if order.status != WorkOrderStatus.OPEN else WorkOrderStatus.ASSIGNED
    transition_work_order(
        context.session,
        work_order=order,
        target=target,
        actor_id=context.actor_id,
        actor_label=f"Automation: {context.rule_code}" if context.rule_code else "Automation",
        note="Assigned by automation rule.",
        vendor_id=params.get("vendor_id"),
        assigned_user_id=params.get("assigned_user_id"),
    )
    return {
        "action": "assign_work_order",
        "work_order": order.work_order_number,
        "vendor_id": order.vendor_id,
        "assigned_user_id": order.assigned_user_id,
        "status": order.status.value,
    }


register(
    ActionSpec(
        name="assign_work_order",
        summary="Dispatch a work order to a vendor or a member of staff.",
        describe=_describe_assign,
        apply=_apply_assign,
        validate=_validate_assign,
        is_sensitive=True,
    )
)


def _validate_note(params: dict[str, Any]) -> None:
    note = _required(params, "note")
    if not isinstance(note, str) or len(note) > 2_000:
        raise ValidationFailed("A note must be text of at most 2000 characters.")


def _describe_note(context: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    order = _work_order(context)
    return {
        "action": "add_work_order_note",
        "work_order": order.work_order_number,
        "note": params.get("note"),
        "resident_visible": bool(params.get("resident_visible")),
    }


def _apply_note(context: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    from app.services.maintenance.service import _append_event

    order = _work_order(context)
    event = _append_event(
        context.session,
        order,
        event_type="note",
        actor_id=context.actor_id,
        actor_label=f"Automation: {context.rule_code}" if context.rule_code else "Automation",
        note=params["note"],
        resident_visible=bool(params.get("resident_visible")),
    )
    return {
        "action": "add_work_order_note",
        "work_order": order.work_order_number,
        "event_id": event.id,
    }


register(
    ActionSpec(
        name="add_work_order_note",
        summary="Append a note to a work order's timeline.",
        describe=_describe_note,
        apply=_apply_note,
        validate=_validate_note,
    )
)


# ---------------------------------------------------------------------------
# Cross-cutting actions
# ---------------------------------------------------------------------------


def _validate_emit(params: dict[str, Any]) -> None:
    event_type = _required(params, "event_type")
    if not isinstance(event_type, str) or len(event_type) > 80:
        raise ValidationFailed("An event type must be text of at most 80 characters.")
    if event_type.startswith("automation."):
        # Otherwise a rule can trigger itself through the outbox, and the
        # cascade guard should be the second line of defence, not the first.
        raise ValidationFailed("A rule may not emit events in the 'automation.' namespace.")


def _describe_emit(context: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "emit_event",
        "event_type": params.get("event_type"),
        "aggregate_type": context.subject_type or "automation",
        "aggregate_id": context.subject_id,
        "would_notify_subscribers": True,
    }


def _apply_emit(context: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    from app.services.integration.webhooks import publish_event

    event = publish_event(
        context.session,
        org_id=context.org_id,
        event_type=params["event_type"],
        aggregate_type=context.subject_type or "automation",
        aggregate_id=context.subject_id or (context.run_id or ""),
        payload={
            "rule": context.rule_code,
            "run_id": context.run_id,
            "subject": context.payload,
            **(params.get("payload") or {}),
        },
    )
    return {"action": "emit_event", "event_type": event.event_type, "outbox_id": event.id}


register(
    ActionSpec(
        name="emit_event",
        summary="Publish a domain event to webhook subscribers.",
        describe=_describe_emit,
        apply=_apply_emit,
        validate=_validate_emit,
        is_sensitive=True,
    )
)


def _validate_approval(params: dict[str, Any]) -> None:
    kind = _required(params, "kind")
    if not isinstance(kind, str) or len(kind) > 60:
        raise ValidationFailed("An approval kind must be text of at most 60 characters.")
    if "amount" in params and params["amount"] is not None:
        _money(params["amount"])


def _describe_approval(context: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "request_approval",
        "kind": params.get("kind"),
        "subject_type": context.subject_type,
        "subject_id": context.subject_id,
        "amount": str(params["amount"]) if params.get("amount") is not None else None,
    }


def _apply_approval(context: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    approval = Approval(
        org_id=context.org_id,
        kind=params["kind"],
        subject_type=context.subject_type or "automation_rule",
        subject_id=context.subject_id or context.run_id or "",
        subject_label=params.get("label"),
        status=ApprovalStatus.PENDING,
        amount=_money(params["amount"]) if params.get("amount") is not None else None,
        justification=params.get("justification"),
        # Deliberately not the actor: automation cannot request and approve.
        requested_by_id=None,
        required_role_code=params.get("required_role_code"),
        payload={"rule": context.rule_code, "run_id": context.run_id, "event": context.payload},
    )
    context.session.add(approval)
    context.session.flush()
    return {"action": "request_approval", "approval_id": approval.id, "kind": approval.kind}


register(
    ActionSpec(
        name="request_approval",
        summary="Raise a human approval checkpoint.",
        describe=_describe_approval,
        apply=_apply_approval,
        validate=_validate_approval,
    )
)


def _validate_audit(params: dict[str, Any]) -> None:
    note = _required(params, "note")
    if not isinstance(note, str) or len(note) > 2_000:
        raise ValidationFailed("A note must be text of at most 2000 characters.")


def _describe_audit(context: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "record_audit_note",
        "note": params.get("note"),
        "subject_type": context.subject_type,
        "subject_id": context.subject_id,
    }


def _apply_audit(context: ActionContext, params: dict[str, Any]) -> dict[str, Any]:
    from app.models.audit import AuditAction, AuditSeverity
    from app.services.audit.recorder import record_audit_event

    record_audit_event(
        action=AuditAction.AUTOMATION_ACTION_EXECUTED,
        resource_type=context.subject_type,
        resource_id=context.subject_id,
        severity=AuditSeverity.INFO,
        payload={"note": params["note"], "rule": context.rule_code},
        reason=params["note"],
        org_id=context.org_id,
        actor_id=context.actor_id,
        session=context.session,
    )
    return {"action": "record_audit_note", "note": params["note"]}


register(
    ActionSpec(
        name="record_audit_note",
        summary="Write a note to the audit trail.",
        describe=_describe_audit,
        apply=_apply_audit,
        validate=_validate_audit,
    )
)

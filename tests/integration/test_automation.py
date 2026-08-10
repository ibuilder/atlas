"""The rule engine.

Three properties carry the safety of the whole feature, and each has a test
that fails loudly if it stops holding: a dry run changes nothing, a rule cannot
trigger itself forever, and a condition is data rather than code.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.errors import BusinessRuleViolation, ValidationFailed
from app.models.automation import ApprovalStatus, AutomationRule, RunStatus, WorkflowRun
from app.models.maintenance import Priority, WorkOrderStatus
from app.services.automation import (
    activate_rule,
    create_rule,
    dispatch_event,
    evaluate_conditions,
    promote_rule_to_live,
    resolve_field,
    run_rule,
    update_rule,
    validate_actions,
    validate_conditions,
)

pytestmark = pytest.mark.integration


def _rule(db, org, **overrides):
    params = {
        "code": "esc-emergency",
        "name": "Escalate emergencies",
        "trigger_event": "work_order.created",
        "conditions": [{"field": "priority", "op": "eq", "value": "emergency"}],
        "actions": [{"type": "add_work_order_note", "params": {"note": "Escalated by rule."}}],
    }
    params.update(overrides)
    rule = create_rule(db.session, org_id=org.id, **params)
    db.session.commit()
    return rule


def _live(db, org, *, subject_id=None, **overrides):
    """A rule that has served a clean dry run and been promoted."""
    rule = _rule(db, org, **overrides)
    activate_rule(db.session, rule=rule)
    run_rule(
        db.session,
        rule=rule,
        event_type=rule.trigger_event,
        payload={"priority": "emergency", "amount": "1.00"},
        subject_type="work_order" if subject_id else None,
        subject_id=subject_id,
        force_dry_run=True,
    )
    promote_rule_to_live(db.session, rule=rule)
    db.session.commit()
    return rule


@pytest.fixture()
def work_order(db, org, scope, property_record):
    from app.services.maintenance.service import create_work_order

    order = create_work_order(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        title="No heat",
        description="No heat in the unit.",
        priority=Priority.NORMAL,
    )
    db.session.commit()
    return order


# ------------------------------------------------------------------ conditions


def test_a_dotted_path_resolves_through_nested_data():
    payload = {"work_order": {"vendor": {"name": "Acme"}}, "tags": ["urgent", "hvac"]}
    assert resolve_field(payload, "work_order.vendor.name") == "Acme"
    assert resolve_field(payload, "tags.1") == "hvac"
    assert resolve_field(payload, "work_order.missing") is None


def test_field_resolution_cannot_reach_python_attributes():
    """The sandbox property: data in, data out, no object graph."""
    payload = {"priority": "emergency"}
    assert resolve_field(payload, "__class__") is None
    assert resolve_field(payload, "priority.__class__.__mro__") is None
    assert resolve_field(payload, "__class__.__base__.__subclasses__") is None


def test_money_compares_numerically_not_as_text():
    """ "1200.00" > "900.00" is false as a string. That would be somebody's rent."""
    conditions = [{"field": "amount", "op": "gt", "value": "900.00"}]
    assert evaluate_conditions(conditions, {"amount": "1200.00"}).matched
    assert not evaluate_conditions(conditions, {"amount": "150.00"}).matched


def test_groups_combine_conditions():
    conditions = [
        {
            "any": [
                {"field": "priority", "op": "eq", "value": "emergency"},
                {"field": "trade", "op": "eq", "value": "hvac"},
            ]
        }
    ]
    assert evaluate_conditions(conditions, {"priority": "normal", "trade": "hvac"}).matched
    assert not evaluate_conditions(conditions, {"priority": "normal", "trade": "plumbing"}).matched


def test_none_group_negates():
    conditions = [{"none": [{"field": "status", "op": "eq", "value": "cancelled"}]}]
    assert evaluate_conditions(conditions, {"status": "open"}).matched
    assert not evaluate_conditions(conditions, {"status": "cancelled"}).matched


def test_a_failed_condition_says_which_one():
    result = evaluate_conditions(
        [{"field": "priority", "op": "eq", "value": "emergency"}], {"priority": "normal"}
    )
    assert not result.matched
    assert "priority" in (result.reason or "")


def test_an_unknown_operator_is_refused_at_save_time():
    with pytest.raises(ValidationFailed):
        validate_conditions([{"field": "priority", "op": "__import__", "value": "os"}])


def test_an_unknown_operator_evaluates_false_rather_than_true():
    """Fail closed: a rule that cannot be understood must not fire."""
    result = evaluate_conditions([{"field": "x", "op": "nonexistent", "value": 1}], {"x": 1})
    assert not result.matched


def test_deeply_nested_conditions_are_refused():
    node: dict = {"field": "x", "op": "eq", "value": 1}
    for _ in range(12):
        node = {"all": [node]}
    with pytest.raises(ValidationFailed):
        validate_conditions([node])


def test_comparing_incompatible_types_is_false_not_a_crash():
    assert not evaluate_conditions(
        [{"field": "when", "op": "gt", "value": "hello"}], {"when": 5}
    ).matched


# --------------------------------------------------------------------- actions


def test_an_unknown_action_is_refused_at_save_time():
    with pytest.raises(ValidationFailed):
        validate_actions([{"type": "delete_everything", "params": {}}])


def test_an_action_with_bad_parameters_is_refused():
    with pytest.raises(ValidationFailed):
        validate_actions([{"type": "set_work_order_priority", "params": {"priority": "urgentish"}}])


def test_a_rule_may_not_emit_into_the_automation_namespace():
    """Otherwise a rule can loop through the outbox before the guard even runs."""
    with pytest.raises(ValidationFailed):
        validate_actions([{"type": "emit_event", "params": {"event_type": "automation.triggered"}}])


# ------------------------------------------------------------------- authoring


def test_a_new_rule_starts_inactive_and_in_dry_run(db, org, scope):
    rule = _rule(db, org)
    assert rule.is_dry_run is True
    assert rule.is_active is False
    assert rule.is_live is False


def test_promotion_requires_evidence_of_a_dry_run(db, org, scope):
    rule = _rule(db, org)
    activate_rule(db.session, rule=rule)
    db.session.commit()
    with pytest.raises(BusinessRuleViolation):
        promote_rule_to_live(db.session, rule=rule)


def test_a_failed_dry_run_is_not_evidence_of_readiness(db, org, scope):
    """A dry run that blew up proves the rule is broken, not that it is ready."""
    rule = _rule(db, org, conditions=[])
    activate_rule(db.session, rule=rule)
    run = run_rule(
        db.session,
        rule=rule,
        event_type=rule.trigger_event,
        payload={},
        force_dry_run=True,
    )
    db.session.commit()

    assert run.status == RunStatus.FAILED
    with pytest.raises(BusinessRuleViolation):
        promote_rule_to_live(db.session, rule=rule)


def test_editing_a_live_rule_returns_it_to_dry_run(db, org, scope, work_order):
    """A one-character edit must not ship untested behaviour to real data."""
    rule = _live(db, org, subject_id=work_order.id)
    assert rule.is_dry_run is False

    update_rule(
        db.session,
        rule=rule,
        conditions=[{"field": "priority", "op": "eq", "value": "urgent"}],
    )
    db.session.commit()
    assert rule.is_dry_run is True


def test_dry_run_state_cannot_be_set_through_a_field_update(db, org, scope):
    rule = _rule(db, org)
    with pytest.raises(ValidationFailed):
        update_rule(db.session, rule=rule, is_dry_run=False)


# ------------------------------------------------------------------- execution


def test_a_matching_event_fires_the_rule(db, org, scope, work_order):
    _live(db, org, subject_id=work_order.id)
    outcome = dispatch_event(
        db.session,
        org_id=org.id,
        event_type="work_order.created",
        payload={"priority": "emergency"},
        subject_type="work_order",
        subject_id=work_order.id,
    )
    db.session.commit()

    assert outcome.executed == 1
    assert outcome.runs[0].status == RunStatus.SUCCEEDED
    assert len(outcome.runs[0].steps) == 1
    assert any(event.event_type == "note" for event in work_order.events)


def test_a_non_matching_event_records_a_skip_with_a_reason(db, org, scope, work_order):
    """ "Why didn't it fire?" needs an answer as much as "why did it?"."""
    _live(db, org, subject_id=work_order.id)
    outcome = dispatch_event(
        db.session,
        org_id=org.id,
        event_type="work_order.created",
        payload={"priority": "normal"},
        subject_type="work_order",
        subject_id=work_order.id,
    )
    db.session.commit()

    assert outcome.skipped == 1
    run = outcome.runs[0]
    assert run.status == RunStatus.SKIPPED
    assert "priority" in (run.outcome_reason or "")


def test_an_inactive_rule_never_runs(db, org, scope, work_order):
    _rule(db, org)  # created, never activated
    outcome = dispatch_event(
        db.session,
        org_id=org.id,
        event_type="work_order.created",
        payload={"priority": "emergency"},
        subject_type="work_order",
        subject_id=work_order.id,
    )
    assert outcome.runs == []


def test_a_dry_run_changes_nothing(db, org, scope, work_order):
    """The property the whole describe/apply split exists to guarantee."""
    rule = _rule(
        db,
        org,
        actions=[{"type": "set_work_order_priority", "params": {"priority": "emergency"}}],
    )
    activate_rule(db.session, rule=rule)
    db.session.commit()

    outcome = dispatch_event(
        db.session,
        org_id=org.id,
        event_type="work_order.created",
        payload={"priority": "emergency"},
        subject_type="work_order",
        subject_id=work_order.id,
    )
    db.session.commit()

    run = outcome.runs[0]
    assert run.status == RunStatus.SUCCEEDED
    assert run.is_dry_run is True
    # It recorded what it would have done...
    assert run.steps[0].output_payload["to"] == "emergency"
    assert run.steps[0].output_payload["would_change"] is True
    # ...and did not do it.
    db.session.refresh(work_order)
    assert work_order.priority == Priority.NORMAL
    assert not [e for e in work_order.events if e.event_type == "priority_changed"]


def test_a_live_run_does_change_things(db, org, scope, work_order):
    """The counterpart: the same rule, promoted, actually acts."""
    _live(
        db,
        org,
        subject_id=work_order.id,
        actions=[{"type": "set_work_order_priority", "params": {"priority": "emergency"}}],
    )
    dispatch_event(
        db.session,
        org_id=org.id,
        event_type="work_order.created",
        payload={"priority": "emergency"},
        subject_type="work_order",
        subject_id=work_order.id,
    )
    db.session.commit()

    db.session.refresh(work_order)
    assert work_order.priority == Priority.EMERGENCY


def test_a_failing_action_fails_its_own_run_and_nothing_else(db, org, scope, work_order):
    """A rule is not entitled to roll back the transaction that triggered it."""
    _live(
        db,
        org,
        subject_id=work_order.id,
        actions=[{"type": "set_work_order_priority", "params": {"priority": "emergency"}}],
    )
    outcome = dispatch_event(
        db.session,
        org_id=org.id,
        event_type="work_order.created",
        payload={"priority": "emergency"},
        subject_type="work_order",
        subject_id="00000000-0000-0000-0000-000000000000",
    )
    db.session.commit()

    assert outcome.failed == 1
    run = outcome.runs[0]
    assert run.status == RunStatus.FAILED
    assert run.error_message


def test_repeated_failures_disable_the_rule(db, org, scope, work_order):
    rule = _live(
        db,
        org,
        subject_id=work_order.id,
        actions=[{"type": "set_work_order_priority", "params": {"priority": "emergency"}}],
    )
    rule.failure_threshold = 2
    db.session.commit()

    for _ in range(2):
        dispatch_event(
            db.session,
            org_id=org.id,
            event_type="work_order.created",
            payload={"priority": "emergency"},
            subject_type="work_order",
            subject_id="00000000-0000-0000-0000-000000000000",
        )
    db.session.commit()

    assert rule.auto_disabled_at is not None
    assert rule.is_live is False


def test_a_success_resets_the_failure_count(db, org, scope, work_order):
    rule = _live(db, org, subject_id=work_order.id)
    rule.consecutive_failures = 3
    db.session.commit()

    dispatch_event(
        db.session,
        org_id=org.id,
        event_type="work_order.created",
        payload={"priority": "emergency"},
        subject_type="work_order",
        subject_id=work_order.id,
    )
    db.session.commit()
    assert rule.consecutive_failures == 0


def test_the_hourly_ceiling_throttles_a_runaway_rule(db, org, scope, work_order):
    rule = _live(db, org, subject_id=work_order.id)
    # The dry run that earned the promotion counts against the ceiling as well.
    rule.max_runs_per_hour = 3
    db.session.commit()

    statuses = []
    for _ in range(4):
        outcome = dispatch_event(
            db.session,
            org_id=org.id,
            event_type="work_order.created",
            payload={"priority": "emergency"},
            subject_type="work_order",
            subject_id=work_order.id,
        )
        statuses.append(outcome.runs[0].status)
    db.session.commit()

    assert statuses[:2] == [RunStatus.SUCCEEDED, RunStatus.SUCCEEDED]
    assert statuses[2:] == [RunStatus.SKIPPED, RunStatus.SKIPPED]
    latest = (
        db.session.query(WorkflowRun)
        .filter(WorkflowRun.is_dry_run.is_(False))
        .order_by(WorkflowRun.created_at.desc())
        .first()
    )
    assert "throttled" in (latest.outcome_reason or "")


# ---------------------------------------------------------------------- safety


def test_a_rule_that_triggers_itself_stops_at_the_first_hop(db, org, scope, work_order):
    """The loop that would otherwise run until the worker dies."""
    _live(
        db,
        org,
        subject_id=work_order.id,
        code="self-trigger",
        trigger_event="work_order.updated",
        conditions=[],
        actions=[
            {"type": "emit_event", "params": {"event_type": "work_order.updated"}},
            {"type": "add_work_order_note", "params": {"note": "Looped."}},
        ],
    )

    # Re-entry is simulated the way it happens in production: the action's own
    # emitted event is dispatched from inside the running rule.
    from app.services.automation.engine import _chain
    from app.services.automation.engine import dispatch_event as dispatch

    rule = db.session.query(AutomationRule).filter_by(code="self-trigger").one()
    token = _chain.set((rule.id,))
    try:
        outcome = dispatch(
            db.session,
            org_id=org.id,
            event_type="work_order.updated",
            payload={},
            subject_type="work_order",
            subject_id=work_order.id,
        )
    finally:
        _chain.reset(token)
    db.session.commit()

    assert outcome.runs == []
    assert "re-enter" in (outcome.blocked_reason or "")


def test_a_cascade_stops_at_the_depth_limit(db, org, scope, work_order):
    from app.services.automation.engine import MAX_CASCADE_DEPTH, _chain

    _live(db, org, subject_id=work_order.id, trigger_event="work_order.updated", conditions=[])
    token = _chain.set(tuple(f"rule-{n}" for n in range(MAX_CASCADE_DEPTH)))
    try:
        outcome = dispatch_event(
            db.session,
            org_id=org.id,
            event_type="work_order.updated",
            payload={},
            subject_type="work_order",
            subject_id=work_order.id,
        )
    finally:
        _chain.reset(token)
    db.session.commit()

    assert outcome.runs == []
    assert "cascade depth" in (outcome.blocked_reason or "")


def test_the_chain_is_empty_again_after_a_run(db, org, scope, work_order):
    """A leaked chain entry would block every later rule in the same worker."""
    from app.services.automation.engine import _chain

    _live(db, org, subject_id=work_order.id)
    dispatch_event(
        db.session,
        org_id=org.id,
        event_type="work_order.created",
        payload={"priority": "emergency"},
        subject_type="work_order",
        subject_id=work_order.id,
    )
    db.session.commit()
    assert _chain.get() == ()


def test_a_sensitive_rule_pauses_for_approval_instead_of_acting(db, org, scope, work_order):
    _live(
        db,
        org,
        subject_id=work_order.id,
        conditions=[],
        actions=[{"type": "set_work_order_priority", "params": {"priority": "emergency"}}],
        requires_approval=True,
        approval_threshold_amount=Decimal("500.00"),
    )
    outcome = dispatch_event(
        db.session,
        org_id=org.id,
        event_type="work_order.created",
        payload={"amount": "750.00"},
        subject_type="work_order",
        subject_id=work_order.id,
    )
    db.session.commit()

    run = outcome.runs[0]
    assert run.status == RunStatus.AWAITING_APPROVAL
    assert run.approval_id is not None
    # Nothing happened while the approval is pending.
    db.session.refresh(work_order)
    assert work_order.priority == Priority.NORMAL


def test_an_approval_raised_by_automation_has_no_requester_to_self_approve(
    db, org, scope, work_order
):
    from app.models.automation import Approval

    _live(
        db,
        org,
        subject_id=work_order.id,
        conditions=[],
        actions=[{"type": "add_work_order_note", "params": {"note": "hi"}}],
        requires_approval=True,
    )
    dispatch_event(
        db.session,
        org_id=org.id,
        event_type="work_order.created",
        payload={},
        subject_type="work_order",
        subject_id=work_order.id,
    )
    db.session.commit()

    approval = db.session.query(Approval).one()
    assert approval.status == ApprovalStatus.PENDING
    assert approval.requested_by_id is None
    assert approval.can_be_decided_by("any-real-user-id") is True


def test_below_the_threshold_the_rule_acts_without_asking(db, org, scope, work_order):
    _live(
        db,
        org,
        subject_id=work_order.id,
        conditions=[],
        actions=[{"type": "set_work_order_priority", "params": {"priority": "urgent"}}],
        requires_approval=True,
        approval_threshold_amount=Decimal("500.00"),
    )
    dispatch_event(
        db.session,
        org_id=org.id,
        event_type="work_order.created",
        payload={"amount": "100.00"},
        subject_type="work_order",
        subject_id=work_order.id,
    )
    db.session.commit()

    db.session.refresh(work_order)
    assert work_order.priority == Priority.URGENT


def test_a_threshold_rule_with_no_amount_fails_closed(db, org, scope, work_order):
    """No amount to measure means ask a human, not assume it is small."""
    _live(
        db,
        org,
        subject_id=work_order.id,
        conditions=[],
        actions=[{"type": "set_work_order_priority", "params": {"priority": "urgent"}}],
        requires_approval=True,
        approval_threshold_amount=Decimal("500.00"),
    )
    outcome = dispatch_event(
        db.session,
        org_id=org.id,
        event_type="work_order.created",
        payload={},
        subject_type="work_order",
        subject_id=work_order.id,
    )
    db.session.commit()
    assert outcome.runs[0].status == RunStatus.AWAITING_APPROVAL


# ------------------------------------------------------------------- isolation


def test_rules_do_not_cross_organizations(db, org, scope, other_org, work_order):
    _live(db, org, subject_id=work_order.id)
    outcome = dispatch_event(
        db.session,
        org_id=other_org.id,
        event_type="work_order.created",
        payload={"priority": "emergency"},
        subject_type="work_order",
        subject_id=work_order.id,
    )
    assert outcome.runs == []


def test_every_run_records_its_trigger_and_its_steps(db, org, scope, work_order):
    """The audit question - "why did this resident get that?" - must be answerable."""
    _live(db, org, subject_id=work_order.id)
    dispatch_event(
        db.session,
        org_id=org.id,
        event_type="work_order.created",
        payload={"priority": "emergency", "work_order_number": work_order.work_order_number},
        subject_type="work_order",
        subject_id=work_order.id,
    )
    db.session.commit()

    run = db.session.query(WorkflowRun).filter(WorkflowRun.is_dry_run.is_(False)).one()
    assert run.trigger_event == "work_order.created"
    assert run.trigger_payload["priority"] == "emergency"
    assert run.subject_id == work_order.id
    assert run.duration_ms is not None
    assert run.steps[0].action_type == "add_work_order_note"
    assert run.steps[0].status.value == "succeeded"


def test_ordering_follows_rule_priority(db, org, scope, work_order):
    _live(db, org, subject_id=work_order.id, code="second", priority=200, conditions=[])
    _live(db, org, subject_id=work_order.id, code="first", priority=10, conditions=[])

    outcome = dispatch_event(
        db.session,
        org_id=org.id,
        event_type="work_order.created",
        payload={},
        subject_type="work_order",
        subject_id=work_order.id,
    )
    db.session.commit()

    codes = [db.session.get(AutomationRule, run.rule_id).code for run in outcome.runs]
    assert codes == ["first", "second"]


def test_a_work_order_action_on_a_non_work_order_event_fails_cleanly(db, org, scope, work_order):
    _live(db, org, subject_id=work_order.id, conditions=[], trigger_event="lease.signed")
    outcome = dispatch_event(
        db.session,
        org_id=org.id,
        event_type="lease.signed",
        payload={},
        subject_type="lease",
        subject_id="00000000-0000-0000-0000-000000000000",
    )
    db.session.commit()

    assert outcome.runs[0].status == RunStatus.FAILED
    assert "work-order" in (outcome.runs[0].error_message or "")


def test_assignment_moves_an_open_work_order_to_assigned(db, org, scope, work_order, vendor_record):
    from app.services.maintenance.service import transition_work_order

    transition_work_order(db.session, work_order=work_order, target=WorkOrderStatus.OPEN)
    db.session.commit()

    _live(
        db,
        org,
        subject_id=work_order.id,
        conditions=[],
        actions=[{"type": "assign_work_order", "params": {"vendor_id": vendor_record.id}}],
    )
    dispatch_event(
        db.session,
        org_id=org.id,
        event_type="work_order.created",
        payload={},
        subject_type="work_order",
        subject_id=work_order.id,
    )
    db.session.commit()

    db.session.refresh(work_order)
    assert work_order.vendor_id == vendor_record.id
    assert work_order.status == WorkOrderStatus.ASSIGNED


# ----------------------------------------------------------------- end to end


def test_raising_a_work_order_triggers_a_live_rule(db, org, scope, property_record, work_order):
    """The wiring, not the engine: a real domain call reaches a real rule."""
    from app.models.integration import OutboxEvent
    from app.services.maintenance.service import create_work_order

    _live(
        db,
        org,
        subject_id=work_order.id,
        conditions=[{"field": "priority", "op": "eq", "value": "emergency"}],
        actions=[{"type": "add_work_order_note", "params": {"note": "Emergency: escalated."}}],
    )

    raised = create_work_order(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        title="Burst pipe",
        description="Water coming through the ceiling.",
        priority=Priority.EMERGENCY,
    )
    db.session.commit()

    notes = [event for event in raised.events if event.event_type == "note"]
    assert [note.note for note in notes] == ["Emergency: escalated."]
    # The same change is announced to outside subscribers, in the same commit.
    assert (
        db.session.query(OutboxEvent).filter_by(aggregate_id=raised.id).one().event_type
        == "work_order.created"
    )


def test_a_normal_work_order_does_not_trigger_the_emergency_rule(
    db, org, scope, property_record, work_order
):
    from app.services.maintenance.service import create_work_order

    _live(
        db,
        org,
        subject_id=work_order.id,
        conditions=[{"field": "priority", "op": "eq", "value": "emergency"}],
        actions=[{"type": "add_work_order_note", "params": {"note": "Emergency: escalated."}}],
    )

    raised = create_work_order(
        db.session,
        org_id=org.id,
        property_id=property_record.id,
        title="Dripping tap",
        description="Slow drip in the kitchen.",
        priority=Priority.NORMAL,
    )
    db.session.commit()
    assert not [event for event in raised.events if event.event_type == "note"]

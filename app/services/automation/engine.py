"""The rule engine: match an event to rules, and run them safely.

A rule engine is a mechanism for letting somebody who cannot deploy code cause
arbitrary changes to production data. Everything here exists to make that
sentence acceptable rather than alarming:

* **Dry run is the default.** A new rule is created with ``is_dry_run`` set, and
  only an explicit, audited promotion clears it. A dry run records exactly what
  it would have done and changes nothing - guaranteed structurally, because in
  dry run the mutating half of an action is never called (see :mod:`.actions`).
* **Cascades terminate.** Actions can emit events, and events trigger rules. The
  engine tracks the chain in a context variable: a rule cannot appear twice in
  one chain, and no chain runs deeper than :data:`MAX_CASCADE_DEPTH`. A rule
  that triggers itself stops at the first hop and says so.
* **Runaway rules degrade, not everything else.** Each rule has an hourly
  ceiling, and consecutive failures disable it. A misconfigured rule burns its
  own quota, not the platform's.
* **Every run is explicable.** A run row exists whether the rule fired, was
  skipped, or failed - with the trigger payload, the reason, and each step's
  input and output. "Why did this happen?" always has an answer, and so does
  "why *didn't* it?".

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import BusinessRuleViolation, NotFound, ValidationFailed
from app.logging import get_logger, redact_value
from app.models.audit import AuditAction, AuditOutcome, AuditSeverity
from app.models.automation import (
    Approval,
    ApprovalStatus,
    AutomationRule,
    RunStatus,
    StepStatus,
    TriggerType,
    WorkflowRun,
    WorkflowRunStep,
)
from app.models.types import utcnow
from app.observability import AUTOMATION_RUNS
from app.services.audit.recorder import record_audit_event
from app.services.automation.actions import (
    ActionContext,
    apply_action,
    describe_action,
    validate_actions,
)
from app.services.automation.conditions import evaluate_conditions, validate_conditions

__all__ = [
    "MAX_CASCADE_DEPTH",
    "MAX_RULES_PER_EVENT",
    "RuleOutcome",
    "activate_rule",
    "create_rule",
    "dispatch_event",
    "promote_rule_to_live",
    "run_rule",
    "update_rule",
]

log = get_logger("services.automation.engine")

#: An event triggers a rule, whose action emits an event, which triggers another
#: rule. Three hops is generous for legitimate use and short enough that a loop
#: is caught before it costs anything.
MAX_CASCADE_DEPTH = 3

#: One event may not fan out to an unbounded number of rules in a single pass.
MAX_RULES_PER_EVENT = 25

#: The rules already executing in this cascade chain. A rule that appears twice
#: is a loop, whatever route it took to get back here.
_chain: ContextVar[tuple[str, ...]] = ContextVar("atlas_automation_chain", default=())


@dataclass
class RuleOutcome:
    """The result of evaluating one event against the rule set."""

    runs: list[WorkflowRun] = field(default_factory=list)
    #: Rules that matched the event name but whose conditions did not hold.
    skipped: int = 0
    executed: int = 0
    failed: int = 0
    blocked_reason: str | None = None

    @property
    def fired(self) -> list[WorkflowRun]:
        return [run for run in self.runs if run.status == RunStatus.SUCCEEDED]


@contextmanager
def _chain_entry(rule_id: str):  # noqa: ANN202
    token = _chain.set((*_chain.get(), rule_id))
    try:
        yield
    finally:
        _chain.reset(token)


# ---------------------------------------------------------------------------
# Authoring
# ---------------------------------------------------------------------------


def create_rule(
    session: Session,
    *,
    org_id: str,
    code: str,
    name: str,
    trigger_event: str | None = None,
    trigger_type: TriggerType = TriggerType.EVENT,
    conditions: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
    description: str | None = None,
    priority: int = 100,
    max_runs_per_hour: int = 100,
    requires_approval: bool = False,
    approval_threshold_amount: Decimal | None = None,
    approver_role_code: str | None = None,
    actor_id: str | None = None,
) -> AutomationRule:
    """Author a rule. It starts inactive and in dry run - always."""
    if trigger_type == TriggerType.EVENT and not trigger_event:
        raise ValidationFailed("An event-triggered rule needs a trigger event.")

    rule = AutomationRule(
        org_id=org_id,
        code=code,
        name=name,
        description=description,
        trigger_type=trigger_type,
        trigger_event=trigger_event,
        conditions=validate_conditions(conditions or []),
        actions=validate_actions(actions or []),
        priority=priority,
        max_runs_per_hour=max_runs_per_hour,
        requires_approval=requires_approval,
        approval_threshold_amount=approval_threshold_amount,
        approver_role_code=approver_role_code,
        # Not negotiable at creation. A rule earns its way to live.
        is_active=False,
        is_dry_run=True,
        consecutive_failures=0,
        run_count=0,
        success_count=0,
    )
    session.add(rule)
    session.flush()

    record_audit_event(
        action=AuditAction.AUTOMATION_TRIGGERED,
        resource_type="automation_rule",
        resource_id=rule.id,
        resource_label=rule.code,
        severity=AuditSeverity.INFO,
        payload={"created": True, "trigger_event": trigger_event},
        reason="Automation rule created in dry run.",
        org_id=org_id,
        actor_id=actor_id,
        session=session,
    )
    return rule


def update_rule(
    session: Session,
    *,
    rule: AutomationRule,
    conditions: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
    actor_id: str | None = None,
    **fields: Any,
) -> AutomationRule:
    """Change a rule's logic.

    Editing the logic of a live rule returns it to dry run. The alternative is
    that a one-character edit to a working rule silently ships untested
    behaviour to production data.
    """
    logic_changed = False
    if conditions is not None:
        rule.conditions = validate_conditions(conditions)
        logic_changed = True
    if actions is not None:
        rule.actions = validate_actions(actions)
        logic_changed = True

    for key, value in fields.items():
        if key in {"is_dry_run", "is_active", "consecutive_failures", "auto_disabled_at"}:
            raise ValidationFailed(f"'{key}' is changed through its own operation, not an update.")
        if not hasattr(rule, key):
            raise ValidationFailed(f"Unknown rule field {key!r}.")
        setattr(rule, key, value)

    if logic_changed and not rule.is_dry_run:
        rule.is_dry_run = True
        log.info(
            "rule returned to dry run after a logic change",
            extra={"event": "automation.demoted", "rule": rule.code},
        )

    session.flush()
    record_audit_event(
        action=AuditAction.AUTOMATION_TRIGGERED,
        resource_type="automation_rule",
        resource_id=rule.id,
        resource_label=rule.code,
        severity=AuditSeverity.NOTICE if logic_changed else AuditSeverity.INFO,
        payload={"logic_changed": logic_changed, "returned_to_dry_run": logic_changed},
        reason="Automation rule updated.",
        org_id=rule.org_id,
        actor_id=actor_id,
        session=session,
    )
    return rule


def activate_rule(
    session: Session, *, rule: AutomationRule, actor_id: str | None = None
) -> AutomationRule:
    """Turn a rule on. It still runs in dry run until promoted."""
    rule.is_active = True
    rule.auto_disabled_at = None
    rule.consecutive_failures = 0
    session.flush()
    record_audit_event(
        action=AuditAction.AUTOMATION_TRIGGERED,
        resource_type="automation_rule",
        resource_id=rule.id,
        resource_label=rule.code,
        payload={"is_active": True, "is_dry_run": rule.is_dry_run},
        reason="Automation rule activated.",
        org_id=rule.org_id,
        actor_id=actor_id,
        session=session,
    )
    return rule


def promote_rule_to_live(
    session: Session,
    *,
    rule: AutomationRule,
    actor_id: str | None = None,
    require_dry_run_evidence: bool = True,
) -> AutomationRule:
    """Clear dry run, so the rule starts changing real data.

    By default this refuses unless the rule has completed a dry run without
    failing. "We tested it" should be a fact in the database, not a claim - and
    a dry run that *failed* is evidence the rule is broken, not evidence it is
    ready, so it does not count.
    """
    if require_dry_run_evidence:
        observed = session.execute(
            select(func.count())
            .select_from(WorkflowRun)
            .where(
                WorkflowRun.org_id == rule.org_id,
                WorkflowRun.rule_id == rule.id,
                WorkflowRun.is_dry_run.is_(True),
                WorkflowRun.status.in_([RunStatus.SUCCEEDED, RunStatus.SKIPPED]),
            )
        ).scalar_one()
        if not observed:
            raise BusinessRuleViolation(
                "This rule has not completed a dry run without failing. Let it observe real "
                "events first, review what it would have done, then promote it."
            )

    rule.is_dry_run = False
    rule.is_active = True
    rule.auto_disabled_at = None
    session.flush()

    record_audit_event(
        action=AuditAction.AUTOMATION_TRIGGERED,
        resource_type="automation_rule",
        resource_id=rule.id,
        resource_label=rule.code,
        severity=AuditSeverity.NOTICE,
        payload={"is_dry_run": False},
        reason="Automation rule promoted to live.",
        org_id=rule.org_id,
        actor_id=actor_id,
        session=session,
    )
    return rule


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def dispatch_event(
    session: Session,
    *,
    org_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    actor_id: str | None = None,
    correlation_id: str | None = None,
) -> RuleOutcome:
    """Run every active rule listening for this event.

    Never raises on a rule's behalf: a failing rule fails its own run and is
    recorded. The caller's transaction is doing real work, and an automation
    rule is not entitled to roll it back.
    """
    outcome = RuleOutcome()
    chain = _chain.get()
    if len(chain) >= MAX_CASCADE_DEPTH:
        outcome.blocked_reason = f"cascade depth {MAX_CASCADE_DEPTH} reached"
        AUTOMATION_RUNS.labels("cascade_blocked").inc()
        log.warning(
            "automation cascade stopped at the depth limit",
            extra={"event": "automation.cascade_blocked", "event_type": event_type},
        )
        record_audit_event(
            action=AuditAction.AUTOMATION_CASCADE_BLOCKED,
            resource_type=subject_type,
            resource_id=subject_id,
            severity=AuditSeverity.WARNING,
            outcome=AuditOutcome.DENIED,
            payload={"event_type": event_type, "depth": len(chain)},
            reason=outcome.blocked_reason,
            org_id=org_id,
            session=session,
        )
        return outcome

    rules = (
        session.execute(
            select(AutomationRule)
            .where(
                AutomationRule.org_id == org_id,
                AutomationRule.trigger_event == event_type,
                AutomationRule.is_active.is_(True),
                AutomationRule.auto_disabled_at.is_(None),
                AutomationRule.deleted_at.is_(None),
            )
            .order_by(AutomationRule.priority, AutomationRule.created_at)
            .limit(MAX_RULES_PER_EVENT)
        )
        .scalars()
        .all()
    )

    for rule in rules:
        if rule.id in chain:
            # The rule is already somewhere in this chain: it has triggered
            # itself, directly or by a longer route.
            outcome.blocked_reason = f"rule {rule.code} would re-enter its own cascade"
            AUTOMATION_RUNS.labels("cascade_blocked").inc()
            log.warning(
                "automation rule refused re-entry into its own cascade",
                extra={"event": "automation.self_trigger", "rule": rule.code},
            )
            continue

        run = run_rule(
            session,
            rule=rule,
            event_type=event_type,
            payload=payload or {},
            subject_type=subject_type,
            subject_id=subject_id,
            actor_id=actor_id,
            correlation_id=correlation_id,
        )
        outcome.runs.append(run)
        if run.status == RunStatus.SKIPPED:
            outcome.skipped += 1
        elif run.status == RunStatus.FAILED:
            outcome.failed += 1
        else:
            outcome.executed += 1

    return outcome


def run_rule(
    session: Session,
    *,
    rule: AutomationRule,
    event_type: str,
    payload: dict[str, Any],
    subject_type: str | None = None,
    subject_id: str | None = None,
    actor_id: str | None = None,
    correlation_id: str | None = None,
    force_dry_run: bool = False,
) -> WorkflowRun:
    """Evaluate and, if it matches, execute one rule. Always returns a run."""
    dry_run = force_dry_run or rule.is_dry_run
    started = utcnow()

    run = WorkflowRun(
        org_id=rule.org_id,
        rule_id=rule.id,
        status=RunStatus.RUNNING,
        is_dry_run=dry_run,
        trigger_event=event_type,
        trigger_payload=redact_value(payload) if isinstance(payload, dict) else {},
        subject_type=subject_type,
        subject_id=subject_id,
        started_at=started,
        correlation_id=correlation_id,
    )
    session.add(run)
    session.flush()

    def _finish(status: RunStatus, *, reason: str | None = None, error: str | None = None) -> None:
        run.status = status
        run.outcome_reason = reason
        run.error_message = error
        run.finished_at = utcnow()
        run.duration_ms = max(0, int((run.finished_at - started).total_seconds() * 1000))
        session.flush()

    # Throttle before doing any work: a runaway rule should cost one COUNT(*).
    if _is_throttled(session, rule, now=started):
        AUTOMATION_RUNS.labels("throttled").inc()
        _finish(RunStatus.SKIPPED, reason=f"throttled at {rule.max_runs_per_hour} runs per hour")
        log.warning(
            "automation rule throttled",
            extra={"event": "automation.throttled", "rule": rule.code},
        )
        return run

    verdict = evaluate_conditions(rule.conditions, payload)
    if not verdict.matched:
        AUTOMATION_RUNS.labels("skipped").inc()
        _finish(RunStatus.SKIPPED, reason=verdict.reason or "conditions did not match")
        return run

    rule.run_count += 1
    rule.last_run_at = started

    # An approval checkpoint stops a *live* run before anything happens. A dry
    # run has nothing to approve, so it proceeds and reports what it would do.
    if not dry_run and _needs_approval(rule, payload):
        approval = _raise_approval(session, rule=rule, run=run, payload=payload)
        run.approval_id = approval.id
        AUTOMATION_RUNS.labels("awaiting_approval").inc()
        _finish(RunStatus.AWAITING_APPROVAL, reason="waiting for a human decision")
        return run

    context = ActionContext(
        session=session,
        org_id=rule.org_id,
        payload=payload,
        subject_type=subject_type,
        subject_id=subject_id,
        actor_id=actor_id,
        rule_code=rule.code,
        run_id=run.id,
    )

    with _chain_entry(rule.id):
        for sequence, entry in enumerate(rule.actions or [], start=1):
            step = WorkflowRunStep(
                org_id=rule.org_id,
                run_id=run.id,
                sequence=sequence,
                action_type=str(entry.get("type", "unknown")),
                status=StepStatus.RUNNING,
                input_payload=redact_value(entry.get("params") or {}),
                started_at=utcnow(),
            )
            session.add(step)
            session.flush()

            try:
                result = (
                    describe_action(context, entry) if dry_run else apply_action(context, entry)
                )
            except Exception as exc:  # noqa: BLE001 - a rule may not break its caller
                step.status = StepStatus.FAILED
                step.error_message = str(exc)[:2_000]
                step.finished_at = utcnow()
                _record_failure(session, rule=rule, run=run, step=step, error=exc)
                AUTOMATION_RUNS.labels("failed").inc()
                _finish(
                    RunStatus.FAILED,
                    reason=f"action {step.action_type} failed",
                    error=str(exc)[:2_000],
                )
                return run

            step.status = StepStatus.SUCCEEDED
            step.output_payload = redact_value(result) if isinstance(result, dict) else {}
            step.finished_at = utcnow()
            session.flush()

    rule.success_count += 1
    rule.consecutive_failures = 0
    AUTOMATION_RUNS.labels("dry_run" if dry_run else "succeeded").inc()
    _finish(
        RunStatus.SUCCEEDED,
        reason="dry run: nothing was changed" if dry_run else None,
    )

    if not dry_run:
        record_audit_event(
            action=AuditAction.AUTOMATION_TRIGGERED,
            resource_type=subject_type,
            resource_id=subject_id,
            resource_label=rule.code,
            payload={"rule": rule.code, "run_id": run.id, "actions": len(rule.actions or [])},
            reason=f"Automation rule {rule.code} executed.",
            org_id=rule.org_id,
            actor_id=actor_id,
            session=session,
        )
    return run


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def _is_throttled(session: Session, rule: AutomationRule, *, now: dt.datetime) -> bool:
    if rule.max_runs_per_hour <= 0:
        return False
    since = now - dt.timedelta(hours=1)
    recent = session.execute(
        select(func.count())
        .select_from(WorkflowRun)
        .where(
            WorkflowRun.org_id == rule.org_id,
            WorkflowRun.rule_id == rule.id,
            WorkflowRun.created_at >= since,
        )
    ).scalar_one()
    # The run being evaluated is already persisted, so the ceiling is exclusive.
    return bool(recent - 1 >= rule.max_runs_per_hour)


def _needs_approval(rule: AutomationRule, payload: dict[str, Any]) -> bool:
    if not rule.requires_approval:
        return False
    if rule.approval_threshold_amount is None:
        return True
    amount = payload.get("amount")
    if amount is None:
        # A threshold rule with no amount to measure fails closed: ask a human.
        return True
    try:
        return Decimal(str(amount)) >= rule.approval_threshold_amount
    except Exception:  # noqa: BLE001
        return True


def _raise_approval(
    session: Session, *, rule: AutomationRule, run: WorkflowRun, payload: dict[str, Any]
) -> Approval:
    amount = payload.get("amount")
    approval = Approval(
        org_id=rule.org_id,
        kind="automation_rule",
        subject_type=run.subject_type or "automation_rule",
        subject_id=run.subject_id or rule.id,
        subject_label=rule.name,
        status=ApprovalStatus.PENDING,
        amount=Decimal(str(amount)) if amount is not None else None,
        threshold=rule.approval_threshold_amount,
        justification=f"Automation rule {rule.code} is waiting to act.",
        requested_by_id=None,
        required_role_code=rule.approver_role_code,
        payload={"rule": rule.code, "run_id": run.id, "event": redact_value(payload)},
    )
    session.add(approval)
    session.flush()

    record_audit_event(
        action=AuditAction.APPROVAL_REQUESTED,
        resource_type="approval",
        resource_id=approval.id,
        resource_label=rule.code,
        severity=AuditSeverity.NOTICE,
        payload={"rule": rule.code, "run_id": run.id},
        reason="Automation paused for approval.",
        org_id=rule.org_id,
        session=session,
    )
    return approval


def _record_failure(
    session: Session,
    *,
    rule: AutomationRule,
    run: WorkflowRun,
    step: WorkflowRunStep,
    error: Exception,
) -> None:
    """Count the failure and, past the threshold, take the rule out of service."""
    rule.consecutive_failures += 1
    log.error(
        "automation action failed",
        extra={
            "event": "automation.action_failed",
            "rule": rule.code,
            "action": step.action_type,
            "run_id": run.id,
        },
        exc_info=error,
    )
    record_audit_event(
        action=AuditAction.AUTOMATION_RULE_FAILED,
        resource_type="automation_rule",
        resource_id=rule.id,
        resource_label=rule.code,
        outcome=AuditOutcome.FAILURE,
        severity=AuditSeverity.WARNING,
        payload={"action": step.action_type, "run_id": run.id},
        reason=str(error)[:255],
        org_id=rule.org_id,
        session=session,
    )

    if rule.failure_threshold > 0 and rule.consecutive_failures >= rule.failure_threshold:
        rule.auto_disabled_at = utcnow()
        AUTOMATION_RUNS.labels("auto_disabled").inc()
        log.error(
            "automation rule auto-disabled after consecutive failures",
            extra={
                "event": "automation.auto_disabled",
                "rule": rule.code,
                "failures": rule.consecutive_failures,
            },
        )
        record_audit_event(
            action=AuditAction.AUTOMATION_RULE_DISABLED,
            resource_type="automation_rule",
            resource_id=rule.id,
            resource_label=rule.code,
            severity=AuditSeverity.CRITICAL,
            payload={"consecutive_failures": rule.consecutive_failures},
            reason="Disabled automatically after repeated failures.",
            org_id=rule.org_id,
            session=session,
        )
    session.flush()


def rule_by_code(session: Session, *, org_id: str, code: str) -> AutomationRule:
    rule = session.execute(
        select(AutomationRule).where(
            AutomationRule.org_id == org_id,
            AutomationRule.code == code,
            AutomationRule.deleted_at.is_(None),
        )
    ).scalar_one_or_none()
    if rule is None:
        raise NotFound(f"No automation rule with code {code!r}.")
    return rule

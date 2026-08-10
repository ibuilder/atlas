"""Workflow automation: the condition language, the action registry, the engine.

SPDX-License-Identifier: MIT
"""

from app.services.automation.actions import (
    ACTIONS,
    ActionContext,
    ActionSpec,
    known_actions,
    validate_actions,
)
from app.services.automation.approvals import (
    approval_by_id,
    approve,
    consume_approval,
    expire_stale_approvals,
    payload_fingerprint,
    pending_approvals,
    reject,
    request_approval,
)
from app.services.automation.conditions import (
    ConditionResult,
    evaluate_conditions,
    resolve_field,
    validate_conditions,
)
from app.services.automation.engine import (
    MAX_CASCADE_DEPTH,
    RuleOutcome,
    activate_rule,
    create_rule,
    dispatch_event,
    promote_rule_to_live,
    rule_by_code,
    run_rule,
    update_rule,
)

__all__ = [
    "ACTIONS",
    "MAX_CASCADE_DEPTH",
    "ActionContext",
    "ActionSpec",
    "ConditionResult",
    "RuleOutcome",
    "activate_rule",
    "approval_by_id",
    "approve",
    "consume_approval",
    "expire_stale_approvals",
    "payload_fingerprint",
    "pending_approvals",
    "reject",
    "request_approval",
    "create_rule",
    "dispatch_event",
    "evaluate_conditions",
    "known_actions",
    "promote_rule_to_live",
    "resolve_field",
    "rule_by_code",
    "run_rule",
    "update_rule",
    "validate_actions",
    "validate_conditions",
]

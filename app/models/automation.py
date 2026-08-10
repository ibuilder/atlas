"""Workflow automation: rules, runs, steps, and approvals.

The rule engine is deliberately data-driven - trigger, conditions, actions - so
an operations manager can express "escalate any emergency work order still
unassigned after two hours" without a deploy. Three properties make that safe
rather than reckless:

* **Dry run.** A rule can execute against real data and record everything it
  *would* have done, changing nothing. No rule reaches production data before
  someone has seen its dry-run output.
* **Full run history.** Every execution stores its trigger payload, each step's
  input and output, and the reason it stopped. "Why did this resident get a
  notice?" always has an answer.
* **Approval checkpoints.** Any action above a configured threshold pauses for a
  human. Automation that can move money without a person in the loop is not a
  feature.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import SoftDeleteMixin, TenantModel
from app.models.types import GUID, JSONType, Money, UTCDateTime, enum_column, utcnow

__all__ = [
    "Approval",
    "ApprovalStatus",
    "AutomationRule",
    "RunStatus",
    "StepStatus",
    "TriggerType",
    "WorkflowRun",
    "WorkflowRunStep",
]


class TriggerType(StrEnum):
    EVENT = "event"
    SCHEDULE = "schedule"
    MANUAL = "manual"
    WEBHOOK = "webhook"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: Conditions evaluated false - a normal, expected outcome worth recording.
    SKIPPED = "skipped"
    AWAITING_APPROVAL = "awaiting_approval"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class AutomationRule(TenantModel, SoftDeleteMixin):
    """A trigger-condition-action rule."""

    __tablename__ = "automation_rules"
    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_automation_rules_org_code"),
        CheckConstraint("max_runs_per_hour >= 0", name="throttle_non_negative"),
        Index("ix_automation_rules_trigger", "org_id", "trigger_event", "is_active"),
        Index("ix_automation_rules_org_created", "org_id", "created_at"),
    )

    code: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)

    trigger_type: Mapped[TriggerType] = mapped_column(
        enum_column(TriggerType), nullable=False, default=TriggerType.EVENT
    )
    #: Domain event name, e.g. ``work_order.created``.
    trigger_event: Mapped[str | None] = mapped_column(String(80), index=True)
    #: Cron expression for scheduled rules.
    schedule: Mapped[str | None] = mapped_column(String(80))

    #: ``[{"field": "priority", "op": "eq", "value": "emergency"}]`` - evaluated
    #: by a restricted interpreter, never by ``eval``.
    conditions: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    #: ``[{"type": "assign_work_order", "params": {...}}]``
    actions: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: New rules start here. Promotion to live requires an explicit action and
    #: is audited.
    is_dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    #: A runaway rule should degrade, not take the platform with it.
    max_runs_per_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    #: Consecutive failures before the rule disables itself.
    failure_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    auto_disabled_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approval_threshold_amount: Mapped[Decimal | None] = mapped_column(Money)
    approver_role_code: Mapped[str | None] = mapped_column(String(60))

    last_run_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    runs: Mapped[list[WorkflowRun]] = relationship(back_populates="rule", passive_deletes=True)

    @property
    def is_live(self) -> bool:
        """Only a rule that is active, not in dry run, and not auto-disabled acts."""
        return self.is_active and not self.is_dry_run and self.auto_disabled_at is None


class WorkflowRun(TenantModel):
    """One execution of a rule."""

    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("ix_workflow_runs_rule_started", "rule_id", "created_at"),
        Index("ix_workflow_runs_org_status", "org_id", "status"),
        Index("ix_workflow_runs_org_created", "org_id", "created_at"),
    )

    rule_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("automation_rules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[RunStatus] = mapped_column(
        enum_column(RunStatus), nullable=False, default=RunStatus.PENDING, index=True
    )
    #: True when this run changed nothing and only recorded intent.
    is_dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    trigger_event: Mapped[str | None] = mapped_column(String(80))
    trigger_payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    subject_type: Mapped[str | None] = mapped_column(String(40), index=True)
    subject_id: Mapped[str | None] = mapped_column(GUID, index=True)

    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    #: Why conditions did not match, or why execution stopped.
    outcome_reason: Mapped[str | None] = mapped_column(String(255))
    error_message: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(String(128), index=True)
    approval_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("approvals.id", ondelete="SET NULL"), index=True
    )

    rule: Mapped[AutomationRule] = relationship(back_populates="runs")
    steps: Mapped[list[WorkflowRunStep]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="WorkflowRunStep.sequence",
        passive_deletes=True,
    )


class WorkflowRunStep(TenantModel):
    """One action within a run, with its input, output, and retries."""

    __tablename__ = "workflow_run_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_workflow_run_steps_run_seq"),
        Index("ix_workflow_run_steps_org_created", "org_id", "created_at"),
    )

    run_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    action_type: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[StepStatus] = mapped_column(
        enum_column(StepStatus), nullable=False, default=StepStatus.PENDING
    )

    input_payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    output_payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)

    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    run: Mapped[WorkflowRun] = relationship(back_populates="steps")

    @property
    def can_retry(self) -> bool:
        return self.status == StepStatus.FAILED and self.attempt < self.max_attempts


class Approval(TenantModel):
    """A human checkpoint on a sensitive action.

    Generic by design: bill payment above a threshold, a bank detail change, a
    permission grant, and an automated rent increase all route through the same
    request-decide-audit path, so a new sensitive action inherits the control
    instead of reinventing it.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        Index("ix_approvals_org_status", "org_id", "status"),
        Index("ix_approvals_subject", "org_id", "subject_type", "subject_id"),
        Index("ix_approvals_org_created", "org_id", "created_at"),
    )

    kind: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    subject_type: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[str] = mapped_column(GUID, nullable=False)
    subject_label: Mapped[str | None] = mapped_column(String(255))

    status: Mapped[ApprovalStatus] = mapped_column(
        enum_column(ApprovalStatus), nullable=False, default=ApprovalStatus.PENDING, index=True
    )
    amount: Mapped[Decimal | None] = mapped_column(Money)
    threshold: Mapped[Decimal | None] = mapped_column(Money)
    justification: Mapped[str | None] = mapped_column(Text)

    requested_by_id: Mapped[str | None] = mapped_column(GUID, index=True)
    requested_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    required_role_code: Mapped[str | None] = mapped_column(String(60))
    #: Whoever requested it can never be the one who approves it.
    decided_by_id: Mapped[str | None] = mapped_column(GUID, index=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    decision_note: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)

    #: Snapshot of the change being approved, so the approver sees exactly what
    #: they authorised even if the record moves on afterwards.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    @property
    def is_pending(self) -> bool:
        if self.status != ApprovalStatus.PENDING:
            return False
        return self.expires_at is None or self.expires_at > utcnow()

    def can_be_decided_by(self, user_id: str) -> bool:
        """Separation of duties, enforced structurally."""
        return user_id != self.requested_by_id

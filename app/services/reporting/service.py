"""Running reports and delivering them.

The one behaviour worth stating plainly: **recipients resolve at send time.**
A scheduled report stores who should receive it as a reference - a user id, a
role code - and turns that into an address at the moment of sending. Storing
resolved addresses is how a departed employee keeps receiving the books for two
years after they left, which is a real and common data-protection failure with
a one-line fix.

Everything else follows the house pattern: the run row exists whether the
report succeeded or failed, the output is stored as a document so it is
retained and access-controlled like any other file, and repeated failures take
a schedule out of service rather than filling the queue forever.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
import io
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import ValidationFailed
from app.logging import get_logger
from app.models.audit import AuditAction, AuditSeverity
from app.models.documents import DocumentCategory, DocumentVisibility
from app.models.iam import User, UserStatus
from app.models.reporting import ReportFormat, ReportRun, ReportStatus, ScheduledReport
from app.models.types import utcnow
from app.services.audit.recorder import record_audit_event
from app.services.reporting.registry import report_definition
from app.services.reporting.renderers import render

__all__ = [
    "MAX_RECIPIENTS",
    "Recipient",
    "ScheduledDelivery",
    "deliver_run",
    "due_schedules",
    "resolve_recipients",
    "run_report",
    "run_due_schedules",
]

log = get_logger("services.reporting")

#: A report going to more than this many people is a mailing list, and should be
#: one address that a mail system fans out - not fifty deliveries from here.
MAX_RECIPIENTS = 50

#: Consecutive failures before a schedule is taken out of service.
FAILURE_THRESHOLD = 5


@dataclass(frozen=True)
class Recipient:
    address: str
    label: str
    source: str


@dataclass
class ScheduledDelivery:
    runs: list[ReportRun] = field(default_factory=list)
    delivered: int = 0
    failed: int = 0
    skipped_no_recipients: int = 0


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------


def resolve_recipients(session: Session, *, org_id: str, recipients: list[Any]) -> list[Recipient]:
    """Turn stored references into addresses, right now.

    A user reference resolves to that user's *current* email, and only if the
    account is still active and belongs to this organization. A departed
    employee therefore stops receiving the books the moment they are disabled,
    with no separate cleanup step to forget.
    """
    resolved: list[Recipient] = []
    seen: set[str] = set()

    for entry in recipients or []:
        if isinstance(entry, str):
            entry = {"type": "email", "address": entry}
        if not isinstance(entry, dict):
            continue

        kind = str(entry.get("type") or "").lower()

        if kind == "email":
            address = str(entry.get("address") or "").strip().lower()
            if not address or "@" not in address:
                log.warning(
                    "scheduled report has an unusable email recipient",
                    extra={"event": "report.recipient_invalid"},
                )
                continue
            candidate = Recipient(address=address, label=address, source="email")

        elif kind == "user":
            user = session.get(User, str(entry.get("id") or ""))
            if user is None or user.org_id != org_id:
                continue
            if user.status != UserStatus.ACTIVE or user.deleted_at is not None:
                log.info(
                    "scheduled report skipped an inactive recipient",
                    extra={"event": "report.recipient_inactive", "user_id": user.id},
                )
                continue
            candidate = Recipient(
                address=user.email.strip().lower(), label=user.full_name, source="user"
            )

        elif kind == "role":
            code = str(entry.get("code") or "")
            for user in _users_with_role(session, org_id=org_id, role_code=code):
                address = user.email.strip().lower()
                if address not in seen:
                    seen.add(address)
                    resolved.append(
                        Recipient(address=address, label=user.full_name, source=f"role:{code}")
                    )
            continue

        else:
            log.warning(
                "unknown scheduled-report recipient type",
                extra={"event": "report.recipient_unknown", "type": kind},
            )
            continue

        if candidate.address not in seen:
            seen.add(candidate.address)
            resolved.append(candidate)

    if len(resolved) > MAX_RECIPIENTS:
        raise ValidationFailed(
            f"A scheduled report may not be delivered to more than {MAX_RECIPIENTS} "
            "addresses. Use a distribution list."
        )
    return resolved


def _users_with_role(session: Session, *, org_id: str, role_code: str) -> list[User]:
    from app.models.iam import Role, RoleAssignment

    return list(
        session.execute(
            select(User)
            .join(RoleAssignment, RoleAssignment.user_id == User.id)
            .join(Role, Role.id == RoleAssignment.role_id)
            .where(
                User.org_id == org_id,
                User.status == UserStatus.ACTIVE,
                User.deleted_at.is_(None),
                Role.code == role_code,
                # A revoked grant is not a reason to keep sending the books.
                RoleAssignment.revoked_at.is_(None),
            )
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def run_report(
    session: Session,
    *,
    org_id: str,
    report_code: str,
    fmt: ReportFormat = ReportFormat.CSV,
    parameters: dict[str, Any] | None = None,
    scheduled_report_id: str | None = None,
    requested_by_id: str | None = None,
    store: bool = True,
) -> ReportRun:
    """Build, render, and store one report. Always returns a run row."""
    started = utcnow()
    params = dict(parameters or {})

    run = ReportRun(
        org_id=org_id,
        report_code=report_code,
        scheduled_report_id=scheduled_report_id,
        parameters=params,
        format=fmt,
        status=ReportStatus.RUNNING,
        requested_by_id=requested_by_id,
        started_at=started,
    )
    session.add(run)
    session.flush()

    try:
        definition = report_definition(report_code)
        rows = definition.build(session, org_id=org_id, parameters=params)
        rendered = render(
            fmt=fmt,
            title=definition.name,
            columns=definition.columns,
            rows=rows,
            generated_at=started.isoformat(timespec="seconds"),
        )
        run.row_count = len(rows)

        if store:
            document = _store(
                session,
                org_id=org_id,
                definition_name=definition.name,
                code=report_code,
                rendered=rendered,
                started=started,
                requested_by_id=requested_by_id,
            )
            run.document_id = document.id

        run.status = ReportStatus.COMPLETED
    except Exception as exc:  # noqa: BLE001 - a failed report is data, not a crash
        run.status = ReportStatus.FAILED
        run.error_message = str(exc)[:2_000]
        log.exception(
            "report generation failed",
            extra={"event": "report.failed", "report_code": report_code},
        )
    finally:
        run.finished_at = utcnow()
        run.duration_ms = max(0, int((run.finished_at - started).total_seconds() * 1000))
        session.flush()

    return run


def _store(
    session: Session,
    *,
    org_id: str,
    definition_name: str,
    code: str,
    rendered: Any,
    started: dt.datetime,
    requested_by_id: str | None,
):  # noqa: ANN202
    """Keep the output as a document, so retention and access apply to it too."""
    from app.services.documents.service import upload_document

    stamp = started.strftime("%Y%m%d-%H%M%S")
    return upload_document(
        session,
        org_id=org_id,
        stream=io.BytesIO(rendered.content),
        filename=f"{code}-{stamp}.{rendered.extension}",
        declared_content_type=rendered.content_type.split(";")[0],
        name=f"{definition_name} ({started.date().isoformat()})",
        category=DocumentCategory.REPORT,
        visibility=DocumentVisibility.INTERNAL,
        uploaded_by_id=requested_by_id,
    )


# ---------------------------------------------------------------------------
# Delivering
# ---------------------------------------------------------------------------


def deliver_run(
    session: Session,
    *,
    run: ReportRun,
    schedule: ScheduledReport,
    actor_id: str | None = None,
) -> list[Recipient]:
    """Send a completed report to its recipients, resolved now."""
    from app.services.notifications.mailer import OutboundEmail, get_mailer

    if run.status != ReportStatus.COMPLETED:
        raise ValidationFailed("An incomplete report cannot be delivered.")

    recipients = resolve_recipients(session, org_id=schedule.org_id, recipients=schedule.recipients)
    if not recipients:
        log.warning(
            "scheduled report has no live recipients",
            extra={"event": "report.no_recipients", "schedule_id": schedule.id},
        )
        return []

    mailer = get_mailer()
    body = (
        f"{schedule.name}\n\n"
        f"{run.row_count if run.row_count is not None else 0} rows, generated "
        f"{run.finished_at.isoformat(timespec='minutes') if run.finished_at else ''}.\n\n"
        "The report is attached to this run in Atlas."
    )
    for recipient in recipients:
        mailer.send(OutboundEmail(to=recipient.address, subject=schedule.name, body=body))

    run.delivered_at = utcnow()
    session.flush()

    record_audit_event(
        action=AuditAction.DATA_EXPORTED,
        resource_type="ReportRun",
        resource_id=run.id,
        resource_label=schedule.name,
        severity=AuditSeverity.NOTICE,
        payload={
            "report_code": run.report_code,
            "format": str(run.format),
            "rows": run.row_count,
            # Who it went to, resolved at this moment - the record an auditor
            # asks for. Addresses pass through the usual redaction, so the trail
            # identifies the recipients without becoming a mailing list itself.
            "recipient_count": len(recipients),
            "recipients": [r.address for r in recipients],
        },
        reason="Scheduled report delivered.",
        org_id=schedule.org_id,
        actor_id=actor_id,
        session=session,
    )
    return recipients


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def due_schedules(
    session: Session, *, org_id: str, as_of: dt.datetime | None = None
) -> list[ScheduledReport]:
    now = as_of or utcnow()
    return list(
        session.execute(
            select(ScheduledReport)
            .where(
                ScheduledReport.org_id == org_id,
                ScheduledReport.is_active.is_(True),
                ScheduledReport.deleted_at.is_(None),
                ScheduledReport.next_run_at.is_not(None),
                ScheduledReport.next_run_at <= now,
            )
            .order_by(ScheduledReport.next_run_at)
        )
        .scalars()
        .all()
    )


def run_due_schedules(
    session: Session, *, org_id: str, as_of: dt.datetime | None = None
) -> ScheduledDelivery:
    """Run and deliver everything that has come due."""
    now = as_of or utcnow()
    outcome = ScheduledDelivery()

    for schedule in due_schedules(session, org_id=org_id, as_of=now):
        run = run_report(
            session,
            org_id=org_id,
            report_code=schedule.report_code,
            fmt=schedule.format,
            parameters=schedule.parameters,
            scheduled_report_id=schedule.id,
        )
        outcome.runs.append(run)

        # The watermark moves whether or not delivery worked. Otherwise a bad
        # recipient list means the same report runs every minute forever.
        schedule.last_run_at = now
        schedule.next_run_at = next_occurrence(schedule, after=now)

        if run.status != ReportStatus.COMPLETED:
            outcome.failed += 1
            schedule.consecutive_failures += 1
            if schedule.consecutive_failures >= FAILURE_THRESHOLD:
                schedule.is_active = False
                log.error(
                    "scheduled report disabled after repeated failures",
                    extra={
                        "event": "report.schedule_disabled",
                        "schedule_id": schedule.id,
                        "failures": schedule.consecutive_failures,
                    },
                )
            continue

        schedule.consecutive_failures = 0
        recipients = deliver_run(session, run=run, schedule=schedule)
        if recipients:
            outcome.delivered += 1
        else:
            outcome.skipped_no_recipients += 1

    session.flush()
    return outcome


def next_occurrence(schedule: ScheduledReport, *, after: dt.datetime) -> dt.datetime:
    """When this schedule should next run.

    A deliberately small cron subset - daily, weekly, monthly at an hour - which
    is what report schedules actually use. Anything a full cron parser would add
    here would be expressive power nobody has asked for, evaluated on a path
    where being wrong means an owner does not get their statement.
    """
    expression = (schedule.schedule or "").strip()
    parts = expression.split()
    minute, hour = 0, 6
    if len(parts) >= 2:
        minute = _int_or(parts[0], 0)
        hour = _int_or(parts[1], 6)

    day_of_month = parts[2] if len(parts) >= 3 else "*"
    day_of_week = parts[4] if len(parts) >= 5 else "*"

    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= after:
        candidate += dt.timedelta(days=1)

    for _ in range(370):
        if day_of_month != "*" and candidate.day != _int_or(day_of_month, candidate.day):
            candidate += dt.timedelta(days=1)
            continue
        if day_of_week != "*" and candidate.weekday() != _cron_weekday(day_of_week):
            candidate += dt.timedelta(days=1)
            continue
        return candidate
    return candidate


def _int_or(value: str, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _cron_weekday(value: str) -> int:
    """Cron counts Sunday as 0; Python counts Monday as 0."""
    cron = _int_or(value, 1) % 7
    return (cron - 1) % 7

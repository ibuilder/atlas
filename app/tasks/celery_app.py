"""Celery application and the scheduled job catalogue.

Tasks run inside a Flask application context so they see the same settings,
database session, and field cipher as a request. They are also *idempotent*:
the beat scheduler and a retry can both deliver the same job twice, and neither
may double-charge a resident or send a notice twice.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from celery import Celery, Task
from celery.schedules import crontab

from app.logging import get_logger

__all__ = ["celery_app", "make_celery"]

log = get_logger("tasks")

celery_app = Celery("atlas")


def make_celery(flask_app=None):  # noqa: ANN001, ANN201
    """Bind Celery to a Flask application."""
    if flask_app is None:
        from app import create_app

        flask_app = create_app()

    settings = flask_app.config["SETTINGS"]

    celery_app.conf.update(
        broker_url=settings.celery_broker_url,
        result_backend=settings.celery_result_backend,
        task_always_eager=settings.celery_task_always_eager,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # A worker that dies mid-task must not lose the job. Late acknowledgement
        # plus a visibility timeout means it is redelivered rather than dropped -
        # which is only safe because every task is idempotent.
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_time_limit=900,
        task_soft_time_limit=840,
        broker_transport_options={"visibility_timeout": 3600},
        task_default_queue="default",
        task_routes={
            "atlas.documents.*": {"queue": "documents"},
            "atlas.reports.*": {"queue": "reports"},
            "atlas.webhooks.*": {"queue": "webhooks"},
        },
        beat_schedule=BEAT_SCHEDULE,
    )

    class ContextTask(Task):
        """Runs every task inside an application context."""

        def __call__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN204
            from app.context import system_context, use_context

            with flask_app.app_context(), use_context(system_context("task")):
                return self.run(*args, **kwargs)

    celery_app.Task = ContextTask
    celery_app.flask_app = flask_app  # type: ignore[attr-defined]
    return celery_app


#: Scheduled work. Times are UTC; anything that must land at a local hour for a
#: tenant resolves the organization's timezone inside the task.
BEAT_SCHEDULE: dict[str, dict] = {
    "generate-recurring-charges": {
        "task": "atlas.billing.generate_recurring_charges",
        "schedule": crontab(hour=2, minute=0),
    },
    "sweep-delinquency": {
        "task": "atlas.collections.sweep_delinquency",
        "schedule": crontab(hour=9, minute=0),
    },
    "escalate-sla-breaches": {
        "task": "atlas.maintenance.escalate_sla_breaches",
        "schedule": crontab(minute="*/15"),
    },
    "generate-preventive-maintenance": {
        "task": "atlas.maintenance.generate_preventive_work",
        "schedule": crontab(hour=3, minute=30),
    },
    "expire-vendor-compliance": {
        "task": "atlas.vendors.refresh_compliance_status",
        "schedule": crontab(hour=4, minute=0),
    },
    "dispatch-webhooks": {
        "task": "atlas.webhooks.dispatch_pending",
        "schedule": 30.0,
    },
    "generate-owner-statements": {
        # Early on the first of the month, for the month just closed.
        "task": "atlas.owners.generate_statements",
        "schedule": crontab(day_of_month="1", hour=6, minute=0),
    },
    "expire-stale-approvals": {
        "task": "atlas.approvals.expire_stale",
        "schedule": crontab(minute="*/30"),
    },
    "expire-signature-envelopes": {
        # Hourly is ample: the consequence of an hour's lag is an envelope that
        # could still be signed an hour past its date, not a lost one.
        "task": "atlas.esign.expire_envelopes",
        "schedule": crontab(minute="7"),
    },
    "run-due-report-schedules": {
        # Every quarter hour; the schedule's own cron decides what is actually due.
        "task": "atlas.reports.run_due_schedules",
        "schedule": crontab(minute="*/15"),
    },
    "snapshot-kpis": {
        # After midnight, for the day just ended.
        "task": "atlas.reports.snapshot_kpis",
        "schedule": crontab(hour=0, minute=30),
    },
    "purge-sso-artifacts": {
        "task": "atlas.sso.purge_expired",
        "schedule": crontab(hour=5, minute=30),
    },
    "verify-audit-chains": {
        "task": "atlas.audit.verify_chains",
        "schedule": crontab(hour=1, minute=0),
    },
    "purge-expired-records": {
        "task": "atlas.maintenance.purge_expired",
        "schedule": crontab(hour=5, minute=0),
    },
}

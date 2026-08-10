"""Scheduled and queued jobs.

Every task here is idempotent. Re-running one must produce the same end state,
because at-least-once delivery guarantees it eventually will: `acks_late` means
a worker that dies mid-task has its job redelivered.

Idempotency is achieved by *checking state*, not by remembering what ran. A
watermark column (``last_billed_through``, ``last_generated_for``) is what makes
a re-run a no-op rather than a duplicate charge.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from sqlalchemy import select

from app.context import system_context, use_context
from app.logging import get_logger
from app.observability import JOB_RUNS
from app.tasks.celery_app import celery_app

log = get_logger("tasks.jobs")

__all__ = [
    "escalate_sla_breaches",
    "refresh_compliance_status",
    "verify_audit_chains",
]


def _session():  # noqa: ANN202
    from app.extensions import db

    return db.session


def _organizations():  # noqa: ANN202
    """Every active tenant. Scheduled work iterates tenants explicitly."""
    from app.models.org import Organization, OrganizationStatus

    return list(
        _session()
        .execute(select(Organization).where(Organization.status == OrganizationStatus.ACTIVE))
        .scalars()
    )


@celery_app.task(name="atlas.maintenance.escalate_sla_breaches", bind=True, max_retries=3)
def escalate_sla_breaches(self) -> dict:  # noqa: ANN001
    """Mark and escalate work orders past their resolution deadline.

    Idempotent: ``sla_breached_at`` is set once and never overwritten, so a
    re-run escalates nothing twice.
    """
    from app.models.audit import AuditAction, AuditSeverity
    from app.models.types import utcnow
    from app.observability import SLA_BREACHES
    from app.services.audit.recorder import record_audit_event
    from app.services.maintenance.service import overdue_work_orders

    session = _session()
    escalated = 0

    try:
        for organization in _organizations():
            with use_context(system_context("task", org_id=organization.id)):
                for work_order in overdue_work_orders(session, org_id=organization.id):
                    if work_order.sla_breached_at is not None:
                        continue  # already escalated
                    work_order.sla_breached_at = utcnow()
                    SLA_BREACHES.labels(str(work_order.priority)).inc()
                    record_audit_event(
                        action=AuditAction.WORK_ORDER_SLA_BREACHED,
                        resource_type="WorkOrder",
                        resource_id=work_order.id,
                        resource_label=work_order.work_order_number,
                        payload={"due_at": work_order.resolution_due_at.isoformat()},
                        severity=AuditSeverity.WARNING,
                        org_id=organization.id,
                        session=session,
                    )
                    escalated += 1
                session.commit()

        JOB_RUNS.labels("escalate_sla_breaches", "success").inc()
        return {"escalated": escalated}
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        JOB_RUNS.labels("escalate_sla_breaches", "retry").inc()
        log.exception("sla escalation failed")
        raise self.retry(exc=exc, countdown=60) from exc


@celery_app.task(name="atlas.vendors.refresh_compliance_status")
def refresh_compliance_status() -> dict:
    """Recompute vendor compliance from document expiry dates.

    Idempotent by construction: it derives status from data rather than
    advancing any counter.
    """
    from app.models.vendor import ComplianceStatus, Vendor, VendorCompliance

    session = _session()
    updated = 0

    for organization in _organizations():
        with use_context(system_context("task", org_id=organization.id)):
            vendors = list(
                session.execute(select(Vendor).where(Vendor.org_id == organization.id)).scalars()
            )
            for vendor in vendors:
                records = list(
                    session.execute(
                        select(VendorCompliance).where(VendorCompliance.vendor_id == vendor.id)
                    ).scalars()
                )
                for record in records:
                    record.status = record.evaluate_status()

                # The vendor's headline status is the worst of its documents,
                # so dispatch can filter on one indexed column.
                statuses = {record.status for record in records}
                if not records:
                    resolved = ComplianceStatus.MISSING
                elif ComplianceStatus.EXPIRED in statuses:
                    resolved = ComplianceStatus.EXPIRED
                elif ComplianceStatus.EXPIRING in statuses:
                    resolved = ComplianceStatus.EXPIRING
                elif ComplianceStatus.PENDING_REVIEW in statuses:
                    resolved = ComplianceStatus.PENDING_REVIEW
                else:
                    resolved = ComplianceStatus.VALID

                expiries = [r.expires_at for r in records if r.expires_at]
                if vendor.compliance_status != resolved:
                    updated += 1
                vendor.compliance_status = resolved
                vendor.compliance_expires_at = min(expiries) if expiries else None

            session.commit()

    JOB_RUNS.labels("refresh_compliance_status", "success").inc()
    return {"vendors_updated": updated}


@celery_app.task(name="atlas.audit.verify_chains")
def verify_audit_chains() -> dict:
    """Re-walk every organization's audit chain and alert on a break.

    A tamper-evident trail is only evident if something looks. This is the thing
    that looks.
    """
    from app.services.audit.recorder import verify_chain

    session = _session()
    results = []

    for organization in _organizations():
        with use_context(system_context("task", org_id=organization.id)):
            outcome = verify_chain(session, org_id=organization.id)
            results.append(outcome)
            if not outcome["intact"]:
                log.critical(
                    "audit chain integrity failure detected by scheduled check",
                    extra={
                        "event": "security.audit_chain_broken",
                        "org_id": organization.id,
                        "failure": outcome["failure"],
                        "at_sequence": outcome["at_sequence"],
                    },
                )

    broken = [r for r in results if not r["intact"]]
    JOB_RUNS.labels("verify_audit_chains", "failure" if broken else "success").inc()
    return {"checked": len(results), "broken": len(broken)}


@celery_app.task(name="atlas.documents.scan", bind=True, max_retries=5)
def scan_document(self, document_id: str) -> dict:  # noqa: ANN001
    """Scan a quarantined upload and release or hold it.

    Idempotent: an already-scanned document is a no-op, so redelivery cannot
    move a released file back into quarantine.
    """
    from app.models.documents import Document, ScanStatus
    from app.services.documents.scanner import get_scanner
    from app.services.documents.service import record_scan_result
    from app.services.documents.storage import get_storage

    session = _session()

    with use_context(system_context("task")):
        from app.models.base import unscoped

        with unscoped(session):
            document = session.get(Document, document_id)

        if document is None:
            log.warning(
                "scan requested for a document that does not exist",
                extra={"event": "document.scan_missing", "document_id": document_id},
            )
            JOB_RUNS.labels("scan_document", "skipped_duplicate").inc()
            return {"document_id": document_id, "status": "not_found"}

        if document.scan_status != ScanStatus.PENDING:
            JOB_RUNS.labels("scan_document", "skipped_duplicate").inc()
            return {"document_id": document_id, "status": str(document.scan_status)}

        with use_context(system_context("task", org_id=document.org_id)):
            stream = None
            try:
                stream = get_storage().get(document.storage_key)
                result = get_scanner().scan(stream)
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                JOB_RUNS.labels("scan_document", "retry").inc()
                # The document stays quarantined while we retry. Failing open
                # would release unscanned files whenever the scanner blinks.
                raise self.retry(exc=exc, countdown=60) from exc
            finally:
                # The local backend hands back an open file handle. Left
                # unclosed, a worker draining an upload backlog exhausts its
                # descriptors and starts failing unrelated tasks.
                if stream is not None and hasattr(stream, "close"):
                    stream.close()

            record_scan_result(session, document=document, clean=result.clean, detail=result.detail)
            session.commit()

    JOB_RUNS.labels("scan_document", "success").inc()
    return {"document_id": document_id, "clean": result.clean, "detail": result.detail}


@celery_app.task(name="atlas.maintenance.purge_expired")
def purge_expired() -> dict:
    """Drop expired sessions and idempotency records."""
    from app.api.idempotency import purge_expired as purge_keys
    from app.services.iam.session_service import purge_expired_sessions

    session = _session()
    with use_context(system_context("task")):
        sessions_removed = purge_expired_sessions(session)
        session.commit()
        keys_removed = purge_keys(session)

    JOB_RUNS.labels("purge_expired", "success").inc()
    return {"sessions": sessions_removed, "idempotency_keys": keys_removed}


@celery_app.task(name="atlas.webhooks.dispatch_pending")
def dispatch_pending_webhooks() -> dict:
    """Fan out the outbox and deliver everything whose backoff has elapsed.

    Idempotent at both stages: fan-out is absorbed by the unique constraint on
    (endpoint, event), and a delivery already marked delivered is no longer due.
    """
    from app.services.integration.webhooks import deliver_due, fan_out_pending

    session = _session()
    fanned = delivered = failed = dead = 0

    for organization in _organizations():
        with use_context(system_context("task", org_id=organization.id)):
            try:
                fanned += fan_out_pending(session, org_id=organization.id)
                outcome = deliver_due(session, org_id=organization.id)
                delivered += outcome.delivered
                failed += outcome.failed
                dead += outcome.dead_lettered
                session.commit()
            except Exception:  # noqa: BLE001 - one tenant must not stop the sweep
                session.rollback()
                log.exception(
                    "webhook dispatch failed for an organization",
                    extra={"event": "webhook.dispatch_failed", "org_id": organization.id},
                )

    JOB_RUNS.labels("dispatch_pending_webhooks", "success").inc()
    return {"fanned_out": fanned, "delivered": delivered, "failed": failed, "dead_lettered": dead}


@celery_app.task(name="atlas.billing.generate_recurring_charges")
def generate_recurring_charges() -> dict:
    """Raise the month's invoices from lease charge schedules.

    Idempotent by watermark: a cycle already behind LeaseCharge.last_billed_through
    is never billed again, so a re-run charges nobody twice.
    """
    from app.services.accounting.billing import generate_recurring_charges as run_billing

    session = _session()
    invoices = cycles = 0

    for organization in _organizations():
        with use_context(system_context("task", org_id=organization.id)):
            try:
                run = run_billing(session, org_id=organization.id)
                invoices += len(run.invoices)
                cycles += run.cycles_billed
                session.commit()
            except Exception:  # noqa: BLE001 - one tenant must not stop the sweep
                session.rollback()
                log.exception(
                    "recurring billing failed for an organization",
                    extra={"event": "billing.failed", "org_id": organization.id},
                )

    JOB_RUNS.labels("generate_recurring_charges", "success").inc()
    return {"invoices": invoices, "cycles": cycles}


@celery_app.task(name="atlas.collections.sweep_delinquency")
def sweep_delinquency() -> dict:
    """Escalate overdue invoices through the notice stages.

    Idempotent by watermark: a stage already reached is never re-assessed, so a
    late fee is charged once per invoice per stage rather than once per run.
    """
    from app.services.accounting.billing import sweep_delinquency as run_sweep

    session = _session()
    escalated = notices = fees = 0

    for organization in _organizations():
        with use_context(system_context("task", org_id=organization.id)):
            try:
                run = run_sweep(session, org_id=organization.id)
                escalated += run.escalated
                notices += len(run.notices)
                fees += len(run.late_fees)
                session.commit()
            except Exception:  # noqa: BLE001
                session.rollback()
                log.exception(
                    "delinquency sweep failed for an organization",
                    extra={"event": "collections.failed", "org_id": organization.id},
                )

    JOB_RUNS.labels("sweep_delinquency", "success").inc()
    return {"escalated": escalated, "notices": notices, "late_fees": fees}


@celery_app.task(name="atlas.owners.generate_statements")
def generate_owner_statements(
    period_start: str | None = None, period_end: str | None = None
) -> dict:
    """Generate owner statements for a closed period.

    Idempotent: regenerating a period updates the existing statement rather than
    creating a second one, so a corrected ledger can be restated.
    """
    import datetime as _dt

    from app.services.accounting.statements import generate_statements_for_period

    session = _session()
    today = _dt.date.today()
    first_of_this_month = today.replace(day=1)
    start = (
        _dt.date.fromisoformat(period_start)
        if period_start
        else ((first_of_this_month - _dt.timedelta(days=1)).replace(day=1))
    )
    end = (
        _dt.date.fromisoformat(period_end)
        if period_end
        else (first_of_this_month - _dt.timedelta(days=1))
    )

    produced = 0
    for organization in _organizations():
        with use_context(system_context("task", org_id=organization.id)):
            try:
                produced += len(
                    generate_statements_for_period(
                        session, org_id=organization.id, period_start=start, period_end=end
                    )
                )
                session.commit()
            except Exception:  # noqa: BLE001
                session.rollback()
                log.exception(
                    "statement generation failed for an organization",
                    extra={"event": "statements.failed", "org_id": organization.id},
                )

    JOB_RUNS.labels("generate_owner_statements", "success").inc()
    return {"statements": produced, "period": f"{start} to {end}"}


@celery_app.task(name="atlas.approvals.expire_stale")
def expire_stale_approvals() -> dict:
    """Lapse approvals nobody decided.

    Idempotent: an approval already expired is not touched, so the sweep can run
    as often as it likes.
    """
    from app.services.automation.approvals import expire_stale_approvals as run_sweep

    session = _session()
    expired = 0

    for organization in _organizations():
        with use_context(system_context("task", org_id=organization.id)):
            try:
                expired += run_sweep(session, org_id=organization.id)
                session.commit()
            except Exception:  # noqa: BLE001 - one tenant must not stop the sweep
                session.rollback()
                log.exception(
                    "approval expiry failed for an organization",
                    extra={"event": "approvals.sweep_failed", "org_id": organization.id},
                )

    JOB_RUNS.labels("expire_stale_approvals", "success").inc()
    return {"expired": expired}


@celery_app.task(name="atlas.maintenance.generate_preventive_work")
def generate_preventive_work() -> dict:
    """Raise work orders for preventive schedules whose lead time has opened.

    Idempotent by watermark: a cycle already generated for is never generated
    again, so a re-run produces no duplicate boiler service.
    """
    from app.services.maintenance.preventive import (
        generate_preventive_work as run_generation,
    )

    session = _session()
    generated = deferred = 0

    for organization in _organizations():
        with use_context(system_context("task", org_id=organization.id)):
            try:
                run = run_generation(session, org_id=organization.id)
                generated += run.generated
                deferred += run.deferred
                session.commit()
            except Exception:  # noqa: BLE001 - one tenant must not stop the sweep
                session.rollback()
                log.exception(
                    "preventive generation failed for an organization",
                    extra={"event": "pm.failed", "org_id": organization.id},
                )

    JOB_RUNS.labels("generate_preventive_work", "success").inc()
    return {"generated": generated, "deferred": deferred}


@celery_app.task(name="atlas.reports.run_due_schedules")
def run_due_report_schedules() -> dict:
    """Run and deliver every report schedule that has come due.

    The schedule's watermark advances whether or not delivery worked, so a bad
    recipient list costs one failed run rather than one run every minute.
    """
    from app.services.reporting.service import run_due_schedules

    session = _session()
    delivered = failed = 0

    for organization in _organizations():
        with use_context(system_context("task", org_id=organization.id)):
            try:
                outcome = run_due_schedules(session, org_id=organization.id)
                delivered += outcome.delivered
                failed += outcome.failed
                session.commit()
            except Exception:  # noqa: BLE001 - one tenant must not stop the sweep
                session.rollback()
                log.exception(
                    "scheduled reporting failed for an organization",
                    extra={"event": "report.sweep_failed", "org_id": organization.id},
                )

    JOB_RUNS.labels("run_due_report_schedules", "success").inc()
    return {"delivered": delivered, "failed": failed}


@celery_app.task(name="atlas.reports.snapshot_kpis")
def snapshot_kpis() -> dict:
    """Compute yesterday's dashboard metrics.

    Upserted on (metric, scope, date), so a re-run corrects the day rather than
    adding a second point - and every metric is a pure function of operational
    data, so a doubtful series is a rebuild and never a correctness problem.
    """
    import datetime as _dt

    from app.services.reporting.projections import snapshot_metrics

    session = _session()
    yesterday = _dt.date.today() - _dt.timedelta(days=1)
    points = 0

    for organization in _organizations():
        with use_context(system_context("task", org_id=organization.id)):
            try:
                points += len(snapshot_metrics(session, org_id=organization.id, as_of=yesterday))
                session.commit()
            except Exception:  # noqa: BLE001 - one tenant must not stop the sweep
                session.rollback()
                log.exception(
                    "kpi snapshot failed for an organization",
                    extra={"event": "kpi.sweep_failed", "org_id": organization.id},
                )

    JOB_RUNS.labels("snapshot_kpis", "success").inc()
    return {"points": points, "as_of": yesterday.isoformat()}

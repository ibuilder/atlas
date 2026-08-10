"""Reporting: the catalogue, the renderers, and scheduled delivery.

SPDX-License-Identifier: MIT
"""

from app.services.reporting.projections import (
    METRICS,
    MetricDefinition,
    MetricValue,
    known_metrics,
    rebuild_series,
    roll_up,
    snapshot_metrics,
)
from app.services.reporting.registry import (
    REPORTS,
    ReportDefinition,
    known_reports,
    report_definition,
)
from app.services.reporting.renderers import RenderedReport, render, supported_formats
from app.services.reporting.service import (
    Recipient,
    ScheduledDelivery,
    deliver_run,
    due_schedules,
    next_occurrence,
    resolve_recipients,
    run_due_schedules,
    run_report,
)

__all__ = [
    "METRICS",
    "MetricDefinition",
    "MetricValue",
    "REPORTS",
    "known_metrics",
    "rebuild_series",
    "roll_up",
    "snapshot_metrics",
    "Recipient",
    "RenderedReport",
    "ReportDefinition",
    "ScheduledDelivery",
    "deliver_run",
    "due_schedules",
    "known_reports",
    "next_occurrence",
    "render",
    "report_definition",
    "resolve_recipients",
    "run_due_schedules",
    "run_report",
    "supported_formats",
]

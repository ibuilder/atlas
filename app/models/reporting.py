"""Reporting: scheduled deliveries, run history, and KPI projections.

:class:`KpiSnapshot` is a *reporting projection*, deliberately separate from the
operational tables it summarises. Dashboards ask "occupancy over the last twelve
months across forty properties", which is a question no operational schema
answers cheaply - and running it against live ledger and lease tables makes the
dashboard the slowest, most contended query in the system.

Snapshots are recomputed on a schedule and are always reconstructible from
operational data, so a stale or wrong projection is a rebuild, never a
correctness problem in the books.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import SoftDeleteMixin, TenantModel
from app.models.types import GUID, JSONType, Money, UTCDateTime, enum_column

__all__ = [
    "KpiSnapshot",
    "ReportFormat",
    "ReportRun",
    "ReportStatus",
    "ScheduledReport",
]


class ReportFormat(StrEnum):
    PDF = "pdf"
    CSV = "csv"
    XLSX = "xlsx"
    JSON = "json"
    HTML = "html"


class ReportStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScheduledReport(TenantModel, SoftDeleteMixin):
    """A recurring report delivery."""

    __tablename__ = "scheduled_reports"
    __table_args__ = (
        Index("ix_scheduled_reports_due", "org_id", "next_run_at", "is_active"),
        Index("ix_scheduled_reports_org_created", "org_id", "created_at"),
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False)
    report_code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)

    #: Cron expression, evaluated in the organization's timezone. Owners expect
    #: their statement on the first of the month in *their* morning.
    schedule: Mapped[str] = mapped_column(String(80), nullable=False)
    timezone: Mapped[str | None] = mapped_column(String(64))
    format: Mapped[ReportFormat] = mapped_column(
        enum_column(ReportFormat), nullable=False, default=ReportFormat.PDF
    )
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    #: Recipients as ``[{"type": "user", "id": "..."}]`` or ``{"type": "email"}``.
    #: Resolved at send time so a departed employee stops receiving the books.
    recipients: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    last_run_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    next_run_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, index=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ReportRun(TenantModel):
    """One execution of a report."""

    __tablename__ = "report_runs"
    __table_args__ = (
        Index("ix_report_runs_org_code_created", "org_id", "report_code", "created_at"),
        Index("ix_report_runs_org_created", "org_id", "created_at"),
    )

    report_code: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    scheduled_report_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("scheduled_reports.id", ondelete="SET NULL"), index=True
    )
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    format: Mapped[ReportFormat] = mapped_column(
        enum_column(ReportFormat), nullable=False, default=ReportFormat.PDF
    )

    status: Mapped[ReportStatus] = mapped_column(
        enum_column(ReportStatus), nullable=False, default=ReportStatus.QUEUED, index=True
    )
    requested_by_id: Mapped[str | None] = mapped_column(GUID, index=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    row_count: Mapped[int | None] = mapped_column(Integer)
    document_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("documents.id", ondelete="SET NULL"), index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)


class KpiSnapshot(TenantModel):
    """A point-in-time metric value for one scope."""

    __tablename__ = "kpi_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "metric_key",
            "scope_type",
            "scope_id",
            "as_of_date",
            name="uq_kpi_snapshots_point",
        ),
        Index("ix_kpi_snapshots_series", "org_id", "metric_key", "as_of_date"),
        Index("ix_kpi_snapshots_org_created", "org_id", "created_at"),
    )

    #: ``occupancy_rate``, ``delinquency_rate``, ``avg_days_to_lease``,
    #: ``work_order_sla_compliance``, ``noi``, ``turn_cost_per_unit``.
    metric_key: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    #: ``organization``, ``portfolio``, ``property``, ``unit_type``.
    scope_type: Mapped[str] = mapped_column(String(30), nullable=False, default="organization")
    scope_id: Mapped[str | None] = mapped_column(GUID, index=True)
    as_of_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)

    #: Two representations because metrics are not one shape: a rate needs
    #: precision, a count needs an integer, and money needs exactness.
    numeric_value: Mapped[Decimal | None] = mapped_column(Money)
    count_value: Mapped[int | None] = mapped_column(Integer)
    #: Numerator/denominator retained so a rate can be re-aggregated correctly.
    #: Averaging percentages across properties is a classic silent error.
    numerator: Mapped[Decimal | None] = mapped_column(Money)
    denominator: Mapped[Decimal | None] = mapped_column(Money)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    computed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

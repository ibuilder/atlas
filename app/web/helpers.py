"""Template helpers, filters, and globals.

Formatting lives here rather than in views so that a money value, a date, or a
status badge looks identical in the admin console, the resident portal, and the
owner statement - and so changing that formatting is one edit.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from flask import Flask
from markupsafe import Markup, escape

from app.models.types import utcnow

__all__ = ["register_template_helpers"]

#: Semantic colour classes for status pills, keyed by the value the domain uses.
_STATUS_TONE: dict[str, str] = {
    # good
    "active": "good",
    "paid": "good",
    "completed": "good",
    "verified": "good",
    "clean": "good",
    "valid": "good",
    "occupied": "good",
    "settled": "good",
    "succeeded": "good",
    "delivered": "good",
    "approved": "good",
    # warn
    "pending": "warn",
    "draft": "warn",
    "open": "warn",
    "new": "warn",
    "triaged": "warn",
    "assigned": "warn",
    "scheduled": "warn",
    "in_progress": "warn",
    "partially_paid": "warn",
    "expiring": "warn",
    "notice": "warn",
    "pending_approval": "warn",
    "awaiting_parts": "warn",
    "on_hold": "warn",
    "pending_review": "warn",
    # bad
    "failed": "bad",
    "expired": "bad",
    "denied": "bad",
    "void": "bad",
    "cancelled": "bad",
    "infected": "bad",
    "suspended": "bad",
    "locked": "bad",
    "returned": "bad",
    "dead_lettered": "bad",
    "written_off": "bad",
    "breached": "bad",
    # critical
    "emergency": "critical",
    "urgent": "critical",
}


def register_template_helpers(app: Flask, settings: Any) -> None:
    """Install Jinja globals and filters."""

    @app.context_processor
    def _globals() -> dict[str, Any]:
        from flask_login import current_user

        from app.security.policies import can

        return {
            "app_name": settings.app_name,
            "app_tagline": "Property intelligence, made operational.",
            "environment": settings.env,
            "current_year": utcnow().year,
            "current_user": current_user,
            # Exposed so a template can hide a control the viewer cannot use.
            # Hiding is courtesy; the policy engine is the enforcement.
            "can": can,
            "features": {
                "automation": settings.feature_automation_engine,
                "owner_portal": settings.feature_owner_portal,
                "vendor_portal": settings.feature_vendor_portal,
                "ai_copilot": settings.feature_ai_copilot,
            },
        }

    @app.template_filter("money")
    def _money(value: Decimal | float | int | None, currency: str = "USD") -> str:
        if value is None:
            return "—"
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
        symbol = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "C$"}.get(currency, "")
        negative = amount < 0
        formatted = f"{symbol}{abs(amount):,.2f}"
        # Accounting convention: negatives in parentheses, not with a minus
        # sign that is easy to miss in a column of figures.
        return f"({formatted})" if negative else formatted

    @app.template_filter("pct")
    def _percent(value: float | Decimal | None, places: int = 1) -> str:
        if value is None:
            return "—"
        return f"{float(value):.{places}f}%"

    @app.template_filter("date")
    def _date(value: dt.date | dt.datetime | None, fmt: str = "%d %b %Y") -> str:
        if value is None:
            return "—"
        return value.strftime(fmt)

    @app.template_filter("datetime")
    def _datetime(value: dt.datetime | None, fmt: str = "%d %b %Y, %H:%M") -> str:
        if value is None:
            return "—"
        return value.strftime(fmt)

    @app.template_filter("since")
    def _since(value: dt.datetime | None) -> str:
        """Relative time. Precise enough to be useful, vague enough to scan."""
        if value is None:
            return "—"
        delta = utcnow() - value
        seconds = int(delta.total_seconds())
        if seconds < 0:
            return _future(-seconds)
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            return f"{seconds // 60}m ago"
        if seconds < 86400:
            return f"{seconds // 3600}h ago"
        if seconds < 604800:
            return f"{seconds // 86400}d ago"
        return value.strftime("%d %b %Y")

    def _future(seconds: int) -> str:
        if seconds < 3600:
            return f"in {max(1, seconds // 60)}m"
        if seconds < 86400:
            return f"in {seconds // 3600}h"
        return f"in {seconds // 86400}d"

    @app.template_filter("humanize")
    def _humanize(value: Any) -> str:
        if value is None:
            return "—"
        return str(value).replace("_", " ").strip().capitalize()

    @app.template_filter("status_pill")
    def _status_pill(value: Any) -> Markup:
        raw = str(value or "").lower()
        # `tone` comes from a fixed lookup table and `label` is escaped, so the
        # only interpolated values are already safe. Built with Markup.format
        # rather than an f-string so that stays true if someone edits it later.
        tone = _STATUS_TONE.get(raw, "neutral")
        label = escape(raw.replace("_", " ").capitalize())
        return Markup('<span class="pill pill--{}">{}</span>').format(tone, label)

    @app.template_filter("initials")
    def _initials(value: str | None) -> str:
        if not value:
            return "?"
        parts = [part for part in str(value).split() if part]
        return "".join(part[0].upper() for part in parts[:2]) or "?"

"""Unauthenticated surfaces: the landing page and the sign-in entry point.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from flask import Blueprint, redirect, render_template, url_for
from flask_login import current_user
from werkzeug.wrappers import Response

public_bp = Blueprint("public", __name__)

__all__ = ["public_bp"]


@public_bp.get("/")
def index() -> Response | str:
    """Route an arriving visitor to the surface that belongs to them."""
    if getattr(current_user, "is_authenticated", False):
        return redirect(url_for(_home_endpoint_for(current_user)))
    return render_template("public/index.html")


def _home_endpoint_for(user) -> str:  # noqa: ANN001
    from app.models.iam import UserType

    return {
        UserType.RESIDENT: "resident.dashboard",
        UserType.OWNER: "owner.dashboard",
        UserType.VENDOR: "vendor.dashboard",
    }.get(user.user_type, "admin.dashboard")

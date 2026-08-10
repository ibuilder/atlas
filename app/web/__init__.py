"""Server-rendered surfaces: admin console and the resident, owner, and vendor portals.

Jinja plus HTMX rather than a single-page application. Property operations are
form-heavy, table-heavy, and permission-heavy - exactly the shape server
rendering handles well, and exactly the shape where a client-side permission
model becomes a second, weaker copy of the policy engine.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from flask import Flask

__all__ = ["register_web_blueprints"]


def register_web_blueprints(app: Flask, settings) -> None:  # noqa: ANN001
    from app.web.admin import admin_bp
    from app.web.auth import auth_bp
    from app.web.portals import owner_bp, resident_bp, vendor_bp
    from app.web.public import public_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(resident_bp, url_prefix="/resident")

    if settings.feature_owner_portal:
        app.register_blueprint(owner_bp, url_prefix="/owner")
    if settings.feature_vendor_portal:
        app.register_blueprint(vendor_bp, url_prefix="/vendor")

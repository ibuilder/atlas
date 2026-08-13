"""Flask extension singletons.

Instantiated unbound at import time and attached to an application inside the
factory. Keeping them here - rather than reaching for ``current_app.extensions``
all over the codebase - is what makes the factory pattern actually work with
Celery workers and CLI commands that build their own app instance.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import logging

from flask import Flask, request
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_talisman import Talisman
from flask_wtf.csrf import CSRFProtect
from sqlalchemy.orm import Session

from app.models.base import Base

__all__ = [
    "cache",
    "csrf",
    "current_session",
    "db",
    "limiter",
    "login_manager",
    "migrate",
    "talisman",
]

# Bound to our metadata rather than given ``model_class=Base``. Models declare
# themselves against :class:`~app.models.base.Base`, which owns the constraint
# naming convention and the type annotation map; Flask-SQLAlchemy contributes the
# engine, the scoped session, and app-context teardown. Passing ``model_class``
# would make it subclass our base and re-declare a registry, which SQLAlchemy
# rejects outright.
db = SQLAlchemy(metadata=Base.metadata)
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
cache = Cache()
talisman = Talisman()


def current_session() -> Session:
    """The request's session, typed as the ``Session`` it actually is.

    ``db.session`` is a ``scoped_session``: a thread-local registry that
    forwards every call to a real ``Session``, but is not one as far as the type
    checker is concerned. Services are annotated against ``Session`` - correctly,
    since that is the contract they use and what a Celery task or a test hands
    them - so passing ``db.session`` straight in is a type error at every one of
    the sixty-odd call sites in the web and CLI layers.

    Calling the registry returns the very same object it would have proxied to,
    so this is free at runtime. It exists so the unwrapping happens once, named
    and explained, rather than as a scattering of casts or an ignore comment on
    each line.
    """
    return db.session()


def rate_limit_key() -> str:
    """Rate-limit bucket key.

    Authenticated callers are limited per identity, not per IP: an office behind
    one NAT gateway should not throttle itself, and an attacker rotating IPs
    should not get a fresh budget with every hop.
    """
    from flask_login import current_user

    try:
        if current_user and current_user.is_authenticated:
            return f"user:{current_user.get_id()}"
    except Exception:  # pragma: no cover - outside a login-aware context
        # Deliberately silent: rate limiting must never be the thing that breaks
        # a request. Falling through to the IP bucket is the safe default.
        logging.getLogger("atlas.ratelimit").debug(
            "falling back to IP rate-limit bucket", exc_info=True
        )

    api_token_id = getattr(request, "atlas_token_id", None)
    if api_token_id:
        return f"token:{api_token_id}"
    return f"ip:{get_remote_address()}"


limiter = Limiter(key_func=rate_limit_key, headers_enabled=True)


def init_login_manager(app: Flask) -> None:
    """Wire Flask-Login, including the loaders that resolve a session to a user."""
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please sign in to continue."
    login_manager.login_message_category = "info"
    login_manager.session_protection = "strong"
    login_manager.refresh_view = "auth.reauthenticate"
    login_manager.needs_refresh_message = "Please confirm your identity to continue."

    @login_manager.user_loader
    def _load_user(user_id: str):  # noqa: ANN202
        from app.services.iam.session_service import load_user_for_session

        return load_user_for_session(user_id)

    @login_manager.request_loader
    def _load_from_token(req):  # noqa: ANN001, ANN202
        from app.services.iam.token_service import authenticate_request_token

        return authenticate_request_token(req)

    @login_manager.unauthorized_handler
    def _unauthorized():  # noqa: ANN202
        from app.errors import AuthenticationRequired

        raise AuthenticationRequired()

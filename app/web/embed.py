"""The embeddable enquiry form: Atlas's only unauthenticated write surface.

An operator pastes an ``<iframe>`` into their own marketing site and Atlas
serves the form from its own origin. That choice is the security design, not a
convenience: the applicant types their details into an Atlas page, so a
cross-site scripting flaw on the operator's site — a WordPress install with a
stale plugin, most often — cannot read what is being typed. A script tag that
rendered the form into the host page would put every field within reach of
whatever else runs there.

Four controls stand between this route and an open spam relay, and none of them
trusts the request:

**The key decides the organization.** Resolved through ``unscoped`` because no
tenant context exists yet, then a context is bound to whatever the key says.
The submitter cannot name an organization, so there is nothing to forge.

**Framing is allow-listed per key.** ``frame-ancestors`` is built from that
key's origins, so a snippet lifted from one operator's page source will not
render on somebody else's site. A key with no origins frames nowhere.

**Origin is checked on write.** A browser sends ``Origin`` on cross-site POSTs
and cannot be talked out of it by page script.

**Bots are filtered before the database.** A honeypot field no human sees, and
a signed timestamp proving the form was actually rendered and then took a human
interval to fill in.

CSRF exemption is deliberate and worth stating plainly, because an exemption
that looks like an oversight gets "fixed" into an outage. CSRF protects actions
that borrow a victim's credentials. This endpoint has none: it is anonymous,
and forging a request achieves precisely what the attacker could achieve by
sending one directly. Meanwhile the session cookie is ``SameSite``, so it is
not sent in a cross-site frame at all and a token check could never pass here.
The controls that matter for an anonymous endpoint are the four above.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

from flask import Blueprint, Response, current_app, g, render_template, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.context import system_context, use_context
from app.errors import NotFound, ValidationFailed
from app.extensions import csrf, current_session, db, limiter, talisman
from app.logging import get_logger
from app.models.base import unscoped
from app.services.leasing import embeds

embed_bp = Blueprint("embed", __name__, url_prefix="/embed")
log = get_logger("web.embed")

__all__ = ["embed_bp"]

#: Talisman's global policy sets ``frame-ancestors 'none'`` and
#: ``X-Frame-Options: DENY``, which is right for every other route and fatal for
#: this one. Both are replaced per response from the key's own allowlist.
_embeddable = talisman(frame_options=None, content_security_policy=None)

#: Per address, so one host cannot flood every form an operator publishes, and
#: per key, so a single popular page cannot exhaust the budget for the rest.
_SUBMIT_LIMITS = "5 per minute; 40 per hour"
_RENDER_LIMITS = "60 per minute"

_HONEYPOT_FIELD = "company_website"
_TIMESTAMP_FIELD = "rendered_at"


@embed_bp.get("/f/<public_key>")
@_embeddable
@limiter.limit(_RENDER_LIMITS)
def form(public_key: str) -> Response:
    """Render the form for a key, framable only by that key's origins."""
    embed_form = _resolve(public_key)
    with use_context(system_context("embed", org_id=embed_form.org_id)):
        html = render_template(
            "embed/form.html",
            form=embed_form,
            honeypot_field=_HONEYPOT_FIELD,
            timestamp_field=_TIMESTAMP_FIELD,
            rendered_token=_issue_render_token(embed_form.public_key),
            errors=[],
            values={},
        )
    return _framed(html, embed_form.allowed_origins)


@embed_bp.post("/f/<public_key>")
@_embeddable
@limiter.limit(_SUBMIT_LIMITS)
def submit(public_key: str) -> Response:
    """Accept a submission, or re-render the form saying why not."""
    embed_form = _resolve(public_key)
    origins = embed_form.allowed_origins

    # A browser attaches Origin to every cross-site POST and page script cannot
    # remove it. Absent means a non-browser client, which is not what this
    # surface is for.
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if origin not in origins:
        log.warning(
            "embed.origin_rejected",
            extra={"public_key": embed_form.public_key, "origin": origin or "(absent)"},
        )
        raise NotFound("No such form.")

    values = {
        "first_name": (request.form.get("first_name") or "").strip(),
        "last_name": (request.form.get("last_name") or "").strip(),
        "email": (request.form.get("email") or "").strip(),
        "phone": (request.form.get("phone") or "").strip(),
        "desired_move_in": (request.form.get("desired_move_in") or "").strip(),
        "message": (request.form.get("message") or "").strip(),
    }

    silent_rejection = _bot_signals(embed_form.public_key)
    errors: list[str] = []
    lead = None

    if silent_rejection is None:
        try:
            with use_context(system_context("embed", org_id=embed_form.org_id)):
                lead = embeds.capture_lead(
                    session=current_session(),
                    form=embed_form,
                    first_name=values["first_name"],
                    last_name=values["last_name"] or None,
                    email=values["email"] or None,
                    phone=values["phone"] or None,
                    desired_move_in=_parse_date(values["desired_move_in"]),
                    message=values["message"] or None,
                    origin=origin,
                )
                db.session.commit()
        except ValidationFailed as failure:
            db.session.rollback()
            errors.append(str(failure))

    if errors:
        with use_context(system_context("embed", org_id=embed_form.org_id)):
            html = render_template(
                "embed/form.html",
                form=embed_form,
                honeypot_field=_HONEYPOT_FIELD,
                timestamp_field=_TIMESTAMP_FIELD,
                rendered_token=_issue_render_token(embed_form.public_key),
                errors=errors,
                values=values,
            )
        return _framed(html, origins, status=400)

    # A rejected bot gets the same thank-you page as a person. Telling it which
    # control caught it is free tuning information for the next attempt, and
    # there is no honest submitter to inform.
    if silent_rejection is not None:
        log.info(
            "embed.submission_rejected",
            extra={"public_key": embed_form.public_key, "signal": silent_rejection},
        )
    else:
        log.info(
            "embed.lead_captured",
            extra={"public_key": embed_form.public_key, "lead_id": getattr(lead, "id", None)},
        )

    html = render_template("embed/thanks.html", form=embed_form)
    return _framed(html, origins)


def _resolve(public_key: str) -> Any:
    """Find the key's form across organizations, before any context exists."""
    with unscoped(current_session()):
        return embeds.resolve_public_form(current_session(), public_key=public_key)


def _framed(html: str, origins: list[str], *, status: int = 200) -> Response:
    """Build the response and hand it the only framing policy it may have.

    ``frame-ancestors`` is the modern control and supersedes ``X-Frame-Options``
    wherever both appear, but the older header cannot express an allowlist at
    all — its ``ALLOW-FROM`` was dropped by every browser. Leaving a stale
    ``DENY`` next to a correct CSP would work today and break on the first
    client that reads the headers in the other order, so it is removed rather
    than left to precedence rules.
    """
    response = current_app.make_response((html, status))
    response.headers["Content-Security-Policy"] = _embed_csp(origins)
    response.headers.pop("X-Frame-Options", None)
    # The global default is same-origin, which blocks the document from loading
    # inside an embedder that sets COEP.
    response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    # Read by the after-request hook, which would otherwise reinstate DENY.
    g.embed_framing_allowed = True
    return response


def _embed_csp(origins: list[str]) -> str:
    """A policy narrower than the site's, not merely different.

    The form has no scripts of its own, so ``script-src 'none'`` is honest and
    removes the whole class of injection this page could otherwise carry.
    """
    ancestors = " ".join(origins) if origins else "'none'"
    return "; ".join(
        [
            "default-src 'none'",
            "style-src 'self'",
            "img-src 'self' data:",
            "font-src 'self'",
            "form-action 'self'",
            "base-uri 'none'",
            "script-src 'none'",
            f"frame-ancestors {ancestors}",
        ]
    )


def _issue_render_token(public_key: str) -> str:
    """A signed marker that this form was rendered, and when.

    Bound to the key so a token minted against one form cannot be replayed
    against another, and signed so the clock cannot simply be edited in the
    DOM before posting.
    """
    return _serializer().dumps({"k": public_key, "t": time.time()})


def _bot_signals(public_key: str) -> str | None:
    """Name the automation signal, or ``None`` for a submission that looks human."""
    if (request.form.get(_HONEYPOT_FIELD) or "").strip():
        return "honeypot"

    token = request.form.get(_TIMESTAMP_FIELD) or ""
    try:
        payload = _serializer().loads(token, max_age=embeds.MAX_FILL_SECONDS)
    except SignatureExpired:
        return "stale_token"
    except BadSignature:
        return "unsigned_token"

    if payload.get("k") != public_key:
        return "token_for_another_form"
    if time.time() - float(payload.get("t", 0)) < embeds.MIN_FILL_SECONDS:
        return "too_fast"
    return None


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="atlas.embed.render")


def _parse_date(raw: str) -> dt.date | None:
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        # A move-in date is a nicety, not the point of the form. Losing an
        # unparseable one is better than refusing a lead over it.
        return None


csrf.exempt(embed_bp)

"""The embeddable enquiry form: Atlas's only unauthenticated write surface.

An operator pastes an ``<iframe>`` into their own marketing site and Atlas
serves the form from its own origin. That choice is the security design, not a
convenience: the applicant types their details into an Atlas page, so a
cross-site scripting flaw on the operator's site — a WordPress install with a
stale plugin, most often — cannot read what is being typed. A script tag that
rendered the form into the host page would put every field within reach of
whatever else runs there.

Several controls stand between this route and an open spam relay, and none of them
trusts the request:

**The key decides the organization.** Resolved through ``unscoped`` because no
tenant context exists yet, then a context is bound to whatever the key says.
The submitter cannot name an organization, so there is nothing to forge.

**Framing is allow-listed per key.** ``frame-ancestors`` is built from that
key's origins, so a snippet lifted from one operator's page source will not
render on somebody else's site. A key with no origins frames nowhere.

**The embedding page is identified once, at render, and then sealed.** It is
readable only there — the referrer on that request is the parent document,
while on the submission that follows it is this iframe — so it is captured and
signed into the render token rather than re-derived later.

There is deliberately **no ``Origin`` header check on the submission**, and the
absence is a correction. This form posts back to the origin that served it, so
the submission is same-origin: a browser sends Atlas's own host or omits the
header entirely. Neither is ever the embedding site, which is not party to that
request at all. An earlier version compared the header against the allowlist
and consequently refused every genuine enquiry, while its tests passed by
forging a header no browser sends. ``frame-ancestors`` is what really confines
the form, and the browser enforces it before a key is pressed.

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
from app.errors import ValidationFailed
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
    # The only moment the embedding page's origin is knowable. On this request
    # the referrer is the parent document; on the submission that follows it is
    # this iframe. Captured here and signed into the token so the submission can
    # still say where it came from.
    parent = _parent_origin()

    with use_context(system_context("embed", org_id=embed_form.org_id)):
        html = render_template(
            "embed/form.html",
            form=embed_form,
            honeypot_field=_HONEYPOT_FIELD,
            timestamp_field=_TIMESTAMP_FIELD,
            rendered_token=_issue_render_token(embed_form.public_key, parent),
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

    # There is deliberately no `Origin` check here, and the absence is the
    # correction of a real bug rather than a relaxation.
    #
    # This form is served from Atlas's own origin and posts back to it, so the
    # submission is *same-origin*: a browser sends `Origin: <atlas>` — its own
    # host — or omits the header entirely, which Firefox does for same-origin
    # posts. Neither value is ever the embedding site's origin, because the
    # embedding site is not party to this request. Comparing the header against
    # the allowlist therefore rejected every genuine submission while the tests
    # passed, because the tests forged a header no browser sends.
    #
    # `frame-ancestors` is the control that actually does this job, is enforced
    # by the browser before a keystroke is typed, and is already applied on the
    # render. What can be checked here is the origin recorded at render time and
    # signed, which `_bot_signals` does.
    values = {
        "first_name": (request.form.get("first_name") or "").strip(),
        "last_name": (request.form.get("last_name") or "").strip(),
        "email": (request.form.get("email") or "").strip(),
        "phone": (request.form.get("phone") or "").strip(),
        "desired_move_in": (request.form.get("desired_move_in") or "").strip(),
        "message": (request.form.get("message") or "").strip(),
    }

    # The origin comes back from the signed token rather than from a header:
    # it was recorded when the form was rendered, and the signature is what
    # makes it evidence instead of a claim.
    silent_rejection, signed_origin = _bot_signals(embed_form.public_key, origins)
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
                    origin=signed_origin,
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
                # Re-issued with the origin carried forward, so a corrected
                # resubmission is still attributed to the page it came from.
                rendered_token=_issue_render_token(embed_form.public_key, signed_origin),
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


def _parent_origin() -> str | None:
    """The origin of the page framing this form, if the browser disclosed it.

    Read from the referrer on the *render*, which is the parent document. The
    site's referrer policy is ``strict-origin-when-cross-origin``, so a
    cross-site framing sends exactly the origin and nothing more.

    Best-effort by nature: a parent page can suppress the referrer entirely,
    and privacy tooling does. Absent is therefore an ordinary outcome and never
    a refusal — attribution is worth having and not worth losing a lead over.
    """
    referrer = request.headers.get("Referer") or ""
    if not referrer:
        return None
    try:
        return embeds.normalize_origin(referrer)
    except Exception:  # noqa: BLE001 - a referrer is not ours to validate
        return None


def _issue_render_token(public_key: str, parent_origin: str | None) -> str:
    """A signed marker that this form was rendered, when, and inside what.

    Bound to the key so a token minted against one form cannot be replayed
    against another, and signed so neither the clock nor the recorded origin
    can be edited in the DOM before posting.
    """
    return _serializer().dumps({"k": public_key, "t": time.time(), "o": parent_origin})


def _bot_signals(public_key: str, allowed_origins: list[str]) -> tuple[str | None, str | None]:
    """Classify a submission, and return the origin its token vouches for.

    Returns ``(signal, origin)``. ``signal`` names the automation trait that
    caught it, or ``None`` if it looks human.
    """
    if (request.form.get(_HONEYPOT_FIELD) or "").strip():
        return "honeypot", None

    token = request.form.get(_TIMESTAMP_FIELD) or ""
    try:
        payload = _serializer().loads(token, max_age=embeds.MAX_FILL_SECONDS)
    except SignatureExpired:
        return "stale_token", None
    except BadSignature:
        return "unsigned_token", None

    if not isinstance(payload, dict) or payload.get("k") != public_key:
        return "token_for_another_form", None
    try:
        rendered_at = float(payload.get("t") or 0)
    except (TypeError, ValueError):
        return "unsigned_token", None
    if time.time() - rendered_at < embeds.MIN_FILL_SECONDS:
        return "too_fast", None

    # Recorded at render, signed, so it cannot be asserted by the submitter.
    # An origin that is present and not allow-listed means the form rendered
    # somewhere `frame-ancestors` should have refused — worth dropping, while
    # an absent one stays acceptable because referrers are routinely withheld.
    origin = payload.get("o")
    if origin is not None and origin not in allowed_origins:
        return "origin_not_allowed", None
    return None, origin


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

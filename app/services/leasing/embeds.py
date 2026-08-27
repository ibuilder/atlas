"""Public lead capture from a form embedded in the operator's own website.

This is the only write surface in Atlas that an unauthenticated stranger can
reach, which changes what the code is allowed to trust. Three rules follow, and
each is enforced here rather than in the view, because a second surface added
later must inherit them rather than reimplement them.

**The key names the organization; the request never does.** A submission
carries a public key that resolves to exactly one :class:`EmbedForm` row, and
the organization, property, and limits all come from that row. Nothing the
submitter types can widen the scope, because there is nothing to widen: the
form does not accept an organization, and a property outside the key's own is
refused rather than silently accepted.

**An unknown key and a revoked key are the same answer.** Both are
:class:`NotFound`. A distinguishable "this key exists but is disabled" turns
the endpoint into an oracle for enumerating which operators use Atlas.

**The allowlist fails closed.** A key with no permitted origins frames nowhere
and captures nothing. An empty allowlist meaning "anyone" is the single most
common way an embed becomes an open relay for spam into somebody's CRM.

What this module deliberately does not do is screen. It captures a name and a
way to reach them, and the existing funnel takes it from there. Income,
employment, and date of birth are collected later, behind authentication, by a
person who knows who they are talking to - so a marketing page defaced or
cloned never becomes a route to screening-grade personal data.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import NotFound, ValidationFailed
from app.logging import get_logger
from app.models.audit import AuditAction
from app.models.leasing import EmbedForm, Lead, LeadStatus
from app.models.org import Property, Unit
from app.models.types import utcnow
from app.services.audit.recorder import record_audit_event

__all__ = [
    "KEY_PREFIX",
    "MIN_FILL_SECONDS",
    "capture_lead",
    "create_embed_form",
    "generate_public_key",
    "normalize_origin",
    "resolve_public_form",
    "revoke_embed_form",
    "snippet_for",
    "update_embed_form",
]

log = get_logger("services.leasing.embeds")

#: Marks the value as publishable wherever it turns up. Somebody grepping their
#: own repository for leaked credentials should be able to tell at a glance
#: that this one was always meant to be readable.
KEY_PREFIX = "pk_live_"

#: 32 hex characters. Not a secret, but still unguessable: a key that can be
#: enumerated lets somebody spray leads into every form an operator publishes.
KEY_BYTES = 16

#: A human filling in five fields takes longer than this. Bots post instantly,
#: and this catches the ones that never render the page at all - which is most
#: of them, and costs the honest submitter nothing.
MIN_FILL_SECONDS = 3.0

#: Beyond this a rendered form is stale enough that the page has probably been
#: sitting open in a tab for a day. Re-render rather than accept a token whose
#: age no longer says anything about who submitted it.
MAX_FILL_SECONDS = 60 * 60 * 6

_ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d{1,5})?$")

#: Length caps applied before anything reaches the database. The columns have
#: their own limits; truncating silently would store a mangled phone number, so
#: anything over the cap is refused instead.
MAX_NAME = 100
MAX_EMAIL = 320
MAX_PHONE = 40
MAX_MESSAGE = 2000


def generate_public_key() -> str:
    return f"{KEY_PREFIX}{secrets.token_hex(KEY_BYTES)}"


def normalize_origin(raw: str) -> str:
    """Reduce a pasted URL to the origin a browser will actually send.

    Operators paste what is in their address bar - ``https://example.com/rent``
    with a path, a trailing slash, sometimes a query string. A browser sends
    only ``https://example.com`` in ``Origin``, so an allowlist storing the
    pasted form would never match and the form would appear broken with no
    indication why.
    """
    text = (raw or "").strip().rstrip("/")
    if not text:
        raise ValidationFailed("An allowed origin cannot be blank.")
    if "://" not in text:
        # Almost always a bare hostname. Guessing http:// would quietly permit
        # an insecure page to host a form collecting contact details.
        raise ValidationFailed(
            f"{raw!r} needs a scheme; write it as https://example.com so the "
            "origin matches what a browser sends."
        )

    parts = urlsplit(text)
    origin = f"{parts.scheme.lower()}://{parts.netloc.lower()}"
    if not _ORIGIN_RE.match(origin):
        raise ValidationFailed(
            f"{raw!r} is not a usable origin. Expected scheme://host, "
            "for example https://example.com."
        )
    return origin


def create_embed_form(
    session: Session,
    *,
    org_id: str,
    label: str,
    allowed_origins: list[str],
    property_id: str | None = None,
    actor_id: str | None = None,
) -> EmbedForm:
    """Issue a key. The origins are required, because a key without them is inert."""
    clean_label = (label or "").strip()
    if not clean_label:
        raise ValidationFailed("An embed form needs a label so it can be told from the others.")

    origins = _normalized_origins(allowed_origins)
    if not origins:
        raise ValidationFailed(
            "An embed form needs at least one allowed origin. Without one the "
            "form refuses to frame anywhere, which looks like a bug rather than "
            "a setting."
        )

    form = EmbedForm(
        org_id=org_id,
        label=clean_label[:120],
        public_key=generate_public_key(),
        property_id=property_id,
        allowed_origins=origins,
        enabled=True,
    )
    session.add(form)
    session.flush()

    record_audit_event(
        action=AuditAction.EMBED_FORM_CREATED,
        resource_type="EmbedForm",
        resource_id=form.id,
        resource_label=form.label,
        # The key itself is recorded: it is public by construction, and knowing
        # which key was live when a wave of junk arrived is the whole point.
        payload={"public_key": form.public_key, "allowed_origins": origins},
        reason="Embed form issued.",
        org_id=org_id,
        actor_id=actor_id,
        session=session,
    )
    return form


def update_embed_form(
    session: Session,
    *,
    form: EmbedForm,
    label: str | None = None,
    allowed_origins: list[str] | None = None,
    enabled: bool | None = None,
    actor_id: str | None = None,
) -> EmbedForm:
    if form.revoked_at is not None:
        raise ValidationFailed("A revoked embed form cannot be edited. Issue a new one.")

    if label is not None:
        clean = label.strip()
        if not clean:
            raise ValidationFailed("An embed form needs a label.")
        form.label = clean[:120]

    if allowed_origins is not None:
        origins = _normalized_origins(allowed_origins)
        if not origins:
            raise ValidationFailed("An embed form needs at least one allowed origin.")
        form.allowed_origins = origins

    if enabled is not None:
        form.enabled = enabled

    session.flush()
    record_audit_event(
        action=AuditAction.EMBED_FORM_UPDATED,
        resource_type="EmbedForm",
        resource_id=form.id,
        resource_label=form.label,
        payload={"allowed_origins": form.allowed_origins, "enabled": form.enabled},
        reason="Embed form updated.",
        org_id=form.org_id,
        actor_id=actor_id,
        session=session,
    )
    return form


def revoke_embed_form(
    session: Session, *, form: EmbedForm, actor_id: str | None = None
) -> EmbedForm:
    """Kill a key permanently.

    Terminal on purpose. The snippet containing this key is pasted into a page
    the operator may no longer control, so "disabled for now" is not a state a
    leaked key should be able to leave.
    """
    if form.revoked_at is not None:
        return form

    form.revoked_at = utcnow()
    form.enabled = False
    session.flush()

    record_audit_event(
        action=AuditAction.EMBED_FORM_REVOKED,
        resource_type="EmbedForm",
        resource_id=form.id,
        resource_label=form.label,
        payload={"public_key": form.public_key},
        reason="Embed form revoked.",
        org_id=form.org_id,
        actor_id=actor_id,
        session=session,
    )
    return form


def resolve_public_form(session: Session, *, public_key: str) -> EmbedForm:
    """Find the form a public request is presenting, across all organizations.

    The caller must run this inside ``unscoped``: the tenant guard filters by
    the active organization, and at this point there is not one - the key is
    what decides it. Binding a tenant context before the key has been resolved
    would mean trusting the request to name its own organization, which is the
    one thing a public endpoint must never do.

    Every failure is :class:`NotFound`, including a key that exists but has
    been disabled or revoked. Distinguishing them would answer "does this
    operator use Atlas" for anybody willing to iterate.
    """
    key = (public_key or "").strip()
    if not key or not key.startswith(KEY_PREFIX):
        raise NotFound("No such form.")

    form = session.execute(
        select(EmbedForm).where(EmbedForm.public_key == key)
    ).scalar_one_or_none()

    if form is None or not form.is_live:
        raise NotFound("No such form.")
    return form


def capture_lead(
    session: Session,
    *,
    form: EmbedForm,
    first_name: str,
    last_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    desired_move_in: dt.date | None = None,
    message: str | None = None,
    property_id: str | None = None,
    unit_id: str | None = None,
    origin: str | None = None,
) -> Lead:
    """Turn a public submission into a lead in the key's organization.

    Called with a tenant context already bound to ``form.org_id``. Nothing here
    reads an organization from its arguments, so there is no path by which a
    submission lands anywhere except the organization that issued the key.
    """
    name = (first_name or "").strip()
    if not name:
        raise ValidationFailed("Please tell us your name.")
    if not (email or "").strip() and not (phone or "").strip():
        raise ValidationFailed("Please leave either an email address or a phone number.")

    _refuse_overlong("name", name, MAX_NAME)
    _refuse_overlong("email", email, MAX_EMAIL)
    _refuse_overlong("phone", phone, MAX_PHONE)
    _refuse_overlong("message", message, MAX_MESSAGE)

    clean_email = (email or "").strip().lower() or None
    if clean_email and "@" not in clean_email:
        raise ValidationFailed("That does not look like an email address.")

    # A key scoped to a property pins every lead to it; an unscoped key may
    # take one from the submission. Either way the id is verified here rather
    # than trusted from the caller. Leaving that to a docstring was a real
    # hazard: PostgreSQL does not apply row-level security to foreign-key
    # checks, so a reference to another tenant's property would insert happily
    # instead of failing closed, and the ORM guard would not see it because the
    # write itself is correctly scoped to this organization.
    resolved_property = form.property_id or _in_this_org(session, Property, property_id, form.org_id)
    resolved_unit = _in_this_org(session, Unit, unit_id, form.org_id)

    lead = Lead(
        org_id=form.org_id,
        first_name=name[:MAX_NAME],
        last_name=((last_name or "").strip() or None),
        email=clean_email,
        phone=((phone or "").strip() or None),
        source="embed",
        # Which page produced this, for an operator deciding where to spend.
        source_detail=(origin or form.label)[:120],
        status=LeadStatus.NEW,
        property_id=resolved_property,
        unit_id=resolved_unit,
        desired_move_in=desired_move_in,
        notes=((message or "").strip() or None),
        attributes={"embed_form_id": form.id, "embed_origin": origin},
    )
    session.add(lead)

    form.submission_count += 1
    form.last_submission_at = utcnow()
    session.flush()

    record_audit_event(
        action=AuditAction.LEAD_CREATED,
        resource_type="Lead",
        resource_id=lead.id,
        resource_label=lead.full_name,
        # Deliberately no name, email, or phone: this is an audit trail, not a
        # second copy of the personal data, and it is read far more widely.
        payload={"source": "embed", "embed_form_id": form.id, "origin": origin},
        reason="Lead captured from an embedded form.",
        org_id=form.org_id,
        actor_id=None,
        session=session,
    )
    return lead


def snippet_for(form: EmbedForm, *, base_url: str) -> str:
    """The paste-able HTML an operator puts on their site.

    An iframe rather than a script tag. The form is served from Atlas's own
    origin, so the applicant's details are typed into Atlas rather than into
    the operator's marketing site - which means a cross-site scripting flaw on
    that site, and WordPress installations acquire them regularly, cannot read
    what somebody is typing.
    """
    src = f"{base_url.rstrip('/')}/embed/f/{form.public_key}"
    return (
        f'<iframe src="{src}"\n'
        f'        title="Rental enquiry form"\n'
        f'        style="width:100%;max-width:640px;height:720px;border:0"\n'
        f'        loading="lazy"></iframe>'
    )


def _in_this_org(session: Session, model: type, record_id: str | None, org_id: str) -> str | None:
    """Return the id only if it names a row this organization owns.

    A foreign id is refused rather than dropped. Silently nulling it would file
    the lead against no property at all, which reads as an Atlas bug to whoever
    picks it up; refusing says the submission was wrong.
    """
    if not record_id:
        return None
    record = session.get(model, record_id)
    if record is None or record.org_id != org_id:
        raise NotFound("No such property or unit.")
    return record_id


def _normalized_origins(raw: list[str]) -> list[str]:
    seen: list[str] = []
    for entry in raw or []:
        origin = normalize_origin(entry)
        if origin not in seen:
            seen.append(origin)
    return seen


def _refuse_overlong(field: str, value: str | None, limit: int) -> None:
    """Refuse rather than truncate.

    Silently cutting a phone number to fit produces a lead nobody can call,
    which is worse than telling the submitter their entry was too long.
    """
    if value is not None and len(value) > limit:
        raise ValidationFailed(f"That {field} is too long; the limit is {limit} characters.")

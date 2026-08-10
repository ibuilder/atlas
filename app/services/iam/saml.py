"""SAML 2.0 service-provider side: consuming an assertion.

A SAML response is a signed XML document that says "this person is who they
claim to be". Verifying that signature correctly is genuinely hard - it needs
exclusive XML canonicalisation, reference resolution, and transform handling -
and getting it subtly wrong yields a system that accepts forged assertions
while appearing to work. This module therefore **delegates signature
verification to signxml** and refuses to run without it, rather than
hand-rolling XML-DSIG. A wrong answer here is an authentication bypass.

On top of the signature, five checks, each corresponding to a real attack:

* **Signature required.** An unsigned assertion is refused unless the provider
  has been explicitly, and audibly, put into a diagnostic mode.
* **The certificate must be the configured one.** Verifying against a
  certificate embedded in the response itself proves only that the sender can
  sign - which anyone can.
* **Conditions.** ``NotBefore``/``NotOnOrAfter`` bound the window, and
  ``AudienceRestriction`` stops an assertion minted for another service
  provider being presented here.
* **Replay.** The assertion id is recorded; the second presentation fails.
* **Domain.** The provider may only speak for its configured domains.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import base64
import datetime as dt
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import AuthenticationRequired, ValidationFailed
from app.logging import get_logger
from app.models.audit import AuditAction, AuditOutcome, AuditSeverity
from app.models.sso import IdentityProvider, SsoProtocol, SsoReplayGuard
from app.models.types import utcnow
from app.services.audit.recorder import record_audit_event
from app.services.iam.oidc import FederatedIdentity, complete_login

__all__ = [
    "CLOCK_SKEW",
    "SamlAssertion",
    "consume_response",
    "parse_response",
    "service_provider_metadata",
]

log = get_logger("services.iam.saml")

NS = {
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "ds": "http://www.w3.org/2000/09/xmldsig#",
}

#: Clocks drift. More than this and the assertion is stale, not skewed.
CLOCK_SKEW = dt.timedelta(minutes=3)

#: An assertion is remembered at least this long past its expiry, so a replay
#: cannot slip through by arriving after the guard row was cleaned up.
REPLAY_RETENTION = dt.timedelta(hours=24)

MAX_RESPONSE_BYTES = 512 * 1024

_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"


@dataclass
class SamlAssertion:
    """The parts of a verified assertion that matter."""

    assertion_id: str
    issuer: str
    subject: str
    attributes: dict[str, list[str]] = field(default_factory=dict)
    not_before: dt.datetime | None = None
    not_on_or_after: dt.datetime | None = None
    audiences: list[str] = field(default_factory=list)

    def first(self, name: str) -> str | None:
        values = self.attributes.get(name) or []
        return values[0] if values else None


# ---------------------------------------------------------------------------
# Parsing and verification
# ---------------------------------------------------------------------------


def _require_signxml():  # noqa: ANN202
    try:
        from signxml import XMLVerifier
    except ImportError as exc:  # pragma: no cover - depends on extras
        raise ValidationFailed(
            "SAML needs the 'signxml' package. Install Atlas with the 'saml' extra. "
            "Signature verification is not implemented locally on purpose: a subtly "
            "wrong XML-DSIG implementation accepts forged assertions."
        ) from exc
    return XMLVerifier


def _parse_instant(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def parse_response(
    xml: bytes,
    *,
    provider: IdentityProvider,
    expected_audience: str,
) -> SamlAssertion:
    """Verify the signature, then read the assertion.

    Verification happens *first* and against the configured certificate. Parsing
    an unverified document and checking the signature afterwards invites acting
    on values that were never signed.
    """
    from lxml import etree

    if len(xml) > MAX_RESPONSE_BYTES:
        raise AuthenticationRequired("That SAML response is implausibly large.")
    if not provider.signing_certificate and provider.require_signed_assertions:
        raise ValidationFailed("This provider has no signing certificate configured.")

    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, huge_tree=False, load_dtd=False
    )
    try:
        document = etree.fromstring(xml, parser=parser)  # noqa: S320 - hardened parser above
    except etree.XMLSyntaxError as exc:
        raise AuthenticationRequired("That SAML response is not well-formed XML.") from exc

    if provider.require_signed_assertions:
        verifier = _require_signxml()
        try:
            verified = (
                verifier().verify(document, x509_cert=provider.signing_certificate).signed_xml
            )
        except Exception as exc:  # noqa: BLE001 - every failure is one refusal
            log.warning(
                "SAML signature rejected",
                extra={"event": "saml.signature_rejected", "provider": provider.code},
            )
            raise AuthenticationRequired(
                f"That SAML assertion's signature was not accepted: {exc}"
            ) from exc
        root = verified
    else:
        # Only reachable when an operator has deliberately disabled the check.
        log.error(
            "SAML signature verification is disabled for this provider",
            extra={"event": "saml.unsigned_accepted", "provider": provider.code},
        )
        root = document

    status = root.find(".//samlp:StatusCode", NS)
    if status is not None and status.get("Value") not in (None, _SUCCESS):
        raise AuthenticationRequired(
            f"The identity provider refused the sign-in: {status.get('Value')}"
        )

    assertion = root if root.tag.endswith("Assertion") else root.find(".//saml:Assertion", NS)
    if assertion is None:
        raise AuthenticationRequired("That SAML response carries no assertion.")

    assertion_id = assertion.get("ID")
    if not assertion_id:
        raise AuthenticationRequired("That SAML assertion has no identifier to track.")

    issuer_node = assertion.find("saml:Issuer", NS)
    issuer = (issuer_node.text or "").strip() if issuer_node is not None else ""
    if provider.entity_id and issuer and issuer != provider.entity_id:
        raise AuthenticationRequired(
            "That assertion was issued by a different entity than this provider."
        )

    name_id = assertion.find(".//saml:Subject/saml:NameID", NS)
    subject = (name_id.text or "").strip() if name_id is not None else ""
    if not subject:
        raise AuthenticationRequired("That SAML assertion names no subject.")

    conditions = assertion.find("saml:Conditions", NS)
    not_before = _parse_instant(conditions.get("NotBefore")) if conditions is not None else None
    not_after = _parse_instant(conditions.get("NotOnOrAfter")) if conditions is not None else None
    audiences = [
        (node.text or "").strip()
        for node in assertion.findall(".//saml:AudienceRestriction/saml:Audience", NS)
    ]

    now = utcnow()
    if not_before and now + CLOCK_SKEW < not_before:
        raise AuthenticationRequired("That SAML assertion is not yet valid.")
    if not_after and now - CLOCK_SKEW >= not_after:
        raise AuthenticationRequired("That SAML assertion has expired.")
    if audiences and expected_audience not in audiences:
        # An assertion minted for another service provider is not ours to accept.
        raise AuthenticationRequired(
            "That SAML assertion was issued for a different service provider."
        )

    attributes: dict[str, list[str]] = {}
    for node in assertion.findall(".//saml:AttributeStatement/saml:Attribute", NS):
        name = node.get("Name") or node.get("FriendlyName")
        if not name:
            continue
        attributes[name] = [
            (value.text or "").strip()
            for value in node.findall("saml:AttributeValue", NS)
            if value.text
        ]

    return SamlAssertion(
        assertion_id=assertion_id,
        issuer=issuer,
        subject=subject,
        attributes=attributes,
        not_before=not_before,
        not_on_or_after=not_after,
        audiences=audiences,
    )


# ---------------------------------------------------------------------------
# Consuming
# ---------------------------------------------------------------------------


def _guard_replay(
    session: Session, *, provider: IdentityProvider, assertion: SamlAssertion
) -> None:
    """Record the assertion id, or refuse if it is already recorded."""
    seen = session.execute(
        select(SsoReplayGuard).where(
            SsoReplayGuard.org_id == provider.org_id,
            SsoReplayGuard.assertion_id == assertion.assertion_id,
        )
    ).scalar_one_or_none()
    if seen is not None:
        record_audit_event(
            action=AuditAction.AUTH_LOGIN_FAILED,
            resource_type="IdentityProvider",
            resource_id=provider.id,
            resource_label=provider.code,
            outcome=AuditOutcome.DENIED,
            severity=AuditSeverity.CRITICAL,
            payload={"assertion_id": assertion.assertion_id},
            reason="A SAML assertion was presented for the second time.",
            org_id=provider.org_id,
            session=session,
        )
        raise AuthenticationRequired("That sign-in has already been used. Please start again.")

    session.add(
        SsoReplayGuard(
            org_id=provider.org_id,
            provider_id=provider.id,
            assertion_id=assertion.assertion_id,
            subject=assertion.subject[:255],
            expires_at=(assertion.not_on_or_after or utcnow()) + REPLAY_RETENTION,
        )
    )
    session.flush()


def identity_from_assertion(
    provider: IdentityProvider, assertion: SamlAssertion
) -> FederatedIdentity:
    """Read the configured attributes off a verified assertion."""
    email = (
        assertion.first(provider.email_claim)
        or assertion.first("email")
        or (assertion.subject if "@" in assertion.subject else "")
    )
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise AuthenticationRequired(
            f"The identity provider did not supply a usable '{provider.email_claim}' attribute."
        )

    groups = (
        assertion.attributes.get(provider.groups_claim or "", []) if provider.groups_claim else []
    )
    return FederatedIdentity(
        subject=assertion.subject,
        email=email,
        full_name=assertion.first(provider.name_claim),
        groups=list(groups),
        claims={key: values for key, values in assertion.attributes.items()},
    )


def consume_response(
    session: Session,
    *,
    provider: IdentityProvider,
    saml_response: str | bytes,
    expected_audience: str,
):  # noqa: ANN201
    """Verify, guard against replay, and resolve to a local account."""
    if provider.protocol != SsoProtocol.SAML:
        raise ValidationFailed("That provider does not speak SAML.")
    if not provider.is_active:
        raise ValidationFailed("That identity provider is not active.")

    raw = saml_response.encode("ascii") if isinstance(saml_response, str) else saml_response
    if not re.fullmatch(rb"[A-Za-z0-9+/=\s]+", raw or b""):
        raise AuthenticationRequired("That SAML response is not valid base64.")
    try:
        xml = base64.b64decode(raw, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise AuthenticationRequired("That SAML response could not be decoded.") from exc

    assertion = parse_response(xml, provider=provider, expected_audience=expected_audience)
    _guard_replay(session, provider=provider, assertion=assertion)
    identity = identity_from_assertion(provider, assertion)
    return complete_login(session, provider=provider, identity=identity)


def purge_replay_guards(session: Session, *, org_id: str) -> int:
    """Drop guard rows whose assertions can no longer be valid."""
    stale = (
        session.execute(
            select(SsoReplayGuard).where(
                SsoReplayGuard.org_id == org_id,
                SsoReplayGuard.expires_at < utcnow(),
            )
        )
        .scalars()
        .all()
    )
    for row in stale:
        session.delete(row)
    session.flush()
    return len(stale)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def _attr(value: str) -> str:
    """Escape a value for an XML attribute.

    Four characters, written out rather than imported from ``xml.sax``: this
    generates a document, it never parses one, and pulling in a parser module
    for two substitutions invites the wrong conclusion about what runs here.
    """
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def service_provider_metadata(*, entity_id: str, acs_url: str) -> str:
    """The XML a customer uploads into their IdP."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" '
        f'entityID="{_attr(entity_id)}">\n'
        '  <md:SPSSODescriptor AuthnRequestsSigned="false" WantAssertionsSigned="true" '
        'protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">\n'
        "    <md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"
        "</md:NameIDFormat>\n"
        '    <md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:'
        f'HTTP-POST" Location="{_attr(acs_url)}" index="0" isDefault="true"/>\n'
        "  </md:SPSSODescriptor>\n"
        "</md:EntityDescriptor>\n"
    )

"""Single sign-on and directory provisioning.

Federated login moves the decision "is this person who they say they are" to
somebody else's system. That is usually an improvement - the customer's IdP
enforces their MFA policy, their password rules, their offboarding - but it
means three things have to be got right here or the improvement becomes a hole.

**A provider may only speak for its own domains.** ``allowed_email_domains``
is not cosmetic: without it, a tenant who configures their own IdP can mint an
account at any address they like, including one belonging to a colleague at
another organization.

**An assertion is single-use.** Both protocols are replayable by anyone who can
see one. The state row for OIDC and the assertion-id row for SAML exist to make
the second presentation fail.

**Directory-managed accounts are not editable locally.** If the customer's
directory is the source of truth for who works there, a local edit either gets
silently reverted on next sync or silently overrides an offboarding. Flagging
the account settles which one wins.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import SoftDeleteMixin, TenantModel
from app.models.types import GUID, EncryptedText, JSONType, UTCDateTime, enum_column, utcnow

__all__ = [
    "IdentityProvider",
    "SsoProtocol",
    "SsoReplayGuard",
    "SsoLoginState",
]


class SsoProtocol(StrEnum):
    OIDC = "oidc"
    SAML = "saml"


class IdentityProvider(TenantModel, SoftDeleteMixin):
    """One configured federation with a customer's identity system."""

    __tablename__ = "identity_providers"
    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_identity_providers_org_code"),
        Index("ix_identity_providers_org_active", "org_id", "is_active"),
        Index("ix_identity_providers_org_created", "org_id", "created_at"),
    )

    code: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    protocol: Mapped[SsoProtocol] = mapped_column(enum_column(SsoProtocol), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Offered on the sign-in page without the user naming their provider.
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- OIDC -------------------------------------------------------------
    issuer: Mapped[str | None] = mapped_column(String(255))
    client_id: Mapped[str | None] = mapped_column(String(255))
    #: Encrypted at rest. A leaked client secret is a login as anybody.
    client_secret: Mapped[str | None] = mapped_column(EncryptedText)
    discovery_url: Mapped[str | None] = mapped_column(String(500))
    authorization_endpoint: Mapped[str | None] = mapped_column(String(500))
    token_endpoint: Mapped[str | None] = mapped_column(String(500))
    jwks_uri: Mapped[str | None] = mapped_column(String(500))
    userinfo_endpoint: Mapped[str | None] = mapped_column(String(500))
    scopes: Mapped[list[Any]] = mapped_column(
        JSONType, nullable=False, default=lambda: ["openid", "email", "profile"]
    )
    #: Cached signing keys, so every login is not a network round trip.
    jwks_cache: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    jwks_fetched_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    # --- SAML -------------------------------------------------------------
    entity_id: Mapped[str | None] = mapped_column(String(255))
    sso_url: Mapped[str | None] = mapped_column(String(500))
    slo_url: Mapped[str | None] = mapped_column(String(500))
    #: The IdP's public signing certificate, PEM. Public by nature, so stored
    #: in the clear - but changing it is changing who may assert identities.
    signing_certificate: Mapped[str | None] = mapped_column(Text)

    # --- Mapping and policy ----------------------------------------------
    email_claim: Mapped[str] = mapped_column(String(80), nullable=False, default="email")
    name_claim: Mapped[str] = mapped_column(String(80), nullable=False, default="name")
    groups_claim: Mapped[str | None] = mapped_column(String(80))
    #: ``{"department": "job_title"}`` - IdP attribute to Atlas field.
    attribute_map: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    #: ``{"Atlas-Admins": "org_admin"}`` - IdP group to Atlas role code.
    group_role_map: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    #: A provider for acme.com must not be able to mint an account at
    #: gmail.com, or at a rival tenant's domain. Empty means no restriction,
    #: which is refused when just-in-time provisioning is on.
    allowed_email_domains: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    #: Create an account on first successful sign-in.
    jit_provisioning: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    default_role_code: Mapped[str | None] = mapped_column(String(60))
    #: Never accept an unsigned assertion. Configurable only so a diagnostic
    #: mode is possible; turning it off is audited as critical.
    require_signed_assertions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    #: Directory provisioning: accounts from this provider are read-only here.
    scim_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    last_login_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)
    login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def allows_email(self, email: str) -> bool:
        """Whether this provider may speak for this address."""
        domains = [str(d).strip().lower().lstrip("@") for d in (self.allowed_email_domains or [])]
        if not domains:
            return True
        candidate = (email or "").strip().lower()
        return any(candidate.endswith("@" + domain) for domain in domains)


class SsoLoginState(TenantModel):
    """One outstanding authentication request.

    Carries the CSRF state and the OIDC nonce, and is *consumed* on first use.
    A callback replayed with the same code therefore fails at this row rather
    than establishing a second session.
    """

    __tablename__ = "sso_login_states"
    __table_args__ = (
        UniqueConstraint("state", name="uq_sso_login_states_state"),
        Index("ix_sso_login_states_expiry", "expires_at"),
        Index("ix_sso_login_states_org_created", "org_id", "created_at"),
    )

    provider_id: Mapped[str] = mapped_column(
        GUID, ForeignKey("identity_providers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    state: Mapped[str] = mapped_column(String(128), nullable=False)
    nonce: Mapped[str | None] = mapped_column(String(128))
    #: PKCE, so an intercepted code is useless without the verifier.
    code_verifier: Mapped[str | None] = mapped_column(String(128))
    #: Where to land after a successful sign-in. Validated as a local path
    #: before it is stored; an open redirect here is a phishing primitive.
    redirect_to: Mapped[str | None] = mapped_column(String(500))
    relay_state: Mapped[str | None] = mapped_column(String(255))

    expires_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime)

    @property
    def is_usable(self) -> bool:
        return self.consumed_at is None and self.expires_at > utcnow()


class SsoReplayGuard(TenantModel):
    """Assertion identifiers already accepted.

    A SAML response is a bearer token in an envelope: anybody who observes one
    inside its validity window can present it again. Recording the assertion id
    is what makes the second attempt fail.
    """

    __tablename__ = "sso_replay_guards"
    __table_args__ = (
        UniqueConstraint("org_id", "assertion_id", name="uq_sso_replay_guards_assertion"),
        Index("ix_sso_replay_guards_expiry", "expires_at"),
        Index("ix_sso_replay_guards_org_created", "org_id", "created_at"),
    )

    provider_id: Mapped[str | None] = mapped_column(
        GUID, ForeignKey("identity_providers.id", ondelete="CASCADE"), index=True
    )
    assertion_id: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, nullable=False)

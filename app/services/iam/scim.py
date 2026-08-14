"""SCIM 2.0: letting the customer's directory own the user list.

The value of SCIM is offboarding. Somebody leaves, HR disables them in the
directory, and their access here goes with it - without anybody remembering to
do anything. That only holds if two things are true.

**Deactivation is honoured immediately and completely.** A PATCH setting
``active: false`` disables the account *and revokes its sessions*. Marking a
user inactive while leaving a live session token is offboarding that does not
offboard, and it is the single most common way this integration is got wrong.

**Directory-managed accounts are read-only locally.** Otherwise a local edit is
either reverted at the next sync or silently overrides an offboarding, and
which one you get depends on timing. The flag settles it: the directory wins.

Deletion is a *deactivation*, never a row removal. A user id appears on ledger
entries, audit events, and approvals; deleting the row would either cascade
into financial history or leave dangling references. SCIM DELETE therefore
deactivates and says so.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from app.logging import get_logger
from app.models.audit import AuditAction, AuditSeverity
from app.models.iam import User, UserStatus, UserType
from app.models.sso import IdentityProvider
from app.models.types import utcnow
from app.services.audit.recorder import record_audit_event

__all__ = [
    "SCIM_USER_SCHEMA",
    "ScimResult",
    "apply_patch",
    "create_user_resource",
    "deactivate_resource",
    "issue_scim_token",
    "list_users",
    "provider_for_token",
    "replace_user_resource",
    "revoke_scim_token",
    "to_scim_user",
]

log = get_logger("services.iam.scim")

SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

#: A directory sync that asks for ten thousand users at once is a mistake or an
#: export; either way it is paged.
MAX_PAGE_SIZE = 200


@dataclass
class ScimResult:
    user: User
    created: bool = False
    changes: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Representation
# ---------------------------------------------------------------------------


def to_scim_user(user: User, *, location: str | None = None) -> dict[str, Any]:
    """Render a user in SCIM's shape."""
    given, _, family = (user.full_name or "").partition(" ")
    return {
        "schemas": [SCIM_USER_SCHEMA],
        "id": user.id,
        "externalId": user.external_id,
        "userName": user.email,
        "name": {
            "formatted": user.full_name,
            "givenName": given or user.full_name,
            "familyName": family or "",
        },
        "displayName": user.full_name,
        "emails": [{"value": user.email, "primary": True, "type": "work"}],
        "active": user.status == UserStatus.ACTIVE,
        "meta": {
            "resourceType": "User",
            "created": user.created_at.isoformat() if user.created_at else None,
            "lastModified": user.updated_at.isoformat() if user.updated_at else None,
            "location": location,
        },
    }


def _email_of(payload: dict[str, Any]) -> str:
    """SCIM lets the address arrive in three places; take the first usable one."""
    candidates: list[str] = []
    for entry in payload.get("emails") or []:
        if isinstance(entry, dict) and entry.get("value"):
            if entry.get("primary"):
                candidates.insert(0, str(entry["value"]))
            else:
                candidates.append(str(entry["value"]))
    if payload.get("userName"):
        candidates.append(str(payload["userName"]))

    for candidate in candidates:
        cleaned = candidate.strip().lower()
        if "@" in cleaned:
            return cleaned
    raise ValidationFailed("A SCIM user needs a usable email address.")


def _name_of(payload: dict[str, Any], fallback: str) -> str:
    name = payload.get("name") or {}
    if isinstance(name, dict):
        formatted = (name.get("formatted") or "").strip()
        if formatted:
            return formatted
        parts = [(name.get("givenName") or "").strip(), (name.get("familyName") or "").strip()]
        joined = " ".join(part for part in parts if part)
        if joined:
            return joined
    display = (payload.get("displayName") or "").strip()
    return display or fallback


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def list_users(
    session: Session,
    *,
    org_id: str,
    filter_expression: str | None = None,
    start_index: int = 1,
    count: int = 100,
) -> dict[str, Any]:
    """List directory users, with SCIM's one commonly-used filter.

    Only ``userName eq "..."`` is supported, because it is what every directory
    actually sends to check whether a user exists before creating one. A filter
    we do not understand is refused rather than quietly ignored - silently
    returning everything to a query meant to match one person is how a sync
    decides to deactivate the entire company.
    """
    query = select(User).where(User.org_id == org_id, User.deleted_at.is_(None))

    if filter_expression:
        parsed = _parse_filter(filter_expression)
        query = query.where(User.email == parsed)

    total = len(list(session.execute(query).scalars().all()))
    page = max(1, min(int(count or 100), MAX_PAGE_SIZE))
    offset = max(0, int(start_index or 1) - 1)

    users = (
        session.execute(query.order_by(User.created_at).offset(offset).limit(page)).scalars().all()
    )
    return {
        "schemas": [SCIM_LIST_SCHEMA],
        "totalResults": total,
        "startIndex": offset + 1,
        "itemsPerPage": len(users),
        "Resources": [to_scim_user(user) for user in users],
    }


def _parse_filter(expression: str) -> str:
    text = (expression or "").strip()
    lowered = text.lower()
    if not lowered.startswith("username eq "):
        raise ValidationFailed(
            f"Unsupported SCIM filter {expression!r}. Only 'userName eq \"...\"' is supported."
        )
    value = text[len("userName eq ") :].strip().strip('"').strip("'")
    if not value:
        raise ValidationFailed("That SCIM filter has no value.")
    return value.lower()


def user_by_id(session: Session, *, org_id: str, user_id: str) -> User:
    user = session.get(User, user_id)
    if user is None or user.org_id != org_id or user.deleted_at is not None:
        raise NotFound("No such user.")
    return user


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def create_user_resource(
    session: Session,
    *,
    org_id: str,
    provider: IdentityProvider,
    payload: dict[str, Any],
) -> ScimResult:
    """Provision a user the directory has announced."""
    from app.services.iam.provisioning import create_user

    email = _email_of(payload)
    if not provider.allows_email(email):
        raise PermissionDenied(
            "That address is outside the domains this identity provider may manage."
        )

    existing = session.execute(
        select(User).where(User.org_id == org_id, User.email == email, User.deleted_at.is_(None))
    ).scalar_one_or_none()
    if existing is not None:
        # SCIM says 409. Adopting the account instead would let a directory
        # take ownership of an account somebody created by hand, silently.
        raise Conflict(f"A user with the address {email} already exists.")

    active = payload.get("active", True)
    user = create_user(
        session,
        org_id=org_id,
        email=email,
        full_name=_name_of(payload, email.split("@")[0]),
        password=None,
        user_type=UserType.STAFF,
        status=UserStatus.ACTIVE if active else UserStatus.DEACTIVATED,
        role_codes=[provider.default_role_code] if provider.default_role_code else [],
    )
    user.external_id = str(payload.get("externalId") or "") or None
    user.identity_provider_id = provider.id
    user.is_directory_managed = True
    session.flush()

    _audit(session, user, action="created", provider=provider)
    return ScimResult(user=user, created=True)


def replace_user_resource(
    session: Session,
    *,
    org_id: str,
    provider: IdentityProvider,
    user: User,
    payload: dict[str, Any],
) -> ScimResult:
    """PUT: the directory's version of this user replaces ours."""
    _assert_manageable(user, provider)

    changes: dict[str, Any] = {}
    email = _email_of(payload)
    if not provider.allows_email(email):
        raise PermissionDenied(
            "That address is outside the domains this identity provider may manage."
        )
    if email != user.email:
        changes["email"] = {"from": user.email, "to": email}
        user.email = email

    name = _name_of(payload, user.full_name)
    if name != user.full_name:
        changes["full_name"] = {"from": user.full_name, "to": name}
        user.full_name = name

    external = str(payload.get("externalId") or "") or None
    if external and external != user.external_id:
        user.external_id = external

    if "active" in payload:
        changes.update(_set_active(session, user, bool(payload["active"])))

    session.flush()
    _audit(session, user, action="replaced", provider=provider, changes=changes)
    return ScimResult(user=user, changes=changes)


def apply_patch(
    session: Session,
    *,
    org_id: str,
    provider: IdentityProvider,
    user: User,
    payload: dict[str, Any],
) -> ScimResult:
    """PATCH: the operation directories actually use for deactivation."""
    _assert_manageable(user, provider)

    operations = payload.get("Operations") or payload.get("operations") or []
    if not isinstance(operations, list) or not operations:
        raise ValidationFailed("A SCIM patch needs at least one operation.")

    changes: dict[str, Any] = {}
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        verb = str(operation.get("op") or "").lower()
        if verb not in ("add", "replace", "remove"):
            raise ValidationFailed(f"Unsupported SCIM patch operation {verb!r}.")

        path = str(operation.get("path") or "").strip()
        value = operation.get("value")

        # A patch with no path carries a partial resource in `value`.
        if not path and isinstance(value, dict):
            if "active" in value:
                changes.update(_set_active(session, user, bool(value["active"])))
            if "name" in value or "displayName" in value:
                user.full_name = _name_of(value, user.full_name)
            if "userName" in value or "emails" in value:
                candidate = _email_of(value)
                if not provider.allows_email(candidate):
                    raise PermissionDenied("That address is outside this provider's domains.")
                user.email = candidate
            continue

        field_name = path.split(".")[0].lower()
        if field_name == "active":
            active = value if isinstance(value, bool) else str(value).lower() == "true"
            changes.update(_set_active(session, user, active if verb != "remove" else False))
        elif field_name in ("displayname", "name"):
            user.full_name = (
                str(value) if isinstance(value, str) else _name_of({"name": value}, user.full_name)
            )
        elif field_name in ("username", "emails"):
            candidate = (
                str(value).strip().lower()
                if isinstance(value, str)
                else _email_of({"emails": value if isinstance(value, list) else [value]})
            )
            if not provider.allows_email(candidate):
                raise PermissionDenied("That address is outside this provider's domains.")
            changes["email"] = {"from": user.email, "to": candidate}
            user.email = candidate
        elif field_name == "externalid":
            user.external_id = str(value) if value else None
        else:
            log.info(
                "ignoring an unmapped SCIM attribute",
                extra={"event": "scim.unmapped", "path": path},
            )

    session.flush()
    _audit(session, user, action="patched", provider=provider, changes=changes)
    return ScimResult(user=user, changes=changes)


def deactivate_resource(
    session: Session,
    *,
    org_id: str,
    provider: IdentityProvider,
    user: User,
) -> ScimResult:
    """DELETE: deactivate, never remove.

    A user id appears on ledger entries, audit events, and approvals. Removing
    the row would either cascade into financial history or leave those pointing
    at nothing, and neither is an acceptable price for an offboarding that
    deactivation already achieves.
    """
    _assert_manageable(user, provider)
    changes = _set_active(session, user, False)
    session.flush()
    _audit(session, user, action="deactivated", provider=provider, changes=changes)
    return ScimResult(user=user, changes=changes)


def _set_active(session: Session, user: User, active: bool) -> dict[str, Any]:
    """Flip the account, and take the sessions with it.

    Leaving a live session behind is offboarding that does not offboard: the
    directory says they are gone, the audit says they were disabled, and they
    are still signed in.
    """
    target = UserStatus.ACTIVE if active else UserStatus.DEACTIVATED
    if user.status == target:
        return {}

    previous = user.status
    user.status = target

    if not active:
        from app.services.iam.session_service import revoke_all_sessions

        try:
            revoked = revoke_all_sessions(user, reason="directory deactivation", session=session)
        except Exception:  # noqa: BLE001 - the deactivation itself must still land
            log.exception(
                "could not revoke sessions on deactivation",
                extra={"event": "scim.revoke_failed", "user_id": user.id},
            )
            revoked = None
        return {"status": {"from": str(previous), "to": str(target)}, "sessions_revoked": revoked}

    return {"status": {"from": str(previous), "to": str(target)}}


def _assert_manageable(user: User, provider: IdentityProvider) -> None:
    if user.identity_provider_id and user.identity_provider_id != provider.id:
        raise PermissionDenied("That user is managed by a different identity provider.")


def _audit(
    session: Session,
    user: User,
    *,
    action: str,
    provider: IdentityProvider,
    changes: dict[str, Any] | None = None,
) -> None:
    deactivating = bool(changes and changes.get("status", {}).get("to") == "deactivated")
    record_audit_event(
        action=AuditAction.USER_DISABLED if deactivating else AuditAction.USER_UPDATED,
        resource_type="User",
        resource_id=user.id,
        resource_label=user.email,
        severity=AuditSeverity.NOTICE if deactivating else AuditSeverity.INFO,
        payload={"scim": action, "provider": provider.code, "changes": changes or {}},
        reason=f"Directory sync {action} this account.",
        org_id=user.org_id,
        session=session,
    )


def purge_deactivated(
    session: Session, *, org_id: str, older_than: dt.timedelta = dt.timedelta(days=400)
) -> int:
    """Count long-deactivated directory accounts. Reports; never deletes.

    Retention of an identity that appears on financial records is a legal
    question, not a housekeeping one, so this surfaces candidates rather than
    acting on them.
    """
    cutoff = utcnow() - older_than
    stale = (
        session.execute(
            select(User).where(
                User.org_id == org_id,
                User.is_directory_managed.is_(True),
                User.status == UserStatus.DEACTIVATED,
                User.updated_at < cutoff,
            )
        )
        .scalars()
        .all()
    )
    return len(stale)


# ---------------------------------------------------------------------------
# The credential the directory presents
# ---------------------------------------------------------------------------


def issue_scim_token(
    session: Session, *, provider: IdentityProvider, actor_id: str | None = None
) -> str:
    """Mint a bearer token for this provider, returning it once.

    Stored hashed, for the same reason a password is: a leaked database should
    not hand somebody the ability to deactivate every account in the tenant.
    The plaintext is returned here and never again — an administrator who
    loses it issues a new one, which is a smaller problem than a recoverable
    credential that can deactivate a company.
    """
    from app.security.crypto import hash_token, new_token, token_fingerprint

    if not provider.scim_enabled:
        raise ValidationFailed(
            "Turn SCIM on for this provider before issuing it a token. A live "
            "credential for a disabled integration is a credential nobody watches."
        )

    token = new_token(prefix="scim_")
    provider.scim_token_hash = hash_token(token)
    provider.scim_token_fingerprint = token_fingerprint(token)
    provider.scim_token_issued_at = utcnow()
    session.flush()

    record_audit_event(
        action=AuditAction.INTEGRATION_CONFIGURED,
        resource_type="IdentityProvider",
        resource_id=provider.id,
        resource_label=provider.code,
        severity=AuditSeverity.CRITICAL,
        payload={"scim_token": "issued", "fingerprint": provider.scim_token_fingerprint},
        reason="A SCIM bearer token was issued. It can deactivate any account in this tenant.",
        org_id=provider.org_id,
        actor_id=actor_id,
        session=session,
    )
    return token


def revoke_scim_token(
    session: Session, *, provider: IdentityProvider, actor_id: str | None = None
) -> IdentityProvider:
    """Withdraw the credential. The directory stops being able to call."""
    provider.scim_token_hash = None
    provider.scim_token_fingerprint = None
    provider.scim_token_issued_at = None
    session.flush()

    record_audit_event(
        action=AuditAction.INTEGRATION_CONFIGURED,
        resource_type="IdentityProvider",
        resource_id=provider.id,
        resource_label=provider.code,
        severity=AuditSeverity.CRITICAL,
        payload={"scim_token": "revoked"},
        reason="The SCIM bearer token was revoked.",
        org_id=provider.org_id,
        actor_id=actor_id,
        session=session,
    )
    return provider


def provider_for_token(session: Session, token: str) -> IdentityProvider:
    """The provider this bearer token speaks for.

    Looked up by hash across every tenant, because the caller is a directory
    that has not told us which tenant it is — the token *is* the claim. Only
    an active, SCIM-enabled provider is accepted: a credential that outlives
    the integration it was issued for is exactly the one nobody notices.
    """
    from app.security.crypto import compare_digest, hash_token

    presented = (token or "").strip()
    if not presented:
        raise PermissionDenied("A SCIM request must present a bearer token.")

    digest = hash_token(presented)
    from app.models.base import unscoped

    with unscoped():
        candidates = (
            session.execute(
                select(IdentityProvider).where(
                    IdentityProvider.scim_token_hash.is_not(None),
                    IdentityProvider.deleted_at.is_(None),
                )
            )
            .scalars()
            .all()
        )

    for provider in candidates:
        # Constant-time even though the hash is not itself secret: the loop
        # should not leak which prefix matched.
        if provider.scim_token_hash and compare_digest(provider.scim_token_hash, digest):
            if not provider.is_active or not provider.scim_enabled:
                raise PermissionDenied(
                    "That token belongs to a provider whose SCIM integration is off."
                )
            provider.scim_last_seen_at = utcnow()
            return provider

    raise PermissionDenied("That SCIM token is not recognised.")

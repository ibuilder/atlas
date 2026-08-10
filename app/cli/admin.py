"""Administrative commands: provisioning, integrity checks, maintenance jobs.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import click
from flask.cli import AppGroup

from app.context import system_context, use_context
from app.extensions import db

admin_cli = AppGroup("atlas", help="Atlas administrative commands.")

__all__ = ["admin_cli"]


@admin_cli.command("sync-permissions")
def sync_permissions() -> None:
    """Upsert the permission catalogue and refresh system roles everywhere."""
    from app.models.org import Organization
    from app.services.iam.provisioning import ensure_system_roles, sync_permission_catalog

    with use_context(system_context("cli")):
        changed = sync_permission_catalog(db.session)
        organizations = db.session.query(Organization).all()
        for organization in organizations:
            ensure_system_roles(db.session, organization.id)
        db.session.commit()

    click.echo(f"Permissions synced ({changed} changed) across {len(organizations)} organizations.")


@admin_cli.command("verify-audit")
@click.option("--org", "org_slug", required=True, help="Organization slug.")
def verify_audit(org_slug: str) -> None:
    """Re-walk an organization's audit chain and report its integrity."""
    from app.models.org import Organization
    from app.services.audit.recorder import verify_chain

    with use_context(system_context("cli")):
        organization = (
            db.session.query(Organization).filter(Organization.slug == org_slug).one_or_none()
        )
        if organization is None:
            raise click.ClickException(f"No organization with slug {org_slug!r}.")

        result = verify_chain(db.session, org_id=organization.id)

    if result["intact"]:
        click.echo(f"Chain intact. {result['events_checked']} events verified.")
    else:
        raise click.ClickException(
            f"Chain broken: {result['failure']} at sequence {result['at_sequence']}."
        )


@admin_cli.command("create-admin")
@click.option("--org", "org_slug", required=True)
@click.option("--email", required=True)
@click.option("--name", required=True)
@click.option("--password", required=True, hide_input=True, prompt=True)
def create_admin(org_slug: str, email: str, name: str, password: str) -> None:
    """Create an organization administrator."""
    from app.models.org import Organization
    from app.services.iam.provisioning import create_user

    with use_context(system_context("cli")):
        organization = (
            db.session.query(Organization).filter(Organization.slug == org_slug).one_or_none()
        )
        if organization is None:
            raise click.ClickException(f"No organization with slug {org_slug!r}.")

        user = create_user(
            db.session,
            org_id=organization.id,
            email=email,
            full_name=name,
            password=password,
            role_codes=["org_admin"],
        )
        db.session.commit()

    click.echo(f"Created administrator {user.email} in {organization.name}.")
    click.echo("Enrol multi-factor authentication before using this account in production.")


@admin_cli.command("check-schema")
def check_schema() -> None:
    """Verify the structural invariants the isolation design relies on."""
    from app.models.registry import validate_schema

    validate_schema()
    click.echo("Schema invariants OK: tenant coverage, audit columns, indexed foreign keys.")


@admin_cli.command("purge-expired")
def purge_expired() -> None:
    """Remove expired sessions and idempotency records."""
    from app.api.idempotency import purge_expired as purge_idempotency
    from app.services.iam.session_service import purge_expired_sessions

    with use_context(system_context("cli")):
        sessions = purge_expired_sessions(db.session)
        db.session.commit()
        keys = purge_idempotency(db.session)

    click.echo(f"Purged {sessions} expired sessions and {keys} idempotency records.")

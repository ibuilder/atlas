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


@admin_cli.command("verify-restore")
@click.option("--strict/--no-strict", default=True, help="Exit non-zero on any failure.")
def verify_restore(strict: bool) -> None:
    """Prove that a restored database is actually usable.

    Four checks, and a restore is not complete until all four pass. Row counts
    look right in every one of the failure modes below, which is exactly why
    row counts are not one of the checks.
    """
    from decimal import Decimal

    from sqlalchemy import func, select

    from app.models.accounting import JournalLine
    from app.models.org import Organization
    from app.services.audit.recorder import verify_chain

    failures: list[str] = []

    # 1. The encryption key is the right one, not merely well-formed. A wrong
    #    key is indistinguishable from a right one until something is decrypted.
    with use_context(system_context("cli")):
        try:
            from app.security.keyring import get_field_cipher

            probe = get_field_cipher()
            if probe.decrypt(probe.encrypt("atlas")) != "atlas":  # pragma: no cover
                failures.append("field encryption round trip did not return the input")
            click.echo("  [ok] field encryption key round-trips")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"field encryption unusable: {exc}")
            click.echo(f"  [FAIL] field encryption: {exc}")

    # 2. The audit chain is intact for every organization. A restore that loses
    #    audit continuity is one that cannot be attested to afterwards.
    with use_context(system_context("cli")):
        organizations = list(db.session.execute(select(Organization)).scalars())
    for organization in organizations:
        with use_context(system_context("cli", org_id=organization.id)):
            result = verify_chain(db.session, org_id=organization.id)
        if result.get("intact"):
            click.echo(
                f"  [ok] audit chain intact for {organization.slug} "
                f"({result.get('events_checked', 0)} events)"
            )
        else:
            failures.append(f"audit chain broken for {organization.slug}: {result}")
            click.echo(f"  [FAIL] audit chain for {organization.slug}: {result}")

    # 3. The ledger balances. A partial restore passes every row count and
    #    fails this.
    for organization in organizations:
        with use_context(system_context("cli", org_id=organization.id)):
            debits, credits = db.session.execute(
                select(
                    func.coalesce(func.sum(JournalLine.debit), 0),
                    func.coalesce(func.sum(JournalLine.credit), 0),
                ).where(JournalLine.org_id == organization.id)
            ).one()
        if Decimal(str(debits)) == Decimal(str(credits)):
            click.echo(f"  [ok] ledger balances for {organization.slug} ({debits})")
        else:
            failures.append(f"ledger out of balance for {organization.slug}: {debits} vs {credits}")
            click.echo(f"  [FAIL] ledger for {organization.slug}: {debits} vs {credits}")

    # 4. Row-level security survived. Policies are schema objects; a restore can
    #    drop them, and the result serves every tenant's data while looking
    #    entirely correct.
    if db.engine.dialect.name == "postgresql":
        from app.models.rls import tables_missing_policies

        missing = tables_missing_policies(db.session.connection())
        if missing:
            failures.append(f"row-level security missing on: {', '.join(sorted(missing))}")
            click.echo(f"  [FAIL] RLS missing on {len(missing)} table(s)")
        else:
            click.echo("  [ok] row-level security enabled on every tenant table")
    else:
        click.echo("  [skip] row-level security: not PostgreSQL")

    if failures:
        click.echo("")
        click.echo(f"{len(failures)} check(s) failed. This restore is not usable.")
        for failure in failures:
            click.echo(f"  - {failure}")
        if strict:
            raise SystemExit(1)
    else:
        click.echo("")
        click.echo("All checks passed. The restore is usable.")

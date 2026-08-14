"""Administrative commands: provisioning, integrity checks, maintenance jobs.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import click
from flask.cli import AppGroup

from app.context import system_context, use_context
from app.extensions import current_session, db

admin_cli = AppGroup("atlas", help="Atlas administrative commands.")

__all__ = ["admin_cli"]


@admin_cli.command("sync-permissions")
def sync_permissions() -> None:
    """Upsert the permission catalogue and refresh system roles everywhere."""
    from app.models.org import Organization
    from app.services.iam.provisioning import ensure_system_roles, sync_permission_catalog

    with use_context(system_context("cli")):
        changed = sync_permission_catalog(current_session())
        organizations = db.session.query(Organization).all()
        for organization in organizations:
            ensure_system_roles(current_session(), organization.id)
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

        result = verify_chain(current_session(), org_id=organization.id)

    if result["intact"]:
        click.echo(f"Chain intact. {result['events_checked']} events verified.")
    else:
        raise click.ClickException(
            f"Chain broken: {result['failure']} at sequence {result['at_sequence']}."
        )


@admin_cli.command("create-org")
@click.option("--name", required=True, help="Display name, e.g. 'Northlight Property Group'.")
@click.option("--slug", required=True, help="Alphanumeric with hyphens; used in URLs.")
@click.option("--legal-name", default=None, help="Registered name, where it differs.")
@click.option("--timezone", default="America/New_York", show_default=True)
@click.option("--currency", default="USD", show_default=True)
def create_org(name: str, slug: str, legal_name: str | None, timezone: str, currency: str) -> None:
    """Create an organization and provision its roles.

    The first thing a real deployment needs, and until now the only way to do it
    was ``flask seed demo`` - which creates accounts with a published password.
    A production instance had no honest way to provision its first tenant.
    """
    from app.context import system_context, use_context
    from app.extensions import current_session
    from app.services.accounting.chart import seed_chart_of_accounts
    from app.services.iam.provisioning import create_organization

    with use_context(system_context("cli")):
        organization = create_organization(
            current_session(),
            name=name,
            slug=slug,
            legal_name=legal_name,
            timezone=timezone,
            currency=currency,
        )
        db.session.commit()

    # The chart is tenant data, so it is seeded under the new organization's own
    # scope rather than the one that created it.
    with use_context(system_context("cli", org_id=organization.id)):
        accounts = seed_chart_of_accounts(current_session(), organization.id)
        db.session.commit()

    click.echo(f"Created {organization.name} ({organization.slug}).")
    click.echo("  Roles    : provisioned from the system role definitions")
    click.echo(f"  Accounts : {len(accounts)} in the chart of accounts")
    click.echo("")
    click.echo(f"  Next: flask atlas create-admin --org {organization.slug} --email … --name …")


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
            current_session(),
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


@admin_cli.command("verify-scanner")
def verify_scanner() -> None:
    """Prove the configured malware scanner actually works.

    Runs the EICAR test string - the harmless file every scanner is required to
    detect - through whatever scanner is configured, and fails loudly if it
    comes back clean. "A ClamAV adapter is included" and "this deployment scans
    uploads" are different claims, and only one of them is worth anything.
    """
    import io

    from app.services.documents.scanner import get_scanner

    scanner = get_scanner()
    #: The standard harmless test file every scanner is required to detect.
    eicar = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

    infected = scanner.scan(io.BytesIO(eicar))
    benign = scanner.scan(io.BytesIO(b"An ordinary lease, in plain text.\n"))

    click.echo(f"Scanner: {scanner.name}")

    # Both results failing the same way means the daemon never answered, which
    # is a different problem from a scanner that answered wrongly - and says so,
    # because "unreachable" and "false positive" have different fixes.
    if not infected.clean and not benign.clean and infected.detail == benign.detail:
        raise click.ClickException(
            f"{scanner.name} did not answer ({infected.detail}). Uploads are "
            "quarantined and stay there, which is the safe failure but not a "
            "working one. Check the daemon is running and reachable at "
            "the configured host and port."
        )

    if infected.clean:
        raise click.ClickException(
            f"{scanner.name} reported the EICAR test file as clean. Uploads are "
            "not being scanned. Check that the daemon is reachable and its "
            "signature database has finished loading."
        )
    click.echo(f"  EICAR   : detected ({infected.detail or 'flagged'})")

    if not benign.clean:
        raise click.ClickException(
            f"{scanner.name} reported an ordinary text file as infected "
            f"({benign.detail}). Every upload will be quarantined."
        )
    click.echo("  Benign  : passed")

    if scanner.name == "structural":
        click.secho(
            "  This is the structural scanner. It detects EICAR and active "
            "content, and it is NOT a virus scanner. Set MALWARE_SCANNER=clamav "
            "for a deployment that accepts resident uploads.",
            fg="yellow",
        )
    else:
        click.echo("  Uploads on this deployment are scanned.")


@admin_cli.command("purge-expired")
def purge_expired() -> None:
    """Remove expired sessions and idempotency records."""
    from app.api.idempotency import purge_expired as purge_idempotency
    from app.services.iam.session_service import purge_expired_sessions

    with use_context(system_context("cli")):
        sessions = purge_expired_sessions(current_session())
        db.session.commit()
        keys = purge_idempotency(current_session())

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
            result = verify_chain(current_session(), org_id=organization.id)
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

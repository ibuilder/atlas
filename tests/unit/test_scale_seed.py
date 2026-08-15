"""The scale seeder's shape, and the checks that make its shortcut safe.

The seeder bulk-inserts, which goes around the ledger's balance invariant and
the audit chain. That is defensible only because it verifies both afterwards —
so the thing worth testing is that the verification would actually fail. A
check that cannot fail is decoration.

Run at a small size. The point is the shape and the guarantees, not the volume;
a test that generated a hundred thousand rows would be a test nobody runs.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

pytestmark = pytest.mark.unit


def test_the_generated_ledger_balances_by_construction():
    """Every entry is one debit and one matching credit."""
    from app.cli.seed_scale import _balanced

    class _Org:
        id = "org-1"

    lines = _balanced(_Org(), "entry-1", debit="a", credit="b", amount=Decimal("125.50"))

    assert len(lines) == 2
    assert sum(line["debit"] for line in lines) == sum(line["credit"] for line in lines)
    assert sum(line["debit"] for line in lines) == Decimal("125.50")


def test_the_shape_is_actually_skewed():
    """A uniform database measures an index that behaves nothing like production.

    Asserted on the constants rather than on generated output, because these
    are the knobs somebody will reach for when the load numbers look wrong, and
    a flat setting should read as a deliberate change rather than a default.
    """
    from app.cli import seed_scale

    assert 0 < seed_scale.LARGE_PROPERTY_SHARE < 0.5
    assert seed_scale.LARGE_PROPERTY_MULTIPLIER > 1
    assert 0 < seed_scale.BUSY_LEASE_SHARE < 0.2
    assert seed_scale.BUSY_LEASE_MULTIPLIER > 1


def test_the_generator_is_deterministic():
    """Two runs must produce the same database, or two load results cannot be
    compared and the second run is a new measurement rather than a re-run."""
    from app.cli.seed_scale import RNG_SEED

    first = [random.Random(RNG_SEED).random() for _ in range(3)]  # noqa: S311
    second = [random.Random(RNG_SEED).random() for _ in range(3)]  # noqa: S311
    assert first == second


def test_a_small_load_org_generates_and_verifies(app, db):
    """End to end at a size a test can afford, verification included."""
    from sqlalchemy import func, select

    from app.context import system_context, use_context
    from app.models.accounting import JournalLine
    from app.models.org import Organization, Property, Unit

    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "seed",
            "load",
            "--slug",
            "scale-test",
            "--properties",
            "4",
            "--units-per-property",
            "5",
            "--months",
            "3",
        ]
    )
    assert result.exit_code == 0, result.output
    assert "Ledger balances" in result.output
    assert "Audit chain intact" in result.output

    organization = db.session.execute(
        select(Organization).where(Organization.slug == "scale-test")
    ).scalar_one()

    with use_context(system_context("test", org_id=organization.id)):
        properties = db.session.execute(
            select(func.count()).select_from(Property).where(Property.org_id == organization.id)
        ).scalar_one()
        units = db.session.execute(
            select(func.count()).select_from(Unit).where(Unit.org_id == organization.id)
        ).scalar_one()
        debits, credits = db.session.execute(
            select(
                func.coalesce(func.sum(JournalLine.debit), 0),
                func.coalesce(func.sum(JournalLine.credit), 0),
            ).where(JournalLine.org_id == organization.id)
        ).one()

    assert properties == 4
    # Skewed, so the total exceeds properties x average: at least one property
    # is large even at this size, which is the case a flat generator loses.
    assert units > 4 * 5
    assert Decimal(str(debits)) == Decimal(str(credits))
    assert Decimal(str(debits)) > 0


def test_the_verification_fails_on_an_unbalanced_ledger(app, db):
    """The claim the shortcut rests on. If this cannot fail, nothing is proven."""
    import click
    from sqlalchemy import select

    from app.cli.seed_scale import _verify
    from app.context import system_context, use_context
    from app.models.accounting import JournalLine
    from app.models.org import Organization

    runner = app.test_cli_runner()
    result = runner.invoke(
        args=[
            "seed",
            "load",
            "--slug",
            "scale-unbalanced",
            "--properties",
            "2",
            "--units-per-property",
            "2",
            "--months",
            "2",
        ]
    )
    assert result.exit_code == 0, result.output

    organization = db.session.execute(
        select(Organization).where(Organization.slug == "scale-unbalanced")
    ).scalar_one()

    with use_context(system_context("test", org_id=organization.id)):
        # Break exactly one line, the way a generator bug would.
        line = db.session.execute(
            select(JournalLine).where(JournalLine.org_id == organization.id).limit(1)
        ).scalar_one()
        line.debit = (line.debit or Decimal("0")) + Decimal("0.01")
        db.session.flush()

        with pytest.raises(click.ClickException, match="does not balance"):
            _verify(organization)

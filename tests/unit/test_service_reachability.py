"""Every service module is reachable from a surface, or is listed here as not.

This exists because the same defect has now shipped three times: code that is
correct, tested, and callable by nothing a user can get to. The trust
reconciliation read a column no code path wrote. The rate limit was configured
and applied to nothing. The e-sign envelope lifecycle had no route at all, which
made the consent record it stores - the one thing that has to be evidence -
something no signer had ever been shown.

A test suite cannot catch that, because the tests *are* the caller. The only
question that catches it is "can anybody who is not a test get here?".

**A surface is an API route, a console or portal view, a CLI command, or a
scheduled job.** The demo seed is deliberately not one: it proves a service
*runs*, not that anyone can reach it, and treating it as a caller is precisely
how the e-sign gap hid - the seed was the only thing signing anything.

Two things keep the measure honest. It works per *module*, because a helper
called by its own module's entry point is fine and flagging it produces noise
that gets ignored, which is how a list like this dies. And reachability is
*transitive*: a service that a reachable service calls is itself reachable,
which is how the 1099 and trust modules are reached through the report registry
and the scheduler that runs it.

Measured on imports rather than call sites, which is deliberately generous: a
module imported for one constant counts as reached. That is the safe direction
for a guard - it under-reports rather than crying wolf, and a guard nobody
trusts is one somebody deletes.

The lists below are the honest state, not an aspiration. ``docs/FEATURES.md``
is where a reader looks for what Atlas does; anything here that reads as
**Complete** there is a claim this file contradicts.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "app" / "services"

#: Callers that do not count as a surface. See the module docstring.
NOT_A_SURFACE = {
    "app/cli/seed.py",
    "app/cli/seed_operations.py",
}

#: Implemented and tested, with no route, view, command, or job that reaches
#: them. These are whole capabilities that exist in the codebase and not in the
#: product.
NO_SURFACE: dict[str, str] = {
    "leasing/tenancy": (
        "Renewals, move-outs, and deposit disposition - the most litigated "
        "thing a management company does."
    ),
    "accounting/payables": (
        "Bills: recording, threshold approval routing, and disbursement. "
        "Money going out has no surface; money coming in has three."
    ),
    "iam/scim": (
        "Directory provisioning, which is HTTP endpoints by definition - an "
        "identity provider has nothing to call."
    ),
    "imports/bulk": (
        "CSV import with a read-only plan step. The plan is the point and "
        "nobody can ask for one."
    ),
    "documents/extraction": (
        "Field extraction from uploaded documents, and the accept-or-reject "
        "review that decides what is believed."
    ),
}

#: Reachable only from the demo seed. Better than nothing - the code has run
#: against realistic data - and still not a way for a user to get there.
SEED_ONLY: dict[str, str] = {
    "accounting/reconciliation": (
        "Statement import, matching, exceptions, and sign-off - the whole "
        "bank reconciliation workspace."
    ),
    "assets/lifecycle": (
        "Service history, warranty recovery, and the repair-or-replace call "
        "that the capital plan is built on."
    ),
    "assets/spaces": (
        "The space hierarchy and what is installed in it. The demo builds one; " "nothing else can."
    ),
    "maintenance/inspections": (
        "Scheduling, performing, and completing an inspection, including the "
        "findings that raise work orders."
    ),
}


def _module_key(path: pathlib.Path) -> str:
    """``app/services/leasing/turns.py`` -> ``leasing/turns``."""
    return path.relative_to(SERVICES).with_suffix("").as_posix()


def _public_functions(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and not node.name.startswith("_")
    }


def _imports(path: pathlib.Path) -> set[str]:
    """Dotted module names this file imports, however it spells the import.

    Imports rather than call sites. A call-name check has to decide whether
    ``parse_csv`` and ``parse_statement_csv`` are the same function, and
    whichever way it decides it is wrong somewhere; an import is unambiguous,
    and a module nothing imports is a module nothing can call.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            # ``from app.services.x import y`` reaches ``app.services.x`` and,
            # where y is a submodule, ``app.services.x.y``.
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _dotted(path: pathlib.Path) -> str:
    return "app." + path.relative_to(ROOT / "app").with_suffix("").as_posix().replace("/", ".")


def _classify() -> tuple[set[str], set[str]]:
    """Split the service modules into (no surface, seed only)."""
    from_surfaces: set[str] = set()
    from_seed: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        key = path.relative_to(ROOT).as_posix()
        if key.startswith("app/services/"):
            continue
        (from_seed if key in NOT_A_SURFACE else from_surfaces).update(_imports(path))

    modules: dict[str, str] = {}
    graph: dict[str, set[str]] = {}
    for path in sorted(SERVICES.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        if not _public_functions(path):
            continue
        key = _module_key(path)
        modules[key] = _dotted(path)
        graph[key] = _imports(path)

    def close_over(start: set[str]) -> set[str]:
        """Everything those modules import, directly or through each other."""
        reached = set(start)
        changed = True
        while changed:
            changed = False
            frontier: set[str] = set()
            for key in reached:
                frontier |= graph[key]
            for key in modules.keys() - reached:
                if modules[key] in frontier:
                    reached.add(key)
                    changed = True
        return reached

    reached = close_over({key for key, name in modules.items() if name in from_surfaces})
    remaining = modules.keys() - reached
    seeded = {key for key in remaining if modules[key] in from_seed}

    return remaining - close_over(seeded) & remaining, close_over(seeded) & remaining


def test_no_capability_became_unreachable_without_being_recorded():
    """A new entry here means something shipped with no way in.

    If this fails on code you have just written, the fix is usually a route
    rather than an entry: adding to the list should be a decision somebody
    makes on purpose.
    """
    no_surface, seed_only = _classify()

    surprises = sorted((no_surface - set(NO_SURFACE)) | (seed_only - set(SEED_ONLY)))
    assert not surprises, (
        "These service modules have no surface a user can reach, and the demo "
        "seed does not count as one:\n" + "\n".join(f"  {name}" for name in surprises)
    )


def test_the_lists_do_not_outlive_the_problem():
    """Entries must go once a surface exists.

    Otherwise the list rots into somewhere names are filed and forgotten, which
    is the failure mode of every allowlist ever written.
    """
    no_surface, seed_only = _classify()
    reachable = (set(NO_SURFACE) | set(SEED_ONLY)) - no_surface - seed_only

    assert not reachable, (
        "These are recorded as unreachable but something now calls them. Remove "
        "them:\n" + "\n".join(f"  {name}" for name in sorted(reachable))
    )


def test_every_entry_says_what_is_missing():
    """A bare list of module names teaches a reader nothing."""
    for name, why in {**NO_SURFACE, **SEED_ONLY}.items():
        assert len(why) > 40, f"{name} needs a real explanation, not {why!r}"

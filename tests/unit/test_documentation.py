"""The documentation refers to things that exist.

Documentation rots quietly. A guide that names a CLI command, an environment
variable, or a metric that has since been renamed is worse than no guide: it
sends somebody looking for something that is not there, usually while they are
already having a bad day.

These check only the claims that are mechanically checkable — commands, links,
settings, and job names. They cannot tell you whether the prose is *true*, and
nothing here should be mistaken for having read it.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOYMENT = ROOT / "DEPLOYMENT.md"
DOMAIN = ROOT / "docs" / "DOMAIN.md"


def _text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def test_every_documented_cli_command_exists():
    """A runbook naming a command that was renamed is a dead end."""
    from app.cli.admin import admin_cli
    from app.cli.seed import seed_cli

    known = set(admin_cli.commands) | set(seed_cli.commands)
    documented = set(re.findall(r"flask (?:atlas|seed) ([a-z][a-z-]+)", _text(DEPLOYMENT)))

    assert documented, "the deployment guide should name some commands"
    missing = sorted(documented - known)
    assert not missing, f"documented but not implemented: {', '.join(missing)}"


def test_every_documented_setting_exists():
    """An environment variable in a table nobody reads is still a promise."""
    from app.config import load_settings

    settings = load_settings("testing")
    text = _text(DEPLOYMENT)

    # Only the ones in backticks in a table cell, which is how the guide lists
    # them; prose mentions are not a promise about a field name.
    documented = set(re.findall(r"^\| `([A-Z][A-Z0-9_]+)`", text, re.M))
    assert documented, "the deployment guide should list some settings"

    # Flask/Celery config keys the settings object exposes rather than owns.
    passthrough = {"CSRF_ENABLED", "RATELIMIT_DEFAULT", "CELERY_TASK_ALWAYS_EAGER"}
    missing = sorted(
        name for name in documented - passthrough if not hasattr(settings, name.lower())
    )
    assert not missing, f"documented but not a setting: {', '.join(missing)}"


def test_every_documented_scheduled_job_is_scheduled():
    """ "What silence means" is only useful if the job actually runs."""
    from app.tasks.celery_app import BEAT_SCHEDULE

    scheduled = {entry["task"] for entry in BEAT_SCHEDULE.values()}
    documented = set(re.findall(r"`(atlas\.[a-z_]+\.[a-z_]+)`", _text(DEPLOYMENT)))

    assert documented, "the deployment guide should name some jobs"
    # `atlas.documents.scan` runs on upload rather than on a schedule, and the
    # guide says so.
    on_demand = {"atlas.documents.scan"}
    missing = sorted(documented - scheduled - on_demand)
    assert not missing, f"documented as scheduled but not in the beat schedule: {missing}"


def test_documented_metrics_exist():
    from app import observability

    # Scoped to the sentence that lists them: `atlas_app` elsewhere in the guide
    # is a PostgreSQL role, and matching every `atlas_*` in backticks would
    # report it as a missing metric.
    paragraph = _text(DEPLOYMENT).split("Metrics worth alerting on:", 1)
    assert len(paragraph) == 2, "the metrics paragraph moved; update this test"
    documented = set(re.findall(r"`(atlas_[a-z_]+)`", paragraph[1].split("\n\n", 1)[0]))
    assert documented, "the deployment guide should name some metrics"

    exported = set()
    for name in observability.__all__:
        metric = getattr(observability, name, None)
        collector = getattr(metric, "_name", None)
        if collector:
            exported.add(collector)
            exported.add(f"{collector}_total")

    missing = sorted(documented - exported)
    assert not missing, f"documented but not exported: {', '.join(missing)}"


@pytest.mark.parametrize("path", [DEPLOYMENT, DOMAIN])
def test_relative_links_resolve(path: pathlib.Path):
    """A broken link in a guide is a paragraph nobody finishes reading."""
    text = _text(path)
    broken = []
    for target in re.findall(r"\]\((?!https?:)([^)#]+)", text):
        resolved = (path.parent / target).resolve()
        if not resolved.exists():
            broken.append(target)
    assert not broken, f"{path.name} links to nothing: {', '.join(sorted(set(broken)))}"


def test_the_readme_points_at_both_guides():
    """Documentation nobody can find is documentation nobody reads."""
    readme = _text(ROOT / "README.md")
    assert "docs/DOMAIN.md" in readme
    assert "DEPLOYMENT.md" in readme


def test_the_version_agrees_with_itself_and_the_changelog():
    """One version, stated in three places, and they drift.

    This is not pedantry: 0.6.0's first two milestones shipped against a
    ``__version__`` of 0.5.0, so ``/api/v1`` reported a version that had not
    included the endpoints being called. Nobody notices until somebody files a
    bug against the wrong release.
    """
    import re
    import tomllib

    from app import __version__

    root = pathlib.Path(__file__).resolve().parents[2]
    packaged = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = packaged["project"]["version"]

    assert __version__ == declared, (
        f"app.__version__ is {__version__} and pyproject says {declared}."
    )

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    released = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.MULTILINE)
    assert released, "The changelog has no released version headings."
    assert released[0] == declared, (
        f"The newest changelog entry is {released[0]} and the package says {declared}. "
        "Cut the release heading, or bump the version."
    )

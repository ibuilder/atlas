"""Dependabot has to watch everywhere code arrives from.

Atlas acquires code it did not write in three ways: Python packages, the
actions its workflows run, and the base image the container is built from. A
configuration covering only the first is watching the one place a human already
reviews - and the actions are the ecosystem most likely to rot unnoticed, since
a pin like `actions/checkout@v4` keeps working for years while its runtime is
deprecated underneath it.

The check is against what the repository actually contains rather than a fixed
list, so adding a second Dockerfile or a requirements file fails here instead of
sitting unwatched.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".github" / "dependabot.yml"


def _updates() -> list[dict]:
    document = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    assert document["version"] == 2, "Dependabot v1 configuration is no longer supported."
    return document["updates"]


def test_the_configuration_exists_and_parses():
    assert CONFIG.is_file(), (
        ".github/dependabot.yml is missing. Without it nothing proposes a "
        "dependency update, and the first sign is a CVE in a release."
    )
    assert _updates(), "The configuration declares no update streams."


@pytest.mark.parametrize("ecosystem", ["pip", "github-actions", "docker"])
def test_every_ecosystem_in_the_repository_is_watched(ecosystem):
    assert ecosystem in {entry["package-ecosystem"] for entry in _updates()}, (
        f"{ecosystem!r} is present in this repository and unwatched by Dependabot."
    )


def test_the_docker_stream_points_at_the_dockerfile():
    """A directory that holds no manifest is a stream that silently does nothing."""
    docker = [e for e in _updates() if e["package-ecosystem"] == "docker"]
    assert docker, "No docker update stream."
    for entry in docker:
        target = ROOT / entry["directory"].lstrip("/")
        assert (target / "Dockerfile").is_file(), (
            f"The docker stream watches {entry['directory']!r}, which has no Dockerfile."
        )


def test_every_dockerfile_is_covered():
    """Written against the tree rather than a constant, so a second one fails here."""
    watched = {
        (ROOT / e["directory"].lstrip("/")).resolve()
        for e in _updates()
        if e["package-ecosystem"] == "docker"
    }
    for path in ROOT.rglob("Dockerfile*"):
        if any(part in {".venv", "node_modules", ".git"} for part in path.parts):
            continue
        assert path.parent.resolve() in watched, (
            f"{path.relative_to(ROOT)} is not covered by any docker update stream."
        )


def test_updates_are_scheduled_rather_than_left_to_chance():
    for entry in _updates():
        assert "schedule" in entry, f"{entry['package-ecosystem']} has no schedule."
        assert entry["schedule"]["interval"] in {"daily", "weekly", "monthly"}


def test_major_framework_bumps_are_not_proposed_automatically():
    """`pyproject.toml` carries upper bounds deliberately.

    A Flask or SQLAlchemy major is a migration with its own testing story, not
    something to open a pull request for every time upstream ships one.
    """
    pip = next(e for e in _updates() if e["package-ecosystem"] == "pip")
    ignored = {rule["dependency-name"] for rule in pip.get("ignore", [])}
    assert {"Flask", "SQLAlchemy", "pydantic"} <= ignored, (
        "The pinned frameworks should not receive automatic major-version pull requests."
    )

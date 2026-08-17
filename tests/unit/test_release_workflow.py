"""The release workflow, checked without running it.

A release workflow is the least testable thing in a repository: it runs once
per version, on a tag, on somebody else's machine, and the way you find out it
was wrong is that a release half-happened.

That is not hypothetical here. `v0.7.0` built and published its image
correctly, then failed generating the SBOM because the step asked the registry
for `:v0.7.0` while `docker/metadata-action` had published `:0.7.0` — it strips
the `v` from a semver pattern. The scan and the draft release were skipped
behind it, on a release that was otherwise fine.

The checks below are cheap and would each have caught something real.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _image_job_steps() -> list[dict]:
    return _workflow()["jobs"]["image"]["steps"]


def test_the_workflow_parses():
    assert _image_job_steps(), "the image job declares no steps"


def test_nothing_addresses_the_image_by_the_git_ref_name():
    """The bug itself.

    `github.ref_name` is the *git* tag (`v0.7.0`). The *image* tag is derived
    separately by metadata-action and does not carry the `v`. Any step that
    builds a registry reference out of the git ref is asking for a tag nobody
    pushed.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if "ghcr.io" in stripped and "github.ref_name" in stripped:
            pytest.fail(
                f"{stripped!r} builds an image reference from the git ref. "
                "metadata-action strips the leading 'v', so this names a tag "
                "that was never published. Use the build step's digest."
            )


@pytest.mark.parametrize("step_name", ["Generate SBOM for the published image", "Scan the published image"])
def test_the_post_build_steps_address_the_image_by_digest(step_name):
    """A digest pins the artifact that was just pushed.

    A tag is a moving reference resolved at a later moment by a different tool,
    which is how the two disagreed in the first place.
    """
    steps = {step.get("name"): step for step in _image_job_steps()}
    assert step_name in steps, f"{step_name!r} is gone from the release workflow"

    reference = " ".join(str(value) for value in steps[step_name].get("with", {}).values())
    assert "steps.build.outputs.digest" in reference, (
        f"{step_name!r} does not address the image by digest: {reference!r}"
    )
    assert "@" in reference, "a digest reference is name@sha256:..., not name:tag"


def test_the_build_step_is_addressable():
    """The digest comes from this step's id; without it the reference is empty,
    which fails in a way that reads like a registry problem rather than a
    workflow one."""
    ids = {step.get("id") for step in _image_job_steps()}
    assert "build" in ids, "the build step lost its `id: build`, so its digest output is unreachable"


def test_the_tag_is_checked_against_the_packaged_version():
    """A v0.7.1 tag on a tree that says 0.7.0 publishes an image whose API
    reports a version it is not."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "pyproject.toml" in text and "GITHUB_REF_NAME" in text, (
        "the release no longer verifies that the tag matches the packaged version"
    )


def test_the_scan_does_not_gate_the_release():
    """Deliberate, and worth pinning so it is not 'tightened' by accident.

    A base-image CVE published between the build and the scan should be
    visible, not a reason a tagged release silently does not exist.
    """
    steps = {step.get("name"): step for step in _image_job_steps()}
    assert str(steps["Scan the published image"]["with"]["exit-code"]) == "0"

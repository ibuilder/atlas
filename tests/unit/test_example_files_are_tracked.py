"""Example files a deployer needs must actually be in the repository.

`.gitignore` carries `.env.*` so nobody commits a filled-in environment file by
accident. That rule also swallows `.env.production.example`, which is the one
file a deployer is told to copy — and it did: the file existed locally, the
deployment tests read it and passed, and it was never committed. On a fresh
clone those tests would have failed on a file that only ever existed on the
machine that wrote it.

The check is `git check-ignore`, run against the real repository, because that
is the only thing that answers the actual question. A test asserting the file
exists on disk is precisely the test that was already passing.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Files somebody following the README or DEPLOYMENT.md is told to copy.
REQUIRED_EXAMPLES = (
    ".env.example",
    ".env.production.example",
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", *args],  # noqa: S607 - git resolved from PATH, as everywhere else
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("name", REQUIRED_EXAMPLES)
def test_the_example_file_exists(name):
    assert (ROOT / name).is_file(), f"{name} is referenced by the docs and missing."


@pytest.mark.parametrize("name", REQUIRED_EXAMPLES)
def test_the_example_file_is_not_ignored(name):
    """Existing on disk and being in the repository are different facts.

    `git check-ignore` exits 0 when the path *is* ignored, which is the failure
    here: a deployer cloning the repository would not receive the file at all.
    """
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")

    # `-q`, not `-v`. In verbose mode git exits 0 whenever *any* pattern
    # matched, including the `!` negation that un-ignores the file — so `-v`
    # reports an un-ignored file as ignored. Quiet mode answers the actual
    # question: 0 means ignored.
    result = _git("check-ignore", "-q", name)
    assert result.returncode != 0, (
        f"{name} is ignored. It exists here and would not exist in a fresh "
        "clone, so anything reading it passes locally and fails in CI."
    )


def test_a_filled_in_environment_file_is_still_ignored():
    """The negation must not have opened the door it was narrowing.

    `!.env.production.example` sitting under `.env.*` is easy to widen by
    accident into something that stops ignoring the real file next to it.
    """
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")

    for name in (".env", ".env.production", ".env.local"):
        assert _git("check-ignore", "-q", name).returncode == 0, (
            f"{name} is no longer ignored. That is a filled-in environment file "
            "one `git add .` away from being published."
        )


@pytest.mark.parametrize("name", REQUIRED_EXAMPLES)
def test_the_example_file_carries_no_filled_in_secret(name):
    """The reason the ignore rule exists in the first place.

    Every secret in an example is present and blank: a placeholder that looks
    like a key is one somebody ships.
    """
    text = (ROOT / name).read_text(encoding="utf-8")
    for line in text.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        # Suffix, not substring: `PASSWORD_MIN_LENGTH=12` is a policy setting
        # and matching it would make this test noise somebody learns to skip.
        if key.strip().upper().endswith(("_SECRET", "_PASSWORD", "_KEY", "_TOKEN")):
            assert not value.strip(), (
                f"{name} sets {key.strip()} to a value. Example secrets stay blank; "
                "one that looks usable is one somebody uses."
            )

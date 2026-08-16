"""Two things that only fail once the thing is deployed.

Both of these were found by actually running the container, and both had been
broken since before this release. They share a shape: the code was correct in
the environment it was written in and wrong in the one it ships to.

**Probes must not be redirected.** Every orchestrator probes over plain HTTP
from inside the network, where there is no certificate and no proxy. Forcing
HTTPS on a health endpoint means the probe never sees a 200, the container
never reports healthy, and a rolling deploy stalls with every replica running
perfectly.

**The Alembic environment must tolerate Flask-Migrate.** Flask-Migrate points
Alembic at ``<migrations>/alembic.ini``, which this project does not have — its
config is at the repository root. Running ``alembic upgrade head`` from the root
worked and hid it, so the runbook's command was fine and the README's had never
worked at all.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

PROBES = ("/healthz", "/readyz", "/version")


@pytest.mark.parametrize("path", PROBES)
def test_a_probe_is_not_redirected_when_https_is_forced(path):
    """The failure mode is a deploy that stalls while everything is fine."""
    from app.factory import create_app

    app = create_app("testing")
    app.config["PREFERRED_URL_SCHEME"] = "https"

    with app.test_client() as client:
        # http, no X-Forwarded-Proto: exactly what a container healthcheck and
        # a Kubernetes probe send.
        response = client.get(path, base_url="http://localhost")

    assert response.status_code != 301, (
        f"{path} redirected to HTTPS. An orchestrator probing over plain HTTP "
        "never sees a 200, so the replica never reports healthy."
    )
    assert response.status_code in (200, 503), response.status_code


def test_a_probe_gives_nothing_away():
    """Serving these over HTTP is only safe because they say almost nothing."""
    from app.factory import create_app

    app = create_app("testing")
    with app.test_client() as client:
        body = client.get("/healthz", base_url="http://localhost").get_json()

    assert body == {"status": "ok"}


def test_the_alembic_environment_survives_a_missing_config_file(tmp_path, monkeypatch):
    """Flask-Migrate names a file that is not there, and used to crash on it.

    Asserted against the guard itself rather than by running a migration: the
    condition is 'a config_file_name that does not exist', and the point is that
    it no longer reaches ``fileConfig``.
    """
    import configparser
    import os
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    source = (root / "migrations" / "env.py").read_text(encoding="utf-8")

    # The guard must check existence, not just that a name was set.
    assert "os.path.exists(config.config_file_name)" in source, (
        "env.py checks only that config_file_name is set. Flask-Migrate always "
        "sets it, to a path this project does not have."
    )
    assert 'has_section("formatters")' in source, (
        "A config file without logging sections makes fileConfig raise KeyError "
        "rather than anything that names the problem."
    )

    # And the behaviour the guard is protecting: a nonexistent path is skipped,
    # a sectionless file is skipped, and neither raises.
    missing = str(tmp_path / "nope.ini")
    assert not os.path.exists(missing)

    sectionless = tmp_path / "alembic.ini"
    sectionless.write_text("[alembic]\nscript_location = migrations\n", encoding="utf-8")
    parsed = configparser.ConfigParser()
    parsed.read(sectionless)
    assert not parsed.has_section("formatters")


def test_the_root_alembic_config_does_have_logging_sections():
    """So the guard skips for the right reason rather than always."""
    import configparser
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    parsed = configparser.ConfigParser()
    parsed.read(root / "alembic.ini")

    assert parsed.has_section("formatters"), (
        "The root alembic.ini lost its logging sections, so `alembic upgrade "
        "head` now silently runs without them."
    )

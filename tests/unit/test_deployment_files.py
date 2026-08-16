"""The deployment files, checked against each other.

Atlas is meant to be deployed by people who did not write it, which means the
first time these files are wrong is somebody else's outage at a time nobody
chose. The checks here are cheap and the failure mode they prevent is not.

Two things are asserted. That every secret the production compose demands is
named in the example env file — a variable that exists in one and not the other
fails at `docker compose up` with no clue where to look. And that the
production file does not carry the development file's conveniences, each of
which is a genuine liability once the thing is reachable.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROD = ROOT / "docker-compose.prod.yml"
DEV = ROOT / "docker-compose.yml"
EXAMPLE = ROOT / ".env.production.example"


def _compose(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_both_compose_files_parse():
    for path in (DEV, PROD):
        document = _compose(path)
        assert document["services"], f"{path.name} declares no services"


def test_every_required_variable_is_in_the_example():
    """Otherwise the first sign is a deploy failing with a bare variable name."""
    required = set(re.findall(r"\$\{([A-Z_]+):\?", PROD.read_text(encoding="utf-8")))
    # `VAR` appears in the header comment explaining the `:?` syntax.
    required.discard("VAR")
    declared = set(re.findall(r"^([A-Z_]+)=", EXAMPLE.read_text(encoding="utf-8"), re.M))

    assert required, "The production compose demands nothing, which cannot be right."
    assert required <= declared, (
        f"{sorted(required - declared)} are required by docker-compose.prod.yml "
        "and absent from .env.production.example."
    )


def test_no_secret_has_a_working_default():
    """A default that boots is a default that ships to production.

    `${VAR:-fallback}` on a secret is the failure this guards: it starts
    cleanly, looks correct, and every deployment shares the same key.
    """
    text = PROD.read_text(encoding="utf-8")
    for name in (
        "SECRET_KEY",
        "FIELD_ENCRYPTION_KEY",
        "WEBHOOK_SIGNING_SECRET",
        "POSTGRES_PASSWORD",
    ):
        assert f"${{{name}:-" not in text, f"{name} has a fallback value in the production compose."
        assert f"${{{name}:?" in text, f"{name} is not demanded in the production compose."


def test_the_production_file_does_not_ship_the_development_conveniences():
    """Each of these is fine locally and a liability once reachable."""
    services = _compose(PROD)["services"]
    app_env = services["web"]["environment"]

    assert app_env["ATLAS_ENV"] == "production"
    assert app_env["FORCE_HTTPS"] == "true"
    assert app_env["SESSION_COOKIE_SECURE"] == "true"
    # A wildcard would hand any origin an authenticated session, and the
    # application refuses to boot on one — but it should never get that far.
    assert "*" not in str(app_env.get("CORS_ALLOWED_ORIGINS", ""))


def test_the_database_is_not_published():
    """The development file exposes 5432 so a client can attach.

    On a deployed host that is the database on the internet, behind a password
    in an env file.
    """
    assert "ports" not in _compose(PROD)["services"]["db"]
    # Stated as a contrast rather than assumed: if the development file stops
    # publishing it, this test's premise has changed and should be re-read.
    assert "ports" in _compose(DEV)["services"]["db"]


def test_the_web_port_is_bound_to_loopback():
    """FORCE_HTTPS with a world-published plain-HTTP port redirects to a
    scheme the app is not being served on. Put a proxy in front."""
    ports = _compose(PROD)["services"]["web"]["ports"]
    assert all(str(entry).startswith("127.0.0.1:") for entry in ports), ports


def test_migrations_run_as_their_own_step():
    """Two web replicas starting together race the same migration, and
    Alembic's lock turns that race into a deploy that hangs."""
    services = _compose(PROD)["services"]
    assert "migrate" in services
    for name in ("web", "worker", "beat"):
        gate = services[name]["depends_on"]["migrate"]
        assert gate["condition"] == "service_completed_successfully", name


def test_the_image_reference_is_pinned():
    """`:latest` means a restart six months from now brings back whatever was
    pushed since, rather than the version that was tested."""
    image = _compose(PROD)["services"]["web"]["image"]
    assert ":latest" not in image, image
    assert re.search(r":\d+\.\d+\.\d+", image), f"{image} is not pinned to a version."


def test_the_pinned_image_matches_the_package_version():
    """A compose file offering an image tag that was never built is worse than
    one offering none: it fails at pull, on somebody else's machine."""
    import tomllib

    from app import __version__

    packaged = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = packaged["project"]["version"]
    image = _compose(PROD)["services"]["web"]["image"]

    assert declared in image, f"{image} does not carry version {declared}."
    assert __version__ == declared

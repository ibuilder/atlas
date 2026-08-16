"""Settings as they actually arrive: from environment variables.

The rest of the suite constructs settings in Python, where a list is a list.
Deployment does not: every value arrives as a string, and pydantic-settings
treats a ``list`` field as *complex* — running ``json.loads`` on it before any
validator runs. A ``mode="before"`` validator written to accept a
comma-separated string therefore never sees one, and the application refuses to
start with a parse error naming a field the operator set correctly.

That is exactly what happened to ``CORS_ALLOWED_ORIGINS``, and it survived
every test until somebody deployed the thing. These tests go through the
environment for that reason.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _settings(monkeypatch, **env: str):
    from app.config import load_settings

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return load_settings("development")


def test_a_single_cors_origin_parses(monkeypatch):
    """The form anybody writes first, and the one that used to fail."""
    settings = _settings(monkeypatch, CORS_ALLOWED_ORIGINS="https://atlas.example.com")
    assert settings.cors_allowed_origins == ["https://atlas.example.com"]


def test_comma_separated_origins_parse_and_are_stripped(monkeypatch):
    settings = _settings(
        monkeypatch,
        CORS_ALLOWED_ORIGINS="https://a.example.com, https://b.example.com ,",
    )
    assert settings.cors_allowed_origins == ["https://a.example.com", "https://b.example.com"]


def test_an_empty_value_is_no_origins_rather_than_one_empty_one(monkeypatch):
    """`CORS_ALLOWED_ORIGINS=` in an env file is a deployer leaving it unset.

    A list holding a single empty string would be read downstream as an origin,
    and matching against it is a bug that looks like configuration.
    """
    settings = _settings(monkeypatch, CORS_ALLOWED_ORIGINS="")
    assert settings.cors_allowed_origins == []


def test_the_json_form_still_parses(monkeypatch):
    """Somebody following pydantic-settings' own documentation writes this."""
    settings = _settings(
        monkeypatch, CORS_ALLOWED_ORIGINS='["https://a.example.com","https://b.example.com"]'
    )
    assert settings.cors_allowed_origins == ["https://a.example.com", "https://b.example.com"]


def test_the_example_env_file_parses_as_written(monkeypatch):
    """The file we hand a deployer, read the way a deployer reads it.

    Every value in `.env.production.example` that is not a secret placeholder
    goes through the real settings loader. A file that documents a format the
    application refuses is worse than no file.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[2]
    example = (root / ".env.production.example").read_text(encoding="utf-8")

    values = dict(re.findall(r"^([A-Z_]+)=(.*)$", example, re.MULTILINE))
    # The secrets are deliberately blank in the example; supply valid ones so
    # the parse is testing format rather than emptiness.
    monkeypatch.setenv("SECRET_KEY", "x" * 64)
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", "y" * 64)
    monkeypatch.setenv("WEBHOOK_SIGNING_SECRET", "z" * 64)

    for key, raw in values.items():
        if raw.strip():
            monkeypatch.setenv(key, raw.strip())

    from app.config import load_settings

    settings = load_settings("development")
    assert settings.cors_allowed_origins == ["https://atlas.example.com"]

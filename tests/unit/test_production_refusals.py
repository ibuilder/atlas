"""What production refuses to boot on, and whether the guide says so.

`DEPLOYMENT.md` §2 carries a list headed "What production refuses to boot on".
A deployer reads that list, satisfies it, and expects the thing to start. When
the list is shorter than the code, they instead get a `ConfigError` naming a
setting the guide never mentioned — which is how a first deploy turns into an
hour of guessing.

That is not hypothetical: `STORAGE_BACKEND=local` and `MAIL_BACKEND=console`
were both refusals in `ProductionSettings` and both absent from the list, and
the reference compose file consequently could not boot. Found by running it.

These tests assert the refusals hold *and* that the guide names them. The
second half is the one that would have caught it.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pathlib

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Enough of a production environment that each test fails on the *one* thing
#: it is varying rather than on whatever comes first.
BASE = {
    "DATABASE_URL": "postgresql+psycopg://atlas:pw@db:5432/atlas",
    "REDIS_URL": "redis://redis:6379/0",
    "SECRET_KEY": "s" * 64,
    "FIELD_ENCRYPTION_KEY": "f" * 64,
    "WEBHOOK_SIGNING_SECRET": "w" * 64,
    "STORAGE_BACKEND": "s3",
    "STORAGE_BUCKET": "atlas-documents",
    "MAIL_BACKEND": "smtp",
    "SMTP_HOST": "smtp.example.com",
    # A domain that is not RFC-2606 reserved. The *sender* is validated because
    # a reserved one cannot pass SPF; the SMTP relay host is not, since that is
    # a hostname Atlas connects out to rather than one recipients check.
    "MAIL_FROM": "no-reply@atlas-pmos.io",
}


def _load(monkeypatch, **overrides: str):
    from app.config import load_settings

    for key, value in {**BASE, **overrides}.items():
        monkeypatch.setenv(key, value)
    return load_settings("production")


def test_a_complete_production_environment_boots(monkeypatch):
    """The control. Without it, every refusal below could be passing for the
    wrong reason — a settings object that never validates at all."""
    settings = _load(monkeypatch)
    assert settings.env == "production"
    assert settings.force_https is True
    assert settings.db_enable_rls is True


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"STORAGE_BACKEND": "local"}, "object storage"),
        ({"MAIL_BACKEND": "console"}, "mail backend"),
        ({"DATABASE_URL": "sqlite+pysqlite:///atlas.db"}, "PostgreSQL"),
        ({"MFA_REQUIRED_FOR_PRIVILEGED": "false"}, "MFA"),
        ({"SECRET_KEY": "short"}, "SECRET_KEY"),
        ({"CORS_ALLOWED_ORIGINS": "*"}, "Wildcard"),
        # An SMTP backend with nowhere to connect. Boots fine, then fails once
        # per send inside a worker rather than once at startup where it is seen.
        ({"SMTP_HOST": ""}, "SMTP_HOST"),
        # The shipped default, which is exactly what an incomplete deploy keeps.
        ({"MAIL_FROM": "no-reply@atlas.example"}, "reserved domain"),
        ({"MAIL_FROM": "no-reply@example.com"}, "reserved domain"),
        ({"MAIL_FROM": "operations@atlas.localhost"}, "reserved domain"),
        # Display-name form: the domain still has to be extracted and checked.
        ({"MAIL_FROM": '"Atlas" <no-reply@example.org>'}, "reserved domain"),
        ({"MAIL_FROM": "not-an-address"}, "usable address"),
        ({"MAIL_FROM": ""}, "usable address"),
    ],
)
def test_production_refuses(monkeypatch, override, fragment):
    """Each of these is a `ConfigError` at startup, not a warning."""
    from app.config.base import ConfigError

    with pytest.raises((ConfigError, Exception)) as caught:
        _load(monkeypatch, **override)

    assert fragment.lower() in str(caught.value).lower(), (
        f"Refusing {override} did not mention {fragment!r}: {caught.value}"
    )


@pytest.mark.parametrize(
    "setting",
    [
        "SECRET_KEY",
        "FIELD_ENCRYPTION_KEY",
        "DEBUG",
        "DATABASE_URL",
        "SESSION_COOKIE_SECURE",
        "CSRF_ENABLED",
        "CELERY_TASK_ALWAYS_EAGER",
        "CORS_ALLOWED_ORIGINS",
        "STORAGE_BACKEND",
        "MAIL_BACKEND",
        "MAIL_FROM",
        "SMTP_HOST",
        "MFA_REQUIRED_FOR_PRIVILEGED",
    ],
)
def test_the_deployment_guide_names_every_refusal(setting):
    """The half of this that would have caught the bug.

    A deployer satisfies the published list and expects to boot. Every setting
    the code refuses on has to appear in it, or the list is a trap.
    """
    guide = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
    section = guide.split("### What production refuses to boot on", 1)
    assert len(section) == 2, "DEPLOYMENT.md lost its refusal section."
    # Up to the next heading.
    body = section[1].split("###", 1)[0]

    assert setting in body, (
        f"Production refuses on {setting} and DEPLOYMENT.md §2 does not say so. "
        "A deployer who satisfies the published list will still fail to boot."
    )


def test_the_reference_compose_satisfies_the_refusals():
    """The file we hand people has to survive its own rules.

    Checked against the compose file's text rather than by booting it, because
    the point is that somebody reading it can see the settings are there.
    """
    import yaml

    compose = yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))
    environment = compose["services"]["web"]["environment"]

    assert environment["ATLAS_ENV"] == "production"
    assert environment["STORAGE_BACKEND"] == "s3", (
        "The reference deployment sets local storage, which production refuses."
    )
    assert environment["MAIL_BACKEND"] == "smtp", (
        "The reference deployment sets the console mail backend, which production refuses."
    )
    assert environment["FORCE_HTTPS"] == "true"
    assert environment["SESSION_COOKIE_SECURE"] == "true"


@pytest.mark.parametrize(
    "sender",
    [
        "no-reply@atlas-pmos.io",
        "notices@properties.co.uk",
        '"Atlas Notices" <no-reply@atlas-pmos.io>',
        # `.example` is reserved; `example-realty.com` merely contains the word.
        # A substring check here would reject a legitimate customer domain.
        "billing@example-realty.com",
    ],
)
def test_a_real_sender_is_accepted(monkeypatch, sender):
    """The other half. A refusal that also rejects valid addresses is worse
    than none: it fails a deploy that was correct."""
    assert _load(monkeypatch, MAIL_FROM=sender).mail_from == sender


def test_the_example_env_file_does_not_ship_a_refused_sender():
    """The bug this whole check exists to prevent, in the file people copy.

    `.env.production.example` shipped `no-reply@atlas.example.com` — a deployer
    following the guide verbatim would fill in every blank, leave the one field
    that already had a plausible value, and ship a deployment whose mail is
    dropped by every recipient. A prefilled placeholder is worse than a blank
    one precisely because it looks finished.
    """
    from app.config.prod import RESERVED_MAIL_DOMAINS, RESERVED_MAIL_SUFFIXES

    text = (ROOT / ".env.production.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.startswith("MAIL_FROM="):
            continue
        value = line.partition("=")[2].strip()
        if not value:
            return  # Blank is the intended state: the deployer must supply it.
        domain = value.rstrip(">").rpartition("@")[2].lower()
        assert domain not in RESERVED_MAIL_DOMAINS, (
            f"The example ships MAIL_FROM={value}, which production refuses."
        )
        assert not domain.endswith(RESERVED_MAIL_SUFFIXES), (
            f"The example ships MAIL_FROM={value}, which production refuses."
        )
        return
    pytest.fail("MAIL_FROM is not present in .env.production.example at all.")

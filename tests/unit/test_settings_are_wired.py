"""Every setting has to do something.

A setting that is declared, typed, validated, and read by nothing is worse than
a missing one. It looks like a control. An operator tightening
`API_TOKEN_TTL_DAYS` for a compliance requirement, or setting `SENTRY_DSN` to
get error reporting, has no way to discover that the value goes nowhere - the
application boots, accepts it, and behaves exactly as before.

Three of these were found together: `SMS_BACKEND`, `PAYMENTS_BACKEND`, and
`SCREENING_BACKEND` named providers that do not exist, and two of them were
advertised in `.env.example`. `API_TOKEN_TTL_DAYS` was worse - the issuance
function carried a hardcoded 90 that happened to equal the setting's default,
so the knob appeared to work and never did.

The guard mirrors `tests/unit/test_service_reachability.py`: a list of known
exceptions, each with a reason, that fails the build when it *grows*. The point
is not that the list is empty today. It is that nothing joins it silently.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASE = ROOT / "app" / "config" / "base.py"

#: Declared but not consumed, with the reason each is tolerated. Anything that
#: is genuinely vestigial belongs deleted rather than listed here - this is for
#: settings that name a real intention the code has not caught up with.
UNWIRED: dict[str, str] = {
    "webhook_tolerance_seconds": (
        "verify_signature() takes tolerance_seconds=300 as a parameter default "
        "rather than reading this. Replay protection works; the knob does not."
    ),
    "ratelimit_api": "Declared limit; no route applies it. RATELIMIT_AUTH is wired, these are not.",
    "ratelimit_expensive": "Declared limit; no route applies it.",
    "log_sql_queries": "Debug toggle with no reader; SQL echo is driven by db_echo.",
    "otel_exporter_endpoint": "No OTLP exporter is configured anywhere.",
    "sentry_dsn": "No Sentry client is initialised.",
}


def _declared_settings() -> list[str]:
    text = BASE.read_text(encoding="utf-8")
    return [m.group(1) for m in re.finditer(r"^    ([a-z_][a-z0-9_]*)\s*:\s*[^=\n]+=", text, re.M)]


def _application_corpus() -> str:
    """All application source, with the declarations themselves removed.

    Without stripping them every setting trivially matches its own declaration
    line, and the check answers nothing.
    """
    chunks = []
    for path in sorted(ROOT.joinpath("app").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if path == BASE:
            text = "\n".join(
                line
                for line in text.splitlines()
                if not re.match(r"^    [a-z_][a-z0-9_]*\s*:\s*[^=\n]+=", line)
            )
        chunks.append(text)
    return "\n".join(chunks)


def test_every_setting_is_read_somewhere():
    corpus = _application_corpus()
    unread = {
        name
        for name in _declared_settings()
        if not re.search(rf"\b{re.escape(name)}\b", corpus)
    }
    unexpected = sorted(unread - set(UNWIRED))

    assert not unexpected, (
        "These settings are declared and read by nothing:\n  "
        + "\n  ".join(unexpected)
        + "\n\nA setting nobody reads is a control that appears to work. Wire it, "
        "delete it, or add it to UNWIRED with the reason it is tolerated."
    )


def test_the_unwired_list_has_not_gone_stale():
    """An entry that is now wired should leave the list.

    Otherwise the exceptions accumulate and the guard slowly stops meaning
    anything - the same failure mode the reachability guard is written against.
    """
    corpus = _application_corpus()
    declared = set(_declared_settings())
    for name, reason in UNWIRED.items():
        if name not in declared:
            pytest.fail(f"UNWIRED lists {name!r}, which is no longer a setting. Remove the entry.")
        assert not re.search(rf"\b{re.escape(name)}\b", corpus), (
            f"UNWIRED says {name!r} is unread ({reason}), but the application now "
            "reads it. Remove it from the list."
        )


def test_the_api_token_ttl_setting_actually_governs_issuance():
    """The specific bug that motivated this file.

    `issue_api_token` carried `ttl_days: int = 90`, matching the setting's
    default exactly, so shortening the window in configuration did nothing and
    looked like it had.
    """
    source = (ROOT / "app" / "services" / "iam" / "token_service.py").read_text(encoding="utf-8")
    assert "api_token_ttl_days" in source, (
        "token_service no longer reads API_TOKEN_TTL_DAYS; token lifetime is "
        "hardcoded again and the setting is decorative."
    )
    assert "ttl_days: int = 90" not in source, (
        "issue_api_token has a hardcoded 90-day default again, which silently "
        "overrides the configured value for every caller that omits it."
    )

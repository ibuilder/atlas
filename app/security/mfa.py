"""Multi-factor authentication: TOTP enrolment, verification, recovery codes.

TOTP (RFC 6238) with a configurable drift window. Two details that are easy to
omit and expensive to omit:

* **Replay protection.** A valid code stays valid for its whole 30-second step.
  Without recording the last accepted counter, a code shoulder-surfed or
  captured in transit can be reused within that window. Atlas stores the last
  accepted step per user and refuses anything at or below it.
* **Recovery codes are credentials.** They are shown exactly once, stored only as
  SHA-256 digests, and consumed atomically on use.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from urllib.parse import quote

import pyotp

from app.security.crypto import compare_digest, hash_token

__all__ = [
    "MfaEnrollment",
    "generate_recovery_codes",
    "generate_totp_secret",
    "hash_recovery_code",
    "provisioning_uri",
    "verify_totp",
]

TOTP_INTERVAL = 30
TOTP_DIGITS = 6
RECOVERY_CODE_BYTES = 5  # 10 hex characters, grouped as XXXXX-XXXXX


@dataclass(frozen=True)
class MfaEnrollment:
    """Everything the enrolment screen needs. The plaintext values here are
    shown once and never persisted in this form."""

    secret: str
    provisioning_uri: str
    recovery_codes: list[str]


def generate_totp_secret() -> str:
    """A fresh base32 TOTP seed (160 bits)."""
    return pyotp.random_base32(length=32)


def provisioning_uri(secret: str, account_name: str, issuer: str = "Atlas PMOS") -> str:
    """otpauth:// URI for an authenticator app QR code."""
    totp = pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_INTERVAL)
    return totp.provisioning_uri(name=quote(account_name, safe="@"), issuer_name=issuer)


def current_counter(at: float | None = None) -> int:
    """The TOTP step number for a moment in time."""
    return int((at if at is not None else time.time()) // TOTP_INTERVAL)


def verify_totp(
    secret: str | None,
    code: str,
    *,
    window: int = 1,
    last_counter: int | None = None,
    at: float | None = None,
) -> tuple[bool, int | None]:
    """Verify a TOTP code.

    Returns ``(accepted, counter)``. The counter must be persisted by the caller
    so the same code cannot be presented twice - checking the code alone leaves
    a 30-second replay window wide open.

    ``window`` allows for clock drift on the user's device, in 30-second steps
    either side of now.
    """
    if not secret or not code:
        return False, None

    cleaned = code.strip().replace(" ", "").replace("-", "")
    if not cleaned.isdigit() or len(cleaned) != TOTP_DIGITS:
        return False, None

    totp = pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_INTERVAL)
    now = at if at is not None else time.time()
    centre = current_counter(now)

    for offset in range(-window, window + 1):
        counter = centre + offset
        candidate = totp.at(counter * TOTP_INTERVAL)
        if not compare_digest(candidate, cleaned):
            continue
        if last_counter is not None and counter <= last_counter:
            # Correct code, already used. Treated as a failure on purpose.
            return False, None
        return True, counter

    return False, None


def generate_recovery_codes(count: int = 10) -> list[str]:
    """Human-transcribable one-time codes.

    Hex rather than base32: recovery codes get written on paper and read back
    over the phone, and hex has no ambiguous characters to mishear.
    """
    codes: list[str] = []
    for _ in range(count):
        raw = secrets.token_hex(RECOVERY_CODE_BYTES).upper()
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes


def normalize_recovery_code(code: str) -> str:
    return code.strip().upper().replace(" ", "").replace("-", "")


def hash_recovery_code(code: str) -> str:
    """Storage form. Recovery codes are high-entropy, so a single SHA-256 is
    the right primitive - there is nothing to brute-force."""
    return hash_token(normalize_recovery_code(code))

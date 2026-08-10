"""Passwords, MFA, money, and identifiers.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from decimal import Decimal

import pytest

from app.models.types import quantize_money, utcnow, uuid7
from app.security import mfa
from app.security.crypto import DecryptionError, FieldCipher, generate_encryption_key, hash_token
from app.security.passwords import (
    hash_password,
    needs_rehash,
    validate_password,
    verify_password,
)

pytestmark = pytest.mark.unit

FAST = {"time_cost": 1, "memory_cost_kib": 8192, "parallelism": 1}


# ------------------------------------------------------------------ passwords


def test_password_round_trip():
    stored = hash_password("a-perfectly-fine-passphrase", **FAST)
    assert stored.startswith("$argon2id$")
    assert verify_password(stored, "a-perfectly-fine-passphrase")
    assert not verify_password(stored, "a-perfectly-fine-passphras")


def test_verify_is_safe_against_missing_hashes():
    assert not verify_password(None, "anything")
    assert not verify_password("", "anything")
    assert not verify_password("not-a-hash", "anything")


def test_hashes_are_salted():
    """Two identical passwords must not produce identical hashes."""
    first = hash_password("same-password-twice-over", **FAST)
    second = hash_password("same-password-twice-over", **FAST)
    assert first != second


def test_unicode_normalisation():
    """The same characters typed on different keyboards must match."""
    composed = "café-passphrase-2026"
    decomposed = "café-passphrase-2026"
    stored = hash_password(composed, **FAST)
    assert verify_password(stored, decomposed)


def test_rehash_detects_weaker_parameters():
    weak = hash_password(
        "some-long-enough-passphrase", time_cost=1, memory_cost_kib=8192, parallelism=1
    )
    assert needs_rehash(weak, time_cost=4, memory_cost_kib=65536, parallelism=2)
    assert not needs_rehash(weak, **FAST)


@pytest.mark.parametrize(
    "candidate",
    ["short", "password", "Password1!", "aaaaaaaaaaaaaaaa", "abcdefghijklmn", "changeme"],
)
def test_weak_passwords_are_rejected(candidate):
    assert not validate_password(candidate).acceptable


@pytest.mark.parametrize(
    "candidate",
    ["correct-horse-battery-staple", "Tr0ubador&3-quinoa-lantern", "brass-lantern-oxide-drift-91"],
)
def test_strong_passwords_are_accepted(candidate):
    result = validate_password(candidate)
    assert result.acceptable, result.reasons


def test_password_cannot_contain_personal_terms():
    result = validate_password(
        "rowan-ellis-long-enough-passphrase", user_terms=["rowan", "ellis@example.com"]
    )
    assert not result.acceptable
    assert any("name" in reason for reason in result.reasons)


# ------------------------------------------------------------------------ MFA


def test_totp_accepts_the_current_code():
    secret = mfa.generate_totp_secret()
    import pyotp

    code = pyotp.TOTP(secret).now()
    accepted, counter = mfa.verify_totp(secret, code)
    assert accepted
    assert counter == mfa.current_counter()


def test_totp_rejects_replay_within_the_same_step():
    """A correct code used twice is a replay, and must fail the second time."""
    secret = mfa.generate_totp_secret()
    import pyotp

    code = pyotp.TOTP(secret).now()
    accepted, counter = mfa.verify_totp(secret, code)
    assert accepted

    replayed, _ = mfa.verify_totp(secret, code, last_counter=counter)
    assert not replayed


def test_totp_rejects_malformed_input():
    secret = mfa.generate_totp_secret()
    for bad in ("", "abc", "12345", "1234567", "abcdef"):
        accepted, _ = mfa.verify_totp(secret, bad)
        assert not accepted


def test_totp_drift_window():
    secret = mfa.generate_totp_secret()
    import pyotp

    totp = pyotp.TOTP(secret)
    previous_step = (mfa.current_counter() - 1) * mfa.TOTP_INTERVAL
    code = totp.at(previous_step)

    assert mfa.verify_totp(secret, code, window=1)[0]
    assert not mfa.verify_totp(secret, code, window=0)[0]


def test_recovery_codes_are_unique_and_hash_stably():
    codes = mfa.generate_recovery_codes(10)
    assert len(set(codes)) == 10
    # Formatting must not affect matching - people retype these from paper.
    assert mfa.hash_recovery_code(codes[0]) == mfa.hash_recovery_code(
        codes[0].replace("-", "").lower()
    )


# ------------------------------------------------------------------- crypto


def test_field_cipher_round_trip():
    cipher = FieldCipher(generate_encryption_key())
    assert cipher.decrypt(cipher.encrypt("123-45-6789")) == "123-45-6789"


def test_ciphertext_differs_each_time():
    cipher = FieldCipher(generate_encryption_key())
    assert cipher.encrypt("same") != cipher.encrypt("same")


def test_wrong_key_cannot_decrypt():
    ciphertext = FieldCipher(generate_encryption_key()).encrypt("secret")
    with pytest.raises(DecryptionError):
        FieldCipher(generate_encryption_key()).decrypt(ciphertext)


def test_key_rotation_keeps_old_ciphertext_readable():
    old = generate_encryption_key()
    new = generate_encryption_key()
    ciphertext = FieldCipher(old).encrypt("rotate me")

    rotated = FieldCipher(f"{new.get_secret_value()},{old.get_secret_value()}")
    assert rotated.decrypt(ciphertext) == "rotate me"
    assert rotated.decrypt(rotated.rotate(ciphertext)) == "rotate me"


def test_token_hashing_is_stable():
    assert hash_token("atlas_api_abc") == hash_token("atlas_api_abc")
    assert hash_token("atlas_api_abc") != hash_token("atlas_api_abd")


# -------------------------------------------------------------------- types


def test_uuid7_is_valid_and_time_ordered():
    first = uuid7()
    time.sleep(0.005)
    second = uuid7()

    assert first.version == 7
    assert uuid.UUID(str(first)) == first
    # Time ordering is the point: it keeps index inserts at the right edge.
    assert str(first) < str(second)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.1", Decimal("0.1000")),
        (0.1, Decimal("0.1000")),
        ("2.00005", Decimal("2.0001")),  # ROUND_HALF_UP, not banker's rounding
        ("-1.23456", Decimal("-1.2346")),
        (1500, Decimal("1500.0000")),
    ],
)
def test_money_quantisation(value, expected):
    assert quantize_money(value) == expected


def test_utcnow_is_timezone_aware():
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == dt.timedelta(0)

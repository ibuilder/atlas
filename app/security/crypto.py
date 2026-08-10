"""Cryptographic primitives: field encryption, token minting, digests.

Two distinct jobs live here and must not be confused:

* **Passwords** are low-entropy human secrets. They go through Argon2id in
  :mod:`app.security.passwords` - never through anything in this module.
* **Tokens** are high-entropy values we generated ourselves. A single SHA-256
  is the correct storage form: there is nothing to brute-force, and we need
  constant-time lookup by digest.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Final

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from pydantic import SecretStr

__all__ = [
    "DecryptionError",
    "FieldCipher",
    "compare_digest",
    "generate_encryption_key",
    "hash_token",
    "hmac_sha256",
    "new_token",
    "sha256_hex",
    "token_fingerprint",
]

TOKEN_ENTROPY_BYTES: Final = 32


class DecryptionError(RuntimeError):
    """Raised when ciphertext cannot be decrypted with any configured key."""


def generate_encryption_key() -> SecretStr:
    """Mint a new Fernet key (44 urlsafe-base64 characters)."""
    return SecretStr(Fernet.generate_key().decode())


class FieldCipher:
    """Envelope encryption for sensitive columns, with key rotation support.

    ``key_material`` is a comma-separated list of Fernet keys. The first key
    encrypts; every key can decrypt. That ordering is what makes rotation a
    deploy rather than a migration: prepend the new key, let it encrypt new
    writes, and drop the old key once a re-encryption job has drained.
    """

    __slots__ = ("_fernet", "_key_count")

    def __init__(self, key_material: str | SecretStr) -> None:
        raw = (
            key_material.get_secret_value() if isinstance(key_material, SecretStr) else key_material
        )
        keys = [part.strip() for part in raw.split(",") if part.strip()]
        if not keys:
            raise ValueError("FieldCipher requires at least one encryption key.")
        try:
            fernets = [Fernet(key.encode() if isinstance(key, str) else key) for key in keys]
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid field encryption key material: {exc}") from exc
        self._fernet = MultiFernet(fernets)
        self._key_count = len(fernets)

    @property
    def key_count(self) -> int:
        return self._key_count

    def encrypt(self, plaintext: str) -> str:
        if not isinstance(plaintext, str):
            raise TypeError("FieldCipher.encrypt expects str")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
            # Deliberately opaque: the caller gets "this did not decrypt", not
            # a hint about which key or why.
            raise DecryptionError("Unable to decrypt value with configured keys.") from exc

    def rotate(self, ciphertext: str) -> str:
        """Re-encrypt existing ciphertext under the primary key."""
        try:
            return self._fernet.rotate(ciphertext.encode("ascii")).decode("ascii")
        except (InvalidToken, ValueError) as exc:
            raise DecryptionError("Unable to rotate value with configured keys.") from exc


def new_token(prefix: str = "", entropy_bytes: int = TOKEN_ENTROPY_BYTES) -> str:
    """Generate an opaque, URL-safe token.

    The optional prefix makes leaked credentials greppable in logs and enables
    secret-scanning rules (``atlas_api_...``, ``atlas_pwreset_...``).
    """
    if entropy_bytes < 16:
        raise ValueError("Tokens require at least 16 bytes of entropy.")
    body = secrets.token_urlsafe(entropy_bytes)
    return f"{prefix}_{body}" if prefix else body


def sha256_hex(value: str | bytes) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def hash_token(token: str) -> str:
    """Storage form for a high-entropy token."""
    return sha256_hex(token)


def token_fingerprint(token: str, length: int = 8) -> str:
    """Short, non-reversible identifier safe to show in a UI or log line."""
    return sha256_hex(token)[:length]


def hmac_sha256(secret: str | bytes, payload: str | bytes) -> str:
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def compare_digest(left: str, right: str) -> bool:
    """Constant-time string comparison for secrets and signatures."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)

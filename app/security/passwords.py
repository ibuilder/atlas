"""Password hashing and policy.

Argon2id, because it is memory-hard: a GPU or ASIC attacker gains far less
against it than against PBKDF2 or bcrypt. Parameters come from configuration so
they can be raised as hardware improves, and :func:`needs_rehash` transparently
upgrades a stored hash the next time its owner signs in successfully.

The policy is length-first with a blocklist, following NIST SP 800-63B rather
than the older "one uppercase, one symbol" school - composition rules push people
toward ``Password1!``, which is both harder to remember and easier to guess.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

__all__ = [
    "PasswordPolicyError",
    "PasswordStrength",
    "hash_password",
    "needs_rehash",
    "validate_password",
    "verify_password",
]

#: Passwords seen in every credential dump, plus product-specific guesses. A
#: real deployment should point at a full breach corpus; this is the floor.
COMMON_PASSWORDS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "password1",
        "password123",
        "123456",
        "12345678",
        "123456789",
        "qwerty",
        "qwerty123",
        "abc123",
        "letmein",
        "welcome",
        "welcome1",
        "monkey",
        "dragon",
        "iloveyou",
        "admin",
        "admin123",
        "administrator",
        "changeme",
        "trustno1",
        "sunshine",
        "princess",
        "football",
        "baseball",
        "master",
        "shadow",
        "superman",
        "passw0rd",
        "p@ssword",
        "p@ssw0rd",
        "atlas",
        "atlas123",
        "property",
        "landlord",
        "tenant",
        "maintenance",
    }
)

_REPEAT_RE = re.compile(r"(.)\1{3,}")
_SEQUENTIAL = ("abcdefghijklmnopqrstuvwxyz", "0123456789", "qwertyuiop", "asdfghjkl")


class PasswordPolicyError(ValueError):
    """Raised when a candidate password fails policy. Carries every reason."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass(frozen=True)
class PasswordStrength:
    acceptable: bool
    score: int  # 0-4
    reasons: list[str] = field(default_factory=list)


def _hasher(
    time_cost: int = 3, memory_cost_kib: int = 65_536, parallelism: int = 2
) -> PasswordHasher:
    return PasswordHasher(
        time_cost=time_cost,
        memory_cost=memory_cost_kib,
        parallelism=parallelism,
        hash_len=32,
        salt_len=16,
        type=Type.ID,
    )


def _normalize(password: str) -> str:
    """NFKC-normalise so visually identical passwords hash identically.

    Without this, a password typed on a phone keyboard can fail to match the
    same password typed on a desktop, and the user has no way to tell why.
    """
    return unicodedata.normalize("NFKC", password)


def hash_password(
    password: str,
    *,
    time_cost: int = 3,
    memory_cost_kib: int = 65_536,
    parallelism: int = 2,
) -> str:
    if not password:
        raise ValueError("Cannot hash an empty password.")
    return _hasher(time_cost, memory_cost_kib, parallelism).hash(_normalize(password))


def verify_password(stored_hash: str | None, password: str) -> bool:
    """Verify a password against a stored hash.

    Returns ``False`` rather than raising for every failure mode, including a
    missing or corrupt hash. Callers must still perform a dummy verification for
    unknown accounts - see :func:`dummy_verify` - or the response time itself
    reveals whether an account exists.
    """
    if not stored_hash or not password:
        return False
    try:
        return _hasher().verify(stored_hash, _normalize(password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


#: A real Argon2 hash of a fixed string, used to burn equivalent CPU when the
#: account does not exist. Computed lazily and cached.
_DUMMY_HASH: str | None = None


def dummy_verify(
    password: str, *, time_cost: int = 3, memory_cost_kib: int = 65_536, parallelism: int = 2
) -> bool:
    """Spend the same work as a real verification, and always fail.

    Called on the unknown-account path so login latency does not leak account
    existence - the timing side channel that makes user enumeration trivial even
    when the error message is identical.
    """
    global _DUMMY_HASH
    hasher = _hasher(time_cost, memory_cost_kib, parallelism)
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hasher.hash("atlas-timing-equalizer")
    try:
        hasher.verify(_DUMMY_HASH, _normalize(password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        pass
    return False


def needs_rehash(
    stored_hash: str,
    *,
    time_cost: int = 3,
    memory_cost_kib: int = 65_536,
    parallelism: int = 2,
) -> bool:
    """Whether this hash was made with weaker parameters than we now require."""
    try:
        return _hasher(time_cost, memory_cost_kib, parallelism).check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


def validate_password(
    password: str,
    *,
    min_length: int = 12,
    max_length: int = 1024,
    user_terms: list[str] | None = None,
) -> PasswordStrength:
    """Evaluate a candidate password against policy."""
    reasons: list[str] = []
    normalized = _normalize(password)
    lowered = normalized.lower()

    if len(normalized) < min_length:
        reasons.append(f"Must be at least {min_length} characters.")
    # An upper bound exists only to stop a megabyte password from turning a
    # login into a denial-of-service against our own KDF.
    if len(normalized) > max_length:
        reasons.append(f"Must be at most {max_length} characters.")

    if lowered in COMMON_PASSWORDS:
        reasons.append("This password appears in well-known breach lists.")

    stripped = re.sub(r"[^a-z0-9]", "", lowered)
    if stripped and stripped in COMMON_PASSWORDS:
        reasons.append("This is a common password with trivial substitutions.")

    if _REPEAT_RE.search(lowered):
        reasons.append("Avoid repeating the same character four or more times.")

    for sequence in _SEQUENTIAL:
        for size in (6, 5, 4):
            if any(sequence[i : i + size] in lowered for i in range(len(sequence) - size + 1)):
                reasons.append("Avoid long keyboard or alphabetical sequences.")
                break
        else:
            continue
        break

    # Personal information is the first thing an attacker who knows the target
    # will try, and the first thing a user reaches for.
    for term in user_terms or []:
        candidate = (term or "").strip().lower()
        if len(candidate) >= 4 and candidate in lowered:
            reasons.append("Must not contain your name, email, or organization.")
            break

    score = _score(normalized)
    if not reasons and score < 2:
        reasons.append("Password is too predictable; add length or unrelated words.")

    return PasswordStrength(acceptable=not reasons, score=score, reasons=reasons)


def _score(password: str) -> int:
    """Coarse 0-4 strength estimate driven mainly by length and variety."""
    if not password:
        return 0
    length = len(password)
    variety = sum(
        bool(re.search(pattern, password))
        for pattern in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
    )
    score = 0
    if length >= 12:
        score += 1
    if length >= 16:
        score += 1
    if length >= 20:
        score += 1
    if variety >= 3:
        score += 1
    if len(set(password)) < max(4, length // 4):
        score -= 1
    return max(0, min(4, score))


def password_fingerprint(password: str) -> str:
    """Non-reversible digest for reuse detection within password history.

    Never a credential store: history comparison uses the real Argon2 hashes.
    This exists only for cheap "is this the same as one of the last five"
    pre-checks where a full verify per historical entry would be wasteful.
    """
    return hashlib.sha256(_normalize(password).encode("utf-8")).hexdigest()

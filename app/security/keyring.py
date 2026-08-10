"""Resolution of the active field-encryption cipher.

Column types need a cipher during bind/result processing, which can happen
inside a Celery task or an Alembic run where there is no Flask application
context. This module resolves the cipher from the app context when one exists
and falls back to a process-wide default installed at startup.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import threading

from app.security.crypto import FieldCipher

__all__ = ["get_field_cipher", "reset_field_cipher", "set_field_cipher"]

_lock = threading.Lock()
_default_cipher: FieldCipher | None = None


def set_field_cipher(cipher: FieldCipher) -> None:
    global _default_cipher
    with _lock:
        _default_cipher = cipher


def reset_field_cipher() -> None:
    global _default_cipher
    with _lock:
        _default_cipher = None


def get_field_cipher() -> FieldCipher:
    """Return the cipher for the current app, or the process default."""
    try:
        from flask import current_app, has_app_context

        if has_app_context():
            cipher = current_app.extensions.get("atlas_field_cipher")
            if isinstance(cipher, FieldCipher):
                return cipher
    except ImportError:  # pragma: no cover - Flask is a hard dependency
        pass

    if _default_cipher is None:
        raise RuntimeError(
            "Field encryption is not configured. "
            "Set FIELD_ENCRYPTION_KEY and initialise the application."
        )
    return _default_cipher

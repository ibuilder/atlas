"""WSGI entry point.

Used by Gunicorn (``gunicorn wsgi:app``) and by the Flask CLI
(``flask --app wsgi ...``).

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from app import create_app

app = create_app()

if __name__ == "__main__":  # pragma: no cover - local convenience only
    app.run(host="127.0.0.1", port=5000)

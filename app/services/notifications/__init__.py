"""Notification services.

SPDX-License-Identifier: MIT
"""

from app.services.notifications.mailer import OutboundEmail, get_mailer, send_password_reset

__all__ = ["OutboundEmail", "get_mailer", "send_password_reset"]

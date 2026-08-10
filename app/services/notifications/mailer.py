"""Outbound email behind an adapter.

Every integration sits behind an interface so the domain never imports a vendor
SDK, and so tests assert on what *would* be sent rather than mocking a transport.
The memory backend is what the test suite inspects; the console backend is what
a developer reads in their terminal.

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from flask import current_app

from app.logging import get_logger

__all__ = ["Mailer", "OutboundEmail", "get_mailer", "send_password_reset"]

log = get_logger("services.notifications.mailer")


@dataclass
class OutboundEmail:
    to: str
    subject: str
    body: str
    template: str | None = None
    context: dict = field(default_factory=dict)


class Mailer(Protocol):
    def send(self, message: OutboundEmail) -> None: ...


class ConsoleMailer:
    """Writes to the log. The development default."""

    def send(self, message: OutboundEmail) -> None:
        log.info(
            "email dispatched",
            extra={
                "event": "email.sent",
                "backend": "console",
                # The address goes through the log redaction filter, which masks
                # it rather than dropping it - enough to correlate, not enough
                # to harvest.
                "recipient": message.to,
                "subject": message.subject,
            },
        )


class MemoryMailer:
    """Captures messages for assertions. Test backend."""

    def __init__(self) -> None:
        self.outbox: list[OutboundEmail] = []

    def send(self, message: OutboundEmail) -> None:
        self.outbox.append(message)


class SmtpMailer:
    """Real SMTP delivery."""

    def __init__(
        self, host: str, port: int, username: str, password: str, use_tls: bool, sender: str
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        self.sender = sender

    def send(self, message: OutboundEmail) -> None:
        import smtplib
        from email.message import EmailMessage

        payload = EmailMessage()
        payload["From"] = self.sender
        payload["To"] = message.to
        payload["Subject"] = message.subject
        payload.set_content(message.body)

        with smtplib.SMTP(self.host, self.port, timeout=15) as client:
            if self.use_tls:
                client.starttls()
            if self.username:
                client.login(self.username, self.password)
            client.send_message(payload)

        log.info(
            "email dispatched",
            extra={"event": "email.sent", "backend": "smtp", "recipient": message.to},
        )


def get_mailer() -> Mailer:
    """Resolve the configured backend, memoised per application."""
    mailer = current_app.extensions.get("atlas_mailer")
    if mailer is not None:
        return mailer

    settings = current_app.config["SETTINGS"]
    if settings.mail_backend == "memory":
        mailer = MemoryMailer()
    elif settings.mail_backend == "smtp":
        mailer = SmtpMailer(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password.get_secret_value(),
            use_tls=settings.smtp_use_tls,
            sender=settings.mail_from,
        )
    else:
        mailer = ConsoleMailer()

    current_app.extensions["atlas_mailer"] = mailer
    return mailer


def send_password_reset(email: str, token: str) -> None:
    """Send a reset link.

    The token appears only in the link. It is never logged, and the audit event
    for the request records that a reset was issued, not what it was.
    """
    settings = current_app.config["SETTINGS"]
    link = f"{settings.app_url.rstrip('/')}/auth/reset?token={token}"
    get_mailer().send(
        OutboundEmail(
            to=email,
            subject=f"Reset your {settings.app_name} password",
            body=(
                "We received a request to reset your password.\n\n"
                f"Use this link within {settings.password_reset_ttl_minutes} minutes:\n{link}\n\n"
                "If you did not request this, you can ignore this message - your "
                "password has not changed."
            ),
            template="auth/password_reset",
        )
    )

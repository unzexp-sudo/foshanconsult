"""SMTP email adapter — the prod backend (contract §8, ``EMAIL_BACKEND=smtp``).

Constructible with no arguments because ``app.deps.get_email_sender`` builds it that
way.  The socket is opened through an injectable ``transport_factory`` so the message
can be exercised in tests without a network.

The product's emails are Chinese, so the message is built with ``EmailMessage`` and an
explicit UTF-8 charset: a plain ``Subject:`` header would come out as mojibake.
"""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any

from app.config import settings

__all__ = ["SmtpEmailSender"]

TransportFactory = Callable[[], Any]


def _default_transport() -> Any:
    """A live ``SMTP_SSL`` connection to the configured host, port 465 by default."""
    return smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port)


class SmtpEmailSender:
    """Send mail over SMTP_SSL, as ``email_from``."""

    def __init__(self, transport_factory: TransportFactory | None = None) -> None:
        self._transport_factory: TransportFactory = transport_factory or _default_transport

    def send(self, *, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["To"] = to
        message["From"] = settings.email_from
        message["Subject"] = subject  # RFC 2047-encoded on serialization
        message.set_content(body, charset="utf-8")

        with self._transport_factory() as smtp:
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(message)

"""Outgoing account email.

No email provider is integrated yet. The rest of the application only depends
on the ``EmailSender`` interface, so adding one later means adding one class
here and returning it from ``get_email_sender``.

The links carry a raw single-use token. The only place such a link may be
written is the development sender's log line; nothing else logs it and no API
response contains it.
"""

import logging
from typing import Protocol
from urllib.parse import urlencode

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmailSender(Protocol):
    def send_email_verification(self, *, to: str, url: str) -> None: ...

    def send_password_reset(self, *, to: str, url: str) -> None: ...


class ConsoleEmailSender:
    """Development only: writes the link to the log instead of sending it.

    Logged at WARNING so that it is visible with the default logging setup.
    """

    def send_email_verification(self, *, to: str, url: str) -> None:
        logger.warning("[development] Email verification link for %s: %s", to, url)

    def send_password_reset(self, *, to: str, url: str) -> None:
        logger.warning("[development] Password reset link for %s: %s", to, url)


class DisabledEmailSender:
    """Used outside development until a provider is integrated.

    Sends nothing, and records that fact without the link or the address.
    """

    def send_email_verification(self, *, to: str, url: str) -> None:
        logger.warning(
            "No email provider is configured; a verification email was not sent."
        )

    def send_password_reset(self, *, to: str, url: str) -> None:
        logger.warning(
            "No email provider is configured; a password reset email was not sent."
        )


def get_email_sender() -> EmailSender:
    if settings.is_development:
        return ConsoleEmailSender()
    return DisabledEmailSender()


def email_verification_url(token: str) -> str:
    return f"{settings.frontend_url}/verify-email?{urlencode({'token': token})}"


def password_reset_url(token: str) -> str:
    return f"{settings.frontend_url}/reset-password?{urlencode({'token': token})}"

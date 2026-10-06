"""Request and response bodies of the authentication API.

Input is validated and normalized here, before it reaches any other code.
Passwords and tokens are ``SecretStr`` so that they are masked if a request
object is ever printed or logged.
"""

import re
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    StringConstraints,
)

from app.core.config import settings
from app.core.security import PASSWORD_MAX_LENGTH

# ASCII letters, digits and underscores. Keeping usernames to ASCII means that
# lowercasing gives one canonical form and that two different usernames cannot
# look identical.
_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_]{3,30}")


def _strip(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


def _normalize_username(value: str) -> str:
    if not _USERNAME_PATTERN.fullmatch(value):
        raise ValueError(
            "Username must be 3 to 30 characters long and contain only "
            "letters, digits and underscores."
        )
    return value.lower()


def _normalize_email(value: str) -> str:
    # Same reasoning as for usernames: the address is stored lowercased and
    # must be unique in that form, which is only well defined for ASCII.
    if not value.isascii():
        raise ValueError("Email address must contain only ASCII characters.")
    return value.lower()


def _check_password_length(value: SecretStr) -> SecretStr:
    if len(value.get_secret_value()) < settings.password_min_length:
        raise ValueError(
            f"Password must be at least {settings.password_min_length} "
            "characters long."
        )
    return value


Username = Annotated[str, BeforeValidator(_strip), AfterValidator(_normalize_username)]
Email = Annotated[
    EmailStr,
    BeforeValidator(_strip),
    AfterValidator(_normalize_email),
    Field(max_length=255),
]
DisplayName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=50),
]
# The policy for choosing a password: a minimum length and nothing else. The
# password is taken exactly as typed; it is not trimmed or otherwise altered.
NewPassword = Annotated[
    SecretStr,
    Field(max_length=PASSWORD_MAX_LENGTH),
    AfterValidator(_check_password_length),
]
Token = Annotated[SecretStr, Field(min_length=1, max_length=256)]


# --- requests ------------------------------------------------------------


class RegisterRequest(BaseModel):
    username: Username
    email: Email
    password: NewPassword
    display_name: DisplayName


class LoginRequest(BaseModel):
    # A username or an email address.
    identifier: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            to_lower=True,
            min_length=1,
            max_length=255,
        ),
    ]
    # Not held to the current password policy, which may be stricter than the
    # one in force when the account was created.
    password: Annotated[SecretStr, Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)]


class VerifyEmailRequest(BaseModel):
    token: Token


class ResendVerificationRequest(BaseModel):
    email: Email


class ForgotPasswordRequest(BaseModel):
    email: Email


class ResetPasswordRequest(BaseModel):
    token: Token
    new_password: NewPassword


# --- responses -----------------------------------------------------------


class AccountResponse(BaseModel):
    """A user as shown to that user.

    It includes private fields such as the email address, so it must only be
    returned to the account's owner.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    email: str
    display_name: str
    bio: str | None
    avatar_url: str | None
    is_active: bool
    email_verified_at: datetime | None
    created_at: datetime


class SessionResponse(BaseModel):
    id: uuid.UUID
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    is_current: bool


class RevokedSessionsResponse(BaseModel):
    revoked_count: int


class MessageResponse(BaseModel):
    message: str

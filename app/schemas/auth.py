"""Request and response bodies of the authentication API.

Input is validated and normalized here, before it reaches any other code.
Passwords and tokens are ``SecretStr`` so that they are masked if a request
object is ever printed or logged.
"""

import re
import unicodedata
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


def reject_nul(value: str) -> str:
    # PostgreSQL cannot store this character in text, nor compare with it.
    if "\x00" in value:
        raise ValueError("Must not contain null characters.")
    return value


# Characters that take part in how neighbouring characters are joined. Some
# scripts cannot be written without them (Persian, several Indic scripts),
# and emoji are combined with them, so they are allowed in that role only:
# between two characters that are neither ASCII nor whitespace.
_JOINERS = frozenset("\u200c\u200d")
# Letters and symbols that are drawn as nothing at all, whatever their
# category says: the Hangul fillers and the blank Braille pattern.
_BLANK_CHARACTERS = frozenset("\u115f\u1160\u3164\uffa0\u2800")
# Control characters, format characters (zero-width characters, direction
# overrides and their like), line and paragraph separators, private-use
# characters and code points that are not assigned.
_UNSEEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cs", "Cn"})
# The first letter of the categories of what is drawn by itself: letters,
# numbers, punctuation and symbols. A mark (an accent, a variation selector)
# changes the character before it and a space separates two; neither is
# anything to look at when there is nothing else.
_VISIBLE_CATEGORIES = frozenset("LNPS")


def _joins(value: str, index: int) -> bool:
    """Is the joiner at ``index`` between two characters it can join?"""
    if index == 0 or index == len(value) - 1:
        return False
    return all(
        not neighbour.isascii()
        and not neighbour.isspace()
        and neighbour not in _JOINERS
        for neighbour in (value[index - 1], value[index + 1])
    )


def _check_display_name(value: str) -> str:
    """A display name can be seen, and holds nothing that cannot.

    It is shown next to a username wherever a user appears, so it must not
    be able to look like something it is not: empty, or longer or shorter
    than it is, or written in another order. Any script is welcome. Two
    things are refused: a character that leaves no mark of its own on the
    page, anywhere in the name; and a name with no letter, number,
    punctuation mark or symbol in it at all, which is one made of marks and
    spaces only and may show as nothing.
    """
    for index, character in enumerate(value):
        if character in _JOINERS:
            if _joins(value, index):
                continue
        elif (
            unicodedata.category(character) not in _UNSEEN_CATEGORIES
            and character not in _BLANK_CHARACTERS
        ):
            continue
        raise ValueError(
            "Display name must not contain control, invisible or "
            "text-direction characters."
        )
    if not any(
        unicodedata.category(character)[0] in _VISIBLE_CATEGORIES
        for character in value
    ):
        raise ValueError(
            "Display name must contain at least one letter, number, "
            "punctuation mark or symbol."
        )
    return value


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
# The one rule for display names, at registration and in the profile alike.
DisplayName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=50),
    AfterValidator(_check_display_name),
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
        AfterValidator(reject_nul),
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


class ChangePasswordRequest(BaseModel):
    # Like the password at login: checked against the account, not against
    # the policy for choosing one.
    current_password: Annotated[
        SecretStr, Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    ]
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

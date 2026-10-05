"""Request and response bodies of the user profile API.

There are two views of a user, and they are separate schemas on purpose. What
anyone may see is ``PublicProfileResponse``; what only the owner may see is
``MyProfileResponse``. Each lists its fields in full, so a column added to the
``User`` model is never exposed until someone adds it here deliberately.
"""

import uuid
from datetime import datetime
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StrictBool,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from app.schemas.auth import DisplayName, Username

_USERNAME = TypeAdapter(Username)


def canonical_username(value: str) -> str | None:
    """The stored form of a username, or None if no account can have it.

    Uses the same rules as registration, so a lookup and the stored value
    always agree on what the username is.
    """
    try:
        return _USERNAME.validate_python(value)
    except ValidationError:
        return None


def _reject_nul(value: str) -> str:
    # PostgreSQL cannot store this character in text.
    if "\x00" in value:
        raise ValueError("Must not contain null characters.")
    return value


def _empty_to_none(value: str | None) -> str | None:
    return value or None


# The same rule as at registration.
ProfileDisplayName = Annotated[DisplayName, AfterValidator(_reject_nul)]
# Plain text. A bio that is empty once trimmed is stored as no bio at all.
Bio = Annotated[
    Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=160),
        AfterValidator(_reject_nul),
    ]
    | None,
    AfterValidator(_empty_to_none),
]


# --- requests ------------------------------------------------------------


class UpdateProfileRequest(BaseModel):
    """A partial update of the caller's own profile.

    Only the fields that are sent are changed; a field that is left out keeps
    its value. Sending ``null`` for ``bio`` or ``avatar_url`` clears it.

    These four fields are the only ones a user can change here. Anything else
    in the body is ignored, like everywhere else in the API.
    """

    display_name: ProfileDisplayName | None = Field(
        default=None,
        description="1 to 50 characters. Cannot be null.",
    )
    bio: Bio = Field(
        default=None,
        description="Up to 160 characters of plain text. null or blank clears it.",
    )
    # Checked for being an absolute http(s) URL and nothing more. The server
    # never requests it.
    avatar_url: HttpUrl | None = Field(
        default=None,
        description="An absolute http(s) URL. null clears it.",
    )
    is_private: StrictBool | None = Field(
        default=None,
        description="true or false. Cannot be null.",
    )

    @field_validator("display_name", "is_private")
    @classmethod
    def _not_null(cls, value: object) -> object:
        # These columns are NOT NULL: they can be changed but not cleared.
        if value is None:
            raise ValueError("This field cannot be null.")
        return value

    @model_validator(mode="after")
    def _has_changes(self) -> Self:
        if not self.model_fields_set:
            raise ValueError(
                "At least one of display_name, bio, avatar_url or is_private "
                "must be provided."
            )
        return self

    def changes(self) -> dict[str, str | bool | None]:
        """The fields that were sent, and only those, as plain values."""
        return self.model_dump(exclude_unset=True, mode="json")


# --- responses -----------------------------------------------------------


class PublicProfileResponse(BaseModel):
    """A user as shown to anyone, signed in or not."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    display_name: str
    bio: str | None
    avatar_url: str | None
    is_private: bool
    followers_count: int
    following_count: int
    created_at: datetime


class MyProfileResponse(BaseModel):
    """A user as shown to that user.

    It includes private fields such as the email address, so it must only be
    returned to the account's owner.
    """

    id: uuid.UUID
    username: str
    email: str
    display_name: str
    bio: str | None
    avatar_url: str | None
    is_private: bool
    email_verified_at: datetime | None
    followers_count: int
    following_count: int
    created_at: datetime

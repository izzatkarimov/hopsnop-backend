"""The export of an account's data: the request for it, and the document.

The document is what Hopsnop holds that is the account's own, in a form a
person can read and a program can load. Every part of it is a schema here
that lists its fields in full. Nothing is taken over from a model or a row as
a whole, so a column added to the database is not in an export until someone
adds it here deliberately, and a column that must never leave the server
(the password hash, the hashes of tokens, the ids of sessions) has no field
to leave through.

What is in it and what is not, column by column, is tested in
``tests/test_data_export.py``.

The document has a version (``export_version``). A change that could break
whoever reads these documents is a new version, not a silent change to this
one.
"""

import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PlainSerializer, SecretStr, WithJsonSchema

from app.core.security import PASSWORD_MAX_LENGTH

EXPORT_VERSION = 1


def _in_utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


# Every moment in the document is written the same way: ISO 8601 (RFC 3339),
# in UTC, to the microsecond the database keeps, ending in "Z". Always 27
# characters, so the texts sort as the moments do.
Timestamp = Annotated[
    datetime,
    PlainSerializer(_in_utc, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "date-time"}, mode="serialization"),
]


# --- request -------------------------------------------------------------


class ExportRequest(BaseModel):
    """Asking for one's own data. The password is all a client can say:
    whose data it is, is the session's to tell."""

    # Like the password at login: checked against the account, not against
    # the policy for choosing one.
    password: Annotated[SecretStr, Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)]


# --- the document --------------------------------------------------------


class ExportedAccount(BaseModel):
    """The account itself: what the profile shows and what only its owner
    is told. The password is not here in any form."""

    id: uuid.UUID
    username: str
    email: str
    display_name: str
    bio: str | None
    avatar_url: str | None
    # False for an account that was deactivated.
    is_active: bool
    # When the email address was confirmed; null if it never was.
    email_verified_at: Timestamp | None
    created_at: Timestamp
    updated_at: Timestamp


class ExportedSession(BaseModel):
    """One login of the account, by when it happened and how it ended.

    A session has an id and a token on the server. Neither is here: with
    them a session could be addressed or used, and neither says anything
    about the user.
    """

    created_at: Timestamp
    last_used_at: Timestamp
    expires_at: Timestamp
    # When it was ended by logging out or otherwise; null if it was not.
    revoked_at: Timestamp | None
    # Whether this is the session the export was asked for with.
    is_current: bool


class ExportedLinkRequest(BaseModel):
    """One emailed link that was asked for: when, until when it was good,
    and whether it was used. The link itself is not kept anywhere."""

    requested_at: Timestamp
    expires_at: Timestamp
    used_at: Timestamp | None


class ExportedAccountRequests(BaseModel):
    """The links that were emailed for the account."""

    email_verifications: list[ExportedLinkRequest]
    password_resets: list[ExportedLinkRequest]


class ExportedPost(BaseModel):
    """A post the account wrote. A reply is a post that answers another:
    ``parent_post_id`` says which, and is null for a post that answers none.

    Only the id of the post that was answered is given. Whatever else there
    is to that post is its author's, not the account's.
    """

    id: uuid.UUID
    content: str
    parent_post_id: uuid.UUID | None
    created_at: Timestamp
    updated_at: Timestamp


class ExportedInteraction(BaseModel):
    """A like, or a repost, the account made: of which post, and when."""

    post_id: uuid.UUID
    created_at: Timestamp


class ExportedFollow(BaseModel):
    """One follow, by the other account's username and when it began."""

    username: str
    created_at: Timestamp


class ExportedStory(BaseModel):
    """A story the account published, for as long as its row is kept."""

    id: uuid.UUID
    media_url: str
    media_type: str
    caption: str | None
    created_at: Timestamp
    expires_at: Timestamp
    # How many other users viewed it. Who they were is their data, and is
    # told to nobody.
    view_count: int


class ExportedStoryView(BaseModel):
    """A story of somebody else's that the account viewed, and when."""

    story_id: uuid.UUID
    viewed_at: Timestamp


class AccountExport(BaseModel):
    """Everything Hopsnop holds that is the account's own.

    Every collection is always there, empty if the account has nothing of
    the kind, and each is in the order its entries came about, oldest first.
    """

    export_version: Literal[1]
    # When the document was put together.
    generated_at: Timestamp
    account: ExportedAccount
    sessions: list[ExportedSession]
    account_requests: ExportedAccountRequests
    # Posts and replies alike.
    posts: list[ExportedPost]
    likes: list[ExportedInteraction]
    reposts: list[ExportedInteraction]
    # The accounts this one follows, and those that follow it.
    following: list[ExportedFollow]
    followers: list[ExportedFollow]
    stories: list[ExportedStory]
    story_views: list[ExportedStoryView]

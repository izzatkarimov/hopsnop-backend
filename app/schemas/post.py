"""Request and response bodies of the posts API.

A request names only what a client may decide: the text and, when creating,
the post being replied to. Who the author is, every timestamp and whether the
post is deleted are the server's to determine, so no request schema has a
field for them, and anything else in a body is ignored, like everywhere else
in the API.

A response lists its fields in full, and so does the author inside it, so a
column added to the ``Post`` or ``User`` model is never exposed until someone
adds it here deliberately.
"""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
)

# The same limit as the posts.content check constraint. Both count characters
# (Unicode code points), not bytes, so they agree on every text.
POST_MAX_LENGTH = 300


def _reject_nul(value: str) -> str:
    # PostgreSQL cannot store this character in text.
    if "\x00" in value:
        raise ValueError("Must not contain null characters.")
    return value


# Plain text. Surrounding whitespace is not part of a post, so text made of
# whitespace alone is empty and is rejected. A text that is too long is
# rejected as well; it is never cut to fit.
PostContent = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=POST_MAX_LENGTH),
    AfterValidator(_reject_nul),
]


# --- requests ------------------------------------------------------------


class CreatePostRequest(BaseModel):
    content: PostContent = Field(
        description=f"1 to {POST_MAX_LENGTH} characters of plain text.",
    )
    parent_post_id: uuid.UUID | None = Field(
        default=None,
        description="The post this one replies to. null or absent for a new post.",
    )


class UpdatePostRequest(BaseModel):
    """An edit of the caller's own post. The text is all that can change."""

    content: PostContent = Field(
        description=f"1 to {POST_MAX_LENGTH} characters of plain text.",
    )


# --- responses -----------------------------------------------------------


class PostAuthorResponse(BaseModel):
    """What a post shows of its author: enough to render a byline."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    display_name: str
    avatar_url: str | None


class PostResponse(BaseModel):
    """A post as shown to someone who may see it.

    There is no deletion field: a deleted post is not returned at all.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    author: PostAuthorResponse
    content: str
    parent_post_id: uuid.UUID | None
    created_at: datetime
    # Equal to created_at for a post that was never edited.
    updated_at: datetime
    # Counted from the likes and reposts at the moment of the request. Who
    # liked or reposted is not part of a post: of all those users, the caller
    # only learns about themselves, and an anonymous caller about nobody.
    like_count: int
    liked_by_me: bool
    repost_count: int
    reposted_by_me: bool

    @computed_field
    @property
    def is_reply(self) -> bool:
        return self.parent_post_id is not None


class LikeResponse(BaseModel):
    """Where a post stands with the caller's like after a request."""

    liked: bool
    like_count: int


class RepostResponse(BaseModel):
    """Where a post stands with the caller's repost after a request."""

    reposted: bool
    repost_count: int


class PostPageResponse(BaseModel):
    """One page of a list of posts, newest first."""

    items: list[PostResponse]
    next_cursor: str | None = Field(
        description=(
            "Pass as `cursor` to get the next page. null when there are no "
            "more posts."
        ),
    )

"""Story endpoints.

Like the other routers these are thin: validation is in ``app.schemas.story``
and the rules (who may see, view or delete which story, and until when) are
in ``app.services.stories``.

Every one of them needs a signed-in user with a verified email address.
Nobody reads a story anonymously.
"""

import uuid

from fastapi import APIRouter, Depends, status

from app.api.deps import DbSession, Pagination, VerifiedUser, no_store
from app.schemas.story import (
    CreateStoryRequest,
    StoryPageResponse,
    StoryResponse,
    StoryViewResponse,
)
from app.services import stories as stories_service

# What a story endpoint answers depends on who is asking and can stop being
# true at any moment: a story expires or is deleted, its author is
# unfollowed. No copy of an answer should outlive that.
router = APIRouter(
    prefix="/stories", tags=["stories"], dependencies=[Depends(no_store)]
)


@router.post("", response_model=StoryResponse, status_code=status.HTTP_201_CREATED)
def create_story(
    data: CreateStoryRequest,
    user: VerifiedUser,
    db: DbSession,
) -> StoryResponse:
    """Publishes a story: an image, by its URL, with an optional caption.

    The author is always the signed-in user; there is no way to name another.
    The story is shown for 24 hours.
    """
    story = stories_service.create_story(
        db,
        user,
        media_url=str(data.media_url),
        caption=data.caption,
    )
    return StoryResponse.model_validate(story)


@router.get("", response_model=StoryPageResponse)
def list_stories(
    viewer: VerifiedUser,
    page: Pagination,
    db: DbSession,
) -> StoryPageResponse:
    """The active stories of the caller and of the users they follow, newest
    first.

    Reading the list records no views.
    """
    stories = stories_service.list_stories(
        db,
        viewer,
        limit=page.limit,
        after=page.after,
    )
    return StoryPageResponse(
        items=[StoryResponse.model_validate(story) for story in stories.items],
        next_cursor=stories.next_cursor,
    )


@router.get("/{story_id}", response_model=StoryResponse)
def get_story(story_id: uuid.UUID, viewer: VerifiedUser, db: DbSession) -> StoryResponse:
    """A single active story, the caller's own or of a user they follow.

    Any other story is not found. Reading a story records no view.
    """
    return StoryResponse.model_validate(
        stories_service.get_story(db, story_id, viewer)
    )


@router.delete("/{story_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_story(story_id: uuid.UUID, user: VerifiedUser, db: DbSession) -> None:
    """Deletes the caller's own story, together with its views."""
    stories_service.delete_story(db, user, story_id)


@router.post("/{story_id}/view", response_model=StoryViewResponse)
def view_story(
    story_id: uuid.UUID, viewer: VerifiedUser, db: DbSession
) -> StoryViewResponse:
    """Records that the caller has viewed a story they can see.

    Whose view it is, is always the signed-in user; there is no way to name
    another. It can be repeated: a story is viewed by a user once, however
    often this is called. An author's look at their own story is not a view.
    """
    viewed = stories_service.record_view(db, viewer, story_id)
    return StoryViewResponse(viewed=viewed)

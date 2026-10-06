"""Feed endpoints.

Like the other routers these are thin. Which posts are in a feed is decided
in ``app.services.posts``, with every other rule about who may see a post.
"""

from fastapi import APIRouter, Depends

from app.api.deps import DbSession, OptionalUser, Pagination, no_store
from app.schemas.post import PostPageResponse, PostResponse
from app.services import posts as posts_service

# A post in the feed can be deleted, and its author's account can be
# deactivated, at any moment. No copy of an answer should outlive that.
router = APIRouter(prefix="/feed", tags=["feed"], dependencies=[Depends(no_store)])


@router.get("", response_model=PostPageResponse)
def get_for_you_feed(
    viewer: OptionalUser,
    page: Pagination,
    db: DbSession,
) -> PostPageResponse:
    """For You: every post that is shown, newest first, replies included.

    No authentication is needed, and the same posts are in it with or without
    it. Whom the caller follows plays no part.
    """
    posts = posts_service.list_for_you_feed(
        db,
        viewer,
        limit=page.limit,
        after=page.after,
    )
    return PostPageResponse(
        items=[PostResponse.model_validate(post) for post in posts.items],
        next_cursor=posts.next_cursor,
    )

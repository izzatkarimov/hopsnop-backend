"""Feed endpoints.

Like the other routers these are thin. Which posts are in a feed is decided
in ``app.services.posts``, with every other rule about who may see a post.
"""

from fastapi import APIRouter, Depends

from app.api.deps import DbSession, OptionalUser, Pagination, no_store
from app.schemas.post import PostPageResponse, PostResponse
from app.services import posts as posts_service

# A post in the feed can be deleted, and its author's account can become
# private, at any moment. No copy of an answer should outlive that.
router = APIRouter(prefix="/feed", tags=["feed"], dependencies=[Depends(no_store)])


@router.get("", response_model=PostPageResponse)
def get_for_you_feed(
    viewer: OptionalUser,
    page: Pagination,
    db: DbSession,
) -> PostPageResponse:
    """For You: the posts of public accounts, newest first, replies included.

    No authentication is needed, and the answer is the same with or without
    it. The posts of a private account are not in this feed for anyone.
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

"""Post endpoints.

Like the other routers these are thin: validation is in ``app.schemas.post``
and the rules (who may see, edit, delete, like or repost what, and until
when) are in ``app.services.posts``.

The list of a user's posts is ``GET /users/{username}/posts`` and lives with
the other ``/users`` routes.
"""

import uuid

from fastapi import APIRouter, Depends, status

from app.api.deps import DbSession, OptionalUser, VerifiedUser, no_store
from app.schemas.post import (
    CreatePostRequest,
    LikeResponse,
    PostResponse,
    RepostResponse,
    UpdatePostRequest,
)
from app.services import posts as posts_service

# What a post endpoint answers depends on who is asking and can stop being
# true at any moment: a post is deleted, an account becomes private. No copy
# of an answer should outlive that.
router = APIRouter(prefix="/posts", tags=["posts"], dependencies=[Depends(no_store)])


@router.post("", response_model=PostResponse, status_code=status.HTTP_201_CREATED)
def create_post(
    data: CreatePostRequest,
    user: VerifiedUser,
    db: DbSession,
) -> PostResponse:
    """Publishes a post, or a reply if `parent_post_id` is given.

    The author is always the signed-in user; there is no way to name another.
    """
    post = posts_service.create_post(
        db,
        user,
        content=data.content,
        parent_post_id=data.parent_post_id,
    )
    return PostResponse.model_validate(post)


@router.get("/{post_id}", response_model=PostResponse)
def get_post(post_id: uuid.UUID, viewer: OptionalUser, db: DbSession) -> PostResponse:
    """A single post. No authentication is needed for a public account's post.

    A post that was deleted, or that the caller may not see, is not found.
    """
    return PostResponse.model_validate(posts_service.get_post(db, post_id, viewer))


@router.patch("/{post_id}", response_model=PostResponse)
def update_post(
    post_id: uuid.UUID,
    data: UpdatePostRequest,
    user: VerifiedUser,
    db: DbSession,
) -> PostResponse:
    """Changes the text of the caller's own post and returns the result.

    Possible for 60 minutes after the post was created.
    """
    post = posts_service.update_post(db, user, post_id, content=data.content)
    return PostResponse.model_validate(post)


@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_post(post_id: uuid.UUID, user: VerifiedUser, db: DbSession) -> None:
    """Deletes the caller's own post."""
    posts_service.delete_post(db, user, post_id)


# Likes and reposts. Each of the four answers with where the post now stands
# with the caller, and each can be repeated: asking for what is already the
# case is not an error and changes nothing. Whose like or repost it is, is
# always the signed-in user; there is no way to name another.


@router.post("/{post_id}/like", response_model=LikeResponse)
def like_post(post_id: uuid.UUID, user: VerifiedUser, db: DbSession) -> LikeResponse:
    """Likes a post the caller can see."""
    state = posts_service.set_like(db, user, post_id, liked=True)
    return LikeResponse(liked=state.active, like_count=state.count)


@router.delete("/{post_id}/like", response_model=LikeResponse)
def unlike_post(post_id: uuid.UUID, user: VerifiedUser, db: DbSession) -> LikeResponse:
    """Takes the caller's like back from a post they can see."""
    state = posts_service.set_like(db, user, post_id, liked=False)
    return LikeResponse(liked=state.active, like_count=state.count)


@router.post("/{post_id}/repost", response_model=RepostResponse)
def repost_post(
    post_id: uuid.UUID, user: VerifiedUser, db: DbSession
) -> RepostResponse:
    """Reposts a post the caller can see."""
    state = posts_service.set_repost(db, user, post_id, reposted=True)
    return RepostResponse(reposted=state.active, repost_count=state.count)


@router.delete("/{post_id}/repost", response_model=RepostResponse)
def unrepost_post(
    post_id: uuid.UUID, user: VerifiedUser, db: DbSession
) -> RepostResponse:
    """Takes the caller's repost back from a post they can see."""
    state = posts_service.set_repost(db, user, post_id, reposted=False)
    return RepostResponse(reposted=state.active, repost_count=state.count)

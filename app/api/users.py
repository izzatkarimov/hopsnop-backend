"""User profile endpoints.

Like the authentication handlers these are thin: validation is in
``app.schemas.user`` and the rules and queries are in ``app.services.users``.

The list of a user's posts is here as well, because of its path. Its rules
are in ``app.services.posts`` with those of the other post endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentUser, DbSession, OptionalUser, Pagination, no_store
from app.schemas.post import PostPageResponse, PostResponse
from app.schemas.user import (
    MyProfileResponse,
    PublicProfileResponse,
    UpdateProfileRequest,
    canonical_username,
)
from app.services import posts as posts_service
from app.services import users as users_service
from app.services.users import OwnProfile

router = APIRouter(prefix="/users", tags=["users"])


def _my_profile_response(profile: OwnProfile) -> MyProfileResponse:
    # Field by field, so the response holds exactly what is listed here.
    user = profile.user
    return MyProfileResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        bio=user.bio,
        avatar_url=user.avatar_url,
        is_private=user.is_private,
        email_verified_at=user.email_verified_at,
        followers_count=profile.followers_count,
        following_count=profile.following_count,
        created_at=user.created_at,
    )


@router.get(
    "/me",
    response_model=MyProfileResponse,
    dependencies=[Depends(no_store)],
)
def get_my_profile(user: CurrentUser, db: DbSession) -> MyProfileResponse:
    """The signed-in user's own profile, including their private account fields."""
    return _my_profile_response(users_service.get_own_profile(db, user))


@router.patch(
    "/me",
    response_model=MyProfileResponse,
    dependencies=[Depends(no_store)],
)
def update_my_profile(
    data: UpdateProfileRequest,
    user: CurrentUser,
    db: DbSession,
) -> MyProfileResponse:
    """Changes the signed-in user's own profile and returns the result.

    Only the fields that are sent are changed. The profile that is updated is
    always the caller's; there is no way to name another user.
    """
    profile = users_service.update_profile(db, user, data.changes())
    return _my_profile_response(profile)


# Declared after /users/me, which therefore always takes precedence. No
# account is hidden by that: usernames are at least three characters long.
@router.get("/{username}", response_model=PublicProfileResponse)
def get_profile(username: str, db: DbSession) -> PublicProfileResponse:
    """A user's public profile. No authentication is needed.

    The profile of a private account is shown as well; `is_private` only says
    that the account's content is restricted.
    """
    canonical = canonical_username(username)
    profile = users_service.get_public_profile(db, canonical) if canonical else None
    if profile is None:
        # One answer for every reason an account is not shown.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )
    return PublicProfileResponse.model_validate(profile)


@router.get(
    "/{username}/posts",
    response_model=PostPageResponse,
    # The answer depends on who is asking, and a post in it can be deleted at
    # any moment.
    dependencies=[Depends(no_store)],
)
def list_user_posts(
    username: str,
    viewer: OptionalUser,
    page: Pagination,
    db: DbSession,
) -> PostPageResponse:
    """A user's posts, newest first, replies included.

    No authentication is needed for a public account. The posts of a private
    account are only returned to that account.
    """
    canonical = canonical_username(username)
    if canonical is None:
        # No account can have this name; the same answer as for one that
        # does not exist.
        raise posts_service.UserNotFoundError
    posts = posts_service.list_user_posts(
        db,
        canonical,
        viewer,
        limit=page.limit,
        after=page.after,
    )
    return PostPageResponse(
        items=[PostResponse.model_validate(post) for post in posts.items],
        next_cursor=posts.next_cursor,
    )

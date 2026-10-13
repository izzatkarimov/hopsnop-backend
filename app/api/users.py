"""User profile endpoints.

Like the authentication handlers these are thin: validation is in
``app.schemas.user`` and the rules and queries are in ``app.services.users``.

The list of a user's posts is here as well, because of its path. Its rules
are in ``app.services.posts`` with those of the other post endpoints.

Following a user, and the lists of who follows whom, are here too; their
rules are in ``app.services.users``.

So is the export of the signed-in user's data, which is made by
``app.services.export``.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.auth import data_export_limit, password_confirmation_limits
from app.api.deps import (
    CurrentSession,
    CurrentUser,
    DbSession,
    OptionalUser,
    Pagination,
    VerifiedUser,
    no_store,
)
from app.schemas.export import AccountExport, ExportRequest
from app.schemas.post import PostPageResponse, PostResponse
from app.schemas.user import (
    FollowResponse,
    MyProfileResponse,
    PublicProfileResponse,
    UpdateProfileRequest,
    UserPageResponse,
    UserSummaryResponse,
    canonical_username,
)
from app.services import auth as auth_service
from app.services import export as export_service
from app.services import posts as posts_service
from app.services import rate_limit
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
    """The signed-in user's own profile, including the fields only they may see."""
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


@router.post(
    "/me/export",
    response_model=AccountExport,
    dependencies=[Depends(no_store)],
)
def export_my_data(
    data: ExportRequest,
    current: CurrentSession,
    response: Response,
    db: DbSession,
) -> AccountExport:
    """Everything Hopsnop holds that is the signed-in user's own, as one
    JSON document to download.

    The password has to be given again: a session alone does not hand out
    all of an account's data. Whose data it is, is always the signed-in
    user's; there is no way to name another.

    The document holds no password, no token and no id of a session, and of
    other people only what the user could already see: the usernames of
    accounts they follow or are followed by, and the ids of posts and
    stories they answered, liked, reposted or viewed.

    It is not a snapshot of one moment. Its parts are read one after the
    other, and each shows the user's data as it was when that part was
    read. What the user does while the export is being made can be in one
    part and not in another.
    """
    # Two counters, and a place is taken in both or in neither: the wrong
    # passwords of the account, whatever asked for the password, and its
    # exports. Which place is kept depends on how the request turns out.
    limits = [*password_confirmation_limits(current), data_export_limit(current)]
    *confirmed, exported = rate_limit.count_all(db, limits)
    try:
        export = export_service.export_account(
            db, current, password=data.password.get_secret_value()
        )
    except auth_service.AccountNoLongerExistsError:
        # The account was removed while this was under way, and its counters
        # with it. What was counted here since is not left behind for an
        # account that is not there.
        rate_limit.forget(db, limits)
        raise
    except auth_service.AuthError:
        # No export came of it, so none is counted. What it was refused
        # for stays counted as that.
        rate_limit.uncount_all(db, [exported])
        raise
    # The password was right, so this was not a guess.
    rate_limit.uncount_all(db, confirmed)
    day = export.generated_at.strftime("%Y-%m-%d")
    response.headers["Content-Disposition"] = (
        f'attachment; filename="hopsnop-export-{export.account.username}-{day}.json"'
    )
    return export


# Declared after /users/me, which therefore always takes precedence. No
# account is hidden by that: usernames are at least three characters long.
@router.get(
    "/{username}",
    response_model=PublicProfileResponse,
    # `following` depends on who is asking.
    dependencies=[Depends(no_store)],
)
def get_profile(
    username: str, viewer: OptionalUser, db: DbSession
) -> PublicProfileResponse:
    """A user's public profile. No authentication is needed.

    `following` says whether the caller follows the user. It is false for an
    anonymous request.
    """
    canonical = canonical_username(username)
    profile = (
        users_service.get_public_profile(db, canonical, viewer) if canonical else None
    )
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

    No authentication is needed.
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


# --- following -----------------------------------------------------------
#
# Who follows whom can change at any moment, and part of it depends on who is
# asking. No copy of an answer should outlive that.


def _canonical(username: str) -> str:
    canonical = canonical_username(username)
    if canonical is None:
        # No account can have this name; the same answer as for one that
        # does not exist.
        raise users_service.UserNotFoundError
    return canonical


@router.post(
    "/{username}/follow",
    response_model=FollowResponse,
    dependencies=[Depends(no_store)],
)
def follow_user(username: str, user: VerifiedUser, db: DbSession) -> FollowResponse:
    """Makes the caller follow the user. Following takes effect at once.

    Who follows is always the signed-in user; there is no way to name
    another. Following a user who is already followed changes nothing and is
    not an error.
    """
    following = users_service.set_follow(db, user, _canonical(username), following=True)
    return FollowResponse(following=following)


@router.delete(
    "/{username}/follow",
    response_model=FollowResponse,
    dependencies=[Depends(no_store)],
)
def unfollow_user(username: str, user: VerifiedUser, db: DbSession) -> FollowResponse:
    """Makes the caller no longer follow the user.

    Unfollowing a user who is not followed changes nothing and is not an
    error.
    """
    following = users_service.set_follow(
        db, user, _canonical(username), following=False
    )
    return FollowResponse(following=following)


@router.get(
    "/{username}/follow-status",
    response_model=FollowResponse,
    dependencies=[Depends(no_store)],
)
def get_follow_status(
    username: str, viewer: OptionalUser, db: DbSession
) -> FollowResponse:
    """Whether the caller follows the user. No authentication is needed.

    An anonymous caller follows nobody, so the answer is then always false.
    """
    following = users_service.get_follow_status(db, viewer, _canonical(username))
    return FollowResponse(following=following)


@router.get(
    "/{username}/followers",
    response_model=UserPageResponse,
    dependencies=[Depends(no_store)],
)
def list_followers(username: str, page: Pagination, db: DbSession) -> UserPageResponse:
    """The users who follow the user, the most recent follower first.

    No authentication is needed.
    """
    users = users_service.list_followers(
        db, _canonical(username), limit=page.limit, after=page.after
    )
    return UserPageResponse(
        items=[UserSummaryResponse.model_validate(user) for user in users.items],
        next_cursor=users.next_cursor,
    )


@router.get(
    "/{username}/following",
    response_model=UserPageResponse,
    dependencies=[Depends(no_store)],
)
def list_following(username: str, page: Pagination, db: DbSession) -> UserPageResponse:
    """The users the user follows, the most recently followed first.

    No authentication is needed.
    """
    users = users_service.list_following(
        db, _canonical(username), limit=page.limit, after=page.after
    )
    return UserPageResponse(
        items=[UserSummaryResponse.model_validate(user) for user in users.items],
        next_cursor=users.next_cursor,
    )

"""User profiles: reading them, and editing one's own. And who follows whom.

Following needs nobody's approval and has no state in between: a row in
``follows`` says that one account follows another, and removing the row ends
it. Follower and following counts are always computed from that table. They
are not stored anywhere else.

Like the other services, every public function that writes is one unit of
work and commits once, at the end.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    ColumnElement,
    ScalarSelect,
    delete,
    exists,
    false,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import InstrumentedAttribute, Session, contains_eager

from app.core.pagination import Cursor, Page, paginate
from app.models import Follow, User

# The only columns the profile API may write. ``update_profile`` refuses
# anything else, whatever the caller passes in.
EDITABLE_FIELDS = frozenset({"display_name", "bio", "avatar_url"})

# Which accounts there are, as far as anyone else is concerned. A deactivated
# account, and one whose email address was never verified, cannot be logged
# in to, so neither is shown: not as a profile, not in a list of followers,
# not as someone to follow, and not as the author of a story.
ACCOUNT_IS_SHOWN = (
    User.is_active.is_(True),
    User.email_verified_at.is_not(None),
)


class UserError(Exception):
    """A failure that is reported to the client as ``detail`` with ``status_code``."""

    status_code = 400
    detail = "The request could not be completed."


class UserNotFoundError(UserError):
    status_code = 404
    detail = "User not found."


class SelfFollowError(UserError):
    detail = "You cannot follow yourself."


@dataclass(frozen=True)
class PublicProfile:
    """What anyone may know about a user. It has no private fields at all."""

    id: uuid.UUID
    username: str
    display_name: str
    bio: str | None
    avatar_url: str | None
    created_at: datetime
    followers_count: int
    following_count: int
    # Whether whoever is reading the profile follows this user. The one value
    # here that depends on who is asking; false for an anonymous reader.
    following: bool


@dataclass(frozen=True)
class OwnProfile:
    """A user's own account together with their counts."""

    user: User
    followers_count: int
    following_count: int


def get_public_profile(
    db: Session, username: str, viewer: User | None
) -> PublicProfile | None:
    """The public profile of the account with this (canonical) username.

    Only the public columns are selected, so the private ones never leave the
    database on this path. The counts come from the same query, and so does
    whether ``viewer`` (None for an anonymous request) follows the account.

    Deactivated accounts, and accounts whose email address was never verified,
    are not found: neither can be logged in to, so neither is shown.
    """
    followers_count, following_count = _follow_counts(User.id)
    row = db.execute(
        select(
            User.id,
            User.username,
            User.display_name,
            User.bio,
            User.avatar_url,
            User.created_at,
            followers_count.label("followers_count"),
            following_count.label("following_count"),
            is_followed_by(viewer, User.id).label("following"),
        ).where(User.username == username, *ACCOUNT_IS_SHOWN)
    ).one_or_none()
    if row is None:
        return None
    return PublicProfile(**row._mapping)


def get_own_profile(db: Session, user: User) -> OwnProfile:
    """The given (already authenticated) user's account with their counts."""
    followers_count, following_count = db.execute(
        select(*_follow_counts(user.id))
    ).one()
    return OwnProfile(user, followers_count, following_count)


def update_profile(
    db: Session,
    user: User,
    changes: Mapping[str, str | None],
) -> OwnProfile:
    """Apply the given changes to the user's profile and nothing else.

    ``changes`` holds only the fields to change; a field that is absent keeps
    its value, and a field mapped to None is cleared.
    """
    not_editable = changes.keys() - EDITABLE_FIELDS
    if not_editable:
        # Reaching this is a bug in the caller, not bad user input: the
        # request schema has no such fields.
        raise ValueError(f"Not editable through the profile: {sorted(not_editable)}")
    for name, value in changes.items():
        setattr(user, name, value)
    db.commit()
    return get_own_profile(db, user)


def _follow_counts(
    user_id: ColumnElement[uuid.UUID] | uuid.UUID,
) -> tuple[ScalarSelect[int], ScalarSelect[int]]:
    """Subqueries counting the user's followers and the accounts they follow.

    Each count is answered from an index on ``follows`` that starts with the
    column it filters on.
    """
    followers_count = (
        select(func.count())
        .select_from(Follow)
        .where(Follow.following_id == user_id)
        .scalar_subquery()
    )
    following_count = (
        select(func.count())
        .select_from(Follow)
        .where(Follow.follower_id == user_id)
        .scalar_subquery()
    )
    return followers_count, following_count


# --- following -----------------------------------------------------------


def is_followed_by(
    viewer: User | None, user_id: ColumnElement[uuid.UUID] | uuid.UUID
) -> ColumnElement[bool]:
    """Condition: does ``viewer`` follow the user with this id?

    Never true for a request that is not authenticated. For one that is, it
    is a lookup by primary key.
    """
    if viewer is None:
        return false()
    return exists().where(
        Follow.follower_id == viewer.id, Follow.following_id == user_id
    )


def _shown_user_id(db: Session, username: str) -> uuid.UUID:
    """The id of the account with this (canonical) username, if it is shown.

    An account that is not shown is not found, for whatever reason it is not
    shown and with the answer its profile gives.
    """
    user_id = db.scalar(
        select(User.id).where(User.username == username, *ACCOUNT_IS_SHOWN)
    )
    if user_id is None:
        raise UserNotFoundError
    return user_id


def set_follow(db: Session, user: User, username: str, *, following: bool) -> bool:
    """Make ``user`` follow the account with this username, or no longer follow it.

    Returns whether ``user`` follows the account afterwards. That is the same
    whatever was the case before: following an account that is already
    followed leaves it followed, and unfollowing one that is not followed
    leaves it not followed. Neither is an error.

    Who follows is always ``user``. Only an account that is shown can be
    followed, and only such an account can be unfollowed; any other is not
    found.
    """
    target_id = _shown_user_id(db, username)

    if following:
        # The database refuses this row too (ck_follows_no_self_follow), but
        # with an error of its own. Here it is an answer to the client.
        if target_id == user.id:
            raise SelfFollowError
        # The primary key (follower, following) is what rules out a second
        # row. The conflict is left to the database rather than looked for
        # first and avoided, so two requests arriving at once cannot both
        # insert.
        db.execute(
            insert(Follow)
            .values(follower_id=user.id, following_id=target_id)
            .on_conflict_do_nothing()
        )
    else:
        db.execute(
            delete(Follow).where(
                Follow.follower_id == user.id, Follow.following_id == target_id
            )
        )

    # Read from the rows, after the change and inside its transaction.
    now_following = db.scalar(select(is_followed_by(user, target_id)))
    db.commit()
    return now_following


def get_follow_status(db: Session, viewer: User | None, username: str) -> bool:
    """Whether ``viewer`` follows the account with this (canonical) username.

    One query answers both that and whether there is such an account to ask
    about, so an account that is not shown is not found here either.
    """
    following = db.scalar(
        select(is_followed_by(viewer, User.id)).where(
            User.username == username, *ACCOUNT_IS_SHOWN
        )
    )
    if following is None:
        raise UserNotFoundError
    return following


def list_followers(
    db: Session, username: str, *, limit: int, after: Cursor | None = None
) -> Page[User]:
    """One page of the accounts that follow the account with this username."""
    return _list_follows(
        db,
        username,
        account=Follow.following_id,
        listed=Follow.follower,
        listed_id="follower_id",
        limit=limit,
        after=after,
    )


def list_following(
    db: Session, username: str, *, limit: int, after: Cursor | None = None
) -> Page[User]:
    """One page of the accounts that the account with this username follows."""
    return _list_follows(
        db,
        username,
        account=Follow.follower_id,
        listed=Follow.following,
        listed_id="following_id",
        limit=limit,
        after=after,
    )


def _list_follows(
    db: Session,
    username: str,
    *,
    account: InstrumentedAttribute[uuid.UUID],
    listed: InstrumentedAttribute[User],
    listed_id: str,
    limit: int,
    after: Cursor | None,
) -> Page[User]:
    """One page of the accounts on the other side of an account's follows.

    ``account`` is the column of ``follows`` that holds the account asked
    about, ``listed`` the relationship to the accounts that are returned and
    ``listed_id`` the name of its column.

    The most recent follow comes first. A follow has no id of its own, but
    within one account's list the other account's id is unique, so that is
    what breaks ties, and with the time of the follow it is what the cursor
    holds.

    Only accounts that are shown are listed. The others are left out by the
    query, before the page is cut. Of each listed account, only what a list
    shows is read from the database, in the statement that reads the follows:
    a page costs no query per account.
    """
    account_id = _shown_user_id(db, username)
    page = paginate(
        db,
        select(Follow)
        .join(listed)
        .options(
            contains_eager(listed).load_only(
                User.username,
                User.display_name,
                User.avatar_url,
            )
        )
        .where(account == account_id, *ACCOUNT_IS_SHOWN),
        Follow,
        limit=limit,
        after=after,
        tiebreaker=listed_id,
    )
    return Page(
        items=[getattr(follow, listed.key) for follow in page.items],
        next_cursor=page.next_cursor,
    )

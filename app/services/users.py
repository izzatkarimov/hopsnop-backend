"""User profiles: reading them, and editing one's own.

Follower and following counts are always computed from the ``follows`` table.
They are not stored anywhere else.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, ScalarSelect, func, select
from sqlalchemy.orm import Session

from app.models import Follow, User

# The only columns the profile API may write. ``update_profile`` refuses
# anything else, whatever the caller passes in.
EDITABLE_FIELDS = frozenset({"display_name", "bio", "avatar_url", "is_private"})


@dataclass(frozen=True)
class PublicProfile:
    """What anyone may know about a user. It has no private fields at all."""

    id: uuid.UUID
    username: str
    display_name: str
    bio: str | None
    avatar_url: str | None
    is_private: bool
    created_at: datetime
    followers_count: int
    following_count: int


@dataclass(frozen=True)
class OwnProfile:
    """A user's own account together with their counts."""

    user: User
    followers_count: int
    following_count: int


def get_public_profile(db: Session, username: str) -> PublicProfile | None:
    """The public profile of the account with this (canonical) username.

    Only the public columns are selected, so the private ones never leave the
    database on this path. The counts come from the same query.

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
            User.is_private,
            User.created_at,
            followers_count.label("followers_count"),
            following_count.label("following_count"),
        ).where(
            User.username == username,
            User.is_active.is_(True),
            User.email_verified_at.is_not(None),
        )
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
    changes: Mapping[str, str | bool | None],
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

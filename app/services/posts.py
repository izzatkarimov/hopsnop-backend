"""Posts: writing, reading, editing, deleting, liking and reposting them, and
the feeds of them.

A reply is a post like any other. It only has ``parent_post_id`` set, and it
follows every rule here in its own right: its own author, its own visibility,
its own edit window, its own likes and reposts.

Nothing in here trusts the client for anything but the text of a post and the
id of the post being replied to, liked or reposted. The author, and whoever
likes or reposts, is always the authenticated user passed in, and every
timestamp comes from this module's clock or from the database.

Like the authentication service, every public function that writes is one
unit of work and commits once, at the end.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    ColumnElement,
    ScalarSelect,
    Select,
    delete,
    exists,
    false,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, contains_eager, with_expression

from app.core.pagination import Cursor, Page, paginate
from app.models import Like, Post, Repost, User
from app.services.users import is_followed_by

EDIT_WINDOW_MINUTES = 60
# How long after it was created a post can still be edited. The deadline is
# always worked out from created_at; it is not stored, and editing a post
# does not move it.
EDIT_WINDOW = timedelta(minutes=EDIT_WINDOW_MINUTES)


class PostError(Exception):
    """A failure that is reported to the client as ``detail`` with ``status_code``."""

    status_code = 400
    detail = "The request could not be completed."


class PostNotFoundError(PostError):
    status_code = 404
    detail = "Post not found."


class ParentPostNotFoundError(PostError):
    status_code = 404
    detail = "Parent post not found."


class NotPostAuthorError(PostError):
    status_code = 403
    detail = "You are not the author of this post."


class EditWindowExpiredError(PostError):
    status_code = 409
    detail = (
        f"A post can only be edited within {EDIT_WINDOW_MINUTES} minutes of "
        "being created."
    )


class UserNotFoundError(PostError):
    status_code = 404
    detail = "User not found."


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- visibility ----------------------------------------------------------
#
# Which posts are shown is decided in this section and nowhere else. Every
# read below, and every check that a post "exists" for someone, goes through
# ``_visible_posts``.

# The rule public profiles use: an account that is deactivated, or whose
# email address was never verified, is not shown, and neither are its posts.
_ACCOUNT_IS_SHOWN = (
    User.is_active.is_(True),
    User.email_verified_at.is_not(None),
)


# A like and a repost are the same thing to the database: a row that says
# "this user, this post". What is written for one below is written for both.
_Interaction = type[Like] | type[Repost]


def _count_of(
    kind: _Interaction, post_id: ColumnElement[uuid.UUID] | uuid.UUID
) -> ScalarSelect[int]:
    """Subquery counting the likes, or the reposts, of a post.

    Counted from the rows every time, through the index on ``post_id``. No
    counter is stored anywhere, so none can drift from the rows.
    """
    return (
        select(func.count())
        .select_from(kind)
        .where(kind.post_id == post_id)
        .scalar_subquery()
    )


def _made_by(
    kind: _Interaction,
    post_id: ColumnElement[uuid.UUID] | uuid.UUID,
    viewer: User | None,
) -> ColumnElement[bool]:
    """Condition: has ``viewer`` liked, or reposted, the post?

    Never true for a request that is not authenticated. For one that is, it
    is a lookup by primary key.
    """
    if viewer is None:
        return false()
    return exists().where(kind.user_id == viewer.id, kind.post_id == post_id)


def _visible_posts(viewer: User | None) -> Select[tuple[Post]]:
    """Every post that is shown, each with its author, likes and reposts.

    A post is shown if it is not deleted and its author's account is shown.
    That is the same for everyone: ``viewer`` (None for a request that is not
    authenticated) decides nothing about which posts these are, only whether
    each is liked and reposted "by me".

    It is the author's account that decides, also for a reply: a reply is
    shown or not on its own, whatever becomes of the post it answers.

    Of the author, only what a post shows is read from the database. Of the
    likes and reposts, only how many there are and whether ``viewer`` is
    among them: the same statement computes both for every post it returns,
    so a list costs no query per post.

    Those four values exist on a post only as it comes out of this query.
    They are gone once the session commits, so a function that commits and
    then returns a post reads it again (``get_post``).
    """
    return (
        select(Post)
        .join(Post.author)
        .options(
            contains_eager(Post.author).load_only(
                User.username,
                User.display_name,
                User.avatar_url,
            ),
            with_expression(Post.like_count, _count_of(Like, Post.id)),
            with_expression(Post.liked_by_me, _made_by(Like, Post.id, viewer)),
            with_expression(Post.repost_count, _count_of(Repost, Post.id)),
            with_expression(Post.reposted_by_me, _made_by(Repost, Post.id, viewer)),
        )
        .where(Post.deleted_at.is_(None), *_ACCOUNT_IS_SHOWN)
    )


# --- reading -------------------------------------------------------------


def get_post(db: Session, post_id: uuid.UUID, viewer: User | None) -> Post:
    """The post with this id, if it is shown.

    A post that does not exist, one that was deleted and one whose author's
    account is not shown are all the same "not found": one query that matches
    nothing, so neither the answer nor the work done tells them apart.
    """
    post = db.scalar(_visible_posts(viewer).where(Post.id == post_id))
    if post is None:
        raise PostNotFoundError
    return post


def list_user_posts(
    db: Session,
    username: str,
    viewer: User | None,
    *,
    limit: int,
    after: Cursor | None = None,
) -> Page[Post]:
    """One page of the posts of the account with this (canonical) username.

    Newest first, replies included. An account that is not shown is not
    found, exactly as for its profile.
    """
    account_id = db.scalar(
        select(User.id).where(User.username == username, *_ACCOUNT_IS_SHOWN)
    )
    if account_id is None:
        raise UserNotFoundError

    return paginate(
        db,
        _visible_posts(viewer).where(Post.author_id == account_id),
        Post,
        limit=limit,
        after=after,
    )


def list_for_you_feed(
    db: Session,
    viewer: User | None,
    *,
    limit: int,
    after: Cursor | None = None,
) -> Page[Post]:
    """One page of the For You feed: every post that is shown, newest first.

    For now this is not a recommendation. It is every post that anyone may
    read, in the order they were written, with replies in it as the posts
    they are. Whom ``viewer`` follows plays no part in it.

    The feed has no rule of its own about which posts are in it: whatever
    ``_visible_posts`` leaves out is left out here, and nothing else is.
    """
    return paginate(
        db,
        _visible_posts(viewer),
        Post,
        limit=limit,
        after=after,
    )


def list_following_feed(
    db: Session,
    viewer: User,
    *,
    limit: int,
    after: Cursor | None = None,
) -> Page[Post]:
    """One page of the Following feed: the posts that are shown and whose
    author ``viewer`` follows, newest first.

    It is ``_visible_posts`` with one more condition, and that condition is
    worked out from ``follows`` by the query itself, every time. Nothing
    about it is stored per reader, so unfollowing an account takes all of its
    posts out at once and following it again brings back those that are
    shown then.

    Replies are in it as the posts they are, by their own author: whose post
    a reply answers plays no part. A repost puts nothing in it. Nobody
    follows themselves, so ``viewer``'s own posts are never in it.
    """
    return paginate(
        db,
        _visible_posts(viewer).where(is_followed_by(viewer, Post.author_id)),
        Post,
        limit=limit,
        after=after,
    )


def list_replies(
    db: Session,
    post_id: uuid.UUID,
    viewer: User | None,
    *,
    limit: int,
    after: Cursor | None = None,
) -> Page[Post]:
    """One page of the replies to the post with this id, newest first.

    Only the posts that answer this one directly: a reply to one of them is
    in that reply's own list. A post that is not shown is not found, exactly
    as when it is read, so its replies cannot be listed through it. Each of
    them is still a post of its own and is found wherever else it is shown.

    Which replies are shown is decided for each by its own author, like for
    any other post. Whom ``viewer`` follows plays no part.
    """
    # Only whether there is such a post is needed here, not the post.
    shown = db.scalar(
        select(Post.id)
        .join(Post.author)
        .where(Post.id == post_id, Post.deleted_at.is_(None), *_ACCOUNT_IS_SHOWN)
    )
    if shown is None:
        raise PostNotFoundError

    return paginate(
        db,
        _visible_posts(viewer).where(Post.parent_post_id == post_id),
        Post,
        limit=limit,
        after=after,
    )


# --- writing -------------------------------------------------------------


def create_post(
    db: Session,
    author: User,
    *,
    content: str,
    parent_post_id: uuid.UUID | None = None,
) -> Post:
    """Publish a post by ``author``, as a reply if ``parent_post_id`` is given.

    A post can be replied to while it is shown. A parent that does not
    exist, was deleted, or is not shown is refused with one and the same
    answer.
    """
    if parent_post_id is not None:
        parent = db.scalar(_visible_posts(author).where(Post.id == parent_post_id))
        if parent is None:
            raise ParentPostNotFoundError

    now = _now()
    post = Post(
        author=author,
        content=content,
        parent_post_id=parent_post_id,
        created_at=now,
        updated_at=now,
    )
    db.add(post)
    # Assigns the id, which is needed to read the post back.
    db.flush()
    post_id = post.id
    db.commit()
    return get_post(db, post_id, author)


def update_post(db: Session, user: User, post_id: uuid.UUID, *, content: str) -> Post:
    """Replace the text of one of the user's own posts, while that is allowed."""
    post = _own_post_for_change(db, user, post_id)
    now = _now()
    # The window closes at the deadline itself: a post created at 12:00 can
    # be edited until 12:59:59 and no longer at 13:00:00.
    if now >= post.created_at + EDIT_WINDOW:
        raise EditWindowExpiredError
    # Saving the same text again changes nothing, so it is not an edit.
    if content != post.content:
        post.content = content
        post.updated_at = now
    db.commit()
    return get_post(db, post_id, user)


def delete_post(db: Session, user: User, post_id: uuid.UUID) -> None:
    """Delete one of the user's own posts, by marking it as deleted.

    The row is kept, and so is everything that refers to it: its likes and
    reposts, and its replies, which stay what they are. From here on the post
    is not found by anyone, its author included, so it can be neither edited
    nor deleted a second time.
    """
    post = _own_post_for_change(db, user, post_id)
    post.deleted_at = _now()
    db.commit()


def _own_post_for_change(db: Session, user: User, post_id: uuid.UUID) -> Post:
    """The post, if ``user`` may change it.

    A post the user cannot see is not found, rather than forbidden, so asking
    to change a post tells nothing about posts that are hidden from the user.
    Being refused as "not the author" is only possible for a post the user
    can read anyway.

    The row is locked until the transaction ends. Of two requests changing
    the same post at once, the second waits for the first and then sees its
    result: a post that has just been deleted is not found.
    """
    post = db.scalar(
        _visible_posts(user).where(Post.id == post_id).with_for_update(of=Post)
    )
    if post is None:
        raise PostNotFoundError
    if post.author_id != user.id:
        raise NotPostAuthorError
    return post


# --- likes and reposts ---------------------------------------------------


@dataclass(frozen=True)
class InteractionState:
    """Where a post stands with one user's like, or repost, after a change."""

    # Whether the user's like, or repost, of the post now exists.
    active: bool
    # How many the post now has, the user's own included.
    count: int


def set_like(
    db: Session, user: User, post_id: uuid.UUID, *, liked: bool
) -> InteractionState:
    """Make ``user`` like the post, or no longer like it."""
    return _set_interaction(db, Like, user, post_id, wanted=liked)


def set_repost(
    db: Session, user: User, post_id: uuid.UUID, *, reposted: bool
) -> InteractionState:
    """Make ``user`` repost the post, or no longer repost it."""
    return _set_interaction(db, Repost, user, post_id, wanted=reposted)


def _set_interaction(
    db: Session,
    kind: _Interaction,
    user: User,
    post_id: uuid.UUID,
    *,
    wanted: bool,
) -> InteractionState:
    """Bring ``user``'s like, or repost, of the post to the state asked for.

    The result is the same whatever the state was before: liking a post that
    is already liked leaves it liked, and taking back a like that is not
    there leaves it not there. Neither is an error.

    Only a post the user can see can be liked or reposted, and only from
    such a post can either be taken back. Any other post is not found, with
    the answer and the lookup that reading it gives.
    """
    get_post(db, post_id, user)

    if wanted:
        # The primary key (user, post) is what rules out a second row. The
        # conflict is left to the database rather than looked for first and
        # avoided, so two requests arriving at once cannot both insert.
        db.execute(
            insert(kind)
            .values(user_id=user.id, post_id=post_id)
            .on_conflict_do_nothing()
        )
    else:
        db.execute(
            delete(kind).where(kind.user_id == user.id, kind.post_id == post_id)
        )

    # Read from the rows, after the change and inside its transaction: what
    # is returned is what the database holds, not what this request assumed.
    active, count = db.execute(
        select(_made_by(kind, post_id, user), _count_of(kind, post_id))
    ).one()
    db.commit()
    return InteractionState(active=active, count=count)

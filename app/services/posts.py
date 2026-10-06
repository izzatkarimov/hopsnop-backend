"""Posts: writing, reading, editing and deleting them, and the feed of them.

A reply is a post like any other. It only has ``parent_post_id`` set, and it
follows every rule here in its own right: its own author, its own visibility,
its own edit window.

Nothing in here trusts the client for anything but the text of a post and the
id of the post being replied to. The author is always the authenticated user
passed in, and every timestamp comes from this module's clock.

Like the authentication service, every public function that writes is one
unit of work and commits once, at the end.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import ColumnElement, Select, or_, select
from sqlalchemy.orm import Session, contains_eager

from app.core.pagination import Cursor, Page, paginate
from app.models import Post, User

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


class PrivateAccountError(PostError):
    status_code = 403
    detail = "This account's posts are private."


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- visibility ----------------------------------------------------------
#
# Who may see which posts is decided in this section and nowhere else. Every
# read below, and every check that a post "exists" for someone, goes through
# ``_visible_posts``.

# The rule public profiles use: an account that is deactivated, or whose
# email address was never verified, is not shown, and neither are its posts.
_ACCOUNT_IS_SHOWN = (
    User.is_active.is_(True),
    User.email_verified_at.is_not(None),
)

# An account whose posts are open to everyone.
_ACCOUNT_IS_PUBLIC = User.is_private.is_(False)


def _posts_readable_by(viewer: User | None) -> ColumnElement[bool]:
    """Condition on a ``users`` row: may ``viewer`` read that account's posts?

    ``viewer`` is None for a request that is not authenticated.

    A public account's posts can be read by anyone. A private account's posts
    can, for now, only be read by the account itself. Once following exists,
    its approved followers are added here, and every query picks that up.
    """
    if viewer is None:
        return _ACCOUNT_IS_PUBLIC
    return or_(_ACCOUNT_IS_PUBLIC, User.id == viewer.id)


def _visible_posts(viewer: User | None) -> Select[tuple[Post]]:
    """Every post ``viewer`` may see, each with its author.

    It is the author's account that decides, also for a reply: replying to a
    public post does not make a private account's reply public.

    Of the author, only what a post shows is read from the database.
    """
    return (
        select(Post)
        .join(Post.author)
        .options(
            contains_eager(Post.author).load_only(
                User.username,
                User.display_name,
                User.avatar_url,
            )
        )
        .where(
            Post.deleted_at.is_(None),
            *_ACCOUNT_IS_SHOWN,
            _posts_readable_by(viewer),
        )
    )


# --- reading -------------------------------------------------------------


def get_post(db: Session, post_id: uuid.UUID, viewer: User | None) -> Post:
    """The post with this id, if ``viewer`` may see it.

    A post that does not exist, one that was deleted and one that the viewer
    may not see are all the same "not found": one query that matches nothing,
    so neither the answer nor the work done tells them apart.
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
    found, exactly as for its profile. A private account's profile is public,
    so here the account is acknowledged and reading its posts is refused.
    """
    account = db.execute(
        select(User.id, _posts_readable_by(viewer).label("posts_readable")).where(
            User.username == username,
            *_ACCOUNT_IS_SHOWN,
        )
    ).one_or_none()
    if account is None:
        raise UserNotFoundError
    if not account.posts_readable:
        raise PrivateAccountError

    return paginate(
        db,
        _visible_posts(viewer).where(Post.author_id == account.id),
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
    """One page of the For You feed: public accounts' posts, newest first.

    For now this is not a recommendation. It is every post that anyone may
    read, in the order they were written, with replies in it as the posts
    they are.

    The feed is public content, so a private account's posts are in it for
    nobody, the account itself included. That is a rule of this feed, and it
    is applied on top of what ``viewer`` may see, not instead of it: whatever
    ``_visible_posts`` hides from a viewer is hidden here as well.
    """
    return paginate(
        db,
        _visible_posts(viewer).where(_ACCOUNT_IS_PUBLIC),
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

    A post can be replied to by whoever can see it. A parent that does not
    exist, was deleted, or is hidden from the author is refused with one and
    the same answer.
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
    db.commit()
    return post


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
    return post


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

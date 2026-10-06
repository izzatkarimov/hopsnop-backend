"""Stories: publishing them, reading them while they last, viewing and
deleting them.

A story is shown for 24 hours, to its author and to whoever follows the
author at the moment of asking. Nothing about that is stored per reader: it
is worked out from ``follows`` by every query, so unfollowing ends it at once
and following again brings it back.

Nothing in here trusts the client for anything but the address of the image,
the caption and the id of a story. The author, and whoever views, is always
the authenticated user passed in, and every timestamp comes from this
module's clock.

Like the other services, every public function that writes is one unit of
work and commits once, at the end.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import ColumnElement, Select, case, delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, contains_eager, with_expression

from app.core.pagination import Cursor, Page, paginate
from app.models import Story, StoryView, User
from app.services.users import ACCOUNT_IS_SHOWN, is_followed_by

STORY_LIFETIME_HOURS = 24
# How long after it was created a story is shown. The moment it ends is
# stored with the story (expires_at) when it is created and never moved.
STORY_LIFETIME = timedelta(hours=STORY_LIFETIME_HOURS)
# The only kind of media a story has for now. It is set here, not asked for.
MEDIA_TYPE = "image"


class StoryError(Exception):
    """A failure that is reported to the client as ``detail`` with ``status_code``."""

    status_code = 400
    detail = "The request could not be completed."


class StoryNotFoundError(StoryError):
    status_code = 404
    detail = "Story not found."


class NotStoryAuthorError(StoryError):
    status_code = 403
    detail = "You are not the author of this story."


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- visibility ----------------------------------------------------------
#
# Who may see which stories is decided in this section and nowhere else.
# Every read below, and every check that a story "exists" for someone, goes
# through ``_shown_to``.


def _shown_to(viewer: User, now: datetime) -> tuple[ColumnElement[bool], ...]:
    """Conditions on a ``stories`` row joined to its author: may ``viewer`` see it?

    The story has not expired, its author's account is shown, and ``viewer``
    is the author or follows the author right now. The story ends at
    ``expires_at`` itself: one created at 12:00 is shown until 11:59:59 the
    next day and no longer at 12:00:00.
    """
    return (
        Story.expires_at > now,
        *ACCOUNT_IS_SHOWN,
        or_(Story.author_id == viewer.id, is_followed_by(viewer, Story.author_id)),
    )


def _visible_stories(viewer: User, now: datetime) -> Select[tuple[Story]]:
    """Every story ``viewer`` may see, each with its author and its views.

    Of the author, only what a story shows is read from the database. Of the
    views, only whether ``viewer`` is among them and, for the viewer's own
    stories alone, how many there are: for anyone else's story the count is
    NULL in the query's result, so it never leaves the database. The same
    statement computes both for every story it returns, so a list costs no
    query per story.

    Those two values exist on a story only as it comes out of this query.
    """
    view_count = (
        select(func.count())
        .select_from(StoryView)
        .where(StoryView.story_id == Story.id)
        .scalar_subquery()
    )
    viewed_by_me = exists().where(
        StoryView.story_id == Story.id, StoryView.viewer_id == viewer.id
    )
    return (
        select(Story)
        .join(Story.author)
        .options(
            contains_eager(Story.author).load_only(
                User.username,
                User.display_name,
                User.avatar_url,
            ),
            with_expression(
                Story.view_count,
                case((Story.author_id == viewer.id, view_count), else_=None),
            ),
            with_expression(Story.viewed_by_me, viewed_by_me),
        )
        .where(*_shown_to(viewer, now))
    )


# --- reading -------------------------------------------------------------


def get_story(db: Session, story_id: uuid.UUID, viewer: User) -> Story:
    """The story with this id, if ``viewer`` may see it.

    A story that does not exist, one that has expired, one whose author is
    not shown and one whose author ``viewer`` does not follow are all the
    same "not found": one query that matches nothing, so neither the answer
    nor the work done tells them apart.
    """
    story = db.scalar(_visible_stories(viewer, _now()).where(Story.id == story_id))
    if story is None:
        raise StoryNotFoundError
    return story


def list_stories(
    db: Session,
    viewer: User,
    *,
    limit: int,
    after: Cursor | None = None,
) -> Page[Story]:
    """One page of the stories ``viewer`` may see, newest first.

    Their own and those of the accounts they follow, in the order they were
    published. Whether a story was viewed already changes neither that it is
    here nor where.
    """
    return paginate(
        db,
        _visible_stories(viewer, _now()),
        Story,
        limit=limit,
        after=after,
    )


# --- writing -------------------------------------------------------------


def create_story(
    db: Session,
    author: User,
    *,
    media_url: str,
    caption: str | None = None,
) -> Story:
    """Publish a story by ``author``. It is shown for 24 hours from now."""
    now = _now()
    story = Story(
        author=author,
        media_url=media_url,
        media_type=MEDIA_TYPE,
        caption=caption,
        created_at=now,
        expires_at=now + STORY_LIFETIME,
    )
    db.add(story)
    # Assigns the id, which is needed to read the story back.
    db.flush()
    story_id = story.id
    db.commit()
    return get_story(db, story_id, author)


def delete_story(db: Session, user: User, story_id: uuid.UUID) -> None:
    """Delete one of the user's own stories, for good.

    The row is removed, and the views of the story go with it (ON DELETE
    CASCADE). A story the user cannot see is not found, rather than
    forbidden, so asking to delete a story tells nothing about stories that
    are hidden from the user; an expired story is among those, also for its
    author. Being refused as "not the author" is only possible for a story
    the user can read anyway.
    """
    # The row is locked until the transaction ends, so of two requests
    # deleting the same story at once, the second waits and then finds none.
    author_id = db.scalar(
        select(Story.author_id)
        .join(Story.author)
        .where(Story.id == story_id, *_shown_to(user, _now()))
        .with_for_update(of=Story)
    )
    if author_id is None:
        raise StoryNotFoundError
    if author_id != user.id:
        raise NotStoryAuthorError
    db.execute(delete(Story).where(Story.id == story_id))
    db.commit()


def record_view(db: Session, viewer: User, story_id: uuid.UUID) -> bool:
    """Record that ``viewer`` has viewed the story. Returns whether a view of
    theirs now exists.

    The result is the same however often it is asked for: viewing a story
    that was viewed already leaves the one row there is. An author's look at
    their own story is not a view, so nothing is recorded for it and the
    answer is False.

    Only a story the viewer can see can be viewed. Any other is not found,
    with the answer that reading it gives.
    """
    # Locked against being deleted until the view is written; nothing else
    # is held up by it. Without it a story deleted at this very moment would
    # fail the view's foreign key instead of being "not found".
    author_id = db.scalar(
        select(Story.author_id)
        .join(Story.author)
        .where(Story.id == story_id, *_shown_to(viewer, _now()))
        .with_for_update(read=True, key_share=True, of=Story)
    )
    if author_id is None:
        raise StoryNotFoundError

    if author_id != viewer.id:
        # The primary key (story, viewer) is what rules out a second row.
        # The conflict is left to the database rather than looked for first
        # and avoided, so two requests arriving at once cannot both insert.
        db.execute(
            insert(StoryView)
            .values(story_id=story_id, viewer_id=viewer.id)
            .on_conflict_do_nothing()
        )

    # Read from the rows, after the change and inside its transaction.
    viewed = db.scalar(
        select(
            exists().where(
                StoryView.story_id == story_id, StoryView.viewer_id == viewer.id
            )
        )
    )
    db.commit()
    return viewed

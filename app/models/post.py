from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column, query_expression, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.like import Like
    from app.models.repost import Repost
    from app.models.user import User


class Post(Base):
    """A post, or a reply to one: a reply is a post with ``parent_post_id`` set.

    A row is in one of two states, and the database allows no other
    (``live_or_erased`` below):

    - a post: it has an author and a text, and ``deleted_at`` is NULL;
    - a tombstone: what is left of a post that was deleted while it had
      replies. It has no author and no text, and ``deleted_at`` says when it
      was erased. It keeps its id, the post it answered and its timestamps,
      which is what its replies need to stay where they are in the thread.

    A post that is deleted without replies is not kept at all, and a
    tombstone goes when its last reply does. See ``app.services.erasure``.
    """

    __tablename__ = "posts"
    __table_args__ = (
        CheckConstraint(
            "char_length(content) BETWEEN 1 AND 300",
            name="content_length",
        ),
        # At least one non-whitespace character.
        CheckConstraint(r"content ~ '\S'", name="content_not_blank"),
        CheckConstraint("parent_post_id != id", name="no_self_reply"),
        # The two states of a row, and nothing in between: a deleted post
        # that still has its text, say, or a post without an author.
        CheckConstraint(
            "(deleted_at IS NULL AND author_id IS NOT NULL AND content IS NOT NULL)"
            " OR "
            "(deleted_at IS NOT NULL AND author_id IS NULL AND content IS NULL)",
            name="live_or_erased",
        ),
        # A user's posts, newest first (profile timeline).
        Index("ix_posts_author_id_created_at", "author_id", "created_at"),
        # All posts, newest first (global timeline).
        Index("ix_posts_created_at", "created_at"),
        # The replies to a post, in order. It also serves the foreign key:
        # without it, checking whether a post still has replies would scan the
        # table. Posts that are not replies are left out of the index.
        Index(
            "ix_posts_parent_post_id_created_at",
            "parent_post_id",
            "created_at",
            postgresql_where="parent_post_id IS NOT NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # NULL in a tombstone. RESTRICT all the same: an account is not removed
    # while a post still names it, and nothing detaches a post from its
    # author but erasing it.
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
    )
    # NULL for a post that starts a thread; otherwise the post replied to.
    # RESTRICT, like the author: a post that has replies cannot be removed
    # from the database, so a reply never loses the post it answers.
    parent_post_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("posts.id", ondelete="RESTRICT"),
    )
    # NULL in a tombstone. The two checks above hold for every text there is
    # and say nothing about there being none.
    content: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # When the content was last edited; equal to created_at until then. The
    # application sets it together with an edit and at no other time, so that
    # nothing else done to the row (deleting it, for one) looks like an edit.
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # NULL in a post. In a tombstone, the moment the post was erased.
    deleted_at: Mapped[datetime | None] = mapped_column()

    # How many likes and reposts the post has, and whether the reader of the
    # post is among them. These are not columns and nothing is stored: the
    # query that loads a post for a reader computes them from the likes and
    # reposts tables (``app.services.posts._visible_posts``). On a post that
    # was loaded in any other way they are None.
    like_count: Mapped[int] = query_expression()
    liked_by_me: Mapped[bool] = query_expression()
    repost_count: Mapped[int] = query_expression()
    reposted_by_me: Mapped[bool] = query_expression()

    author: Mapped[User | None] = relationship(back_populates="posts")

    parent: Mapped[Post | None] = relationship(
        remote_side=[id],
        back_populates="replies",
    )
    # passive_deletes="all" keeps the ORM from detaching the replies when
    # their parent is deleted, which would silently turn them into top-level
    # posts. The database refuses the deletion instead.
    replies: Mapped[list[Post]] = relationship(
        back_populates="parent",
        passive_deletes="all",
    )

    # Likes and reposts only exist in relation to a post and go with it.
    likes: Mapped[list[Like]] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    reposts: Mapped[list[Repost]] = relationship(
        back_populates="post",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

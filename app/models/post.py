from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.like import Like
    from app.models.repost import Repost
    from app.models.user import User


class Post(Base):
    """A post, or a reply to one: a reply is a post with ``parent_post_id`` set.

    A post is deleted by setting ``deleted_at``. The row stays, and with it
    the likes, reposts and replies that refer to it.
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
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
    )
    # NULL for a post that starts a thread; otherwise the post replied to.
    # RESTRICT, like the author: a post that has replies cannot be removed
    # from the database, so a reply never loses the post it answers.
    parent_post_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("posts.id", ondelete="RESTRICT"),
    )
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # When the content was last edited; equal to created_at until then. The
    # application sets it together with an edit and at no other time, so that
    # nothing else done to the row (deleting it, for one) looks like an edit.
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # NULL while the post exists for its readers; then the moment it was
    # deleted.
    deleted_at: Mapped[datetime | None] = mapped_column()

    author: Mapped[User] = relationship(back_populates="posts")

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

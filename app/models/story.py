from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.story_view import StoryView
    from app.models.user import User


class Story(Base):
    """A temporary story. It is active while ``expires_at`` is in the future.

    ``expires_at`` has no database default on purpose: the application decides
    the lifetime (normally 24 hours) and sets it explicitly.
    """

    __tablename__ = "stories"
    __table_args__ = (
        CheckConstraint("media_type IN ('image', 'video')", name="media_type_valid"),
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        # A user's currently active stories.
        Index("ix_stories_author_id_expires_at", "author_id", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
    )
    # A reference to the media in external storage, never the file itself.
    media_url: Mapped[str] = mapped_column(String)
    media_type: Mapped[str] = mapped_column(String)
    caption: Mapped[str | None] = mapped_column(String(150))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]

    author: Mapped[User] = relationship(back_populates="stories")

    # View records only exist in relation to a story and go with it.
    views: Mapped[list[StoryView]] = relationship(
        back_populates="story",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

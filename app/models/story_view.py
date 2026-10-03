from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.story import Story
    from app.models.user import User


class StoryView(Base):
    """One row per (story, viewer), however many times the story is opened."""

    __tablename__ = "story_views"
    __table_args__ = (
        # The primary key (story_id, viewer_id) answers "who viewed this
        # story"; this reversed index answers "which stories has this user
        # already seen".
        Index("ix_story_views_viewer_id_story_id", "viewer_id", "story_id"),
    )

    story_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("stories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    viewer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    viewed_at: Mapped[datetime] = mapped_column(server_default=func.now())

    story: Mapped[Story] = relationship(back_populates="views")
    viewer: Mapped[User] = relationship()

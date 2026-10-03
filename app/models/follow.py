from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class Follow(Base):
    """``follower`` follows ``following``."""

    __tablename__ = "follows"
    __table_args__ = (
        CheckConstraint("follower_id != following_id", name="no_self_follow"),
        # The primary key (follower_id, following_id) answers "who do I
        # follow"; this reversed index answers "who follows me".
        Index("ix_follows_following_id_follower_id", "following_id", "follower_id"),
    )

    follower_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    following_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Both foreign keys point at users, so each relationship names its own.
    follower: Mapped[User] = relationship(
        foreign_keys=[follower_id],
        back_populates="following",
    )
    following: Mapped[User] = relationship(
        foreign_keys=[following_id],
        back_populates="followers",
    )

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class UserSession(Base):
    """A login session, stored in the ``sessions`` table.

    The class is not called ``Session`` only to keep it apart from
    SQLAlchemy's own ``Session``.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        # A user's sessions: listing them, revoking the others, revoking all.
        Index("ix_sessions_user_id", "user_id"),
        # Finding expired sessions when they are eventually purged.
        Index("ix_sessions_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
    )
    # SHA-256 hex digest of the token held in the browser's cookie. The raw
    # token is never stored. The unique constraint is also the lookup index.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # Set by the application from the configured lifetime, never by a
    # database default.
    expires_at: Mapped[datetime] = mapped_column()
    last_used_at: Mapped[datetime] = mapped_column()
    # NULL while the session is usable. Logging out sets this rather than
    # deleting the row, so that the session's history is kept.
    revoked_at: Mapped[datetime | None] = mapped_column()

    user: Mapped[User] = relationship(back_populates="sessions")

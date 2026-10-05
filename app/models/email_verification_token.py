from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.user import User


class EmailVerificationToken(Base):
    """A single-use token proving control of the account's email address."""

    __tablename__ = "email_verification_tokens"
    __table_args__ = (
        CheckConstraint("expires_at > created_at", name="expires_after_created"),
        # A user's tokens: replacing the outstanding one when a new one is
        # issued.
        Index("ix_email_verification_tokens_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
    )
    # SHA-256 hex digest of the token in the emailed link. The raw token is
    # never stored. The unique constraint is also the lookup index.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # Set by the application from the configured lifetime.
    expires_at: Mapped[datetime] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    # NULL until the token is redeemed. A token that was superseded by a newer
    # one is deleted instead, so a timestamp here always means "was used".
    used_at: Mapped[datetime | None] = mapped_column()

    user: Mapped[User] = relationship(back_populates="email_verification_tokens")

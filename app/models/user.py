from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, String, func, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.email_verification_token import EmailVerificationToken
    from app.models.follow import Follow
    from app.models.like import Like
    from app.models.password_reset_token import PasswordResetToken
    from app.models.post import Post
    from app.models.repost import Repost
    from app.models.story import Story
    from app.models.user_session import UserSession


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Usernames and emails are normalized to lowercase by the application.
        # These checks make the plain unique constraints case-insensitive in
        # effect: "Alice" and "alice" can never coexist.
        CheckConstraint("username = lower(username)", name="username_lowercase"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(30), unique=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String(50))
    bio: Mapped[str | None] = mapped_column(String(160))
    avatar_url: Mapped[str | None] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(server_default=true())
    # NULL until the address has been confirmed through an emailed token; then
    # the moment it was confirmed.
    email_verified_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Authored content is never deleted as a side effect of deleting a user.
    # The foreign keys are ON DELETE RESTRICT, and passive_deletes="all" keeps
    # the ORM from touching these rows so the database makes that decision.
    # Accounts are deactivated through is_active instead.
    posts: Mapped[list[Post]] = relationship(
        back_populates="author",
        passive_deletes="all",
    )
    stories: Mapped[list[Story]] = relationship(
        back_populates="author",
        passive_deletes="all",
    )

    # Pure association rows. They carry no meaning without both sides, so they
    # are removed with the user (ON DELETE CASCADE in the database).
    likes: Mapped[list[Like]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    reposts: Mapped[list[Repost]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # Follow rows where this user is the follower: the accounts they follow.
    following: Mapped[list[Follow]] = relationship(
        foreign_keys="Follow.follower_id",
        back_populates="follower",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    # Follow rows where this user is being followed: their followers.
    followers: Mapped[list[Follow]] = relationship(
        foreign_keys="Follow.following_id",
        back_populates="following",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # Authentication state. It belongs to the account and is removed with it
    # (ON DELETE CASCADE in the database).
    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    email_verification_tokens: Mapped[list[EmailVerificationToken]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    password_reset_tokens: Mapped[list[PasswordResetToken]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

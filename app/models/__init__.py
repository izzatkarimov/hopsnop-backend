"""SQLAlchemy models.

Importing this package registers every model on ``Base.metadata``, which is
what Alembic autogenerate and the relationship string lookups rely on.
"""

from app.models.email_verification_token import EmailVerificationToken
from app.models.follow import Follow
from app.models.like import Like
from app.models.password_reset_token import PasswordResetToken
from app.models.post import Post
from app.models.repost import Repost
from app.models.story import Story
from app.models.story_view import StoryView
from app.models.user import User
from app.models.user_session import UserSession

__all__ = [
    "EmailVerificationToken",
    "Follow",
    "Like",
    "PasswordResetToken",
    "Post",
    "Repost",
    "Story",
    "StoryView",
    "User",
    "UserSession",
]

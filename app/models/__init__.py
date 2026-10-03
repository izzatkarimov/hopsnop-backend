"""SQLAlchemy models.

Importing this package registers every model on ``Base.metadata``, which is
what Alembic autogenerate and the relationship string lookups rely on.
"""

from app.models.follow import Follow
from app.models.like import Like
from app.models.post import Post
from app.models.repost import Repost
from app.models.story import Story
from app.models.story_view import StoryView
from app.models.user import User

__all__ = [
    "Follow",
    "Like",
    "Post",
    "Repost",
    "Story",
    "StoryView",
    "User",
]

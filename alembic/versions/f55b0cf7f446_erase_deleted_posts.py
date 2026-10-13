"""erase deleted posts

Until now a deleted post was a row with ``deleted_at`` set: its text, its
author, its likes and its reposts all stayed in the database. From here on a
deleted post is erased. If nobody replied to it its row is removed; if
somebody did, the row is kept as a tombstone with no author and no text.

This migration makes room for that in the schema (``author_id`` and
``content`` may be NULL, and a check allows exactly the two states) and
brings the posts that were deleted the old way under the new rule.

Revision ID: f55b0cf7f446
Revises: 7f984c985213
Create Date: 2026-10-09 10:12:41.000000

"""
from typing import Sequence, Union

from alembic import op
from alembic.util import CommandError
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f55b0cf7f446'
down_revision: Union[str, Sequence[str], None] = '7f984c985213'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# A row is a post, or it is what is left of one. Nothing in between.
_LIVE_OR_ERASED = (
    "(deleted_at IS NULL AND author_id IS NOT NULL AND content IS NOT NULL)"
    " OR "
    "(deleted_at IS NOT NULL AND author_id IS NULL AND content IS NULL)"
)


def upgrade() -> None:
    """Upgrade schema."""
    connection = op.get_bind()

    # Posts that were deleted the old way and that nobody has replied to are
    # removed, and their likes and reposts go with them (ON DELETE CASCADE).
    # Removing one can leave the deleted post it answered without a reply,
    # so this is repeated until it finds nothing more.
    remove_unanswered = sa.text(
        "DELETE FROM posts AS post "
        "WHERE post.deleted_at IS NOT NULL "
        "AND NOT EXISTS ("
        "SELECT 1 FROM posts AS reply WHERE reply.parent_post_id = post.id)"
    )
    while connection.execute(remove_unanswered).rowcount:
        pass

    # What is left of them has replies and stays, as a tombstone: without
    # its likes and reposts, without its author and without its text.
    for interactions in ("likes", "reposts"):
        connection.execute(
            sa.text(
                f"DELETE FROM {interactions} WHERE post_id IN "
                "(SELECT id FROM posts WHERE deleted_at IS NOT NULL)"
            )
        )
    op.alter_column('posts', 'author_id', existing_type=sa.Uuid(), nullable=True)
    op.alter_column('posts', 'content', existing_type=sa.Text(), nullable=True)
    connection.execute(
        sa.text(
            "UPDATE posts SET author_id = NULL, content = NULL "
            "WHERE deleted_at IS NOT NULL"
        )
    )

    op.create_check_constraint(
        op.f('ck_posts_live_or_erased'), 'posts', _LIVE_OR_ERASED
    )


def downgrade() -> None:
    """Downgrade schema."""
    # The schema before this one requires every post to have an author and a
    # text. A tombstone has neither, and what it had is gone: erasing is not
    # something a downgrade can take back. So this refuses, and changes
    # nothing, as long as there is one.
    tombstones = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM posts "
            "WHERE deleted_at IS NOT NULL OR author_id IS NULL OR content IS NULL"
        )
    )
    if tombstones:
        raise CommandError(
            f"Cannot downgrade: {tombstones} erased post(s) are kept as "
            "tombstones, without author and text. Erasure cannot be undone, "
            "and the previous schema has no place for them."
        )

    op.drop_constraint(op.f('ck_posts_live_or_erased'), 'posts', type_='check')
    op.alter_column('posts', 'content', existing_type=sa.Text(), nullable=False)
    op.alter_column('posts', 'author_id', existing_type=sa.Uuid(), nullable=False)

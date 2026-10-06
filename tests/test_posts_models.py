"""The posts table as extended for replies and soft deletion.

These run against the migrated schema, so they also show that the migration
created what the model describes.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Like, Post, Repost, User


def reply_to(
    session: Session, parent: Post, author: User, content: str = "Reply"
) -> Post:
    reply = Post(author=author, parent=parent, content=content)
    session.add(reply)
    session.flush()
    return reply


# --- columns -------------------------------------------------------------


def test_new_post_is_neither_a_reply_nor_deleted(session: Session, post: Post) -> None:
    session.refresh(post)

    assert post.parent_post_id is None
    assert post.parent is None
    assert post.replies == []
    assert post.deleted_at is None


def test_new_post_has_equal_created_and_updated_times(
    session: Session, post: Post
) -> None:
    session.refresh(post)

    assert post.updated_at == post.created_at


def test_deleted_at_is_timezone_aware(session: Session, post: Post) -> None:
    post.deleted_at = datetime.now(timezone.utc)
    session.flush()
    session.refresh(post)

    assert post.deleted_at.tzinfo is not None


def test_posts_has_no_separate_deleted_flag_or_stored_edit_deadline() -> None:
    columns = set(Post.__table__.columns.keys())

    assert columns == {
        "id",
        "author_id",
        "parent_post_id",
        "content",
        "created_at",
        "updated_at",
        "deleted_at",
    }


def test_migrated_table_has_the_models_columns(session: Session) -> None:
    migrated = {
        column["name"]: column
        for column in inspect(session.connection()).get_columns("posts")
    }

    assert set(migrated) == set(Post.__table__.columns.keys())
    assert migrated["parent_post_id"]["nullable"] is True
    assert migrated["deleted_at"]["nullable"] is True
    assert migrated["deleted_at"]["type"].timezone is True


def test_updated_at_only_changes_when_the_application_sets_it(
    session: Session, alice: User
) -> None:
    # Marking a post as deleted is not an edit and must not look like one.
    written = datetime.now(timezone.utc) - timedelta(days=3)
    post = Post(author=alice, content="Old", created_at=written, updated_at=written)
    session.add(post)
    session.flush()

    post.deleted_at = datetime.now(timezone.utc)
    session.flush()
    session.refresh(post)

    assert post.updated_at == written


# --- replies -------------------------------------------------------------


def test_reply_is_a_post_that_points_at_its_parent(
    session: Session, bob: User, post: Post
) -> None:
    reply = reply_to(session, post, bob)
    session.refresh(post)

    assert isinstance(reply, Post)
    assert reply.parent_post_id == post.id
    assert reply.parent is post
    assert post.replies == [reply]
    assert reply.author is bob


def test_replies_live_in_the_posts_table(
    session: Session, bob: User, post: Post
) -> None:
    reply_to(session, post, bob)

    assert session.scalar(select(func.count()).select_from(Post)) == 2
    tables = inspect(session.connection()).get_table_names()
    assert "replies" not in tables
    assert "comments" not in tables


def test_reply_can_be_replied_to(
    session: Session, alice: User, bob: User, post: Post
) -> None:
    first = reply_to(session, post, bob, "B")
    second = reply_to(session, first, alice, "C")

    assert second.parent is first
    assert second.parent.parent is post
    assert post.parent is None


def test_post_can_have_several_replies(
    session: Session, alice: User, bob: User, post: Post
) -> None:
    replies = [reply_to(session, post, author) for author in (alice, bob, bob)]
    session.refresh(post)

    assert sorted(post.replies, key=id) == sorted(replies, key=id)


def test_reply_requires_an_existing_parent(session: Session, alice: User) -> None:
    session.add(Post(author=alice, content="Orphan", parent_post_id=uuid.uuid4()))

    with pytest.raises(IntegrityError, match="fk_posts_parent_post_id_posts"):
        session.flush()


def test_post_cannot_be_a_reply_to_itself(session: Session, alice: User) -> None:
    id = uuid.uuid4()
    session.add(Post(id=id, author=alice, content="Loop", parent_post_id=id))

    with pytest.raises(IntegrityError, match="ck_posts_no_self_reply"):
        session.flush()


def test_reply_is_held_to_the_same_content_rules(
    session: Session, bob: User, post: Post
) -> None:
    session.add(Post(author=bob, parent=post, content="x" * 301))

    with pytest.raises(IntegrityError, match="ck_posts_content_length"):
        session.flush()


# --- deletion ------------------------------------------------------------


def test_marking_a_post_as_deleted_keeps_the_row_and_what_refers_to_it(
    session: Session, bob: User, post: Post
) -> None:
    reply = reply_to(session, post, bob)
    session.add_all([Like(user=bob, post=post), Repost(user=bob, post=post)])
    session.flush()

    post.deleted_at = datetime.now(timezone.utc)
    session.flush()
    session.expire_all()

    assert session.get(Post, post.id).content == "Hello, Hopsnop!"
    assert session.get(Post, reply.id).parent_post_id == post.id
    assert session.scalar(select(func.count()).select_from(Like)) == 1
    assert session.scalar(select(func.count()).select_from(Repost)) == 1


def test_post_with_replies_cannot_be_removed_from_the_database(
    session: Session, bob: User, post: Post
) -> None:
    reply_to(session, post, bob)

    session.delete(post)
    with pytest.raises(IntegrityError, match="fk_posts_parent_post_id_posts"):
        session.flush()


def test_removing_a_parent_row_is_refused_by_the_database_itself(
    session: Session, bob: User, post: Post
) -> None:
    # Not only through the ORM: the foreign key is what protects the replies.
    reply = reply_to(session, post, bob)

    with pytest.raises(IntegrityError, match="fk_posts_parent_post_id_posts"):
        session.execute(text("DELETE FROM posts WHERE id = :id"), {"id": post.id})
    session.rollback()

    assert session.get(Post, reply.id) is None  # rolled back with the test data


def test_reply_is_never_detached_from_its_parent_by_the_orm(
    session: Session, bob: User, post: Post
) -> None:
    # Without passive_deletes="all" the ORM would first set the reply's
    # parent_post_id to NULL, and the deletion would then succeed.
    reply = reply_to(session, post, bob)
    session.refresh(post)
    assert post.replies == [reply]

    session.delete(post)
    with pytest.raises(IntegrityError):
        session.flush()


def test_reply_itself_can_be_removed_without_touching_its_parent(
    session: Session, bob: User, post: Post
) -> None:
    reply = reply_to(session, post, bob)

    session.delete(reply)
    session.flush()

    assert session.get(Post, post.id) is post


# --- indexes -------------------------------------------------------------


def test_replies_of_a_post_are_indexed(session: Session) -> None:
    indexes = {
        index["name"]: index
        for index in inspect(session.connection()).get_indexes("posts")
    }

    replies = indexes["ix_posts_parent_post_id_created_at"]
    assert replies["column_names"] == ["parent_post_id", "created_at"]
    # Only replies are in it.
    condition = replies["dialect_options"]["postgresql_where"]
    assert "parent_post_id IS NOT NULL" in condition
    # The indexes that were already there are untouched.
    assert indexes["ix_posts_author_id_created_at"]["column_names"] == [
        "author_id",
        "created_at",
    ]
    assert indexes["ix_posts_created_at"]["column_names"] == ["created_at"]

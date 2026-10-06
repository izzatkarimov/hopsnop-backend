import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Follow, Like, Post, Repost, Story, StoryView, User


# --- users ---------------------------------------------------------------


def test_user_can_be_created(session: Session, alice: User) -> None:
    session.refresh(alice)

    assert isinstance(alice.id, uuid.UUID)
    assert alice.is_active is True
    assert alice.bio is None
    assert alice.created_at.tzinfo is not None
    assert alice.updated_at.tzinfo is not None


def test_username_must_be_unique(session: Session, alice: User) -> None:
    session.add(
        User(
            username="alice",
            email="someone-else@example.com",
            password_hash="not-a-real-hash",
            display_name="Other",
        )
    )
    with pytest.raises(IntegrityError, match="uq_users_username"):
        session.flush()


def test_email_must_be_unique(session: Session, alice: User) -> None:
    session.add(
        User(
            username="someone_else",
            email="alice@example.com",
            password_hash="not-a-real-hash",
            display_name="Other",
        )
    )
    with pytest.raises(IntegrityError, match="uq_users_email"):
        session.flush()


def test_username_must_be_lowercase(session: Session) -> None:
    session.add(
        User(
            username="Alice",
            email="alice@example.com",
            password_hash="not-a-real-hash",
            display_name="Alice",
        )
    )
    with pytest.raises(IntegrityError, match="ck_users_username_lowercase"):
        session.flush()


def test_email_must_be_lowercase(session: Session) -> None:
    session.add(
        User(
            username="alice",
            email="Alice@Example.com",
            password_hash="not-a-real-hash",
            display_name="Alice",
        )
    )
    with pytest.raises(IntegrityError, match="ck_users_email_lowercase"):
        session.flush()


# --- posts ---------------------------------------------------------------


def test_post_can_be_created_for_user(
    session: Session, alice: User, post: Post
) -> None:
    session.refresh(alice)

    assert isinstance(post.id, uuid.UUID)
    assert post.author_id == alice.id
    assert post.author is alice
    assert alice.posts == [post]
    assert post.created_at.tzinfo is not None


def test_post_of_300_characters_is_accepted(session: Session, alice: User) -> None:
    session.add(Post(author=alice, content="x" * 300))
    session.flush()


def test_post_over_300_characters_is_rejected(session: Session, alice: User) -> None:
    session.add(Post(author=alice, content="x" * 301))
    with pytest.raises(IntegrityError, match="ck_posts_content_length"):
        session.flush()


@pytest.mark.parametrize("content", ["", " ", "   ", "\n\t  \n"])
def test_blank_post_is_rejected(session: Session, alice: User, content: str) -> None:
    session.add(Post(author=alice, content=content))
    with pytest.raises(IntegrityError, match="ck_posts_content"):
        session.flush()


def test_post_requires_an_existing_author(session: Session) -> None:
    session.add(Post(author_id=uuid.uuid4(), content="orphan"))
    with pytest.raises(IntegrityError, match="fk_posts_author_id_users"):
        session.flush()


# --- likes ---------------------------------------------------------------


def test_user_can_like_a_post(session: Session, bob: User, post: Post) -> None:
    like = Like(user=bob, post=post)
    session.add(like)
    session.flush()
    session.refresh(post)

    assert post.likes == [like]
    assert bob.likes == [like]
    assert like.created_at.tzinfo is not None


def test_user_cannot_like_the_same_post_twice(
    session: Session, bob: User, post: Post
) -> None:
    session.add(Like(user_id=bob.id, post_id=post.id))
    session.flush()
    session.expunge_all()

    session.add(Like(user_id=bob.id, post_id=post.id))
    with pytest.raises(IntegrityError, match="pk_likes"):
        session.flush()


# --- reposts -------------------------------------------------------------


def test_user_can_repost_a_post(session: Session, bob: User, post: Post) -> None:
    repost = Repost(user=bob, post=post)
    session.add(repost)
    session.flush()
    session.refresh(post)

    assert post.reposts == [repost]
    assert bob.reposts == [repost]


def test_user_cannot_repost_the_same_post_twice(
    session: Session, bob: User, post: Post
) -> None:
    session.add(Repost(user_id=bob.id, post_id=post.id))
    session.flush()
    session.expunge_all()

    session.add(Repost(user_id=bob.id, post_id=post.id))
    with pytest.raises(IntegrityError, match="pk_reposts"):
        session.flush()


# --- follows -------------------------------------------------------------


def test_user_can_follow_another_user(
    session: Session, alice: User, bob: User
) -> None:
    follow = Follow(follower=alice, following=bob)
    session.add(follow)
    session.flush()
    session.refresh(alice)
    session.refresh(bob)

    assert alice.following == [follow]
    assert alice.followers == []
    assert bob.followers == [follow]
    assert bob.following == []


def test_user_cannot_follow_themselves(session: Session, alice: User) -> None:
    session.add(Follow(follower_id=alice.id, following_id=alice.id))
    with pytest.raises(IntegrityError, match="ck_follows_no_self_follow"):
        session.flush()


def test_follow_cannot_be_duplicated(session: Session, alice: User, bob: User) -> None:
    session.add(Follow(follower_id=alice.id, following_id=bob.id))
    session.flush()
    session.expunge_all()

    session.add(Follow(follower_id=alice.id, following_id=bob.id))
    with pytest.raises(IntegrityError, match="pk_follows"):
        session.flush()


def test_follow_is_directional(session: Session, alice: User, bob: User) -> None:
    session.add_all(
        [
            Follow(follower_id=alice.id, following_id=bob.id),
            Follow(follower_id=bob.id, following_id=alice.id),
        ]
    )
    session.flush()


# --- stories -------------------------------------------------------------


def test_story_can_be_created(session: Session, alice: User, story: Story) -> None:
    session.refresh(alice)

    assert isinstance(story.id, uuid.UUID)
    assert story.author is alice
    assert alice.stories == [story]
    assert story.caption is None


def test_story_has_an_expiration_timestamp(session: Session, story: Story) -> None:
    session.refresh(story)

    assert story.expires_at.tzinfo is not None
    assert story.expires_at > story.created_at

    active = session.scalars(
        select(Story).where(Story.expires_at > func.now())
    ).all()
    assert active == [story]


def test_story_requires_an_expiration_timestamp(
    session: Session, alice: User
) -> None:
    session.add(
        Story(
            author=alice,
            media_url="https://media.example.com/stories/2.jpg",
            media_type="image",
        )
    )
    with pytest.raises(IntegrityError, match="expires_at"):
        session.flush()


def test_story_cannot_expire_before_it_is_created(
    session: Session, alice: User
) -> None:
    session.add(
        Story(
            author=alice,
            media_url="https://media.example.com/stories/2.jpg",
            media_type="image",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    with pytest.raises(IntegrityError, match="ck_stories_expires_after_created"):
        session.flush()


def test_story_media_type_must_be_image_or_video(
    session: Session, alice: User
) -> None:
    session.add(
        Story(
            author=alice,
            media_url="https://media.example.com/stories/2.pdf",
            media_type="document",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
    )
    with pytest.raises(IntegrityError, match="ck_stories_media_type_valid"):
        session.flush()


# --- story views ---------------------------------------------------------


def test_user_can_view_a_story(session: Session, bob: User, story: Story) -> None:
    view = StoryView(story=story, viewer=bob)
    session.add(view)
    session.flush()
    session.refresh(story)

    assert story.views == [view]
    assert view.viewer is bob
    assert view.viewed_at.tzinfo is not None


def test_story_view_cannot_be_duplicated(
    session: Session, bob: User, story: Story
) -> None:
    session.add(StoryView(story_id=story.id, viewer_id=bob.id))
    session.flush()
    session.expunge_all()

    session.add(StoryView(story_id=story.id, viewer_id=bob.id))
    with pytest.raises(IntegrityError, match="pk_story_views"):
        session.flush()


# --- deletion behaviour --------------------------------------------------


def test_user_with_posts_cannot_be_deleted(
    session: Session, alice: User, post: Post
) -> None:
    session.delete(alice)
    with pytest.raises(IntegrityError, match="fk_posts_author_id_users"):
        session.flush()


def test_user_with_stories_cannot_be_deleted(
    session: Session, alice: User, story: Story
) -> None:
    session.delete(alice)
    with pytest.raises(IntegrityError, match="fk_stories_author_id_users"):
        session.flush()


def test_deleting_a_post_removes_its_likes_and_reposts(
    session: Session, bob: User, post: Post
) -> None:
    session.add_all([Like(user=bob, post=post), Repost(user=bob, post=post)])
    session.flush()

    session.delete(post)
    session.flush()

    assert session.scalar(select(func.count()).select_from(Like)) == 0
    assert session.scalar(select(func.count()).select_from(Repost)) == 0
    assert session.get(User, bob.id) is bob


def test_deleting_a_user_removes_only_their_association_rows(
    session: Session, alice: User, bob: User, post: Post, story: Story
) -> None:
    session.add_all(
        [
            Like(user_id=bob.id, post_id=post.id),
            Repost(user_id=bob.id, post_id=post.id),
            Follow(follower_id=bob.id, following_id=alice.id),
            Follow(follower_id=alice.id, following_id=bob.id),
            StoryView(story_id=story.id, viewer_id=bob.id),
        ]
    )
    session.flush()
    session.expire_all()

    session.delete(bob)
    session.flush()

    for model in (Like, Repost, Follow, StoryView):
        assert session.scalar(select(func.count()).select_from(model)) == 0
    assert session.get(Post, post.id) is not None
    assert session.get(Story, story.id) is not None
    assert session.get(User, alice.id) is not None

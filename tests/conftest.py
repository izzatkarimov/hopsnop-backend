"""Fixtures for the database/model tests.

The tests run against the database from DATABASE_URL, with the schema created
by ``alembic upgrade head``, so they exercise the real migrated schema. Each
test runs inside a transaction that is rolled back afterwards, so no test data
is ever committed.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.db.session import engine
from app.models import Post, Story, User


@pytest.fixture
def session() -> Iterator[Session]:
    with engine.connect() as connection:
        transaction = connection.begin()
        # The session works in savepoints inside the outer transaction, so a
        # test can provoke an IntegrityError and the rollback stays contained.
        with Session(
            bind=connection,
            join_transaction_mode="create_savepoint",
        ) as session:
            yield session
        transaction.rollback()


def make_user(session: Session, username: str) -> User:
    user = User(
        username=username,
        email=f"{username}@example.com",
        password_hash="not-a-real-hash",
        display_name=username.title(),
    )
    session.add(user)
    session.flush()
    return user


@pytest.fixture
def alice(session: Session) -> User:
    return make_user(session, "alice")


@pytest.fixture
def bob(session: Session) -> User:
    return make_user(session, "bob")


@pytest.fixture
def post(session: Session, alice: User) -> Post:
    post = Post(author=alice, content="Hello, Hopsnop!")
    session.add(post)
    session.flush()
    return post


@pytest.fixture
def story(session: Session, alice: User) -> Story:
    story = Story(
        author=alice,
        media_url="https://media.example.com/stories/1.jpg",
        media_type="image",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    session.add(story)
    session.flush()
    return story

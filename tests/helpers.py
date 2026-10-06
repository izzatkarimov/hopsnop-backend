"""Small helpers shared by the API tests."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.cookies import Morsel, SimpleCookie
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_password
from app.db.session import engine
from app.models import Follow, Post, User

PASSWORD = "correct horse battery staple"
# Hashed once: Argon2 is slow by design, and most tests only need an account
# that can log in.
PASSWORD_HASH = hash_password(PASSWORD)

# Nothing with one of these names may ever appear in a response body.
SENSITIVE_KEYS = {"password", "new_password", "password_hash", "token", "token_hash"}


def add_user(
    session: Session,
    username: str,
    *,
    verified: bool = True,
    active: bool = True,
) -> User:
    """An account whose password is PASSWORD."""
    user = User(
        username=username,
        email=f"{username}@example.com",
        password_hash=PASSWORD_HASH,
        display_name=username.title(),
        is_active=active,
        email_verified_at=datetime.now(timezone.utc) if verified else None,
    )
    session.add(user)
    session.flush()
    return user


def follow(session: Session, follower: User, following: User) -> None:
    """Make one user follow another, directly in the database."""
    session.add(Follow(follower_id=follower.id, following_id=following.id))
    session.flush()


def add_post(
    session: Session,
    author: User,
    content: str = "Hello, Hopsnop!",
    *,
    parent: Post | None = None,
    created_at: datetime | None = None,
    deleted: bool = False,
) -> Post:
    """A post written directly to the database, as if created at ``created_at``."""
    created_at = created_at or datetime.now(timezone.utc)
    post = Post(
        author_id=author.id,
        content=content,
        parent_post_id=parent.id if parent is not None else None,
        created_at=created_at,
        updated_at=created_at,
        deleted_at=created_at if deleted else None,
    )
    session.add(post)
    session.flush()
    return post


def post_columns(session: Session, post_id: object) -> dict[str, object]:
    """Every column of a post's row, as currently stored.

    Read with a plain query, so it shows what is in the database and not what
    an object in the session remembers.
    """
    row = session.execute(select(Post.__table__).where(Post.id == post_id)).one()
    return dict(row._mapping)


def registration(**overrides: object) -> dict:
    return {
        "username": "alice",
        "email": "alice@example.com",
        "password": PASSWORD,
        "display_name": "Alice",
        **overrides,
    }


def log_in(
    client: TestClient,
    identifier: str = "alice",
    password: str = PASSWORD,
) -> Response:
    return client.post(
        "/auth/login",
        json={"identifier": identifier, "password": password},
    )


def session_token(client: TestClient) -> str | None:
    """The raw session token the client currently holds in its cookie jar."""
    return client.cookies.get(settings.session_cookie_name)


def plant_session_cookie(client: TestClient, value: str) -> None:
    """Put a session cookie into the client's jar, as if the browser held it."""
    # The domain and path are the ones the jar records for cookies the test
    # server sets, so a later Set-Cookie replaces this cookie.
    client.cookies.set(
        settings.session_cookie_name,
        value,
        domain="testserver.local",
        path="/",
    )


def set_cookie(response: Response) -> Morsel:
    """The session cookie as set by a response, with its attributes."""
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    return cookie[settings.session_cookie_name]


def token_from(url: str) -> str:
    """The raw token carried by an emailed link."""
    return parse_qs(urlsplit(url).query)["token"][0]


def expire(session: Session, row: object) -> None:
    """Move a session or token into the past, as if its lifetime had run out."""
    now = datetime.now(timezone.utc)
    row.created_at = now - timedelta(hours=2)
    row.expires_at = now - timedelta(hours=1)
    session.flush()


def keys_in(payload: object) -> set[str]:
    """Every key that occurs anywhere in a decoded JSON document."""
    if isinstance(payload, dict):
        found = set(payload)
        for value in payload.values():
            found |= keys_in(value)
        return found
    if isinstance(payload, list):
        return set().union(*(keys_in(item) for item in payload))
    return set()


def columns(session: Session, user: User) -> dict[str, object]:
    """Every column of a user's row, as currently stored."""
    session.refresh(user)
    return {column.key: getattr(user, column.key) for column in User.__table__.columns}


@contextmanager
def recorded_selects() -> Iterator[list[str]]:
    """Collects the SELECT statements sent to the database inside the block."""
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)

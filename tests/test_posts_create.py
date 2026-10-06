"""Creating posts: who may, what text is accepted, and what the server decides."""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Post, User
from helpers import add_user, log_in, plant_session_cookie, post_columns

POST_FIELDS = {
    "id",
    "author",
    "content",
    "parent_post_id",
    "created_at",
    "updated_at",
    "is_reply",
    "like_count",
    "liked_by_me",
    "repost_count",
    "reposted_by_me",
}
AUTHOR_FIELDS = {"id", "username", "display_name", "avatar_url"}
NOT_AUTHENTICATED = {"detail": "Not authenticated."}


def create(client: TestClient, content: object = "Hello Hopsnop!", **extra: object):
    return client.post("/posts", json={"content": content, **extra})


def post_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Post))


# --- creating a post -----------------------------------------------------


def test_authenticated_user_can_create_a_post(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    response = create(alice_client, "Hello Hopsnop!")

    assert response.status_code == 201
    body = response.json()
    assert body["content"] == "Hello Hopsnop!"
    assert body["parent_post_id"] is None
    assert body["is_reply"] is False
    assert body["author"] == {
        "id": str(alice_account.id),
        "username": "alice",
        "display_name": "Alice",
        "avatar_url": None,
    }
    stored = post_columns(session, uuid.UUID(body["id"]))
    assert stored["author_id"] == alice_account.id
    assert stored["content"] == "Hello Hopsnop!"
    assert stored["parent_post_id"] is None
    assert stored["deleted_at"] is None


def test_created_post_contains_exactly_the_intended_fields(
    alice_client: TestClient,
) -> None:
    body = create(alice_client).json()

    assert set(body) == POST_FIELDS
    assert set(body["author"]) == AUTHOR_FIELDS


def test_explicit_null_parent_creates_an_ordinary_post(
    alice_client: TestClient,
) -> None:
    response = create(alice_client, "Hello Hopsnop!", parent_post_id=None)

    assert response.status_code == 201
    assert response.json()["parent_post_id"] is None
    assert response.json()["is_reply"] is False


def test_created_post_can_be_read_back(alice_client: TestClient, make_client) -> None:
    created = create(alice_client).json()

    read = make_client().get(f"/posts/{created['id']}")

    assert read.status_code == 200
    assert read.json() == created


def test_new_post_is_timestamped_by_the_server(
    alice_client: TestClient, session: Session, clock
) -> None:
    body = create(alice_client).json()

    assert datetime.fromisoformat(body["created_at"]) == clock.now
    # Never edited: the two timestamps are the same moment.
    assert body["updated_at"] == body["created_at"]
    stored = post_columns(session, uuid.UUID(body["id"]))
    assert stored["created_at"] == stored["updated_at"] == clock.now
    assert stored["created_at"].tzinfo is not None


def test_each_post_gets_its_own_random_id(alice_client: TestClient) -> None:
    ids = [uuid.UUID(create(alice_client).json()["id"]) for _ in range(5)]

    assert len(set(ids)) == 5
    assert all(id.version == 4 for id in ids)


def test_user_can_create_several_posts(
    alice_client: TestClient, session: Session
) -> None:
    for number in range(3):
        assert create(alice_client, f"Post {number}").status_code == 201

    assert post_count(session) == 3


def test_post_responses_are_not_to_be_cached(alice_client: TestClient) -> None:
    assert create(alice_client).headers["cache-control"] == "no-store"


def test_private_account_can_post(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    alice_account.is_private = True
    session.flush()

    assert create(alice_client).status_code == 201


# --- who may create a post -----------------------------------------------


def test_create_requires_authentication(client: TestClient, session: Session) -> None:
    response = create(client)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert post_count(session) == 0


def test_create_with_a_made_up_session_cookie_is_rejected(
    client: TestClient, session: Session, alice_account: User
) -> None:
    plant_session_cookie(client, "not-a-session-token")

    assert create(client).status_code == 401
    assert post_count(session) == 0


def test_create_after_logout_is_rejected(
    alice_client: TestClient, session: Session
) -> None:
    alice_client.post("/auth/logout")

    assert create(alice_client).status_code == 401
    assert post_count(session) == 0


def test_create_with_a_revoked_session_is_rejected(
    make_client, session: Session, alice_account: User
) -> None:
    kept, revoked = make_client(), make_client()
    log_in(kept)
    log_in(revoked)
    kept.post("/auth/sessions/revoke-others")

    assert create(revoked).status_code == 401
    assert create(kept).status_code == 201
    assert post_count(session) == 1


def test_unverified_user_cannot_create_a_post(
    client: TestClient, session: Session
) -> None:
    # The ordinary case: an unverified account never gets a session.
    add_user(session, "unverified", verified=False)

    assert log_in(client, "unverified").status_code == 403
    response = create(client)

    assert response.status_code == 401
    assert post_count(session) == 0


def test_session_of_an_unverified_account_cannot_create_a_post(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    # Not reachable by logging in. Should a session ever belong to an
    # unverified account all the same, posting is still refused.
    alice_account.email_verified_at = None
    session.flush()

    response = create(alice_client)

    assert response.status_code == 403
    assert response.json() == {"detail": "Email address is not verified."}
    assert post_count(session) == 0


def test_inactive_user_cannot_create_a_post(
    client: TestClient, session: Session
) -> None:
    add_user(session, "inactive", active=False)

    assert log_in(client, "inactive").status_code == 401
    assert create(client).status_code == 401
    assert post_count(session) == 0


def test_user_deactivated_while_logged_in_cannot_create_a_post(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    assert create(alice_client, "Before").status_code == 201

    alice_account.is_active = False
    session.flush()
    response = create(alice_client, "After")

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert post_count(session) == 1


# --- content -------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        "Hello Hopsnop!",
        "a",
        "x" * 300,
        "Two lines\nof text",
        "Tabs\tand   several   spaces",
        "Привет, Hopsnop!",
        "こんにちは",
        "مرحبا",
        "👋",
        "👨‍👩‍👧‍👦 family",
        "0",
        "false",
        "null",
    ],
)
def test_valid_content_is_accepted_and_stored_as_written(
    alice_client: TestClient, session: Session, content: str
) -> None:
    response = create(alice_client, content)

    assert response.status_code == 201
    assert response.json()["content"] == content
    assert post_columns(session, uuid.UUID(response.json()["id"]))["content"] == content


def test_one_character_is_accepted(alice_client: TestClient) -> None:
    response = create(alice_client, "a")

    assert response.status_code == 201
    assert response.json()["content"] == "a"


def test_300_characters_are_accepted(alice_client: TestClient) -> None:
    response = create(alice_client, "x" * 300)

    assert response.status_code == 201
    assert len(response.json()["content"]) == 300


def test_301_characters_are_rejected(
    alice_client: TestClient, session: Session
) -> None:
    response = create(alice_client, "x" * 301)

    assert response.status_code == 422
    assert post_count(session) == 0


@pytest.mark.parametrize("length", [301, 302, 600, 10_000])
def test_too_long_content_is_rejected_rather_than_truncated(
    alice_client: TestClient, session: Session, length: int
) -> None:
    response = create(alice_client, "x" * length)

    assert response.status_code == 422
    # In particular, no post holding the first 300 characters was stored.
    assert post_count(session) == 0


@pytest.mark.parametrize(
    ("character", "bytes_each"),
    [("x", 1), ("é", 2), ("я", 2), ("字", 3), ("😀", 4)],
)
def test_the_limit_counts_characters_not_bytes(
    alice_client: TestClient, session: Session, character: str, bytes_each: int
) -> None:
    assert len(character.encode("utf-8")) == bytes_each

    at_the_limit = create(alice_client, character * 300)
    over_the_limit = create(alice_client, character * 301)

    assert at_the_limit.status_code == 201
    assert at_the_limit.json()["content"] == character * 300
    assert over_the_limit.status_code == 422
    assert post_count(session) == 1


def test_application_and_database_agree_on_the_length_of_a_post(
    alice_client: TestClient, session: Session
) -> None:
    # 300 characters and 1200 bytes. The database constraint counts it the
    # way the request validation does, or this insert would fail.
    body = create(alice_client, "😀" * 300).json()

    stored_length = session.scalar(
        select(func.char_length(Post.content)).where(Post.id == uuid.UUID(body["id"]))
    )
    assert stored_length == 300


def test_empty_content_is_rejected(alice_client: TestClient, session: Session) -> None:
    response = create(alice_client, "")

    assert response.status_code == 422
    assert post_count(session) == 0


@pytest.mark.parametrize(
    "content",
    [
        " ",
        "   ",
        "\t",
        "\n",
        "\r\n",
        " \n\t \r\n ",
        " " * 300,
        " ",  # no-break space
        " ",  # em space
        "　",  # ideographic space
        " ",  # line separator
        "   　 ",
    ],
)
def test_whitespace_only_content_is_rejected(
    alice_client: TestClient, session: Session, content: str
) -> None:
    response = create(alice_client, content)

    assert response.status_code == 422
    assert post_count(session) == 0


def test_surrounding_whitespace_is_not_part_of_the_post(
    alice_client: TestClient,
) -> None:
    response = create(alice_client, "  \n Hello Hopsnop! \t\n")

    assert response.status_code == 201
    assert response.json()["content"] == "Hello Hopsnop!"


def test_length_is_measured_without_the_surrounding_whitespace(
    alice_client: TestClient,
) -> None:
    response = create(alice_client, "  " + "x" * 300 + "\n")

    assert response.status_code == 201
    assert response.json()["content"] == "x" * 300


def test_whitespace_inside_the_text_is_kept(alice_client: TestClient) -> None:
    content = "First line\n\n    indented\tline\nlast  line"

    assert create(alice_client, content).json()["content"] == content


@pytest.mark.parametrize("content", [None, 123, 1.5, True, ["Hello"], {"text": "Hi"}])
def test_content_must_be_a_string(
    alice_client: TestClient, session: Session, content: object
) -> None:
    response = create(alice_client, content)

    assert response.status_code == 422
    assert post_count(session) == 0


def test_content_is_required(alice_client: TestClient, session: Session) -> None:
    for body in ({}, {"parent_post_id": None}, {"text": "Hello"}, []):
        assert alice_client.post("/posts", json=body).status_code == 422

    assert post_count(session) == 0


def test_null_character_is_rejected(alice_client: TestClient, session: Session) -> None:
    # PostgreSQL cannot store it; without the check this would be a 500.
    response = create(alice_client, "Hello\x00Hopsnop")

    assert response.status_code == 422
    assert post_count(session) == 0


def test_text_that_is_not_valid_unicode_is_rejected(
    alice_client: TestClient, session: Session
) -> None:
    # A lone surrogate: representable in JSON, but not a character.
    response = alice_client.post(
        "/posts",
        content='{"content": "broken \\ud800 text"}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert post_count(session) == 0


def test_validation_error_does_not_echo_the_content(alice_client: TestClient) -> None:
    response = create(alice_client, "SECRET-DRAFT " + "x" * 300)

    assert response.status_code == 422
    assert "SECRET-DRAFT" not in response.text
    assert response.json()["detail"][0]["loc"] == ["body", "content"]


def test_body_must_be_declared_as_json(
    alice_client: TestClient, session: Session
) -> None:
    # A cross-site HTML form can send JSON-looking text, but only with a
    # form content type.
    response = alice_client.post(
        "/posts",
        content=json.dumps({"content": "Hello Hopsnop!"}),
        headers={"Content-Type": "text/plain"},
    )

    assert response.status_code == 422
    assert post_count(session) == 0


@pytest.mark.parametrize(
    "content",
    [
        "<script>alert(1)</script>",
        '<img src=x onerror="alert(1)">',
        "{{7*7}} ${7*7} <%= 7*7 %>",
        "'; DROP TABLE posts; --",
        "' OR '1'='1",
        "%s %(x)s",
        "@alice #hopsnop https://example.com/?a=1&b=2",
    ],
)
def test_content_is_plain_text_that_nothing_interprets(
    alice_client: TestClient, session: Session, content: str
) -> None:
    # Stored exactly as written and served as a JSON string in a JSON
    # response, never as HTML. Escaping it for display is the client's job.
    response = create(alice_client, content)

    assert response.status_code == 201
    assert response.headers["content-type"] == "application/json"
    assert response.json()["content"] == content
    assert post_columns(session, uuid.UUID(response.json()["id"]))["content"] == content
    assert post_count(session) == 1


# --- what the server decides ---------------------------------------------


def test_author_is_the_authenticated_user(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    body = create(alice_client).json()

    assert body["author"]["id"] == str(alice_account.id)
    assert post_columns(session, uuid.UUID(body["id"]))["author_id"] == alice_account.id


def test_client_cannot_post_as_another_user(
    alice_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    # Every way a client might try to name someone else as the author.
    response = alice_client.post(
        f"/posts?author_id={bob_account.id}&user_id={bob_account.id}&username=bob",
        json={
            "content": "Bob never wrote this",
            "author_id": str(bob_account.id),
            "user_id": str(bob_account.id),
            "username": "bob",
            "author": {"id": str(bob_account.id), "username": "bob"},
        },
    )

    assert response.status_code == 201
    assert response.json()["author"]["username"] == "alice"
    authors = session.scalars(select(Post.author_id)).all()
    assert authors == [alice_account.id]


def test_each_user_posts_as_themselves(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    by_alice = create(alice_client, "From Alice").json()
    by_bob = create(bob_client, "From Bob").json()

    assert by_alice["author"]["username"] == "alice"
    assert by_bob["author"]["username"] == "bob"
    stored_authors = [
        post_columns(session, uuid.UUID(post["id"]))["author_id"]
        for post in (by_alice, by_bob)
    ]
    assert stored_authors == [alice_account.id, bob_account.id]


def test_client_cannot_set_the_fields_the_server_controls(
    alice_client: TestClient, session: Session, clock
) -> None:
    chosen_id = str(uuid.uuid4())
    long_ago = "2000-01-01T00:00:00Z"

    response = create(
        alice_client,
        "Hello Hopsnop!",
        id=chosen_id,
        created_at=long_ago,
        updated_at=long_ago,
        deleted_at=long_ago,
        is_reply=True,
        editable_until="2099-01-01T00:00:00Z",
        is_private=False,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"] != chosen_id
    assert body["is_reply"] is False
    assert set(body) == POST_FIELDS
    stored = post_columns(session, uuid.UUID(body["id"]))
    assert stored["created_at"] == stored["updated_at"] == clock.now
    assert stored["deleted_at"] is None
    assert stored["parent_post_id"] is None


def test_client_cannot_backdate_a_post_to_reopen_or_extend_its_edit_window(
    alice_client: TestClient, clock
) -> None:
    future = (clock.now + timedelta(days=365)).isoformat()
    post = create(alice_client, "Hello Hopsnop!", created_at=future).json()

    clock.advance(minutes=60)
    response = alice_client.patch(
        f"/posts/{post['id']}", json={"content": "Too late", "created_at": future}
    )

    assert response.status_code == 409


def test_timestamps_are_timezone_aware_utc(alice_client: TestClient) -> None:
    body = create(alice_client).json()

    created_at = datetime.fromisoformat(body["created_at"])
    assert created_at.tzinfo is not None
    assert created_at.utcoffset() == timedelta(0)
    assert abs(datetime.now(timezone.utc) - created_at) < timedelta(minutes=1)

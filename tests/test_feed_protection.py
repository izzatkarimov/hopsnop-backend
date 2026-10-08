"""Security properties of the feed.

What it gives away about the posts that are not in it, what no response may
ever contain, and that reading is all that can be done with it.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.main import app
from app.models import (
    EmailVerificationToken,
    PasswordResetToken,
    Post,
    User,
    UserSession,
)
from app.services import posts as posts_service
from helpers import (
    SENSITIVE_KEYS,
    add_post,
    add_user,
    columns,
    keys_in,
    log_in,
    post_columns,
    registration,
    token_from,
)

FOREIGN_ORIGIN = "https://evil.example"
START = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)
# Every key a feed response is made of.
FEED_KEYS = {
    "items",
    "next_cursor",
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
    "username",
    "display_name",
    "avatar_url",
}
# Account and session data that must not travel with the feed.
NEVER_IN_THE_FEED = {
    "email",
    "email_verified_at",
    "is_active",
    "is_private",
    "bio",
    "password_hash",
    "author_id",
    "deleted_at",
    "session_id",
    "expires_at",
    "last_used_at",
}


def all_posts(session: Session) -> dict[uuid.UUID, dict[str, object]]:
    """Every row of the posts table, as currently stored."""
    return {
        id: post_columns(session, id) for id in session.scalars(select(Post.id)).all()
    }


def add_hidden_posts(session: Session, *, start: datetime = START) -> list[Post]:
    """One post for each reason a post is kept out of the feed.

    Their text starts with "Hidden" and their authors are named "hidden_...".
    """
    authors = {
        "inactive": add_user(session, "hidden_inactive", active=False),
        "unverified": add_user(session, "hidden_unverified", verified=False),
        "deleted": add_user(session, "hidden_deleted"),
    }
    return [
        add_post(
            session,
            author,
            f"Hidden: {kind}",
            created_at=start + number * MINUTE + timedelta(seconds=30),
            deleted=kind == "deleted",
        )
        for number, (kind, author) in enumerate(authors.items())
    ]


def walk(client: TestClient, *, limit: int) -> list:
    """The responses for every page of the feed, first to last."""
    responses = []
    cursor = None
    while True:
        params = {"limit": limit, **({"cursor": cursor} if cursor else {})}
        responses.append(client.get("/feed", params=params))
        assert responses[-1].status_code == 200, responses[-1].text
        cursor = responses[-1].json()["next_cursor"]
        if cursor is None:
            return responses
        assert len(responses) < 200, "pagination does not terminate"


# --- the surface ---------------------------------------------------------


def test_the_only_feed_routes_are_the_intended_ones() -> None:
    routes = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if "feed" in path or "timeline" in path
        for method in operations
    }

    # For You and Following, and both can only be read.
    assert routes == {("GET", "/feed"), ("GET", "/feed/following")}


@pytest.mark.parametrize(
    "path",
    [
        "/feed/followers",
        "/feed/for-you",
        "/feed/home",
        "/feed/alice",
        "/feed/following/alice",
        "/feeds",
    ],
)
def test_there_is_no_other_feed(
    alice_client: TestClient, session: Session, alice_account: User, path: str
) -> None:
    add_post(session, alice_account)

    assert alice_client.get(path).status_code == 404


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_feed_can_only_be_read(
    alice_client: TestClient, session: Session, alice_account: User, method: str
) -> None:
    add_post(session, alice_account)
    before = all_posts(session)

    response = alice_client.request(method, "/feed", json={"content": "Planted"})

    assert response.status_code == 405
    assert all_posts(session) == before


@pytest.mark.parametrize(
    "query",
    [
        "include_private=true",
        "include_deleted=true&deleted=1",
        "private=1&is_private=true",
        "all=1&visibility=all",
        "is_active=false&verified=false",
        "author=hidden_inactive&username=hidden_inactive",
        "filter=none&scope=everything",
    ],
)
def test_no_parameter_widens_the_feed(
    alice_client: TestClient, session: Session, alice_account: User, query: str
) -> None:
    add_post(session, alice_account, "Shown")
    add_hidden_posts(session)

    plain = alice_client.get("/feed")
    asked = alice_client.get(f"/feed?{query}")

    assert asked.status_code == 200
    assert asked.json() == plain.json()
    assert len(asked.json()["items"]) == 1
    assert "Hidden" not in asked.text


# --- posts that are not in the feed --------------------------------------


def test_hidden_posts_leave_no_trace_in_any_response(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    for number in range(4):
        author = (alice_account, bob_account)[number % 2]
        at = START + number * MINUTE
        add_post(session, author, f"Shown {number}", created_at=at)
    hidden = add_hidden_posts(session)
    # The author of a hidden post is shown it no more than anyone else.
    bob, its_author = make_client(), make_client()
    log_in(bob, "bob")
    assert log_in(its_author, "hidden_deleted").status_code == 200

    for viewer in (make_client(), bob, its_author):
        for limit in (1, 3, 50):
            responses = walk(viewer, limit=limit)
            assert sum(len(response.json()["items"]) for response in responses) == 4
            for response in responses:
                # Neither their text, nor their ids, nor who wrote them.
                assert "Hidden" not in response.text
                assert "hidden_" not in response.text
                for post in hidden:
                    assert str(post.id) not in response.text
                    assert str(post.author_id) not in response.text


def test_feed_is_the_same_with_and_without_hidden_posts(
    client: TestClient, session: Session, alice_account: User
) -> None:
    for number in range(4):
        at = START + number * MINUTE
        add_post(session, alice_account, f"Shown {number}", created_at=at)
    without = walk(client, limit=2)

    # Hidden posts between the shown ones, and more of them after the last.
    add_hidden_posts(session)
    for number in range(3):
        add_post(
            session,
            alice_account,
            "Hidden: older",
            created_at=START - (number + 1) * MINUTE,
            deleted=True,
        )
    with_hidden = walk(client, limit=2)

    # Not the bodies, not the cursors, not the number of pages and not the
    # headers say that anything was left out.
    assert len(with_hidden) == len(without) == 2
    for before, after in zip(without, with_hidden):
        assert after.text == before.text
        assert dict(after.headers) == dict(before.headers)


def test_author_is_not_shown_their_own_deleted_posts_or_those_of_another(
    make_client, session: Session
) -> None:
    first = add_user(session, "first_author")
    second = add_user(session, "second_author")
    add_post(session, first, "Hidden: first", deleted=True)
    add_post(session, second, "Hidden: second", deleted=True)
    viewer = make_client()
    log_in(viewer, "first_author")

    response = viewer.get("/feed")

    assert response.json() == {"items": [], "next_cursor": None}


# --- what responses may contain ------------------------------------------


def test_feed_response_is_made_of_the_intended_keys_and_no_others(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    post = add_post(session, alice_account, "Hello Hopsnop!", created_at=START)
    add_post(session, bob_account, "Reply", parent=post, created_at=START + MINUTE)
    bob = make_client()
    log_in(bob, "bob")

    for viewer in (make_client(), bob):
        for response in walk(viewer, limit=1):
            found = keys_in(response.json())
            assert found == FEED_KEYS
            assert found.isdisjoint(SENSITIVE_KEYS | NEVER_IN_THE_FEED)


def test_feed_exposes_no_account_data(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    alice_account.bio = "Something only the profile shows"
    add_post(session, alice_account, "Hello Hopsnop!")
    alice, bob = make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")

    for viewer in (make_client(), alice, bob):
        response = viewer.get("/feed")

        assert len(response.json()["items"]) == 1
        assert alice_account.email not in response.text
        assert "@example.com" not in response.text
        assert alice_account.password_hash not in response.text
        assert "argon2" not in response.text
        assert "Something only the profile shows" not in response.text


def test_feed_shows_the_author_no_more_than_anyone_else(
    alice_client: TestClient, make_client, session: Session, alice_account: User
) -> None:
    add_post(session, alice_account, "Hello Hopsnop!")

    own = alice_client.get("/feed")
    anonymous = make_client().get("/feed")

    assert own.json() == anonymous.json()
    assert keys_in(own.json()) == FEED_KEYS


def test_no_feed_response_contains_a_secret(
    make_client, session: Session, alice_account: User, bob_account: User, outbox
) -> None:
    """Asks for the feed in every way and inspects everything that came back."""
    alice, bob, anonymous = make_client(), make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")
    # A verification token and a password reset token are outstanding.
    registered = anonymous.post(
        "/auth/register",
        json=registration(username="carol", email="carol@example.com"),
    )
    assert registered.status_code == 201
    anonymous.post("/auth/forgot-password", json={"email": "alice@example.com"})
    verification_token = token_from(outbox.verification[0][1])
    reset_token = token_from(outbox.password_reset[0][1])
    carol = session.scalars(select(User).where(User.username == "carol")).one()

    post = add_post(session, alice_account, "Hello Hopsnop!", created_at=START)
    add_post(session, bob_account, "Reply", parent=post, created_at=START + MINUTE)
    add_post(session, carol, "Hidden: not verified yet")
    add_hidden_posts(session)
    session_tokens = [
        client.cookies.get("hopsnop_session") for client in (alice, bob)
    ]

    responses = []
    for viewer in (anonymous, alice, bob):
        first = viewer.get("/feed?limit=1")
        responses += [
            first,
            viewer.get("/feed"),
            viewer.get(f"/feed?limit=1&cursor={first.json()['next_cursor']}"),
            viewer.get("/feed?cursor=abc"),  # 400
            viewer.get("/feed?limit=0"),  # 422
            # A secret sent in is not sent back either.
            viewer.get(f"/feed?cursor={session_tokens[0]}"),  # 400
            viewer.get(f"/feed?limit={reset_token}"),  # 422
            viewer.post("/feed", json={"content": "x"}),  # 405
        ]

    assert {response.status_code for response in responses} == {200, 400, 405, 422}

    token_hashes = [
        *session.scalars(select(UserSession.token_hash)),
        *session.scalars(select(EmailVerificationToken.token_hash)),
        *session.scalars(select(PasswordResetToken.token_hash)),
    ]
    session_ids = [str(id) for id in session.scalars(select(UserSession.id))]
    secrets = [
        *session_tokens,
        verification_token,
        reset_token,
        *token_hashes,
        *session_ids,
        alice_account.password_hash,
        carol.password_hash,
        alice_account.email,
        bob_account.email,
        carol.email,
    ]
    assert len(token_hashes) == 4
    assert all(secrets)

    for response in responses:
        if response.status_code == 200:
            assert keys_in(response.json()) == FEED_KEYS, response.url
        assert keys_in(response.json()).isdisjoint(
            SENSITIVE_KEYS | NEVER_IN_THE_FEED
        ), response.url
        for secret in secrets:
            assert secret not in response.text, response.url
        assert "Hidden" not in response.text, response.url
        assert "set-cookie" not in response.headers, response.url
        for name, value in response.headers.items():
            for secret in secrets:
                assert secret not in value, (response.url, name)


# --- reading and nothing else --------------------------------------------


def test_reading_the_feed_changes_nothing(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    post = add_post(session, alice_account, created_at=START)
    add_post(session, bob_account, "Reply", parent=post, created_at=START + MINUTE)
    add_post(session, alice_account, "Removed", deleted=True)
    bob = make_client()
    log_in(bob, "bob")
    posts_before = all_posts(session)
    users_before = [columns(session, user) for user in (alice_account, bob_account)]

    for viewer in (make_client(), bob):
        cursor = viewer.get("/feed?limit=1").json()["next_cursor"]
        viewer.get("/feed")
        viewer.get(f"/feed?limit=1&cursor={cursor}")
        viewer.get("/feed?cursor=abc")

    assert all_posts(session) == posts_before
    users_after = [columns(session, user) for user in (alice_account, bob_account)]
    assert users_after == users_before


def test_cross_site_read_gets_no_cors_permission(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    add_post(session, alice_account)

    response = alice_client.get("/feed", headers={"Origin": FOREIGN_ORIGIN})

    # The browser will not let the other site's page read this.
    assert "access-control-allow-origin" not in response.headers


def test_feed_gives_the_viewer_no_cookie(
    make_client, session: Session, alice_account: User
) -> None:
    add_post(session, alice_account)
    alice = make_client()
    log_in(alice, "alice")

    for viewer in (make_client(), alice):
        assert "set-cookie" not in viewer.get("/feed").headers


# --- errors --------------------------------------------------------------


def test_every_feed_error_has_the_same_shape(alice_client: TestClient) -> None:
    errors = [
        alice_client.get("/feed?cursor=abc"),  # 400
        alice_client.get("/feed?limit=0"),  # 422
        alice_client.get("/feed?limit=51"),  # 422
        alice_client.post("/feed", json={}),  # 405
        alice_client.get("/feed/home"),  # 404
    ]

    assert [response.status_code for response in errors] == [400, 422, 422, 405, 404]
    for response in errors:
        assert set(response.json()) == {"detail"}
        assert response.headers["content-type"] == "application/json"


def test_error_details_name_no_internals(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    add_post(session, alice_account)

    responses = [
        alice_client.get("/feed?cursor=abc"),
        alice_client.get("/feed?cursor=' OR '1'='1"),
        alice_client.get("/feed?limit=0"),
        alice_client.get("/feed?limit=abc"),
    ]

    for response in responses:
        text = response.text.lower()
        for internal in ("sql", "select", "traceback", "posts.", "users.", "uuid("):
            assert internal not in text


def test_unexpected_error_reveals_nothing_about_itself(
    make_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("connection to postgresql://hopsnop:hopsnop@db failed")

    monkeypatch.setattr(posts_service, "list_for_you_feed", fail)
    client = make_client(raise_server_exceptions=False)

    response = client.get("/feed")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    assert "postgresql" not in response.text

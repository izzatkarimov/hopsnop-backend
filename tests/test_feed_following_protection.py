"""Security properties of the Following feed.

Whose feed a request gets, what it gives away about posts that are not in it,
what no response may ever contain, and that reading is all that can be done
with it.
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
    Follow,
    Like,
    PasswordResetToken,
    Post,
    Repost,
    User,
    UserSession,
)
from app.services import posts as posts_service
from helpers import (
    SENSITIVE_KEYS,
    add_post,
    add_user,
    columns,
    follow,
    keys_in,
    log_in,
    post_columns,
    registration,
    token_from,
)

FOREIGN_ORIGIN = "https://evil.example"
START = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)
EMPTY = {"items": [], "next_cursor": None}
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
# Account, session and follow data that must not travel with the feed.
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
    "follower_id",
    "following_id",
    "following",
    "followers_count",
    "following_count",
}


def state(session: Session) -> dict[str, object]:
    """Everything reading the feed could conceivably change, as stored."""
    return {
        "posts": {
            id: post_columns(session, id)
            for id in session.scalars(select(Post.id)).all()
        },
        "follows": set(
            session.execute(
                select(Follow.follower_id, Follow.following_id, Follow.created_at)
            ).all()
        ),
        "likes": set(session.execute(select(Like.user_id, Like.post_id)).all()),
        "reposts": set(session.execute(select(Repost.user_id, Repost.post_id)).all()),
    }


@pytest.fixture
def carol(session: Session) -> User:
    return add_user(session, "carol")


@pytest.fixture
def network(
    session: Session, alice_account: User, bob_account: User, carol: User
) -> dict[str, Post]:
    """Bob follows alice and not carol. Each has written one post."""
    follow(session, bob_account, alice_account)
    return {
        "alice": add_post(session, alice_account, "By alice", created_at=START),
        "carol": add_post(
            session, carol, "Not followed", created_at=START + MINUTE
        ),
        "bob": add_post(session, bob_account, "Own", created_at=START + 2 * MINUTE),
    }


# --- the surface ---------------------------------------------------------


def test_following_feed_can_only_be_read() -> None:
    operations = app.openapi()["paths"]["/feed/following"]

    assert set(operations) == {"get"}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_other_methods_are_refused_and_change_nothing(
    bob_client: TestClient, session: Session, network: dict, method: str
) -> None:
    before = state(session)

    response = bob_client.request(
        method, "/feed/following", json={"content": "Planted", "username": "carol"}
    )

    assert response.status_code == 405
    assert state(session) == before


@pytest.mark.parametrize(
    "path",
    [
        "/feed/following/alice",
        "/feed/following/carol",
        "/feed/followers",
        "/feed/home",
        "/feed/alice",
        "/users/alice/feed",
        "/users/bob/feed/following",
        "/feeds",
    ],
)
def test_there_is_no_way_to_address_someone_elses_following_feed(
    bob_client: TestClient, network: dict, path: str
) -> None:
    response = bob_client.get(path)

    assert response.status_code == 404
    assert "By alice" not in response.text


def test_feed_takes_no_parameter_that_names_a_user() -> None:
    operation = app.openapi()["paths"]["/feed/following"]["get"]

    assert {param["name"] for param in operation["parameters"]} == {"limit", "cursor"}
    assert all(param["in"] == "query" for param in operation["parameters"])


# --- whose feed it is ----------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "user_id={carol}",
        "username=carol&user=carol",
        "viewer={carol}&viewer_id={carol}&as=carol",
        "follower_id={carol}&follower=carol",
        "following_id={carol}&author_id={carol}&author=carol",
        "include_all=true&all=1&scope=everything",
        "following=false&followed=0&filter=none",
        "include_self=true&include_own=1&me=true",
        "include_deleted=true&is_active=false&verified=false",
        "feed=for_you&type=for-you&tab=foryou",
    ],
)
def test_no_parameter_changes_whose_feed_it_is_or_what_is_in_it(
    bob_client: TestClient,
    session: Session,
    network: dict,
    alice_account: User,
    bob_account: User,
    carol: User,
    query: str,
) -> None:
    # Carol has a Following feed of her own, with other posts in it.
    follow(session, carol, bob_account)
    add_post(session, add_user(session, "gone", active=False), "Hidden")
    add_post(session, alice_account, "Hidden", deleted=True)
    query = query.format(carol=carol.id)

    plain = bob_client.get("/feed/following")
    asked = bob_client.get(f"/feed/following?{query}")

    assert asked.status_code == 200
    assert asked.json() == plain.json()
    assert [item["content"] for item in asked.json()["items"]] == ["By alice"]


def test_viewer_cannot_be_named_by_a_header_or_a_body(
    bob_client: TestClient, network: dict, carol: User
) -> None:
    plain = bob_client.get("/feed/following").json()

    asked = bob_client.request(
        "GET",
        "/feed/following",
        headers={
            "X-User-Id": str(carol.id),
            "X-Forwarded-User": "carol",
            "Authorization": f"Bearer {carol.id}",
        },
        json={"user_id": str(carol.id), "username": "carol"},
    )

    assert asked.status_code == 200
    assert asked.json() == plain


def test_by_me_is_decided_by_the_session_and_nothing_else(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    network: dict,
    alice_account: User,
    carol: User,
) -> None:
    follow(session, alice_account, carol)
    follow(session, add_user(session, "dave"), carol)
    alice_client.post(f"/posts/{network['alice'].id}/like")

    response = bob_client.get(
        f"/feed/following?viewer_id={alice_account.id}&user_id={alice_account.id}"
    )

    [item] = response.json()["items"]
    assert (item["like_count"], item["liked_by_me"]) == (1, False)


def test_each_session_gets_its_own_feed_from_the_same_request(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    network: dict,
    alice_account: User,
    carol: User,
) -> None:
    follow(session, alice_account, carol)

    for_alice = alice_client.get("/feed/following").json()["items"]
    for_bob = bob_client.get("/feed/following").json()["items"]

    assert [item["content"] for item in for_alice] == ["Not followed"]
    assert [item["content"] for item in for_bob] == ["By alice"]


def test_anonymous_request_learns_nothing(
    client: TestClient, network: dict, carol: User
) -> None:
    for path in (
        "/feed/following",
        f"/feed/following?user_id={carol.id}&username=bob",
        "/feed/following?cursor=abc",
        "/feed/following?limit=0",
    ):
        response = client.get(path)

        assert response.status_code == 401, path
        assert response.json() == {"detail": "Not authenticated."}


def test_service_gives_no_one_a_feed_without_a_viewer(
    session: Session, network: dict
) -> None:
    # Not reachable through the API, which always passes the signed-in user.
    # Should it ever be called without one, nobody's posts are the answer.
    page = posts_service.list_following_feed(session, None, limit=50)

    assert list(page.items) == []
    assert page.next_cursor is None


# --- what it gives away --------------------------------------------------


def test_posts_that_are_not_in_it_leave_no_trace(
    bob_client: TestClient, session: Session, network: dict, carol: User
) -> None:
    with_them = [
        bob_client.get("/feed/following"),
        bob_client.get("/feed/following?limit=1"),
    ]
    for response in with_them:
        assert "Not followed" not in response.text
        assert "Own" not in response.text
        assert str(carol.id) not in response.text
        assert str(network["carol"].id) not in response.text
        assert response.json()["next_cursor"] is None


def test_feed_is_the_same_with_and_without_posts_it_does_not_show(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, bob_account, alice_account)
    add_post(session, alice_account, "By alice", created_at=START)
    alone = bob_client.get("/feed/following")

    add_post(session, carol, "Not followed")
    add_post(session, bob_account, "Own")
    add_post(session, alice_account, "Hidden", deleted=True)
    beside = bob_client.get("/feed/following")

    assert beside.json() == alone.json()
    assert beside.headers["content-length"] == alone.headers["content-length"]


def test_response_is_made_of_the_intended_keys_and_no_others(
    bob_client: TestClient, session: Session, network: dict, bob_account: User
) -> None:
    add_post(session, network["alice"].author, "Reply", parent=network["carol"])

    body = bob_client.get("/feed/following").json()

    assert len(body["items"]) == 2
    assert keys_in(body) == FEED_KEYS
    assert keys_in(body).isdisjoint(SENSITIVE_KEYS | NEVER_IN_THE_FEED)


def test_no_following_feed_response_contains_a_secret(
    make_client,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
    outbox,
) -> None:
    """Asks for the feed in every way and inspects everything that came back."""
    alice, bob, anonymous = make_client(), make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")
    # A verification token and a password reset token are outstanding.
    registered = anonymous.post(
        "/auth/register",
        json=registration(username="erin", email="erin@example.com"),
    )
    assert registered.status_code == 201
    anonymous.post("/auth/forgot-password", json={"email": "alice@example.com"})
    verification_token = token_from(outbox.verification[0][1])
    reset_token = token_from(outbox.password_reset[0][1])
    erin = session.scalars(select(User).where(User.username == "erin")).one()

    follow(session, bob_account, alice_account)
    follow(session, alice_account, bob_account)
    follow(session, bob_account, erin)
    post = add_post(session, alice_account, "Hello Hopsnop!", created_at=START)
    add_post(session, alice_account, "Again", created_at=START + 2 * MINUTE)
    add_post(session, bob_account, "Reply", parent=post, created_at=START + MINUTE)
    add_post(session, erin, "Hidden: not verified yet")
    add_post(session, carol, "Hidden: not followed")
    add_post(session, alice_account, "Hidden: deleted", deleted=True)
    session_tokens = [
        client.cookies.get("__Host-hopsnop_session") for client in (alice, bob)
    ]

    responses = []
    for viewer in (anonymous, alice, bob):
        first = viewer.get("/feed/following?limit=1")
        cursor = first.json().get("next_cursor")
        responses += [
            first,
            viewer.get("/feed/following"),
            viewer.get(f"/feed/following?limit=1&cursor={cursor}"),
            viewer.get("/feed/following?cursor=abc"),
            viewer.get("/feed/following?limit=0"),
            # A secret sent in is not sent back either.
            viewer.get(f"/feed/following?cursor={session_tokens[0]}"),
            viewer.get(f"/feed/following?limit={reset_token}"),
            viewer.get(f"/feed/following?user_id={alice_account.id}"),
            viewer.post("/feed/following", json={"content": "x"}),
        ]

    assert {response.status_code for response in responses} == {
        200,
        400,
        401,
        405,
        422,
    }

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
        erin.password_hash,
        alice_account.email,
        bob_account.email,
        erin.email,
    ]
    assert len(token_hashes) == 4
    assert all(secrets)

    for response in responses:
        if response.status_code == 200:
            assert keys_in(response.json()) <= FEED_KEYS, response.url
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
    bob_client: TestClient,
    session: Session,
    network: dict,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    add_post(session, alice_account, "More", created_at=START - MINUTE)
    bob_client.post(f"/posts/{network['alice'].id}/like")
    before = state(session)
    users_before = [
        columns(session, user) for user in (alice_account, bob_account, carol)
    ]

    cursor = bob_client.get("/feed/following?limit=1").json()["next_cursor"]
    bob_client.get("/feed/following")
    bob_client.get(f"/feed/following?limit=1&cursor={cursor}")
    bob_client.get("/feed/following?cursor=abc")
    bob_client.get(f"/feed/following?user_id={carol.id}&follow=carol")

    # No follow, like, repost or view comes of reading.
    assert state(session) == before
    assert [
        columns(session, user) for user in (alice_account, bob_account, carol)
    ] == users_before


def test_feed_gives_the_viewer_no_cookie(bob_client: TestClient, network: dict) -> None:
    assert "set-cookie" not in bob_client.get("/feed/following").headers


def test_cross_site_read_gets_no_cors_permission(
    bob_client: TestClient, network: dict
) -> None:
    response = bob_client.get("/feed/following", headers={"Origin": FOREIGN_ORIGIN})

    # The cookie travels, so the answer is produced. The browser will not
    # let the other site's page read it.
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_cross_site_preflight_is_given_no_permission(bob_client: TestClient) -> None:
    response = bob_client.options(
        "/feed/following",
        headers={
            "Origin": FOREIGN_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_the_frontend_is_let_through(bob_client: TestClient, network: dict) -> None:
    response = bob_client.get(
        "/feed/following", headers={"Origin": "http://localhost:3000"}
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_cross_site_write_attempt_finds_nothing_to_write_to(
    bob_client: TestClient, session: Session, network: dict
) -> None:
    before = state(session)

    response = bob_client.post(
        "/feed/following", headers={"Origin": FOREIGN_ORIGIN}, json={}
    )

    # There is no such operation to refuse the origin of.
    assert response.status_code == 405
    assert "access-control-allow-origin" not in response.headers
    assert state(session) == before


# --- errors --------------------------------------------------------------


def test_every_error_has_the_same_shape(
    bob_client: TestClient, client: TestClient
) -> None:
    errors = [
        client.get("/feed/following"),  # 401
        bob_client.get("/feed/following?cursor=abc"),  # 400
        bob_client.get("/feed/following?limit=0"),  # 422
        bob_client.get("/feed/following?limit=51"),  # 422
        bob_client.post("/feed/following", json={}),  # 405
        bob_client.get("/feed/following/alice"),  # 404
    ]

    assert [r.status_code for r in errors] == [401, 400, 422, 422, 405, 404]
    for response in errors:
        assert set(response.json()) == {"detail"}
        assert response.headers["content-type"] == "application/json"


def test_error_details_name_no_internals(bob_client: TestClient) -> None:
    for path in (
        "/feed/following?cursor=abc",
        "/feed/following?limit=0",
        f"/feed/following?cursor={uuid.uuid4()}",
    ):
        text = bob_client.get(path).text.lower()

        for word in ("traceback", "sqlalchemy", "psycopg", "select ", "follows"):
            assert word not in text, (path, word)


def test_unexpected_error_reveals_nothing_about_itself(
    make_client, monkeypatch: pytest.MonkeyPatch, session: Session, bob_account: User
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("follows table is on fire: secret-detail")

    monkeypatch.setattr(posts_service, "list_following_feed", fail)
    bob = make_client(raise_server_exceptions=False)
    log_in(bob, "bob")

    response = bob.get("/feed/following")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    assert "secret-detail" not in response.text

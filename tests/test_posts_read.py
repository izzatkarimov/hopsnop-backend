"""Reading a single post: when it is shown, and what the answer contains."""

import re
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User, UserSession
from helpers import (
    SENSITIVE_KEYS,
    add_post,
    add_user,
    expire,
    follow,
    keys_in,
    log_in,
    plant_session_cookie,
    post_columns,
    recorded_selects,
)

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
NOT_FOUND = {"detail": "Post not found."}
# Account fields that have no place in a post, whoever is looking.
ACCOUNT_FIELDS = {
    "email",
    "email_verified_at",
    "is_active",
    "is_private",
    "bio",
    "followers_count",
    "following_count",
    "author_id",
    "deleted_at",
}


# --- reading a post ------------------------------------------------------


def test_post_can_be_read_without_authentication(
    client: TestClient, session: Session, alice_account: User
) -> None:
    alice_account.avatar_url = "https://cdn.example.com/avatars/alice.png"
    post = add_post(session, alice_account, "Hello Hopsnop!")

    response = client.get(f"/posts/{post.id}")

    assert response.status_code == 200
    body = response.json()
    assert datetime.fromisoformat(body.pop("created_at")) == post.created_at
    assert datetime.fromisoformat(body.pop("updated_at")) == post.updated_at
    assert body == {
        "id": str(post.id),
        "author": {
            "id": str(alice_account.id),
            "username": "alice",
            "display_name": "Alice",
            "avatar_url": "https://cdn.example.com/avatars/alice.png",
        },
        "content": "Hello Hopsnop!",
        "parent_post_id": None,
        "is_reply": False,
        "like_count": 0,
        "liked_by_me": False,
        "repost_count": 0,
        "reposted_by_me": False,
    }


def test_post_can_be_read_by_another_user(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account)

    response = bob_client.get(f"/posts/{post.id}")

    assert response.status_code == 200
    assert response.json()["author"]["username"] == "alice"


def test_post_is_the_same_for_every_viewer(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    post = add_post(session, alice_account)
    anonymous, bob, alice = make_client(), make_client(), make_client()
    log_in(bob, "bob")
    log_in(alice, "alice")

    seen = [
        viewer.get(f"/posts/{post.id}").json() for viewer in (anonymous, bob, alice)
    ]

    assert seen[0] == seen[1] == seen[2]


def test_post_contains_exactly_the_intended_fields(
    client: TestClient, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account)

    body = client.get(f"/posts/{post.id}").json()

    assert set(body) == POST_FIELDS
    assert set(body["author"]) == AUTHOR_FIELDS


def test_reply_is_marked_as_one(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    post = add_post(session, alice_account)
    reply = add_post(session, bob_account, "Nice post!", parent=post)

    body = client.get(f"/posts/{reply.id}").json()

    assert body["parent_post_id"] == str(post.id)
    assert body["is_reply"] is True
    assert body["author"]["username"] == "bob"


def test_post_responses_are_not_to_be_cached(
    client: TestClient, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account)

    assert client.get(f"/posts/{post.id}").headers["cache-control"] == "no-store"


# --- no account keeps its posts to itself --------------------------------


def test_posts_are_readable_across_accounts_in_both_directions(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    alices = add_post(session, alice_account)
    bobs = add_post(session, bob_account)
    alice, bob = make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")

    for viewer in (make_client(), alice, bob):
        for post in (alices, bobs):
            response = viewer.get(f"/posts/{post.id}")
            assert response.status_code == 200
            assert "private" not in response.text


def test_following_plays_no_part_in_reading_a_post(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    post = add_post(session, alice_account)
    before = bob_client.get(f"/posts/{post.id}")

    follow(session, bob_account, alice_account)

    # Readable without the follow, and no different with it.
    assert before.status_code == 200
    assert bob_client.get(f"/posts/{post.id}").json() == before.json()


def test_no_profile_change_hides_a_post(
    alice_client: TestClient, make_client, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account)
    anonymous = make_client()
    before = anonymous.get(f"/posts/{post.id}").json()

    # There is no privacy setting to switch on, alone or next to a real change.
    alone = alice_client.patch("/users/me", json={"is_private": True})
    beside = alice_client.patch("/users/me", json={"is_private": True, "bio": "Hi"})

    after = anonymous.get(f"/posts/{post.id}")
    assert after.status_code == 200
    assert after.json() == before
    assert (alone.status_code, beside.status_code) == (422, 200)


# --- posts that are not found --------------------------------------------


def test_nonexistent_post_returns_404(client: TestClient) -> None:
    response = client.get(f"/posts/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


@pytest.mark.parametrize(
    "post_id",
    ["1", "abc", "null", "me", "alice", "00000000-0000-0000-0000", "%27%20OR%201%3D1"],
)
def test_malformed_post_id_is_rejected(client: TestClient, post_id: str) -> None:
    response = client.get(f"/posts/{post_id}")

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["path", "post_id"]


def test_post_cannot_be_looked_up_by_anything_but_its_id(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_post(session, alice_account)

    # Not by the author's id either: that names a user, not a post.
    assert client.get(f"/posts/{alice_account.id}").status_code == 404


def test_deleted_post_is_not_found(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    post = add_post(session, alice_account, "Regretted", deleted=True)
    anonymous, bob, alice = make_client(), make_client(), make_client()
    log_in(bob, "bob")
    log_in(alice, "alice")

    # Not for its author either.
    for viewer in (anonymous, bob, alice):
        response = viewer.get(f"/posts/{post.id}")
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
        assert "Regretted" not in response.text


def test_post_of_a_deactivated_account_is_not_found(
    client: TestClient, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account)
    assert client.get(f"/posts/{post.id}").status_code == 200

    alice_account.is_active = False
    session.flush()

    # Its profile is not shown either; the post would give the account away.
    assert client.get("/users/alice").status_code == 404
    assert client.get(f"/posts/{post.id}").status_code == 404


def test_post_of_an_unverified_account_is_not_found(
    client: TestClient, session: Session
) -> None:
    unverified = add_user(session, "unverified", verified=False)
    post = add_post(session, unverified)

    assert client.get(f"/posts/{post.id}").status_code == 404


def test_every_post_that_is_not_shown_looks_like_one_that_does_not_exist(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    deleted = add_post(session, alice_account, deleted=True)
    inactive = add_post(session, add_user(session, "inactive", active=False))
    unverified = add_post(session, add_user(session, "unverified", verified=False))

    responses = [
        bob_client.get(f"/posts/{id}")
        for id in (uuid.uuid4(), deleted.id, inactive.id, unverified.id)
    ]

    # Nothing about the answer says that there is a post behind the id.
    assert {response.status_code for response in responses} == {404}
    assert len({response.text for response in responses}) == 1
    assert len({tuple(sorted(response.headers)) for response in responses}) == 1


def test_hidden_post_takes_the_same_queries_as_a_nonexistent_one(
    client: TestClient, session: Session, alice_account: User
) -> None:
    hidden = add_post(session, add_user(session, "inactive", active=False))
    unverified = add_post(session, add_user(session, "unverified", verified=False))
    deleted = add_post(session, alice_account, deleted=True)

    statements = []
    for id in (uuid.uuid4(), hidden.id, unverified.id, deleted.id):
        with recorded_selects() as recorded:
            assert client.get(f"/posts/{id}").status_code == 404
        statements.append(recorded)

    # One identical lookup in each case: the work done does not depend on
    # whether, or why, the post is hidden.
    assert [len(recorded) for recorded in statements] == [1, 1, 1, 1]
    assert len({tuple(recorded) for recorded in statements}) == 1


# --- who is asking -------------------------------------------------------


def test_request_with_a_stale_cookie_is_answered_as_anonymous(
    client: TestClient, make_client, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account)
    expected = make_client().get(f"/posts/{post.id}").json()
    plant_session_cookie(client, "left-over-from-an-old-login")

    response = client.get(f"/posts/{post.id}")

    # Not refused: the post stays readable, as it is for anyone.
    assert response.status_code == 200
    assert response.json() == expected


def test_expired_session_is_answered_as_anonymous(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account)
    alice_client.post(f"/posts/{post.id}/like")
    assert alice_client.get(f"/posts/{post.id}").json()["liked_by_me"] is True

    expire(session, session.scalars(select(UserSession)).one())

    # Still readable, but no longer as alice.
    response = alice_client.get(f"/posts/{post.id}")
    assert response.status_code == 200
    assert response.json()["like_count"] == 1
    assert response.json()["liked_by_me"] is False


def test_logging_out_leaves_the_post_readable_as_for_anyone(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account)
    alice_client.post(f"/posts/{post.id}/like")
    assert alice_client.get(f"/posts/{post.id}").json()["liked_by_me"] is True

    alice_client.post("/auth/logout")

    response = alice_client.get(f"/posts/{post.id}")
    assert response.status_code == 200
    assert response.json()["liked_by_me"] is False


def test_viewer_cannot_be_named_by_the_request(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
) -> None:
    post = add_post(session, alice_account)
    alice_client.post(f"/posts/{post.id}/like")

    response = bob_client.get(
        f"/posts/{post.id}?viewer_id={alice_account.id}&user_id={alice_account.id}"
        "&username=alice&as=alice"
    )

    # Whose "by me" it is, is decided by the session and by nothing else.
    assert response.status_code == 200
    assert response.json()["like_count"] == 1
    assert response.json()["liked_by_me"] is False


# --- what a post must not contain ----------------------------------------


def test_post_does_not_expose_account_data(
    make_client, session: Session, alice_account: User
) -> None:
    # Give alice a live session, so that there is session data to leak.
    log_in(make_client(), "alice")
    user_session = session.scalars(select(UserSession)).one()
    post = add_post(session, alice_account)

    response = make_client().get(f"/posts/{post.id}")

    body = response.json()
    assert keys_in(body).isdisjoint(SENSITIVE_KEYS | ACCOUNT_FIELDS)
    assert not any("session" in key for key in keys_in(body))
    for secret in (
        alice_account.email,
        alice_account.password_hash,
        str(user_session.id),
        user_session.token_hash,
    ):
        assert secret not in response.text
    assert "set-cookie" not in response.headers


def test_post_shows_the_owner_no_more_than_anyone_else(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account)

    response = alice_client.get(f"/posts/{post.id}")

    assert keys_in(response.json()).isdisjoint(SENSITIVE_KEYS | ACCOUNT_FIELDS)
    assert alice_account.email not in response.text
    assert alice_account.password_hash not in response.text
    assert alice_client.cookies.get("hopsnop_session") not in response.text


def test_reading_a_post_does_not_read_the_authors_private_columns(
    client: TestClient, session: Session, alice_account: User
) -> None:
    # The strongest form of "not exposed": the data is never fetched.
    post_id = add_post(session, alice_account).id
    # Like a real request, which starts with nothing loaded.
    session.expunge_all()

    with recorded_selects() as statements:
        assert client.get(f"/posts/{post_id}").status_code == 200

    [statement] = statements
    assert "users.username" in statement
    assert "password_hash" not in statement
    assert not re.search(r"users\.email\b(?!_)", statement)
    assert "users.bio" not in statement


def test_reading_a_post_changes_nothing(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    post = add_post(session, alice_account)
    before = post_columns(session, post.id)
    bob = make_client()
    log_in(bob, "bob")

    for viewer in (make_client(), bob):
        viewer.get(f"/posts/{post.id}")

    assert post_columns(session, post.id) == before

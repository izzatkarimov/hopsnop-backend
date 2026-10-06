"""Reading a single post: who may see it, and what the answer contains."""

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


def make_private(session: Session, user: User) -> None:
    user.is_private = True
    session.flush()


# --- public posts --------------------------------------------------------


def test_public_post_can_be_read_without_authentication(
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


def test_public_post_can_be_read_by_another_user(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account)

    response = bob_client.get(f"/posts/{post.id}")

    assert response.status_code == 200
    assert response.json()["author"]["username"] == "alice"


def test_public_post_is_the_same_for_every_viewer(
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


# --- private accounts ----------------------------------------------------


def test_private_accounts_post_is_not_found_without_authentication(
    client: TestClient, session: Session, alice_account: User
) -> None:
    make_private(session, alice_account)
    post = add_post(session, alice_account, "For my eyes only")

    response = client.get(f"/posts/{post.id}")

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert "For my eyes only" not in response.text


def test_private_accounts_post_is_not_found_for_another_user(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    make_private(session, alice_account)
    post = add_post(session, alice_account, "For my eyes only")

    response = bob_client.get(f"/posts/{post.id}")

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert "For my eyes only" not in response.text


def test_owner_can_read_their_own_private_post(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    make_private(session, alice_account)
    post = add_post(session, alice_account, "For my eyes only")

    response = alice_client.get(f"/posts/{post.id}")

    assert response.status_code == 200
    assert response.json()["content"] == "For my eyes only"


def test_following_a_private_account_does_not_reveal_its_posts_yet(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    # Follow approval does not exist yet, so a row in follows grants nothing.
    make_private(session, alice_account)
    post = add_post(session, alice_account)
    follow(session, bob_account, alice_account)

    assert bob_client.get(f"/posts/{post.id}").status_code == 404


def test_visibility_follows_the_accounts_current_privacy_setting(
    alice_client: TestClient, make_client, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account)
    anonymous = make_client()
    assert anonymous.get(f"/posts/{post.id}").status_code == 200

    alice_client.patch("/users/me", json={"is_private": True})
    assert anonymous.get(f"/posts/{post.id}").status_code == 404
    assert alice_client.get(f"/posts/{post.id}").status_code == 200

    alice_client.patch("/users/me", json={"is_private": False})
    assert anonymous.get(f"/posts/{post.id}").status_code == 200


def test_private_post_is_only_readable_through_the_owners_session(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    make_private(session, alice_account)
    make_private(session, bob_account)
    alices = add_post(session, alice_account)
    bobs = add_post(session, bob_account)
    alice, bob = make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")

    # Being private oneself gives no access to other private accounts.
    assert alice.get(f"/posts/{alices.id}").status_code == 200
    assert alice.get(f"/posts/{bobs.id}").status_code == 404
    assert bob.get(f"/posts/{bobs.id}").status_code == 200
    assert bob.get(f"/posts/{alices.id}").status_code == 404


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
    private = add_post(session, add_user(session, "private_user"))
    private.author.is_private = True
    inactive = add_post(session, add_user(session, "inactive", active=False))
    unverified = add_post(session, add_user(session, "unverified", verified=False))
    session.flush()

    responses = [
        bob_client.get(f"/posts/{id}")
        for id in (uuid.uuid4(), deleted.id, private.id, inactive.id, unverified.id)
    ]

    # Nothing about the answer says that there is a post behind the id.
    assert {response.status_code for response in responses} == {404}
    assert len({response.text for response in responses}) == 1
    assert len({tuple(sorted(response.headers)) for response in responses}) == 1


def test_hidden_post_takes_the_same_queries_as_a_nonexistent_one(
    client: TestClient, session: Session, alice_account: User
) -> None:
    make_private(session, alice_account)
    hidden = add_post(session, alice_account)
    deleted = add_post(session, alice_account, deleted=True)

    statements = []
    for id in (uuid.uuid4(), hidden.id, deleted.id):
        with recorded_selects() as recorded:
            assert client.get(f"/posts/{id}").status_code == 404
        statements.append(recorded)

    # One identical lookup in each case: the work done does not depend on
    # whether, or why, the post is hidden.
    assert [len(recorded) for recorded in statements] == [1, 1, 1]
    assert statements[0] == statements[1] == statements[2]


# --- who is asking -------------------------------------------------------


def test_request_with_a_stale_cookie_is_answered_as_anonymous(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    public = add_post(session, alice_account)
    make_private(session, bob_account)
    private = add_post(session, bob_account)
    plant_session_cookie(client, "left-over-from-an-old-login")

    # Not refused: public content stays readable. But nothing more than that.
    assert client.get(f"/posts/{public.id}").status_code == 200
    assert client.get(f"/posts/{private.id}").status_code == 404


def test_expired_session_no_longer_shows_private_posts(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    make_private(session, alice_account)
    post = add_post(session, alice_account)
    assert alice_client.get(f"/posts/{post.id}").status_code == 200

    expire(session, session.scalars(select(UserSession)).one())

    assert alice_client.get(f"/posts/{post.id}").status_code == 404


def test_logging_out_hides_private_posts_again(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    make_private(session, alice_account)
    post = add_post(session, alice_account)
    assert alice_client.get(f"/posts/{post.id}").status_code == 200

    alice_client.post("/auth/logout")

    assert alice_client.get(f"/posts/{post.id}").status_code == 404


def test_viewer_cannot_be_named_by_the_request(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    make_private(session, alice_account)
    post = add_post(session, alice_account)

    response = bob_client.get(
        f"/posts/{post.id}?viewer_id={alice_account.id}&user_id={alice_account.id}"
        "&username=alice&as=alice"
    )

    assert response.status_code == 404


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

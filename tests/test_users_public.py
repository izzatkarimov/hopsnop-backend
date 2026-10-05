import re
import uuid
from datetime import datetime
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User, UserSession
from helpers import SENSITIVE_KEYS, add_user, follow, keys_in, log_in, recorded_selects

PUBLIC_FIELDS = {
    "id",
    "username",
    "display_name",
    "bio",
    "avatar_url",
    "is_private",
    "followers_count",
    "following_count",
    "created_at",
}
NOT_FOUND = {"detail": "User not found."}


# --- viewing a profile ---------------------------------------------------


def test_public_profile_returns_the_users_profile(
    client: TestClient, session: Session, alice_account: User
) -> None:
    alice_account.display_name = "Alice Smith"
    alice_account.bio = "Hello, Hopsnop!"
    alice_account.avatar_url = "https://cdn.example.com/avatars/alice.png"
    session.flush()

    response = client.get("/users/alice")

    assert response.status_code == 200
    body = response.json()
    assert datetime.fromisoformat(body.pop("created_at")) == alice_account.created_at
    assert body == {
        "id": str(alice_account.id),
        "username": "alice",
        "display_name": "Alice Smith",
        "bio": "Hello, Hopsnop!",
        "avatar_url": "https://cdn.example.com/avatars/alice.png",
        "is_private": False,
        "followers_count": 0,
        "following_count": 0,
    }


def test_profile_without_bio_or_avatar_returns_nulls(
    client: TestClient, alice_account: User
) -> None:
    body = client.get("/users/alice").json()

    assert body["bio"] is None
    assert body["avatar_url"] is None


def test_public_profile_needs_no_authentication(
    client: TestClient, alice_account: User
) -> None:
    assert client.cookies.get("hopsnop_session") is None

    assert client.get("/users/alice").status_code == 200


def test_public_profile_is_the_same_for_every_viewer(
    make_client, alice_account: User, bob_account: User
) -> None:
    anonymous, bob, alice = make_client(), make_client(), make_client()
    log_in(bob, "bob")
    log_in(alice, "alice")

    seen = [viewer.get("/users/alice").json() for viewer in (anonymous, bob, alice)]

    # Not even the owner gets private fields from this endpoint.
    assert seen[0] == seen[1] == seen[2]
    assert set(seen[2]) == PUBLIC_FIELDS


@pytest.mark.parametrize("username", ["alice", "Alice", "ALICE", " alice "])
def test_username_lookup_uses_the_canonical_username(
    client: TestClient, alice_account: User, username: str
) -> None:
    response = client.get(f"/users/{quote(username)}")

    assert response.status_code == 200
    assert response.json()["username"] == "alice"


def test_profile_of_a_private_account_is_still_visible(
    client: TestClient, session: Session, alice_account: User
) -> None:
    alice_account.is_private = True
    alice_account.bio = "Private, but findable."
    session.flush()

    response = client.get("/users/alice")

    assert response.status_code == 200
    body = response.json()
    assert body["is_private"] is True
    assert set(body) == PUBLIC_FIELDS
    assert body["username"] == "alice"
    assert body["display_name"] == "Alice"
    assert body["bio"] == "Private, but findable."


# --- what a public profile must not contain ------------------------------


def test_public_profile_contains_exactly_the_public_fields(
    client: TestClient, alice_account: User
) -> None:
    assert set(client.get("/users/alice").json()) == PUBLIC_FIELDS


def test_public_profile_does_not_expose_private_account_data(
    make_client, session: Session, alice_account: User
) -> None:
    # Give alice a live session, so that there is session data to leak.
    log_in(make_client(), "alice")
    user_session = session.scalars(select(UserSession)).one()

    response = make_client().get("/users/alice")

    body = response.json()
    for field in ("email", "password_hash", "email_verified_at", "is_active"):
        assert field not in body
    assert keys_in(body).isdisjoint(SENSITIVE_KEYS)
    assert not any("session" in key for key in body)
    for secret in (
        alice_account.email,
        alice_account.password_hash,
        str(user_session.id),
        user_session.token_hash,
    ):
        assert secret not in response.text
    assert "set-cookie" not in response.headers


def test_public_profile_query_does_not_read_private_columns(
    client: TestClient, alice_account: User
) -> None:
    # The strongest form of "not exposed": the data is never fetched.
    with recorded_selects() as statements:
        client.get("/users/alice")

    [statement] = statements
    assert "password_hash" not in statement
    assert not re.search(r"users\.email\b(?!_)", statement)
    assert "updated_at" not in statement


# --- accounts that are not found -----------------------------------------


def test_nonexistent_username_returns_404(client: TestClient) -> None:
    response = client.get("/users/nonexistent")

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


@pytest.mark.parametrize(
    "username",
    [
        "al",
        "a" * 31,
        "alice-smith",
        "alice smith",
        "álice",
        "alice@example.com",
        "' OR '1'='1",
        "alice'; DROP TABLE users; --",
        "%",
        str(uuid.uuid4()),
    ],
)
def test_impossible_username_returns_the_same_404(
    client: TestClient, alice_account: User, username: str
) -> None:
    response = client.get(f"/users/{quote(username, safe='')}")

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


def test_users_cannot_be_looked_up_by_id_or_email(
    client: TestClient, alice_account: User
) -> None:
    by_id = client.get(f"/users/{alice_account.id}")
    by_email = client.get(f"/users/{quote(alice_account.email, safe='')}")

    assert by_id.status_code == by_email.status_code == 404


def test_inactive_account_is_not_shown(client: TestClient, session: Session) -> None:
    add_user(session, "inactive", active=False)

    response = client.get("/users/inactive")

    assert response.status_code == 404


def test_account_that_is_deactivated_stops_being_shown(
    client: TestClient, session: Session, alice_account: User
) -> None:
    assert client.get("/users/alice").status_code == 200

    alice_account.is_active = False
    session.flush()

    assert client.get("/users/alice").status_code == 404


def test_unverified_account_is_not_shown(client: TestClient, session: Session) -> None:
    add_user(session, "unverified", verified=False)

    response = client.get("/users/unverified")

    assert response.status_code == 404


def test_hidden_account_looks_the_same_as_one_that_does_not_exist(
    client: TestClient, session: Session
) -> None:
    add_user(session, "inactive", active=False)
    add_user(session, "unverified", verified=False)

    responses = [
        client.get("/users/nonexistent"),
        client.get("/users/inactive"),
        client.get("/users/unverified"),
    ]

    # Nothing about the answer says that a deactivated account is there.
    assert {response.status_code for response in responses} == {404}
    assert len({response.text for response in responses}) == 1
    assert len({tuple(sorted(response.headers)) for response in responses}) == 1


# --- follower and following counts ---------------------------------------


def counts(client: TestClient, username: str) -> tuple[int, int]:
    body = client.get(f"/users/{username}").json()
    return body["followers_count"], body["following_count"]


def test_user_without_follows_has_zero_counts(
    client: TestClient, alice_account: User
) -> None:
    assert counts(client, "alice") == (0, 0)


def test_counts_are_computed_from_the_follows_table(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    carol = add_user(session, "carol")
    follow(session, alice_account, bob_account)
    follow(session, carol, bob_account)
    follow(session, bob_account, alice_account)

    assert counts(client, "alice") == (1, 1)
    assert counts(client, "bob") == (2, 1)
    assert counts(client, "carol") == (0, 1)


def test_following_is_not_counted_as_being_followed(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, alice_account, bob_account)

    assert counts(client, "alice") == (0, 1)
    assert counts(client, "bob") == (1, 0)


def test_counts_follow_changes_to_the_follows_table(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, alice_account, bob_account)
    assert counts(client, "bob") == (1, 0)

    session.delete(alice_account.following[0])
    session.flush()

    assert counts(client, "bob") == (0, 0)


def test_profile_takes_one_query_however_many_followers_there_are(
    client: TestClient, session: Session, alice_account: User
) -> None:
    with recorded_selects() as without_followers:
        client.get("/users/alice")

    for number in range(5):
        fan = add_user(session, f"fan_{number}")
        follow(session, fan, alice_account)
        follow(session, alice_account, fan)

    with recorded_selects() as with_followers:
        assert counts(client, "alice") == (5, 5)

    assert len(without_followers) == len(with_followers) == 1

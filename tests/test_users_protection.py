"""Security properties of the profile API.

Who may change what, what can never be changed or read through it, and that
profile fields cannot be used to attack the server or other users.
"""

import socket
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.main import app
from app.models import User
from app.schemas.user import (
    MyProfileResponse,
    PublicProfileResponse,
    UpdateProfileRequest,
)
from app.services import users as users_service
from helpers import SENSITIVE_KEYS, add_user, columns, keys_in, log_in

EDITABLE = {"display_name", "bio", "avatar_url", "is_private"}
PRIVATE_ONLY = {"email", "email_verified_at"}
NEVER_EXPOSED = {"password_hash", "is_active", "updated_at"}

# Columns the profile API must never write, each with a value a client might
# try to plant.
PROTECTED_COLUMNS = {
    "id": str(uuid.uuid4()),
    "username": "someone_else",
    "email": "attacker@example.com",
    "password_hash": "chosen-by-client",
    "is_active": False,
    "email_verified_at": None,
    "created_at": "2000-01-01T00:00:00Z",
    "updated_at": "2000-01-01T00:00:00Z",
}


def patch(client: TestClient, **changes: object):
    return client.patch("/users/me", json=changes)


# --- the schemas are the boundary ----------------------------------------


def test_update_schema_has_only_the_editable_fields() -> None:
    assert set(UpdateProfileRequest.model_fields) == EDITABLE
    assert users_service.EDITABLE_FIELDS == EDITABLE


def test_public_schema_has_no_private_fields() -> None:
    public = set(PublicProfileResponse.model_fields)

    assert public.isdisjoint(PRIVATE_ONLY | NEVER_EXPOSED)
    assert public.isdisjoint(SENSITIVE_KEYS)


def test_own_schema_adds_only_the_owners_private_fields() -> None:
    public = set(PublicProfileResponse.model_fields)
    own = set(MyProfileResponse.model_fields)

    assert own - public == PRIVATE_ONLY
    assert own.isdisjoint(NEVER_EXPOSED | SENSITIVE_KEYS)


def test_documented_responses_match_the_schemas() -> None:
    schemas = app.openapi()["components"]["schemas"]

    public = set(schemas["PublicProfileResponse"]["properties"])
    assert public == set(PublicProfileResponse.model_fields)
    assert set(schemas["UpdateProfileRequest"]["properties"]) == EDITABLE


# --- mass assignment -----------------------------------------------------


@pytest.mark.parametrize("field", PROTECTED_COLUMNS)
def test_protected_field_sent_alone_changes_nothing(
    alice_client: TestClient, session: Session, alice_account: User, field: str
) -> None:
    before = columns(session, alice_account)

    response = patch(alice_client, **{field: PROTECTED_COLUMNS[field]})

    # With nothing editable in it, the request is an empty update.
    assert response.status_code == 422
    assert columns(session, alice_account) == before


@pytest.mark.parametrize("field", PROTECTED_COLUMNS)
def test_protected_field_sent_with_a_valid_change_is_ignored(
    alice_client: TestClient, session: Session, alice_account: User, field: str
) -> None:
    before = columns(session, alice_account)

    response = patch(alice_client, bio="Changed", **{field: PROTECTED_COLUMNS[field]})

    assert response.status_code == 200
    after = columns(session, alice_account)
    assert after["bio"] == "Changed"
    changed = {name for name in before if before[name] != after[name]}
    assert changed <= {"bio", "updated_at"}
    assert after["updated_at"] >= before["updated_at"]


def test_every_protected_field_at_once_is_ignored(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    before = columns(session, alice_account)

    response = patch(alice_client, display_name="Alice Smith", **PROTECTED_COLUMNS)

    assert response.status_code == 200
    after = columns(session, alice_account)
    changed = {name for name in before if before[name] != after[name]}
    assert changed <= {"display_name", "updated_at"}
    assert after["is_active"] is True
    assert after["email_verified_at"] is not None
    assert after["password_hash"].startswith("$argon2id$")
    # Still able to use the account, under the same name.
    assert alice_client.get("/users/me").json()["username"] == "alice"


def test_unknown_fields_are_ignored_and_change_nothing(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    before = columns(session, alice_account)

    response = patch(
        alice_client,
        bio="Changed",
        followers_count=1_000_000,
        following_count=1_000_000,
        is_admin=True,
        role="admin",
        nickname="Al",
    )

    assert response.status_code == 200
    assert response.json()["followers_count"] == 0
    assert response.json()["following_count"] == 0
    assert "is_admin" not in response.json()
    after = columns(session, alice_account)
    changed = {name for name in before if before[name] != after[name]}
    assert changed <= {"bio", "updated_at"}
    assert set(after) == set(before)


@pytest.mark.parametrize("field", PROTECTED_COLUMNS)
def test_service_refuses_to_write_a_protected_column(
    session: Session, alice_account: User, field: str
) -> None:
    # The second line of defence, should a caller ever bypass the schema.
    before = columns(session, alice_account)

    with pytest.raises(ValueError, match="Not editable"):
        users_service.update_profile(
            session, alice_account, {"bio": "Changed", field: PROTECTED_COLUMNS[field]}
        )

    assert columns(session, alice_account) == before


# --- authorization -------------------------------------------------------


def test_update_only_ever_changes_the_authenticated_user(
    alice_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    bob_before = columns(session, bob_account)

    # Every way a client might try to point the update at someone else.
    response = alice_client.patch(
        f"/users/me?user_id={bob_account.id}&username=bob",
        json={
            "id": str(bob_account.id),
            "user_id": str(bob_account.id),
            "username": "bob",
            "display_name": "Changed by Alice",
        },
    )

    assert response.status_code == 200
    assert response.json()["username"] == "alice"
    assert columns(session, alice_account)["display_name"] == "Changed by Alice"
    assert columns(session, bob_account) == bob_before


def test_each_user_updates_their_own_profile(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    alice, bob = make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")

    patch(alice, bio="Alice's bio")
    patch(bob, bio="Bob's bio", is_private=True)

    assert columns(session, alice_account)["bio"] == "Alice's bio"
    assert columns(session, alice_account)["is_private"] is False
    assert columns(session, bob_account)["bio"] == "Bob's bio"
    assert columns(session, bob_account)["is_private"] is True


def test_the_only_user_routes_are_the_intended_ones() -> None:
    routes = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/users")
        for method in operations
    }

    assert routes == {
        ("GET", "/users/me"),
        ("PATCH", "/users/me"),
        ("GET", "/users/{username}"),
        # Read-only, and the only route that reaches a user's posts.
        ("GET", "/users/{username}/posts"),
    }


@pytest.mark.parametrize("method", ["PATCH", "PUT", "POST", "DELETE"])
def test_another_users_profile_cannot_be_written_by_addressing_it(
    alice_client: TestClient,
    session: Session,
    bob_account: User,
    method: str,
) -> None:
    before = columns(session, bob_account)

    by_name = alice_client.request(method, "/users/bob", json={"bio": "Hacked"})
    by_id = alice_client.request(
        method, f"/users/{bob_account.id}", json={"bio": "Hacked"}
    )

    assert by_name.status_code == by_id.status_code == 405
    assert columns(session, bob_account) == before


@pytest.mark.parametrize("method", ["PUT", "POST", "DELETE"])
def test_own_profile_accepts_no_other_write_methods(
    alice_client: TestClient, session: Session, alice_account: User, method: str
) -> None:
    before = columns(session, alice_account)

    response = alice_client.request(method, "/users/me", json={"bio": "Changed"})

    assert response.status_code == 405
    assert columns(session, alice_account) == before
    assert session.scalar(select(func.count()).select_from(User)) == 1


def test_viewing_someone_as_a_logged_in_user_reveals_nothing_private(
    alice_client: TestClient, bob_account: User
) -> None:
    response = alice_client.get("/users/bob")

    assert response.status_code == 200
    assert set(response.json()).isdisjoint(PRIVATE_ONLY | NEVER_EXPOSED)
    assert bob_account.email not in response.text


def test_cross_site_update_is_rejected_and_has_no_effect(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    before = columns(session, alice_account)

    response = alice_client.patch(
        "/users/me",
        json={"is_private": True},
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert columns(session, alice_account) == before


# --- deactivated accounts ------------------------------------------------


def test_deactivated_user_can_neither_read_nor_change_their_profile(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    alice_account.is_active = False
    session.flush()
    before = columns(session, alice_account)

    assert alice_client.get("/users/me").status_code == 401
    assert patch(alice_client, bio="Still here").status_code == 401
    assert columns(session, alice_account) == before


# --- profile fields as an attack vector ----------------------------------

MARKUP = [
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    "\"><svg/onload=alert(1)>",
    "{{7*7}} ${7*7} <%= 7*7 %>",
]


@pytest.mark.parametrize("text", MARKUP)
def test_markup_in_profile_text_is_only_ever_served_as_json_data(
    make_client, alice_client: TestClient, text: str
) -> None:
    # The text is kept exactly as written; nothing interprets it. It leaves
    # the API as a JSON string in a JSON response, never as HTML, so escaping
    # it for display is the job of whatever renders it.
    patched = patch(alice_client, display_name=text, bio=text)
    public = make_client().get("/users/alice")

    for response in (patched, public):
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.json()["display_name"] == text
        assert response.json()["bio"] == text


@pytest.mark.parametrize(
    "text",
    [
        "'; DROP TABLE users; --",
        "' OR '1'='1",
        "\\'; SELECT pg_sleep(10); --",
        "%s %(x)s",
    ],
)
def test_sql_in_profile_text_is_stored_as_text(
    alice_client: TestClient, session: Session, alice_account: User, text: str
) -> None:
    add_user(session, "bob")

    response = patch(alice_client, display_name=text, bio=text)

    assert response.status_code == 200
    assert columns(session, alice_account)["bio"] == text
    assert session.scalar(select(func.count()).select_from(User)) == 2


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:5433/",
        "http://127.0.0.1:8000/auth/me",
        "https://internal.service.local/admin",
    ],
)
def test_avatar_url_is_never_requested_by_the_server(
    alice_client: TestClient, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    # The URL is only checked for its form and stored. If the server resolved
    # or fetched it, it could be pointed at internal addresses (SSRF).
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the server tried to reach the avatar URL")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    response = patch(alice_client, avatar_url=url)

    assert response.status_code == 200
    assert response.json()["avatar_url"] == url


# --- what the responses contain ------------------------------------------


def test_no_profile_response_contains_a_secret(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    alice, anonymous = make_client(), make_client()
    log_in(alice, "alice")
    session_token = alice.cookies.get("hopsnop_session")

    responses = [
        alice.get("/users/me"),
        patch(alice, bio="Hello"),
        patch(alice),  # 422
        patch(alice, display_name=""),  # 422
        alice.get("/users/bob"),
        alice.get("/users/nonexistent"),  # 404
        anonymous.get("/users/alice"),
        anonymous.get("/users/me"),  # 401
        patch(anonymous, bio="Hello"),  # 401
    ]

    statuses = {response.status_code for response in responses}
    assert statuses == {200, 401, 404, 422}
    secrets = [session_token, alice_account.password_hash, bob_account.password_hash]
    for response in responses:
        assert keys_in(response.json()).isdisjoint(SENSITIVE_KEYS | NEVER_EXPOSED)
        assert "set-cookie" not in response.headers
        for secret in secrets:
            assert secret not in response.text
        if response.status_code != 200:
            assert set(response.json()) == {"detail"}
    # Only the owner's own responses carry an email address.
    assert "bob@example.com" not in "".join(response.text for response in responses)
    assert "alice@example.com" not in responses[6].text

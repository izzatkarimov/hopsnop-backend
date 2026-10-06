from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import User
from helpers import SENSITIVE_KEYS, add_user, columns, follow, keys_in, recorded_selects

MY_FIELDS = {
    "id",
    "username",
    "email",
    "display_name",
    "bio",
    "avatar_url",
    "email_verified_at",
    "followers_count",
    "following_count",
    "created_at",
}
NOT_AUTHENTICATED = {"detail": "Not authenticated."}


def patch(client: TestClient, **changes: object):
    return client.patch("/users/me", json=changes)


# --- own profile ---------------------------------------------------------


def test_authenticated_user_can_get_their_own_profile(
    alice_client: TestClient, alice_account: User
) -> None:
    response = alice_client.get("/users/me")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(alice_account.id)
    assert body["username"] == "alice"
    assert body["display_name"] == "Alice"
    assert datetime.fromisoformat(body["created_at"]) == alice_account.created_at


def test_own_profile_includes_the_owner_only_fields(
    alice_client: TestClient, alice_account: User
) -> None:
    body = alice_client.get("/users/me").json()

    assert body["email"] == "alice@example.com"
    verified_at = datetime.fromisoformat(body["email_verified_at"])
    assert verified_at == alice_account.email_verified_at


def test_own_profile_contains_exactly_the_intended_fields(
    alice_client: TestClient, alice_account: User
) -> None:
    response = alice_client.get("/users/me")

    assert set(response.json()) == MY_FIELDS
    assert keys_in(response.json()).isdisjoint(SENSITIVE_KEYS)
    assert alice_account.password_hash not in response.text
    assert alice_client.cookies.get("hopsnop_session") not in response.text


def test_own_profile_is_not_to_be_cached(alice_client: TestClient) -> None:
    assert alice_client.get("/users/me").headers["cache-control"] == "no-store"
    assert patch(alice_client, bio="Hi").headers["cache-control"] == "no-store"


def test_own_profile_has_the_users_counts(
    alice_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    carol = add_user(session, "carol")
    follow(session, bob_account, alice_account)
    follow(session, carol, alice_account)
    follow(session, alice_account, bob_account)
    follow(session, carol, bob_account)  # nothing to do with alice

    body = alice_client.get("/users/me").json()

    assert body["followers_count"] == 2
    assert body["following_count"] == 1


def test_own_profile_has_zero_counts_without_follows(alice_client: TestClient) -> None:
    body = alice_client.get("/users/me").json()

    assert body["followers_count"] == 0
    assert body["following_count"] == 0


def test_own_profile_takes_the_same_queries_however_many_followers(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    with recorded_selects() as without_followers:
        alice_client.get("/users/me")

    for number in range(5):
        follow(session, add_user(session, f"fan_{number}"), alice_account)

    with recorded_selects() as with_followers:
        assert alice_client.get("/users/me").json()["followers_count"] == 5

    # One to authenticate the session, one for both counts.
    assert len(without_followers) == len(with_followers) == 2


def test_own_profile_requires_authentication(client: TestClient) -> None:
    response = client.get("/users/me")

    # 401, and not a 404 from looking for a user called "me".
    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


def test_own_profile_is_rejected_after_logout(alice_client: TestClient) -> None:
    alice_client.post("/auth/logout")

    assert alice_client.get("/users/me").status_code == 401


# --- updating single fields ----------------------------------------------


def test_display_name_can_be_updated(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    response = patch(alice_client, display_name="Alice Smith")

    assert response.status_code == 200
    assert response.json()["display_name"] == "Alice Smith"
    assert columns(session, alice_account)["display_name"] == "Alice Smith"


def test_bio_can_be_updated(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    response = patch(alice_client, bio="Hello, Hopsnop!")

    assert response.status_code == 200
    assert response.json()["bio"] == "Hello, Hopsnop!"
    assert columns(session, alice_account)["bio"] == "Hello, Hopsnop!"


def test_avatar_url_can_be_updated(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    url = "https://cdn.example.com/avatars/alice.png"

    response = patch(alice_client, avatar_url=url)

    assert response.status_code == 200
    assert response.json()["avatar_url"] == url
    assert columns(session, alice_account)["avatar_url"] == url


def test_several_fields_can_be_updated_at_once(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    changes = {
        "display_name": "Alice Smith",
        "bio": "Hello, Hopsnop!",
        "avatar_url": "https://cdn.example.com/avatars/alice.png",
    }

    response = patch(alice_client, **changes)

    assert response.status_code == 200
    stored = columns(session, alice_account)
    for field, value in changes.items():
        assert response.json()[field] == value
        assert stored[field] == value


def test_update_returns_the_whole_own_profile(
    alice_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)

    response = patch(alice_client, bio="Hello")

    assert set(response.json()) == MY_FIELDS
    assert response.json() == alice_client.get("/users/me").json()
    assert response.json()["followers_count"] == 1


def test_update_is_visible_on_the_public_profile(
    make_client, alice_client: TestClient
) -> None:
    patch(alice_client, display_name="Alice Smith", bio="Hello, Hopsnop!")

    body = make_client().get("/users/alice").json()

    assert body["display_name"] == "Alice Smith"
    assert body["bio"] == "Hello, Hopsnop!"


# --- PATCH semantics -----------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"display_name": "Alice Smith"},
        {"bio": "New bio"},
        {"avatar_url": "https://cdn.example.com/new.png"},
    ],
)
def test_omitted_fields_remain_unchanged(
    alice_client: TestClient, session: Session, alice_account: User, changes: dict
) -> None:
    alice_account.bio = "Original bio"
    alice_account.avatar_url = "https://cdn.example.com/original.png"
    session.flush()
    before = columns(session, alice_account)

    assert patch(alice_client, **changes).status_code == 200

    after = columns(session, alice_account)
    changed = {field for field in before if before[field] != after[field]}
    assert changed <= set(changes) | {"updated_at"}
    for field, value in changes.items():
        assert after[field] == value


def test_null_clears_the_bio(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    alice_account.bio = "Original bio"
    session.flush()

    response = patch(alice_client, bio=None)

    assert response.status_code == 200
    assert response.json()["bio"] is None
    assert columns(session, alice_account)["bio"] is None


def test_null_clears_the_avatar_url(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    alice_account.avatar_url = "https://cdn.example.com/original.png"
    session.flush()

    response = patch(alice_client, avatar_url=None)

    assert response.status_code == 200
    assert response.json()["avatar_url"] is None
    assert columns(session, alice_account)["avatar_url"] is None


def test_display_name_cannot_be_cleared_with_null(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    before = columns(session, alice_account)

    response = patch(alice_client, display_name=None)

    assert response.status_code == 422
    assert columns(session, alice_account) == before


@pytest.mark.parametrize("body", [{}, None, [], "display_name", 5])
def test_request_without_any_change_is_rejected(
    alice_client: TestClient, session: Session, alice_account: User, body: object
) -> None:
    before = columns(session, alice_account)

    response = alice_client.patch("/users/me", json=body)

    assert response.status_code == 422
    assert columns(session, alice_account) == before


def test_empty_request_says_what_is_expected(alice_client: TestClient) -> None:
    [error] = alice_client.patch("/users/me", json={}).json()["detail"]

    assert "At least one of display_name, bio or avatar_url" in error["msg"]
    assert "is_private" not in error["msg"]


def test_invalid_field_keeps_the_valid_ones_from_being_applied(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    before = columns(session, alice_account)

    response = patch(alice_client, display_name="Alice Smith", bio="x" * 161)

    assert response.status_code == 422
    assert columns(session, alice_account) == before


# --- display name --------------------------------------------------------


@pytest.mark.parametrize(
    "display_name",
    ["Izzat Karimov", "Илья", "山田太郎", "José Ángel", "Zoë 🌱", "x", "x" * 50],
)
def test_display_name_may_be_any_text_of_1_to_50_characters(
    alice_client: TestClient, session: Session, alice_account: User, display_name: str
) -> None:
    response = patch(alice_client, display_name=display_name)

    assert response.status_code == 200
    assert response.json()["display_name"] == display_name
    assert columns(session, alice_account)["display_name"] == display_name


def test_display_name_is_trimmed(alice_client: TestClient) -> None:
    response = patch(alice_client, display_name="  Alice Smith \n")

    assert response.json()["display_name"] == "Alice Smith"


@pytest.mark.parametrize("display_name", ["", " ", "   ", "\n\t ", "x" * 51, 42, ["A"]])
def test_invalid_display_name_is_rejected(
    alice_client: TestClient,
    session: Session,
    alice_account: User,
    display_name: object,
) -> None:
    response = patch(alice_client, display_name=display_name)

    assert response.status_code == 422
    assert columns(session, alice_account)["display_name"] == "Alice"


# --- bio -----------------------------------------------------------------


def test_bio_of_160_characters_is_accepted(alice_client: TestClient) -> None:
    response = patch(alice_client, bio="x" * 160)

    assert response.status_code == 200
    assert response.json()["bio"] == "x" * 160


def test_bio_over_160_characters_is_rejected(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    response = patch(alice_client, bio="x" * 161)

    assert response.status_code == 422
    assert columns(session, alice_account)["bio"] is None


def test_bio_length_is_measured_after_trimming(alice_client: TestClient) -> None:
    response = patch(alice_client, bio="   " + "x" * 160 + "   ")

    assert response.status_code == 200
    assert response.json()["bio"] == "x" * 160


@pytest.mark.parametrize("bio", ["", " ", "     ", "\n\t \n"])
def test_blank_bio_is_stored_as_no_bio(
    alice_client: TestClient, session: Session, alice_account: User, bio: str
) -> None:
    alice_account.bio = "Original bio"
    session.flush()

    response = patch(alice_client, bio=bio)

    assert response.status_code == 200
    assert response.json()["bio"] is None
    assert columns(session, alice_account)["bio"] is None


def test_bio_is_trimmed_and_otherwise_kept_as_written(
    alice_client: TestClient,
) -> None:
    response = patch(alice_client, bio="  Line one\nLine two — 你好 🌱  ")

    assert response.json()["bio"] == "Line one\nLine two — 你好 🌱"


@pytest.mark.parametrize("bio", [42, ["bio"], {"text": "bio"}])
def test_bio_must_be_text(alice_client: TestClient, bio: object) -> None:
    assert patch(alice_client, bio=bio).status_code == 422


@pytest.mark.parametrize("field", ["display_name", "bio"])
def test_text_the_database_cannot_store_is_rejected_cleanly(
    alice_client: TestClient, field: str
) -> None:
    # A NUL character would otherwise surface as a database error.
    response = patch(alice_client, **{field: "before\x00after"})

    assert response.status_code == 422


# --- avatar URL ----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.com/avatars/alice.png",
        "http://example.com/a.png",
        "https://example.com/a.png?size=200&v=2",
        "https://example.com:8443/a%20b.png",
    ],
)
def test_absolute_http_urls_are_accepted(alice_client: TestClient, url: str) -> None:
    response = patch(alice_client, avatar_url=url)

    assert response.status_code == 200
    assert response.json()["avatar_url"] == url


@pytest.mark.parametrize(
    "url",
    [
        "not a url",
        "example.com/a.png",
        "/avatars/alice.png",
        "//example.com/a.png",
        "https://",
        "",
        "   ",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "file:///etc/passwd",
        "ftp://example.com/a.png",
        "https://example.com/" + "a" * 2100,
        42,
        ["https://example.com/a.png"],
    ],
)
def test_invalid_avatar_url_is_rejected(
    alice_client: TestClient, session: Session, alice_account: User, url: object
) -> None:
    alice_account.avatar_url = "https://cdn.example.com/original.png"
    session.flush()

    response = patch(alice_client, avatar_url=url)

    assert response.status_code == 422
    stored = columns(session, alice_account)["avatar_url"]
    assert stored == "https://cdn.example.com/original.png"


def test_avatar_url_is_stored_in_normalized_form(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    response = patch(alice_client, avatar_url="HTTPS://CDN.Example.COM/Avatars/A.png")

    assert response.json()["avatar_url"] == "https://cdn.example.com/Avatars/A.png"
    stored = columns(session, alice_account)["avatar_url"]
    assert stored == "https://cdn.example.com/Avatars/A.png"


# --- there is no privacy setting -----------------------------------------
#
# An account is not public or private. `is_private` is no field of a profile:
# like any other name the API does not know, it is not read.


@pytest.mark.parametrize("is_private", [True, False, "true", 1, None])
def test_is_private_sent_alone_is_an_empty_update(
    alice_client: TestClient, session: Session, alice_account: User, is_private: object
) -> None:
    before = columns(session, alice_account)

    response = patch(alice_client, is_private=is_private)

    # With nothing editable in it, the request changes nothing and says so.
    assert response.status_code == 422
    [error] = response.json()["detail"]
    assert "At least one of display_name, bio or avatar_url" in error["msg"]
    assert columns(session, alice_account) == before


@pytest.mark.parametrize("is_private", [True, False])
def test_is_private_sent_with_a_valid_change_is_not_read(
    alice_client: TestClient, session: Session, alice_account: User, is_private: bool
) -> None:
    before = columns(session, alice_account)

    response = patch(alice_client, bio="Changed", is_private=is_private)

    assert response.status_code == 200
    assert set(response.json()) == MY_FIELDS
    assert "is_private" not in keys_in(response.json())
    after = columns(session, alice_account)
    assert after["bio"] == "Changed"
    changed = {name for name in before if before[name] != after[name]}
    assert changed <= {"bio", "updated_at"}
    # Nowhere to keep it: neither the account nor its row has such a thing.
    assert "is_private" not in after
    assert "is_private" not in alice_client.get("/users/me").json()
    assert "is_private" not in alice_client.get("/users/alice").json()


# --- timestamps ----------------------------------------------------------


def test_update_refreshes_updated_at_and_leaves_created_at(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    alice_account.created_at = long_ago
    alice_account.updated_at = long_ago
    session.flush()

    patch(alice_client, bio="Hello")

    stored = columns(session, alice_account)
    assert stored["created_at"] == long_ago
    assert stored["updated_at"] > long_ago


# --- authentication ------------------------------------------------------


def test_update_requires_authentication(
    client: TestClient, session: Session, alice_account: User
) -> None:
    before = columns(session, alice_account)

    response = patch(client, display_name="Someone Else")

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert columns(session, alice_account) == before


def test_unauthenticated_update_is_rejected_before_it_is_validated(
    client: TestClient,
) -> None:
    # Someone who is not signed in learns nothing from the validation.
    assert patch(client, display_name="").status_code == 401
    assert client.patch("/users/me", json={}).status_code == 401

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_token
from app.models import User, UserSession
from helpers import (
    SENSITIVE_KEYS,
    expire,
    keys_in,
    log_in,
    plant_session_cookie,
    session_token,
    set_cookie,
)

NOT_AUTHENTICATED = {"detail": "Not authenticated."}


def stored_session(session: Session, client: TestClient) -> UserSession:
    """The database row behind the session a client is logged in with."""
    return session.scalars(
        select(UserSession).where(
            UserSession.token_hash == hash_token(session_token(client))
        )
    ).one()


def replay(make_client, raw_token: str) -> TestClient:
    """A fresh client presenting a token, as someone who copied it would."""
    client = make_client()
    plant_session_cookie(client, raw_token)
    return client


# --- authenticating with a session ---------------------------------------


def test_valid_session_authenticates_a_request(
    alice_client: TestClient, alice_account: User
) -> None:
    response = alice_client.get("/auth/me")

    assert response.status_code == 200
    assert response.json()["id"] == str(alice_account.id)
    assert response.json()["username"] == "alice"


def test_me_returns_safe_account_fields_only(alice_client: TestClient) -> None:
    response = alice_client.get("/auth/me")

    assert set(response.json()) == {
        "id",
        "username",
        "email",
        "display_name",
        "bio",
        "avatar_url",
        "is_active",
        "email_verified_at",
        "created_at",
    }
    assert session_token(alice_client) not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_request_without_a_session_cookie_is_rejected(client: TestClient) -> None:
    response = client.get("/auth/me")

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


@pytest.mark.parametrize("token", ["", "unknown-token", "a" * 43, "a" * 5000])
def test_unknown_session_token_is_rejected(
    make_client, alice_client: TestClient, token: str
) -> None:
    response = replay(make_client, token).get("/auth/me")

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


def test_token_hash_is_not_accepted_as_a_token(
    make_client, alice_client: TestClient, session: Session
) -> None:
    # What a leaked database contains must not be usable as a credential.
    token_hash = stored_session(session, alice_client).token_hash

    assert replay(make_client, token_hash).get("/auth/me").status_code == 401


def test_expired_session_is_rejected(
    alice_client: TestClient, session: Session
) -> None:
    expire(session, stored_session(session, alice_client))

    response = alice_client.get("/auth/me")

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


def test_revoked_session_is_rejected(
    alice_client: TestClient, session: Session
) -> None:
    stored_session(session, alice_client).revoked_at = datetime.now(timezone.utc)
    session.flush()

    response = alice_client.get("/auth/me")

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


def test_session_of_an_inactive_user_is_rejected(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    alice_account.is_active = False
    session.flush()

    response = alice_client.get("/auth/me")

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/auth/me"),
        ("GET", "/auth/sessions"),
        ("POST", "/auth/sessions/revoke-others"),
        ("DELETE", f"/auth/sessions/{uuid.uuid4()}"),
    ],
)
def test_protected_endpoints_reject_unauthenticated_requests(
    client: TestClient, method: str, path: str
) -> None:
    response = client.request(method, path)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


# --- last_used_at --------------------------------------------------------


def test_last_used_at_is_not_rewritten_on_every_request(
    alice_client: TestClient, session: Session
) -> None:
    last_used_at = stored_session(session, alice_client).last_used_at

    alice_client.get("/auth/me")
    alice_client.get("/auth/me")

    assert stored_session(session, alice_client).last_used_at == last_used_at


def test_last_used_at_is_updated_once_the_interval_has_passed(
    alice_client: TestClient, session: Session
) -> None:
    stale = datetime.now(timezone.utc) - settings.session_last_used_interval
    stored_session(session, alice_client).last_used_at = stale - timedelta(seconds=1)
    session.flush()

    before = datetime.now(timezone.utc)
    assert alice_client.get("/auth/me").status_code == 200

    assert stored_session(session, alice_client).last_used_at >= before


def test_using_a_session_does_not_extend_its_expiry(
    alice_client: TestClient, session: Session
) -> None:
    user_session = stored_session(session, alice_client)
    expires_at = user_session.expires_at
    user_session.last_used_at -= timedelta(days=1)
    session.flush()

    alice_client.get("/auth/me")

    assert stored_session(session, alice_client).expires_at == expires_at


# --- logout --------------------------------------------------------------


def test_logout_revokes_the_current_session(
    alice_client: TestClient, session: Session
) -> None:
    user_session = stored_session(session, alice_client)

    response = alice_client.post("/auth/logout")

    assert response.status_code == 204
    # Revoked, not deleted.
    session.refresh(user_session)
    assert user_session.revoked_at is not None


def test_logout_clears_the_cookie(alice_client: TestClient) -> None:
    response = alice_client.post("/auth/logout")

    cookie = set_cookie(response)
    assert cookie.value == ""
    assert cookie["max-age"] == "0"
    assert cookie["path"] == "/"
    assert cookie["httponly"] is True
    assert cookie["secure"] is True
    assert session_token(alice_client) is None


def test_session_token_cannot_be_used_after_logout(
    make_client, alice_client: TestClient
) -> None:
    # Clearing the cookie is not enough: the token itself must stop working.
    raw_token = session_token(alice_client)
    assert replay(make_client, raw_token).get("/auth/me").status_code == 200

    alice_client.post("/auth/logout")

    assert replay(make_client, raw_token).get("/auth/me").status_code == 401


def test_logout_without_a_session_succeeds(client: TestClient) -> None:
    response = client.post("/auth/logout")

    assert response.status_code == 204
    assert response.content == b""


def test_logout_with_an_unusable_session_succeeds_and_clears_the_cookie(
    make_client, alice_client: TestClient, session: Session
) -> None:
    expire(session, stored_session(session, alice_client))

    for client in (alice_client, replay(make_client, "unknown-token")):
        response = client.post("/auth/logout")

        assert response.status_code == 204
        assert set_cookie(response)["max-age"] == "0"


def test_logout_twice_succeeds(alice_client: TestClient) -> None:
    assert alice_client.post("/auth/logout").status_code == 204
    assert alice_client.post("/auth/logout").status_code == 204


# --- multiple sessions ---------------------------------------------------


def test_multiple_sessions_can_coexist(make_client, alice_account: User) -> None:
    laptop, phone, tablet = make_client(), make_client(), make_client()
    for device in (laptop, phone, tablet):
        log_in(device)

    for device in (laptop, phone, tablet):
        assert device.get("/auth/me").status_code == 200


def test_logout_ends_only_the_current_session(make_client, alice_account: User) -> None:
    laptop, phone = make_client(), make_client()
    log_in(laptop)
    log_in(phone)

    laptop.post("/auth/logout")

    assert laptop.get("/auth/me").status_code == 401
    assert phone.get("/auth/me").status_code == 200


# --- listing sessions ----------------------------------------------------


def test_session_list_shows_the_users_sessions(
    make_client, session: Session, alice_account: User
) -> None:
    laptop, phone = make_client(), make_client()
    log_in(laptop)
    log_in(phone)

    response = laptop.get("/auth/sessions")

    assert response.status_code == 200
    listed = {item["id"]: item for item in response.json()}
    assert set(listed) == {
        str(stored_session(session, laptop).id),
        str(stored_session(session, phone).id),
    }
    assert listed[str(stored_session(session, laptop).id)]["is_current"] is True
    assert listed[str(stored_session(session, phone).id)]["is_current"] is False


def test_session_list_contains_only_safe_metadata(
    make_client, session: Session, alice_account: User
) -> None:
    laptop, phone = make_client(), make_client()
    log_in(laptop)
    log_in(phone)

    response = laptop.get("/auth/sessions")

    expected = {"id", "created_at", "last_used_at", "expires_at", "is_current"}
    for item in response.json():
        assert set(item) == expected
    assert keys_in(response.json()).isdisjoint(SENSITIVE_KEYS)
    for client in (laptop, phone):
        assert session_token(client) not in response.text
        assert stored_session(session, client).token_hash not in response.text


def test_session_list_leaves_out_revoked_and_expired_sessions(
    make_client, session: Session, alice_account: User
) -> None:
    current, logged_out, expired = make_client(), make_client(), make_client()
    for client in (current, logged_out, expired):
        log_in(client)
    expire(session, stored_session(session, expired))
    logged_out.post("/auth/logout")

    response = current.get("/auth/sessions")

    assert [item["id"] for item in response.json()] == [
        str(stored_session(session, current).id)
    ]


def test_users_cannot_see_each_others_sessions(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    alice, bob = make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")

    alice_listing = [item["id"] for item in alice.get("/auth/sessions").json()]
    bob_listing = [item["id"] for item in bob.get("/auth/sessions").json()]

    assert alice_listing == [str(stored_session(session, alice).id)]
    assert bob_listing == [str(stored_session(session, bob).id)]


# --- revoking one session ------------------------------------------------


def test_user_can_revoke_one_of_their_other_sessions(
    make_client, session: Session, alice_account: User
) -> None:
    laptop, phone = make_client(), make_client()
    log_in(laptop)
    log_in(phone)

    response = laptop.delete(f"/auth/sessions/{stored_session(session, phone).id}")

    assert response.status_code == 204
    assert "set-cookie" not in response.headers
    assert phone.get("/auth/me").status_code == 401
    assert laptop.get("/auth/me").status_code == 200


def test_revoking_the_current_session_also_clears_the_cookie(
    alice_client: TestClient, session: Session
) -> None:
    session_id = stored_session(session, alice_client).id

    response = alice_client.delete(f"/auth/sessions/{session_id}")

    assert response.status_code == 204
    assert set_cookie(response)["max-age"] == "0"
    assert alice_client.get("/auth/me").status_code == 401


def test_user_cannot_revoke_another_users_session(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    alice, bob = make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")
    bobs_session = stored_session(session, bob)

    response = alice.delete(f"/auth/sessions/{bobs_session.id}")

    assert response.status_code == 404
    session.refresh(bobs_session)
    assert bobs_session.revoked_at is None
    assert bob.get("/auth/me").status_code == 200


def test_another_users_session_looks_the_same_as_one_that_does_not_exist(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    alice, bob = make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")

    someone_elses = alice.delete(f"/auth/sessions/{stored_session(session, bob).id}")
    nonexistent = alice.delete(f"/auth/sessions/{uuid.uuid4()}")

    assert someone_elses.status_code == nonexistent.status_code == 404
    assert someone_elses.json() == nonexistent.json()
    assert nonexistent.json() == {"detail": "Session not found."}


def test_session_that_is_already_revoked_cannot_be_revoked_again(
    make_client, session: Session, alice_account: User
) -> None:
    laptop, phone = make_client(), make_client()
    log_in(laptop)
    log_in(phone)
    phone_session_id = stored_session(session, phone).id
    laptop.delete(f"/auth/sessions/{phone_session_id}")

    assert laptop.delete(f"/auth/sessions/{phone_session_id}").status_code == 404


def test_malformed_session_id_is_rejected(alice_client: TestClient) -> None:
    assert alice_client.delete("/auth/sessions/not-a-uuid").status_code == 422


# --- revoking the other sessions -----------------------------------------


def test_revoking_other_sessions_keeps_the_current_one(
    make_client, session: Session, alice_account: User
) -> None:
    laptop, phone, tablet = make_client(), make_client(), make_client()
    for device in (laptop, phone, tablet):
        log_in(device)

    response = laptop.post("/auth/sessions/revoke-others")

    assert response.status_code == 200
    assert response.json() == {"revoked_count": 2}
    assert laptop.get("/auth/me").status_code == 200
    assert phone.get("/auth/me").status_code == 401
    assert tablet.get("/auth/me").status_code == 401
    assert stored_session(session, laptop).revoked_at is None


def test_revoking_other_sessions_with_no_others_changes_nothing(
    alice_client: TestClient,
) -> None:
    response = alice_client.post("/auth/sessions/revoke-others")

    assert response.json() == {"revoked_count": 0}
    assert alice_client.get("/auth/me").status_code == 200


def test_revoking_other_sessions_does_not_touch_other_users(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    alice, alice_phone, bob = make_client(), make_client(), make_client()
    log_in(alice, "alice")
    log_in(alice_phone, "alice")
    log_in(bob, "bob")

    response = alice.post("/auth/sessions/revoke-others")

    assert response.json() == {"revoked_count": 1}
    assert bob.get("/auth/me").status_code == 200
    assert stored_session(session, bob).revoked_at is None

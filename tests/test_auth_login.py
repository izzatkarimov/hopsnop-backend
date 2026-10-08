from datetime import datetime, timezone

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    hash_token,
    password_needs_rehash,
    verify_password,
)
from app.models import User, UserSession
from app.services import auth as auth_service
from helpers import (
    PASSWORD,
    SENSITIVE_KEYS,
    add_user,
    keys_in,
    log_in,
    plant_session_cookie,
    session_token,
    set_cookie,
)

GENERIC_FAILURE = {"detail": "Invalid username/email or password."}


def count_sessions(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(UserSession))


# --- successful login ----------------------------------------------------


@pytest.mark.parametrize(
    "identifier",
    ["alice", "Alice", "  ALICE ", "alice@example.com", "Alice@Example.COM"],
)
def test_login_succeeds_with_username_or_email(
    client: TestClient, alice_account: User, identifier: str
) -> None:
    response = log_in(client, identifier)

    assert response.status_code == 200
    assert response.json()["username"] == "alice"
    assert response.json()["id"] == str(alice_account.id)


def test_login_creates_a_session(
    client: TestClient, session: Session, alice_account: User
) -> None:
    before = datetime.now(timezone.utc)
    log_in(client)
    after = datetime.now(timezone.utc)

    user_session = session.scalars(select(UserSession)).one()
    assert user_session.user_id == alice_account.id
    assert user_session.revoked_at is None
    assert before <= user_session.created_at <= after
    assert user_session.last_used_at == user_session.created_at
    lifetime = user_session.expires_at - user_session.created_at
    assert lifetime == settings.session_lifetime


def test_session_lifetime_comes_from_configuration(
    client: TestClient,
    session: Session,
    alice_account: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "session_lifetime_days", 1)

    response = log_in(client)

    user_session = session.scalars(select(UserSession)).one()
    assert (user_session.expires_at - user_session.created_at).days == 1
    assert set_cookie(response)["max-age"] == str(24 * 60 * 60)


def test_raw_session_token_is_not_stored(
    client: TestClient, session: Session, alice_account: User
) -> None:
    log_in(client)

    raw_token = session_token(client)
    user_session = session.scalars(select(UserSession)).one()
    assert len(raw_token) >= 43
    assert user_session.token_hash != raw_token
    assert user_session.token_hash == hash_token(raw_token)


def test_every_login_gets_its_own_token(
    make_client, session: Session, alice_account: User
) -> None:
    first, second = make_client(), make_client()
    log_in(first)
    log_in(second)

    assert session_token(first) != session_token(second)
    assert count_sessions(session) == 2


def test_login_response_does_not_contain_the_token_or_other_secrets(
    client: TestClient, alice_account: User
) -> None:
    response = log_in(client)

    assert session_token(client) not in response.text
    assert keys_in(response.json()).isdisjoint(SENSITIVE_KEYS)
    assert alice_account.password_hash not in response.text
    assert response.headers["cache-control"] == "no-store"


# --- the session cookie --------------------------------------------------


def test_session_cookie_is_http_only(client: TestClient, alice_account: User) -> None:
    cookie = set_cookie(log_in(client))

    assert cookie["httponly"] is True


def test_session_cookie_attributes(client: TestClient, alice_account: User) -> None:
    cookie = set_cookie(log_in(client))

    assert cookie.key == "__Host-hopsnop_session"
    assert cookie["samesite"].lower() == "lax"
    assert cookie["path"] == "/"
    # Host-only: without a Domain attribute it is not sent to subdomains.
    assert cookie["domain"] == ""
    assert cookie["max-age"] == str(int(settings.session_lifetime.total_seconds()))


def test_session_cookie_is_secure_in_production(
    client: TestClient, alice_account: User
) -> None:
    assert settings.environment == "production"

    cookie = set_cookie(log_in(client))

    assert cookie["secure"] is True


def test_session_cookie_is_not_secure_only_in_development(
    client: TestClient, alice_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "environment", "development")

    cookie = set_cookie(log_in(client))

    assert cookie["secure"] == ""
    # Everything else about the cookie is unchanged.
    assert cookie["httponly"] is True
    assert cookie["samesite"].lower() == "lax"


def test_samesite_policy_comes_from_configuration(
    client: TestClient, alice_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "session_cookie_samesite", "strict")

    assert set_cookie(log_in(client))["samesite"].lower() == "strict"


# --- failed login --------------------------------------------------------


def test_incorrect_password_fails(
    client: TestClient, session: Session, alice_account: User
) -> None:
    response = log_in(client, "alice", "not the right password")

    assert response.status_code == 401
    assert response.json() == GENERIC_FAILURE
    assert "set-cookie" not in response.headers
    assert count_sessions(session) == 0


@pytest.mark.parametrize("identifier", ["nobody", "nobody@example.com"])
def test_nonexistent_account_fails(
    client: TestClient, session: Session, alice_account: User, identifier: str
) -> None:
    response = log_in(client, identifier)

    assert response.status_code == 401
    assert response.json() == GENERIC_FAILURE
    assert count_sessions(session) == 0


def test_inactive_user_cannot_log_in(client: TestClient, session: Session) -> None:
    add_user(session, "alice", active=False)

    response = log_in(client)

    assert response.status_code == 401
    assert response.json() == GENERIC_FAILURE
    assert "set-cookie" not in response.headers
    assert count_sessions(session) == 0


def test_login_failure_does_not_reveal_whether_the_account_exists(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_user(session, "inactive", active=False)
    add_user(session, "unverified", verified=False)

    failures = [
        log_in(client, "alice", "wrong password"),
        log_in(client, "alice@example.com", "wrong password"),
        log_in(client, "nobody", PASSWORD),
        log_in(client, "nobody@example.com", PASSWORD),
        log_in(client, "inactive", PASSWORD),
        log_in(client, "inactive", "wrong password"),
        log_in(client, "unverified", "wrong password"),
    ]

    assert {response.status_code for response in failures} == {401}
    assert {response.text for response in failures} == {failures[0].text}
    assert len({tuple(sorted(response.headers)) for response in failures}) == 1


def test_password_is_verified_even_when_the_account_does_not_exist(
    client: TestClient, alice_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Otherwise an unknown account would answer measurably faster than a
    # known one, which reveals the same thing a different error would.
    verified_against = []

    def spy(password: str, password_hash: str) -> bool:
        verified_against.append(password_hash)
        return verify_password(password, password_hash)

    monkeypatch.setattr(auth_service, "verify_password", spy)

    log_in(client, "nobody", PASSWORD)
    log_in(client, "alice", "wrong password")

    assert verified_against == [DUMMY_PASSWORD_HASH, alice_account.password_hash]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"identifier": "alice"},
        {"password": PASSWORD},
        {"identifier": "", "password": PASSWORD},
        {"identifier": "alice", "password": ""},
        {"identifier": "alice", "password": "x" * 129},
        {"identifier": "a" * 256, "password": PASSWORD},
    ],
)
def test_malformed_login_is_rejected(
    client: TestClient, alice_account: User, payload: dict
) -> None:
    response = client.post("/auth/login", json=payload)

    assert response.status_code == 422
    assert PASSWORD not in response.text


# --- email verification requirement --------------------------------------


def test_unverified_user_cannot_log_in(client: TestClient, session: Session) -> None:
    add_user(session, "alice", verified=False)

    response = log_in(client)

    assert response.status_code == 403
    assert response.json() == {"detail": "Email address is not verified."}
    assert "set-cookie" not in response.headers
    assert count_sessions(session) == 0


def test_unverified_status_is_only_revealed_to_someone_with_the_password(
    client: TestClient, session: Session
) -> None:
    add_user(session, "alice", verified=False)

    response = log_in(client, "alice", "wrong password")

    assert response.status_code == 401
    assert response.json() == GENERIC_FAILURE


# --- session hygiene -----------------------------------------------------


def test_login_never_adopts_a_token_supplied_by_the_client(
    client: TestClient, session: Session, alice_account: User
) -> None:
    # Session fixation: an attacker plants a token they know in the victim's
    # browser and waits for the victim to log in with it.
    planted = "token-chosen-by-an-attacker"
    plant_session_cookie(client, planted)

    log_in(client)

    assert session_token(client) != planted
    assert session.scalars(select(UserSession.token_hash)).all() == [
        hash_token(session_token(client))
    ]


def test_logging_in_again_ends_the_session_it_replaces(
    client: TestClient, session: Session, alice_account: User
) -> None:
    log_in(client)
    first_token = session_token(client)

    log_in(client)

    assert session_token(client) != first_token
    replaced = session.scalars(
        select(UserSession).where(UserSession.token_hash == hash_token(first_token))
    ).one()
    assert replaced.revoked_at is not None
    assert client.get("/auth/me").status_code == 200


def test_failed_login_keeps_the_existing_session(
    alice_client: TestClient, session: Session
) -> None:
    log_in(alice_client, "alice", "wrong password")

    assert session.scalars(select(UserSession)).one().revoked_at is None
    assert alice_client.get("/auth/me").status_code == 200


def test_outdated_password_hash_is_upgraded_at_login(
    client: TestClient, session: Session, alice_account: User
) -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    alice_account.password_hash = weak.hash(PASSWORD)
    session.flush()

    assert log_in(client).status_code == 200

    session.refresh(alice_account)
    assert not password_needs_rehash(alice_account.password_hash)
    assert verify_password(PASSWORD, alice_account.password_hash)


def test_current_password_hash_is_left_alone_at_login(
    client: TestClient, session: Session, alice_account: User
) -> None:
    original = alice_account.password_hash

    log_in(client)

    session.refresh(alice_account)
    assert alice_account.password_hash == original

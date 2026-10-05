from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_token, verify_password
from app.models import EmailVerificationToken, User, UserSession
from app.services import auth as auth_service
from helpers import (
    PASSWORD,
    SENSITIVE_KEYS,
    add_user,
    keys_in,
    registration,
    token_from,
)


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model))


# --- successful registration ---------------------------------------------


def test_valid_registration_succeeds(client: TestClient, session: Session) -> None:
    response = client.post("/auth/register", json=registration())

    assert response.status_code == 201
    body = response.json()
    assert body["username"] == "alice"
    assert body["email"] == "alice@example.com"
    assert body["display_name"] == "Alice"
    assert body["is_active"] is True
    assert body["is_private"] is False

    user = session.scalars(select(User)).one()
    assert str(user.id) == body["id"]


def test_password_is_stored_only_as_an_argon2id_hash(
    client: TestClient, session: Session
) -> None:
    client.post("/auth/register", json=registration())

    user = session.scalars(select(User)).one()
    assert user.password_hash != PASSWORD
    assert PASSWORD not in user.password_hash
    assert user.password_hash.startswith("$argon2id$")
    assert verify_password(PASSWORD, user.password_hash)


def test_password_is_hashed_exactly_as_typed(
    client: TestClient, session: Session
) -> None:
    password = "  spaces are part of it  "
    client.post("/auth/register", json=registration(password=password))

    user = session.scalars(select(User)).one()
    assert verify_password(password, user.password_hash)
    assert not verify_password(password.strip(), user.password_hash)


def test_username_is_normalized_to_lowercase(
    client: TestClient, session: Session
) -> None:
    response = client.post("/auth/register", json=registration(username="  Alice_01 "))

    assert response.status_code == 201
    assert response.json()["username"] == "alice_01"
    assert session.scalars(select(User.username)).one() == "alice_01"


def test_email_is_normalized_to_lowercase(client: TestClient, session: Session) -> None:
    response = client.post(
        "/auth/register", json=registration(email="  Alice.Smith@Example.COM ")
    )

    assert response.status_code == 201
    assert response.json()["email"] == "alice.smith@example.com"
    assert session.scalars(select(User.email)).one() == "alice.smith@example.com"


def test_display_name_is_trimmed_but_otherwise_kept(client: TestClient) -> None:
    response = client.post(
        "/auth/register", json=registration(display_name="  Alice Smith  ")
    )

    assert response.json()["display_name"] == "Alice Smith"


def test_registration_creates_an_unverified_account(
    client: TestClient, session: Session
) -> None:
    response = client.post("/auth/register", json=registration())

    assert response.json()["email_verified_at"] is None
    assert session.scalars(select(User)).one().email_verified_at is None


def test_registration_does_not_log_the_user_in(
    client: TestClient, session: Session
) -> None:
    response = client.post("/auth/register", json=registration())

    assert "set-cookie" not in response.headers
    assert count(session, UserSession) == 0
    assert client.get("/auth/me").status_code == 401


def test_registration_sends_a_verification_link(client: TestClient, outbox) -> None:
    client.post("/auth/register", json=registration())

    [(recipient, url)] = outbox.verification
    assert recipient == "alice@example.com"
    assert url.startswith(f"{settings.frontend_url}/verify-email?token=")


def test_verification_token_is_stored_hashed(
    client: TestClient, session: Session, outbox
) -> None:
    before = datetime.now(timezone.utc)
    client.post("/auth/register", json=registration())

    raw_token = token_from(outbox.verification[0][1])
    token = session.scalars(select(EmailVerificationToken)).one()
    assert token.token_hash == hash_token(raw_token)
    assert token.token_hash != raw_token
    assert token.used_at is None
    lifetime = settings.email_verification_token_lifetime
    after = datetime.now(timezone.utc)
    assert before + lifetime <= token.expires_at <= after + lifetime


def test_registration_response_contains_no_secrets(client: TestClient, outbox) -> None:
    response = client.post("/auth/register", json=registration())

    assert keys_in(response.json()).isdisjoint(SENSITIVE_KEYS)
    assert PASSWORD not in response.text
    assert token_from(outbox.verification[0][1]) not in response.text


# --- conflicts -----------------------------------------------------------


@pytest.mark.parametrize("username", ["alice", "ALICE", " Alice "])
def test_duplicate_username_is_rejected(
    client: TestClient, session: Session, username: str
) -> None:
    add_user(session, "alice")

    response = client.post(
        "/auth/register",
        json=registration(username=username, email="another@example.com"),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Username already in use."}
    assert count(session, User) == 1


@pytest.mark.parametrize("email", ["alice@example.com", "Alice@Example.com"])
def test_duplicate_email_is_rejected(
    client: TestClient, session: Session, email: str
) -> None:
    add_user(session, "alice")

    response = client.post(
        "/auth/register",
        json=registration(username="another", email=email),
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Email already in use."}
    assert count(session, User) == 1


@pytest.mark.parametrize(
    ("taken", "detail"),
    [
        (
            {"username": "alice", "email": "another@example.com"},
            "Username already in use.",
        ),
        (
            {"username": "another", "email": "alice@example.com"},
            "Email already in use.",
        ),
    ],
)
def test_conflict_that_is_only_caught_by_the_database_is_reported_cleanly(
    client: TestClient,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    taken: dict,
    detail: str,
) -> None:
    # Two registrations racing for the same name: the second one's check runs
    # before the first one's insert, so only the unique constraint stops it.
    add_user(session, "alice")
    monkeypatch.setattr(auth_service, "_registration_conflict", lambda *args: None)

    response = client.post("/auth/register", json=registration(**taken))

    assert response.status_code == 409
    assert response.json() == {"detail": detail}
    assert session.scalars(select(User.username)).all() == ["alice"]


# --- invalid input -------------------------------------------------------


@pytest.mark.parametrize(
    "invalid",
    [
        {"username": "al"},
        {"username": "a" * 31},
        {"username": "alice smith"},
        {"username": "alice-smith"},
        {"username": "alice@home"},
        {"username": "álice"},
        {"username": ""},
        {"username": None},
        {"email": "not-an-email"},
        {"email": "alice@"},
        {"email": "alice@localhost"},
        {"email": "a" * 250 + "@example.com"},
        {"email": "älice@example.com"},
        {"email": ""},
        {"password": "short"},
        {"password": "x" * 11},
        {"password": "x" * 129},
        {"password": ""},
        {"password": 123456789012},
        {"display_name": ""},
        {"display_name": "   "},
        {"display_name": "x" * 51},
    ],
)
def test_invalid_registration_data_is_rejected(
    client: TestClient, session: Session, invalid: dict
) -> None:
    response = client.post("/auth/register", json=registration(**invalid))

    assert response.status_code == 422
    assert count(session, User) == 0


@pytest.mark.parametrize("missing", ["username", "email", "password", "display_name"])
def test_registration_requires_every_field(
    client: TestClient, session: Session, missing: str
) -> None:
    payload = registration()
    del payload[missing]

    response = client.post("/auth/register", json=payload)

    assert response.status_code == 422
    assert count(session, User) == 0


def test_field_length_limits_are_inclusive(client: TestClient) -> None:
    response = client.post(
        "/auth/register",
        json=registration(username="a" * 30, display_name="x" * 50, password="x" * 12),
    )

    assert response.status_code == 201


def test_password_minimum_length_comes_from_configuration(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "password_min_length", 20)

    too_short = client.post("/auth/register", json=registration(password="x" * 19))
    long_enough = client.post("/auth/register", json=registration(password="x" * 20))

    assert too_short.status_code == 422
    assert long_enough.status_code == 201


def test_password_needs_no_particular_character_classes(client: TestClient) -> None:
    response = client.post("/auth/register", json=registration(password="a" * 12))

    assert response.status_code == 201


def test_validation_error_does_not_echo_the_submitted_values(
    client: TestClient,
) -> None:
    # A missing field makes the whole body the "rejected input"; a too-short
    # password makes the password itself the rejected input.
    payload = registration()
    del payload["username"]
    missing_field = client.post("/auth/register", json=payload)
    short_password = client.post(
        "/auth/register", json=registration(password="hunter2")
    )

    assert PASSWORD not in missing_field.text
    assert "hunter2" not in short_password.text
    for response in (missing_field, short_password):
        assert response.status_code == 422
        for error in response.json()["detail"]:
            assert set(error) == {"loc", "msg", "type"}


def test_client_cannot_set_fields_it_does_not_own(
    client: TestClient, session: Session
) -> None:
    response = client.post(
        "/auth/register",
        json=registration(
            is_active=False,
            email_verified_at="2020-01-01T00:00:00Z",
            password_hash="chosen-by-client",
            id="00000000-0000-0000-0000-000000000000",
        ),
    )

    assert response.status_code == 201
    user = session.scalars(select(User)).one()
    assert user.is_active is True
    assert user.email_verified_at is None
    assert user.password_hash.startswith("$argon2id$")
    assert str(user.id) != "00000000-0000-0000-0000-000000000000"


# --- transaction ---------------------------------------------------------


def test_registration_is_all_or_nothing(
    make_client, session: Session, monkeypatch: pytest.MonkeyPatch, outbox
) -> None:
    # The user row has been written when creating the token fails.
    def fail() -> str:
        raise RuntimeError("token generation failed")

    monkeypatch.setattr(auth_service, "generate_token", fail)
    client = make_client(raise_server_exceptions=False)

    response = client.post("/auth/register", json=registration())

    assert response.status_code == 500
    assert count(session, User) == 0
    assert count(session, EmailVerificationToken) == 0
    assert outbox.verification == []

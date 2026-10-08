from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import generate_token, hash_token, verify_password
from app.models import PasswordResetToken, User, UserSession
from app.services import auth as auth_service
from helpers import (
    PASSWORD,
    add_user,
    expire,
    let_cooldown_pass,
    log_in,
    registration,
    token_from,
)

NEW_PASSWORD = "a brand new passphrase"
INVALID_TOKEN = {"detail": "Invalid or expired password reset token."}


def forgot(client: TestClient, email: str = "alice@example.com"):
    return client.post("/auth/forgot-password", json={"email": email})


def reset(client: TestClient, token: str, new_password: str = NEW_PASSWORD):
    return client.post(
        "/auth/reset-password",
        json={"token": token, "new_password": new_password},
    )


@pytest.fixture
def raw_token(
    client: TestClient, session: Session, alice_account: User, outbox
) -> str:
    """The token from a reset link requested for alice.

    Issued long enough ago that another link may be asked for; what happens
    sooner than that is in ``test_rate_limiting.py``.
    """
    assert forgot(client).status_code == 202
    let_cooldown_pass(session)
    return token_from(outbox.password_reset[0][1])


# --- requesting a reset --------------------------------------------------


def test_forgot_password_sends_a_reset_link(
    client: TestClient, alice_account: User, outbox
) -> None:
    response = forgot(client)

    assert response.status_code == 202
    [(recipient, url)] = outbox.password_reset
    assert recipient == "alice@example.com"
    assert url.startswith(f"{settings.frontend_url}/reset-password?token=")


def test_forgot_password_response_is_generic(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_user(session, "inactive", active=False)

    responses = [
        forgot(client, "alice@example.com"),  # exists
        forgot(client, "nobody@example.com"),  # no such account
        forgot(client, "inactive@example.com"),  # deactivated
    ]

    assert {response.status_code for response in responses} == {202}
    assert len({response.text for response in responses}) == 1
    assert len({tuple(sorted(response.headers)) for response in responses}) == 1


def test_forgot_password_sends_nothing_for_unknown_or_inactive_accounts(
    client: TestClient, session: Session, outbox
) -> None:
    add_user(session, "inactive", active=False)

    forgot(client, "nobody@example.com")
    forgot(client, "inactive@example.com")

    assert outbox.password_reset == []
    assert session.scalar(select(func.count()).select_from(PasswordResetToken)) == 0


def test_forgot_password_response_does_not_contain_the_token(
    client: TestClient, alice_account: User, outbox
) -> None:
    response = forgot(client)

    assert token_from(outbox.password_reset[0][1]) not in response.text
    assert set(response.json()) == {"message"}


def test_forgot_password_accepts_the_email_in_any_case(
    client: TestClient, alice_account: User, outbox
) -> None:
    forgot(client, " Alice@Example.COM ")

    assert len(outbox.password_reset) == 1


def test_reset_token_is_stored_hashed(
    client: TestClient, session: Session, raw_token: str
) -> None:
    token = session.scalars(select(PasswordResetToken)).one()

    assert token.token_hash == hash_token(raw_token)
    assert token.token_hash != raw_token
    assert token.used_at is None


def test_reset_token_lifetime_comes_from_configuration(
    client: TestClient,
    session: Session,
    alice_account: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert settings.password_reset_token_lifetime.total_seconds() == 30 * 60
    monkeypatch.setattr(settings, "password_reset_token_lifetime_minutes", 5)

    forgot(client)

    token = session.scalars(select(PasswordResetToken)).one()
    assert (token.expires_at - token.created_at).total_seconds() == 5 * 60


def test_new_reset_request_invalidates_the_previous_token(
    client: TestClient, session: Session, outbox, raw_token: str
) -> None:
    forgot(client)
    newest = token_from(outbox.password_reset[1][1])

    assert session.scalars(select(PasswordResetToken.token_hash)).all() == [
        hash_token(newest)
    ]
    assert reset(client, raw_token).status_code == 400
    assert reset(client, newest).status_code == 200


def test_forgot_password_does_not_end_sessions_or_change_the_password(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    # Anyone can request a reset for any address; that alone must not affect
    # the account.
    forgot(alice_client)

    assert alice_client.get("/auth/me").status_code == 200
    session.refresh(alice_account)
    assert verify_password(PASSWORD, alice_account.password_hash)


@pytest.mark.parametrize("payload", [{}, {"email": ""}, {"email": "not-an-email"}])
def test_malformed_forgot_password_request_is_rejected(
    client: TestClient, payload: dict
) -> None:
    assert client.post("/auth/forgot-password", json=payload).status_code == 422


# --- completing a reset --------------------------------------------------


def test_valid_reset_token_works(client: TestClient, raw_token: str) -> None:
    response = reset(client, raw_token)

    assert response.status_code == 200
    assert response.json() == {"message": "Password has been reset."}
    assert log_in(client, "alice", NEW_PASSWORD).status_code == 200


def test_old_password_stops_working_after_reset(
    client: TestClient, raw_token: str
) -> None:
    reset(client, raw_token)

    assert log_in(client, "alice", PASSWORD).status_code == 401


def test_password_is_rehashed_after_reset(
    client: TestClient, session: Session, alice_account: User, raw_token: str
) -> None:
    old_hash = alice_account.password_hash

    reset(client, raw_token)

    session.refresh(alice_account)
    assert alice_account.password_hash != old_hash
    assert alice_account.password_hash.startswith("$argon2id$")
    assert NEW_PASSWORD not in alice_account.password_hash
    assert verify_password(NEW_PASSWORD, alice_account.password_hash)
    assert not verify_password(PASSWORD, alice_account.password_hash)


def test_reset_marks_the_token_as_used(
    client: TestClient, session: Session, raw_token: str
) -> None:
    before = datetime.now(timezone.utc)

    reset(client, raw_token)

    token = session.scalars(select(PasswordResetToken)).one()
    assert before <= token.used_at <= datetime.now(timezone.utc)


def test_reset_token_cannot_be_reused(
    client: TestClient, session: Session, alice_account: User, raw_token: str
) -> None:
    reset(client, raw_token)
    session.refresh(alice_account)
    hash_after_first_use = alice_account.password_hash

    response = reset(client, raw_token, "yet another passphrase")

    assert response.status_code == 400
    assert response.json() == INVALID_TOKEN
    session.refresh(alice_account)
    assert alice_account.password_hash == hash_after_first_use
    assert log_in(client, "alice", "yet another passphrase").status_code == 401


def test_expired_reset_token_fails(
    client: TestClient, session: Session, alice_account: User, raw_token: str
) -> None:
    expire(session, session.scalars(select(PasswordResetToken)).one())

    response = reset(client, raw_token)

    assert response.status_code == 400
    assert response.json() == INVALID_TOKEN
    session.refresh(alice_account)
    assert verify_password(PASSWORD, alice_account.password_hash)


def test_invalid_reset_token_fails(
    client: TestClient, session: Session, alice_account: User, raw_token: str
) -> None:
    response = reset(client, generate_token())

    assert response.status_code == 400
    assert response.json() == INVALID_TOKEN
    session.refresh(alice_account)
    assert verify_password(PASSWORD, alice_account.password_hash)


def test_token_hash_is_not_accepted_as_a_reset_token(
    client: TestClient, session: Session, alice_account: User, raw_token: str
) -> None:
    # What a leaked database contains must not be usable as a credential.
    response = reset(client, hash_token(raw_token))

    assert response.status_code == 400
    session.refresh(alice_account)
    assert verify_password(PASSWORD, alice_account.password_hash)


def test_reset_token_of_an_inactive_user_fails(
    client: TestClient, session: Session, alice_account: User, raw_token: str
) -> None:
    alice_account.is_active = False
    session.flush()

    response = reset(client, raw_token)

    assert response.status_code == 400
    assert response.json() == INVALID_TOKEN
    session.refresh(alice_account)
    assert verify_password(PASSWORD, alice_account.password_hash)
    assert session.scalars(select(PasswordResetToken)).one().used_at is None


def test_verification_token_is_not_accepted_as_a_reset_token(
    client: TestClient, session: Session, outbox
) -> None:
    client.post("/auth/register", json=registration())
    verification_token = token_from(outbox.verification[0][1])

    response = reset(client, verification_token)

    assert response.status_code == 400
    user = session.scalars(select(User)).one()
    assert verify_password(PASSWORD, user.password_hash)


@pytest.mark.parametrize("new_password", ["short", "x" * 11, "x" * 129, ""])
def test_new_password_must_satisfy_the_password_policy(
    client: TestClient,
    session: Session,
    alice_account: User,
    raw_token: str,
    new_password: str,
) -> None:
    response = reset(client, raw_token, new_password)

    assert response.status_code == 422
    assert raw_token not in response.text
    # The attempt did not consume the token.
    assert session.scalars(select(PasswordResetToken)).one().used_at is None
    assert reset(client, raw_token).status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"token": "abc"},
        {"new_password": NEW_PASSWORD},
        {"token": "", "new_password": NEW_PASSWORD},
    ],
)
def test_malformed_reset_request_is_rejected(client: TestClient, payload: dict) -> None:
    assert client.post("/auth/reset-password", json=payload).status_code == 422


def test_reset_does_not_log_the_user_in(
    client: TestClient, session: Session, raw_token: str
) -> None:
    response = reset(client, raw_token)

    assert "set-cookie" not in response.headers
    assert session.scalar(select(func.count()).select_from(UserSession)) == 0


def test_reset_does_not_verify_an_unverified_email(
    client: TestClient, session: Session, outbox
) -> None:
    client.post("/auth/register", json=registration())
    forgot(client)

    assert reset(client, token_from(outbox.password_reset[0][1])).status_code == 200

    assert session.scalars(select(User)).one().email_verified_at is None
    assert log_in(client, "alice", NEW_PASSWORD).status_code == 403


# --- sessions ------------------------------------------------------------


def test_password_reset_revokes_existing_sessions(
    make_client, session: Session, alice_account: User, outbox
) -> None:
    laptop, phone = make_client(), make_client()
    log_in(laptop)
    log_in(phone)
    forgot(laptop)

    reset(make_client(), token_from(outbox.password_reset[0][1]))

    assert laptop.get("/auth/me").status_code == 401
    assert phone.get("/auth/me").status_code == 401
    revoked = session.scalars(select(UserSession.revoked_at)).all()
    assert len(revoked) == 2
    assert all(revoked_at is not None for revoked_at in revoked)


def test_password_reset_leaves_other_users_sessions_alone(
    make_client, alice_account: User, bob_account: User, outbox
) -> None:
    alice, bob = make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")
    forgot(alice, "alice@example.com")

    reset(make_client(), token_from(outbox.password_reset[0][1]))

    assert alice.get("/auth/me").status_code == 401
    assert bob.get("/auth/me").status_code == 200


# --- transaction ---------------------------------------------------------


def test_password_reset_is_all_or_nothing(
    make_client,
    session: Session,
    alice_account: User,
    outbox,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logged_in = make_client()
    log_in(logged_in)
    forgot(logged_in)
    raw_token = token_from(outbox.password_reset[0][1])

    # Revoking the sessions is the last step and it fails: the new password
    # and the used token must not survive on their own.
    def fail(*args: object) -> None:
        raise RuntimeError("could not revoke sessions")

    with monkeypatch.context() as patch:
        patch.setattr(auth_service, "update", fail)
        response = reset(make_client(raise_server_exceptions=False), raw_token)

    assert response.status_code == 500
    session.refresh(alice_account)
    assert verify_password(PASSWORD, alice_account.password_hash)
    assert session.scalars(select(PasswordResetToken)).one().used_at is None
    assert logged_in.get("/auth/me").status_code == 200

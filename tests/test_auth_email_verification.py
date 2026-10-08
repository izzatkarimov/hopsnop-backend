from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import generate_token, hash_token
from app.models import EmailVerificationToken, User, UserSession
from app.services import auth as auth_service
from helpers import (
    add_user,
    expire,
    let_cooldown_pass,
    log_in,
    registration,
    token_from,
)

INVALID_TOKEN = {"detail": "Invalid or expired verification token."}


@pytest.fixture
def raw_token(client: TestClient, session: Session, outbox) -> str:
    """The token from the verification link of a newly registered alice.

    Issued long enough ago that another link may be asked for; what happens
    sooner than that is in ``test_rate_limiting.py``.
    """
    assert client.post("/auth/register", json=registration()).status_code == 201
    let_cooldown_pass(session)
    return token_from(outbox.verification[0][1])


def alice(session: Session) -> User:
    return session.scalars(select(User).where(User.username == "alice")).one()


def verify(client: TestClient, token: str):
    return client.post("/auth/verify-email", json={"token": token})


def resend(client: TestClient, email: str = "alice@example.com"):
    return client.post("/auth/resend-verification", json={"email": email})


# --- verifying -----------------------------------------------------------


def test_valid_verification_succeeds(client: TestClient, raw_token: str) -> None:
    response = verify(client, raw_token)

    assert response.status_code == 200
    assert response.json() == {"message": "Email address verified."}


def test_verification_sets_email_verified_at(
    client: TestClient, session: Session, raw_token: str
) -> None:
    assert alice(session).email_verified_at is None
    before = datetime.now(timezone.utc)

    verify(client, raw_token)

    assert before <= alice(session).email_verified_at <= datetime.now(timezone.utc)


def test_verification_marks_the_token_as_used(
    client: TestClient, session: Session, raw_token: str
) -> None:
    verify(client, raw_token)

    token = session.scalars(select(EmailVerificationToken)).one()
    assert token.used_at is not None
    assert token.used_at == alice(session).email_verified_at


def test_verified_user_can_log_in(client: TestClient, raw_token: str) -> None:
    assert log_in(client).status_code == 403

    verify(client, raw_token)

    assert log_in(client).status_code == 200
    assert client.get("/auth/me").json()["email_verified_at"] is not None


def test_verification_does_not_log_the_user_in(
    client: TestClient, session: Session, raw_token: str
) -> None:
    response = verify(client, raw_token)

    assert "set-cookie" not in response.headers
    assert session.scalar(select(func.count()).select_from(UserSession)) == 0


def test_verification_token_cannot_be_used_twice(
    client: TestClient, session: Session, raw_token: str
) -> None:
    verify(client, raw_token)
    verified_at = alice(session).email_verified_at

    response = verify(client, raw_token)

    assert response.status_code == 400
    assert response.json() == INVALID_TOKEN
    assert alice(session).email_verified_at == verified_at


def test_expired_verification_token_is_rejected(
    client: TestClient, session: Session, raw_token: str
) -> None:
    expire(session, session.scalars(select(EmailVerificationToken)).one())

    response = verify(client, raw_token)

    assert response.status_code == 400
    assert response.json() == INVALID_TOKEN
    assert alice(session).email_verified_at is None


def test_invalid_verification_token_is_rejected(
    client: TestClient, session: Session, raw_token: str
) -> None:
    response = verify(client, generate_token())

    assert response.status_code == 400
    assert response.json() == INVALID_TOKEN
    assert alice(session).email_verified_at is None


def test_token_hash_is_not_accepted_as_a_verification_token(
    client: TestClient, session: Session, raw_token: str
) -> None:
    # What a leaked database contains must not be usable as a credential.
    response = verify(client, hash_token(raw_token))

    assert response.status_code == 400
    assert alice(session).email_verified_at is None


def test_verification_token_of_an_inactive_user_is_rejected(
    client: TestClient, session: Session, raw_token: str
) -> None:
    alice(session).is_active = False
    session.flush()

    response = verify(client, raw_token)

    assert response.status_code == 400
    assert response.json() == INVALID_TOKEN
    assert alice(session).email_verified_at is None
    assert session.scalars(select(EmailVerificationToken)).one().used_at is None


@pytest.mark.parametrize(
    "payload", [{}, {"token": ""}, {"token": None}, {"token": "x" * 257}]
)
def test_malformed_verification_request_is_rejected(
    client: TestClient, payload: dict
) -> None:
    assert client.post("/auth/verify-email", json=payload).status_code == 422


def test_verification_token_lifetime_comes_from_configuration(
    client: TestClient, session: Session, outbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "email_verification_token_lifetime_hours", 2)

    client.post("/auth/register", json=registration())

    token = session.scalars(select(EmailVerificationToken)).one()
    assert (token.expires_at - token.created_at).total_seconds() == 2 * 3600


def test_verification_is_all_or_nothing(
    make_client, session: Session, raw_token: str
) -> None:
    # Marking the user as verified fails; the token must not end up consumed.
    def fail(mapper: object, connection: object, target: object) -> None:
        raise RuntimeError("user update failed")

    event.listen(User, "before_update", fail)
    try:
        response = verify(make_client(raise_server_exceptions=False), raw_token)
    finally:
        event.remove(User, "before_update", fail)

    assert response.status_code == 500
    assert alice(session).email_verified_at is None
    assert session.scalars(select(EmailVerificationToken)).one().used_at is None


# --- requesting a new link -----------------------------------------------


def test_resend_issues_a_new_verification_link(
    client: TestClient, outbox, raw_token: str
) -> None:
    response = resend(client)

    assert response.status_code == 202
    assert len(outbox.verification) == 2
    recipient, url = outbox.verification[1]
    assert recipient == "alice@example.com"
    new_token = token_from(url)
    assert new_token != raw_token
    assert verify(client, new_token).status_code == 200


def test_resend_invalidates_the_previous_token(
    client: TestClient, session: Session, raw_token: str
) -> None:
    resend(client)

    assert verify(client, raw_token).status_code == 400
    assert alice(session).email_verified_at is None


def test_only_one_verification_token_is_kept_per_user(
    client: TestClient, session: Session, outbox, raw_token: str
) -> None:
    for _ in range(3):
        resend(client)
        let_cooldown_pass(session)

    assert len(outbox.verification) == 4
    newest = token_from(outbox.verification[-1][1])
    assert session.scalars(select(EmailVerificationToken.token_hash)).all() == [
        hash_token(newest)
    ]


def test_resend_accepts_the_email_in_any_case(
    client: TestClient, outbox, raw_token: str
) -> None:
    resend(client, " Alice@Example.COM ")

    assert len(outbox.verification) == 2


def test_resend_does_not_reveal_whether_the_account_exists(
    client: TestClient, session: Session, raw_token: str
) -> None:
    add_user(session, "verified")
    add_user(session, "inactive", verified=False, active=False)

    responses = [
        resend(client, "alice@example.com"),  # exists, needs verification
        resend(client, "nobody@example.com"),  # no such account
        resend(client, "verified@example.com"),  # already verified
        resend(client, "inactive@example.com"),  # deactivated
    ]

    assert {response.status_code for response in responses} == {202}
    assert len({response.text for response in responses}) == 1
    assert len({tuple(sorted(response.headers)) for response in responses}) == 1


def test_resend_sends_nothing_unless_the_account_needs_verification(
    client: TestClient, session: Session, outbox
) -> None:
    add_user(session, "verified")
    add_user(session, "inactive", verified=False, active=False)

    for email in ("nobody@example.com", "verified@example.com", "inactive@example.com"):
        resend(client, email)

    assert outbox.verification == []
    assert session.scalar(select(func.count()).select_from(EmailVerificationToken)) == 0


def test_resend_response_does_not_contain_the_token(
    client: TestClient, outbox, raw_token: str
) -> None:
    response = resend(client)

    assert token_from(outbox.verification[1][1]) not in response.text
    assert set(response.json()) == {"message"}


@pytest.mark.parametrize("payload", [{}, {"email": ""}, {"email": "not-an-email"}])
def test_malformed_resend_request_is_rejected(
    client: TestClient, payload: dict
) -> None:
    assert client.post("/auth/resend-verification", json=payload).status_code == 422


def test_reset_token_is_not_accepted_as_a_verification_token(
    client: TestClient, session: Session, outbox, raw_token: str
) -> None:
    client.post("/auth/forgot-password", json={"email": "alice@example.com"})
    reset_token = token_from(outbox.password_reset[0][1])

    assert verify(client, reset_token).status_code == 400
    assert alice(session).email_verified_at is None


def test_service_reports_whether_there_is_something_to_send(session: Session) -> None:
    add_user(session, "alice", verified=False)
    add_user(session, "bob")

    request = auth_service.request_email_verification

    assert request(session, "alice@example.com")
    assert request(session, "bob@example.com") is None
    assert request(session, "nobody@example.com") is None

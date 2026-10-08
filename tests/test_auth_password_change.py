"""POST /auth/change-password."""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import hash_token, verify_password
from app.main import app
from app.models import PasswordResetToken, RateLimit, User, UserSession
from app.services import auth as auth_service
from app.services import rate_limit
from helpers import (
    PASSWORD,
    SENSITIVE_KEYS,
    columns,
    keys_in,
    log_in,
    session_token,
    set_cookie,
    token_from,
)

NEW_PASSWORD = "an entirely different passphrase"
NOT_AUTHENTICATED = {"detail": "Not authenticated."}
WRONG_PASSWORD = {"detail": "Current password is incorrect."}
CHANGED = {"message": "Password has been changed."}
FOREIGN_ORIGIN = "https://evil.example"


def change(
    client: TestClient,
    current_password: str = PASSWORD,
    new_password: str = NEW_PASSWORD,
    **kwargs: object,
):
    return client.post(
        "/auth/change-password",
        json={"current_password": current_password, "new_password": new_password},
        **kwargs,
    )


def failures_counted(session: Session, user: User) -> int | None:
    """The counter of wrong current passwords for the account, if it exists."""
    return session.scalar(
        select(RateLimit.count).where(
            RateLimit.key_hash
            == rate_limit.key_hash("change-password:account", str(user.id))
        )
    )


def stored_hash(session: Session, user: User) -> str:
    return columns(session, user)["password_hash"]


# --- who may ask ---------------------------------------------------------


def test_unauthenticated_request_is_refused(
    client: TestClient, session: Session, alice_account: User
) -> None:
    before = stored_hash(session, alice_account)

    response = change(client)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert stored_hash(session, alice_account) == before


def test_unauthenticated_request_is_refused_before_it_is_validated(
    client: TestClient,
) -> None:
    assert client.post("/auth/change-password", json={}).status_code == 401
    assert change(client, new_password="short").status_code == 401


def test_session_of_an_unverified_account_is_refused(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    # Not reachable by logging in. Should a session ever belong to an
    # unverified account all the same, its password cannot be changed.
    before = stored_hash(session, alice_account)
    alice_account.email_verified_at = None
    session.flush()

    response = change(alice_client)

    assert response.status_code == 403
    assert response.json() == {"detail": "Email address is not verified."}
    assert stored_hash(session, alice_account) == before


def test_deactivated_account_is_refused_like_no_session_at_all(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    before = stored_hash(session, alice_account)
    alice_account.is_active = False
    session.flush()

    response = change(alice_client)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert stored_hash(session, alice_account) == before


def test_revoked_session_cannot_change_the_password(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    session.scalars(select(UserSession)).one().revoked_at = datetime.now(timezone.utc)
    session.flush()

    assert change(alice_client).status_code == 401


def test_cross_site_request_is_refused_and_changes_nothing(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    before = stored_hash(session, alice_account)

    response = change(alice_client, headers={"Origin": FOREIGN_ORIGIN})

    assert response.status_code == 403
    assert stored_hash(session, alice_account) == before
    assert log_in(alice_client).status_code == 200


def test_password_can_only_be_changed_with_post(alice_client: TestClient) -> None:
    operations = app.openapi()["paths"]["/auth/change-password"]

    assert set(operations) == {"post"}
    for method in ("GET", "PUT", "PATCH", "DELETE"):
        response = alice_client.request(method, "/auth/change-password")
        assert response.status_code == 405


# --- the current password ------------------------------------------------


def test_wrong_current_password_is_refused(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    before = stored_hash(session, alice_account)

    response = change(alice_client, current_password="not my password at all")

    assert response.status_code == 400
    assert response.json() == WRONG_PASSWORD
    assert stored_hash(session, alice_account) == before
    # Nothing else came of it either: still signed in, with the old cookie.
    assert "set-cookie" not in response.headers
    assert alice_client.get("/auth/me").status_code == 200


def test_new_password_is_not_accepted_without_the_current_one(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    before = stored_hash(session, alice_account)

    answers = [
        alice_client.post("/auth/change-password", json={"new_password": NEW_PASSWORD}),
        change(alice_client, current_password=""),
        change(alice_client, current_password=NEW_PASSWORD),
        alice_client.post(
            "/auth/change-password",
            json={"current_password": None, "new_password": NEW_PASSWORD},
        ),
    ]

    assert [response.status_code for response in answers] == [422, 422, 400, 422]
    assert stored_hash(session, alice_account) == before


def test_wrong_current_password_does_not_end_any_session(
    alice_client: TestClient, make_client, session: Session, alice_account: User
) -> None:
    other = make_client()
    log_in(other)

    change(alice_client, current_password="not my password at all")

    assert other.get("/auth/me").status_code == 200
    assert session.scalars(select(UserSession.revoked_at)).all() == [None, None]


# --- the new password ----------------------------------------------------


@pytest.mark.parametrize(
    "new_password",
    ["", "short", "x" * (settings.password_min_length - 1), "x" * 129],
)
def test_new_password_is_held_to_the_password_policy(
    alice_client: TestClient, session: Session, alice_account: User, new_password: str
) -> None:
    before = stored_hash(session, alice_account)

    response = change(alice_client, new_password=new_password)

    assert response.status_code == 422
    assert stored_hash(session, alice_account) == before
    assert log_in(alice_client).status_code == 200


def test_policy_is_the_one_registration_and_reset_use() -> None:
    schemas = app.openapi()["components"]["schemas"]

    def rules(schema: str, field: str) -> dict:
        documented = dict(schemas[schema]["properties"][field])
        del documented["title"]
        return documented

    new_password = rules("ChangePasswordRequest", "new_password")
    assert new_password == rules("RegisterRequest", "password")
    assert new_password == rules("ResetPasswordRequest", "new_password")
    assert new_password["maxLength"] == 128
    assert set(schemas["ChangePasswordRequest"]["properties"]) == {
        "current_password",
        "new_password",
    }


def test_new_password_is_taken_exactly_as_typed(alice_client: TestClient) -> None:
    padded = "  spaces at both ends are kept  "

    assert change(alice_client, new_password=padded).status_code == 200

    assert log_in(alice_client, "alice", padded.strip()).status_code == 401
    assert log_in(alice_client, "alice", padded).status_code == 200


def test_invalid_new_password_is_refused_whatever_the_current_one_is(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    response = change(alice_client, current_password="wrong", new_password="short")

    # Refused on its form alone: no password was checked or counted.
    assert response.status_code == 422
    assert failures_counted(session, alice_account) is None


def test_rejected_passwords_are_not_echoed(alice_client: TestClient) -> None:
    response = change(
        alice_client, current_password="my-current-secret", new_password="short-pw"
    )

    assert response.status_code == 422
    assert "my-current-secret" not in response.text
    assert "short-pw" not in response.text


# --- a successful change -------------------------------------------------


def test_password_is_changed(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    before = stored_hash(session, alice_account)

    response = change(alice_client)

    assert response.status_code == 200
    assert response.json() == CHANGED
    after = stored_hash(session, alice_account)
    assert after != before
    # A fresh Argon2id hash of the new password, and nothing like it stored.
    assert after.startswith("$argon2id$")
    assert NEW_PASSWORD not in after
    assert verify_password(NEW_PASSWORD, after)
    assert not verify_password(PASSWORD, after)
    assert not auth_service.password_needs_rehash(after)


def test_old_password_is_rejected_and_the_new_one_accepted(
    alice_client: TestClient, make_client
) -> None:
    change(alice_client)
    elsewhere = make_client()

    assert log_in(elsewhere, "alice", PASSWORD).status_code == 401
    assert log_in(elsewhere, "alice", NEW_PASSWORD).status_code == 200


def test_current_session_stays_valid(
    alice_client: TestClient, session: Session
) -> None:
    before = session.scalars(select(UserSession)).one()
    session_id, expires_at = before.id, before.expires_at

    assert change(alice_client).status_code == 200

    assert alice_client.get("/auth/me").status_code == 200
    assert alice_client.get("/users/me").json()["username"] == "alice"
    # The very same session: not ended and replaced by a new one.
    after = session.scalars(select(UserSession)).one()
    session.refresh(after)
    assert (after.id, after.expires_at, after.revoked_at) == (
        session_id,
        expires_at,
        None,
    )
    [listed] = alice_client.get("/auth/sessions").json()
    assert listed["id"] == str(session_id)
    assert listed["is_current"] is True


def test_every_other_session_is_revoked(
    alice_client: TestClient, make_client, session: Session
) -> None:
    others = [make_client(), make_client()]
    for other in others:
        log_in(other)

    assert change(alice_client).status_code == 200

    for other in others:
        # No authenticated endpoint is left to a revoked session.
        assert other.get("/auth/me").status_code == 401
        assert other.get("/users/me").status_code == 401
        assert other.get("/feed/following").status_code == 401
        assert other.post("/posts", json={"content": "Hi"}).status_code == 401
        assert change(other, NEW_PASSWORD, PASSWORD).status_code == 401
    assert alice_client.get("/auth/me").status_code == 200
    revoked = session.scalars(select(UserSession.revoked_at)).all()
    assert sum(at is None for at in revoked) == 1


def test_sessions_of_other_users_are_untouched(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    bob_account: User,
) -> None:
    before = stored_hash(session, bob_account)

    assert change(alice_client).status_code == 200

    assert bob_client.get("/auth/me").json()["username"] == "bob"
    assert stored_hash(session, bob_account) == before
    assert log_in(bob_client, "bob", PASSWORD).status_code == 200


def test_current_session_continues_under_a_new_cookie(
    alice_client: TestClient, make_client, session: Session
) -> None:
    old_token = session_token(alice_client)

    response = change(alice_client)

    new_token = session_token(alice_client)
    assert new_token and new_token != old_token
    assert session.scalars(select(UserSession.token_hash)).all() == [
        hash_token(new_token)
    ]
    # A copy of the cookie as it was before the change is worth nothing:
    # whoever held one is logged out together with the other sessions.
    thief = make_client()
    thief.cookies.set(settings.session_cookie, old_token)
    assert thief.get("/auth/me").status_code == 401
    assert new_token not in response.text


def test_new_cookie_has_every_attribute_of_the_session_cookie(
    alice_client: TestClient, session: Session
) -> None:
    expires_at = session.scalars(select(UserSession.expires_at)).one()

    cookie = set_cookie(change(alice_client))

    assert cookie.key == "__Host-hopsnop_session"
    assert cookie["httponly"] is True
    assert cookie["secure"] is True
    assert cookie["samesite"].lower() == "lax"
    assert cookie["path"] == "/"
    assert cookie["domain"] == ""
    # The time that is left to the session, not a new lifetime.
    left = (expires_at - datetime.now(timezone.utc)).total_seconds()
    assert left - 5 <= int(cookie["max-age"]) <= left


def test_change_does_not_extend_the_session(
    alice_client: TestClient, session: Session
) -> None:
    current = session.scalars(select(UserSession)).one()
    current.expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    session.flush()

    cookie = set_cookie(change(alice_client))

    assert 0 < int(cookie["max-age"]) <= 600
    session.refresh(current)
    assert current.expires_at - datetime.now(timezone.utc) <= timedelta(minutes=10)


def test_outstanding_reset_link_is_withdrawn(
    alice_client: TestClient, make_client, session: Session, outbox
) -> None:
    make_client().post("/auth/forgot-password", json={"email": "alice@example.com"})
    token = token_from(outbox.password_reset[0][1])

    assert change(alice_client).status_code == 200

    # A link asked for under the old password does not outlive it.
    reset = make_client().post(
        "/auth/reset-password",
        json={"token": token, "new_password": "yet another passphrase"},
    )
    assert reset.status_code == 400
    assert session.scalars(select(PasswordResetToken)).all() == []
    assert log_in(make_client(), "alice", NEW_PASSWORD).status_code == 200


def test_password_can_be_changed_again_with_the_new_one(
    alice_client: TestClient,
) -> None:
    assert change(alice_client).status_code == 200

    assert change(alice_client, PASSWORD, "a third passphrase").status_code == 400
    assert change(alice_client, NEW_PASSWORD, "a third passphrase").status_code == 200
    assert log_in(alice_client, "alice", "a third passphrase").status_code == 200


def test_nothing_else_about_the_account_changes(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    before = columns(session, alice_account)

    assert change(alice_client).status_code == 200

    after = columns(session, alice_account)
    changed = {name for name in before if before[name] != after[name]}
    assert changed <= {"password_hash", "updated_at"}


# --- what the answers contain --------------------------------------------


def test_no_password_material_is_in_any_answer(
    alice_client: TestClient, make_client, session: Session, alice_account: User
) -> None:
    old_hash = stored_hash(session, alice_account)
    old_token = session_token(alice_client)
    answers = [
        change(make_client()),  # 401
        change(alice_client, new_password="short"),  # 422
        change(alice_client, current_password="not my password at all"),  # 400
        change(alice_client),  # 200
    ]
    new_hash = stored_hash(session, alice_account)
    new_token = session_token(alice_client)

    assert [response.status_code for response in answers] == [401, 422, 400, 200]
    secrets = [
        PASSWORD,
        NEW_PASSWORD,
        "not my password at all",
        old_hash,
        new_hash,
        old_token,
        new_token,
        hash_token(old_token),
        hash_token(new_token),
        "argon2",
    ]
    for response in answers:
        assert keys_in(response.json()).isdisjoint(SENSITIVE_KEYS)
        for secret in secrets:
            assert secret not in response.text
        assert response.headers["cache-control"] == "no-store"
    # The new token leaves the server in the cookie and nowhere else.
    assert set(answers[3].json()) == {"message"}


def test_passwords_do_not_appear_in_logs(
    alice_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)

    change(alice_client, current_password="not my password at all")
    change(alice_client)

    assert "not my password at all" not in caplog.text
    assert PASSWORD not in caplog.text
    assert NEW_PASSWORD not in caplog.text
    assert session_token(alice_client) not in caplog.text


# --- guessing the current password ---------------------------------------


def test_guessing_the_current_password_is_limited(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    before = stored_hash(session, alice_account)
    allowed = settings.password_change_failures

    guesses = [
        change(alice_client, current_password=f"guess number {number}")
        for number in range(allowed)
    ]
    refused = change(alice_client)

    assert [response.status_code for response in guesses] == [400] * allowed
    # Also with the right password, like a login.
    assert refused.status_code == 429
    assert refused.json() == {"detail": "Too many requests. Try again later."}
    assert refused.headers["retry-after"].isdigit()
    assert stored_hash(session, alice_account) == before
    # The session itself is not in question.
    assert alice_client.get("/auth/me").status_code == 200


def test_limit_follows_the_account_to_every_session(
    alice_client: TestClient, make_client
) -> None:
    for number in range(settings.password_change_failures):
        change(alice_client, current_password=f"guess number {number}")
    other = make_client(client=("198.51.100.23", 50000))
    log_in(other)

    # A stolen session gets no more guesses by being used from elsewhere.
    assert change(other).status_code == 429


def test_limit_is_each_accounts_own(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    for number in range(settings.password_change_failures + 1):
        change(alice_client, current_password=f"guess number {number}")

    assert change(bob_client).status_code == 200


def test_successful_changes_are_not_counted(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    passwords = [PASSWORD] + [
        f"passphrase number {number}"
        for number in range(settings.password_change_failures + 2)
    ]

    for current, new in zip(passwords, passwords[1:]):
        assert change(alice_client, current, new).status_code == 200

    assert failures_counted(session, alice_account) == 0


def test_change_is_possible_again_when_the_window_is_over(
    alice_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(settings.password_change_failures):
        change(alice_client, current_password=f"guess number {number}")
    assert change(alice_client).status_code == 429

    later: Callable[[], datetime] = lambda: datetime.now(timezone.utc) + timedelta(
        minutes=settings.rate_limit_window_minutes
    )
    monkeypatch.setattr(rate_limit, "_now", later)

    assert change(alice_client).status_code == 200


# --- cost ----------------------------------------------------------------


def test_change_takes_the_same_statements_however_many_sessions_there_are(
    alice_client: TestClient, make_client, session: Session
) -> None:
    from sqlalchemy import event

    from app.db.session import engine

    def statements_of_a_change(current: str, new: str) -> int:
        seen: list[str] = []

        def record(conn, cursor, statement, parameters, context, executemany) -> None:
            if not statement.lstrip().upper().startswith(("SAVEPOINT", "RELEASE")):
                seen.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            assert change(alice_client, current, new).status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return len(seen)

    with_one = statements_of_a_change(PASSWORD, NEW_PASSWORD)
    for _ in range(6):
        log_in(make_client(), "alice", NEW_PASSWORD)
    with_many = statements_of_a_change(NEW_PASSWORD, "a third passphrase")

    # The other sessions are ended by one statement, not one each.
    assert with_many == with_one
    revoked = session.scalars(select(UserSession.revoked_at)).all()
    assert sum(at is None for at in revoked) == 1

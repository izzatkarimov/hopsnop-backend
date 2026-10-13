"""Authentication when requests for one account arrive together.

What must hold however a login, a password change, a password reset and the
requests for emailed links are interleaved.

These tests cannot run in the one transaction the other tests share: what
they are about is what happens between transactions. They use the
application as it really runs, with a database session per request, on
accounts that are really committed, and they remove what they wrote. Each
works on an account and on addresses that nothing else uses.

The interleavings are forced, not hoped for. A request is stopped at a known
point (``Gate``), the other one is started, and the first is let go once the
other has finished or is seen to be waiting for it.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.core.config import settings
from app.core.security import hash_token
from app.db.session import SessionLocal
from app.models import EmailVerificationToken, PasswordResetToken, User
from app.services import auth as auth_service
from concurrency import TIMEOUT, Gate, let_run
from helpers import PASSWORD, log_in

NEW_PASSWORD = "an entirely different passphrase"


# --- a login with the old password, and the password being replaced ------
#
# The login is stopped in the middle, at one of two points, and the password
# is changed or reset before it goes on. Wherever it was stopped, no session
# that was opened with the old password is left afterwards.

STOPS = {
    # The old password has been found right, and nothing has been done yet.
    "after the password was checked": "verify_password",
    # The token of the session about to be created has just been made.
    "while the session is being created": "generate_token",
}


@pytest.mark.parametrize("stopped", STOPS)
@pytest.mark.parametrize("replaced_by", ["change", "reset"])
def test_login_with_the_old_password_does_not_outlive_its_replacement(
    account,
    issued: list[str],
    monkeypatch: pytest.MonkeyPatch,
    replaced_by: str,
    stopped: str,
) -> None:
    owner_account = account()
    owner = owner_account.client(0)
    intruder = owner_account.client(1)
    elsewhere = owner_account.client(2)
    name = owner_account.name
    assert log_in(owner, name).status_code == 200
    if replaced_by == "reset":
        assert (
            elsewhere.post(
                "/auth/forgot-password", json={"email": owner_account.email}
            ).status_code
            == 202
        )
    reset_token = issued[-1]

    def replace_the_password() -> int:
        if replaced_by == "change":
            response = owner.post(
                "/auth/change-password",
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            )
        else:
            response = elsewhere.post(
                "/auth/reset-password",
                json={"token": reset_token, "new_password": NEW_PASSWORD},
            )
        return response.status_code

    gate = Gate()
    function = STOPS[stopped]
    monkeypatch.setattr(
        auth_service, function, gate.wrap(getattr(auth_service, function))
    )

    with ThreadPoolExecutor(2) as pool:
        gate.close()
        # Someone who knows the password as it still is.
        login = pool.submit(lambda: log_in(intruder, name).status_code)
        assert gate.reached.wait(TIMEOUT), "the login never got that far"
        # Now, with that login under way, the password is replaced.
        replacement = pool.submit(replace_the_password)
        let_run(replacement)
        gate.open()
        login_status = login.result(TIMEOUT)
        replacement_status = replacement.result(TIMEOUT)

    assert replacement_status == 200
    # The login was either refused or let in. Let in or not, nothing that it
    # opened is alive once the password has been replaced.
    assert login_status in (200, 401)
    assert intruder.get("/auth/me").status_code == 401
    with SessionLocal() as db:
        alive = [s for s in owner_account.sessions(db) if s.revoked_at is None]
        if replaced_by == "change":
            # The one session left is the one that changed the password.
            assert [s.token_hash for s in alive] == [
                hash_token(owner.cookies.get(settings.session_cookie))
            ]
        else:
            assert alive == []
    if replaced_by == "change":
        assert owner.get("/auth/me").status_code == 200
    # And the old password opens nothing more.
    assert log_in(owner_account.client(3), name).status_code == 401
    assert log_in(owner_account.client(4), name, NEW_PASSWORD).status_code == 200


@pytest.mark.parametrize("replaced_by", ["change", "reset"])
def test_login_that_checked_the_old_password_before_the_change_is_refused(
    account, issued: list[str], monkeypatch: pytest.MonkeyPatch, replaced_by: str
) -> None:
    owner_account = account()
    owner = owner_account.client(0)
    intruder = owner_account.client(1)
    elsewhere = owner_account.client(2)
    assert log_in(owner, owner_account.name).status_code == 200
    if replaced_by == "reset":
        elsewhere.post("/auth/forgot-password", json={"email": owner_account.email})
    reset_token = issued[-1]
    gate = Gate()
    monkeypatch.setattr(
        auth_service, "verify_password", gate.wrap(auth_service.verify_password)
    )

    with ThreadPoolExecutor(1) as pool:
        gate.close()
        login = pool.submit(lambda: log_in(intruder, owner_account.name))
        assert gate.reached.wait(TIMEOUT)
        # The whole replacement happens while the login stands still, with
        # the old password already found right.
        if replaced_by == "change":
            replaced = owner.post(
                "/auth/change-password",
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            )
        else:
            replaced = elsewhere.post(
                "/auth/reset-password",
                json={"token": reset_token, "new_password": NEW_PASSWORD},
            )
        assert replaced.status_code == 200
        gate.open()
        response = login.result(TIMEOUT)

    # What it proved is a password that is no longer the account's: the
    # answer of any wrong password, no cookie, and no session row at all.
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid username/email or password."}
    assert "set-cookie" not in response.headers
    with SessionLocal() as db:
        sessions = owner_account.sessions(db)
    # Only the owner's own: kept by a change, ended by a reset.
    assert len(sessions) == 1
    assert (sessions[0].revoked_at is None) == (replaced_by == "change")


def test_two_changes_of_one_password_at_once_do_not_both_take_effect(
    account, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_account = account()
    first, second = owner_account.client(0), owner_account.client(1)
    for client in (first, second):
        assert log_in(client, owner_account.name).status_code == 200
    gate = Gate()
    monkeypatch.setattr(
        auth_service, "verify_password", gate.wrap(auth_service.verify_password)
    )

    def change(client: TestClient, new_password: str) -> int:
        return client.post(
            "/auth/change-password",
            json={"current_password": PASSWORD, "new_password": new_password},
        ).status_code

    with ThreadPoolExecutor(1) as pool:
        gate.close()
        # Both know the current password. One has had it checked...
        waiting = pool.submit(change, first, "the first new passphrase")
        assert gate.reached.wait(TIMEOUT)
        # ...when the other changes it.
        assert change(second, "the second new passphrase") == 200
        gate.open()
        outcome = waiting.result(TIMEOUT)

    # The password the first one proved is not the current one any more.
    assert outcome == 400
    assert second.get("/auth/me").status_code == 200
    assert first.get("/auth/me").status_code == 401
    third, name = owner_account.client(2), owner_account.name
    assert log_in(third, name, "the first new passphrase").status_code == 401
    assert log_in(third, name, "the second new passphrase").status_code == 200


# --- a link being redeemed while another is asked for --------------------
#
# Redeeming a link works on the token and on the account; asking for a new
# link works on the account and on the token. If the two took their locks in
# opposite orders they could each hold what the other needs, which the
# database ends by failing one of them.

LINKS = {
    "reset": (PasswordResetToken, "/auth/forgot-password"),
    "verification": (EmailVerificationToken, "/auth/resend-verification"),
}


@pytest.mark.parametrize("kind", LINKS)
def test_redeeming_a_link_while_another_is_requested_fails_neither(
    account, issued: list[str], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    model, request_path = LINKS[kind]
    owner_account = account(verified=kind == "reset")
    asking, redeeming = owner_account.client(0), owner_account.client(1)
    asked = asking.post(request_path, json={"email": owner_account.email})
    assert asked.status_code == 202
    old_link = issued[-1]
    # Old enough that asking again issues a new link and withdraws this one.
    with SessionLocal() as db:
        age = timedelta(seconds=settings.email_token_cooldown_seconds + 1)
        db.execute(
            update(model)
            .where(model.user_id == owner_account.id)
            .values(created_at=model.created_at - age)
        )
        db.commit()

    def redeem() -> int:
        if kind == "reset":
            response = redeeming.post(
                "/auth/reset-password",
                json={"token": old_link, "new_password": NEW_PASSWORD},
            )
        else:
            response = redeeming.post("/auth/verify-email", json={"token": old_link})
        return response.status_code

    # The request for a new link is stopped where it holds the account and
    # is about to withdraw the old link.
    gate = Gate()
    monkeypatch.setattr(
        auth_service, "_issued_recently", gate.wrap(auth_service._issued_recently)
    )

    with ThreadPoolExecutor(2) as pool:
        gate.close()
        request = pool.submit(
            lambda: asking.post(
                request_path, json={"email": owner_account.email}
            ).status_code
        )
        assert gate.reached.wait(TIMEOUT), "the request never got that far"
        redemption = pool.submit(redeem)
        let_run(redemption)
        gate.open()
        # A deadlock would surface here, as the error of whichever of the
        # two the database gave up on.
        request_status = request.result(TIMEOUT)
        redemption_status = redemption.result(TIMEOUT)

    # The request was first, so it is the one that counts: the old link was
    # withdrawn before it could be redeemed, and the new one is the only one.
    assert request_status == 202
    assert redemption_status == 400
    new_link = issued[-1]
    assert new_link != old_link
    assert owner_account.unused_tokens(model) == [hash_token(new_link)]
    with SessionLocal() as db:
        user = db.get(User, owner_account.id)
        if kind == "reset":
            # The password is the one it was: the old link changed nothing.
            still = log_in(owner_account.client(2), owner_account.name)
            assert still.status_code == 200
        else:
            assert user.email_verified_at is None
        assert db.scalar(select(func.count()).select_from(model)) == 1

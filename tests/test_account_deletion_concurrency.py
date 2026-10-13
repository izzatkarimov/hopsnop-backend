"""Deleting an account while something else is done with it, by it or to it.

These run against real transactions (see ``concurrency``): one request is
stopped at a known point, another is started, and the first is let go once
the other has finished or is seen to be waiting for it. Nothing here sleeps
and hopes.

Whatever the order, three things hold. Nobody is answered with a server
error. An account whose deletion was answered with success is gone, with
everything of it, also what another request was writing at that very moment.
And an account whose deletion was refused is all there.

An export of the account's data at the same moment is in
``test_data_export_concurrency.py``, and the retention purge in
``test_retention_concurrency.py``.
"""

import copy
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import event, func, select, update

from app.core.config import settings
from app.db.session import SessionLocal, engine
from app.models import (
    EmailVerificationToken,
    Follow,
    Like,
    PasswordResetToken,
    Post,
    RateLimit,
    Repost,
    Story,
    StoryView,
    User,
    UserSession,
)
from app.services import auth as auth_service
from app.services import erasure
from app.services import posts as posts_service
from app.services import stories as stories_service
from app.services import users as users_service
from app.services.rate_limit import key_hash
from concurrency import (
    TIMEOUT,
    Account,
    Gate,
    at_once,
    one_after_the_other,
    signed_in,
    stopped_after,
    until_waiting,
)
from helpers import PASSWORD, log_in, registration, what_is_left_of

NEW_PASSWORD = "an entirely different passphrase"
NOT_AUTHENTICATED = {"detail": "Not authenticated."}
WRONG_PASSWORD = {"detail": "Current password is incorrect."}
INVALID_CREDENTIALS = {"detail": "Invalid username/email or password."}
MEDIA = "https://media.example.com/stories"

# Where a deletion is stopped:
# - once it holds the account's row, and has removed nothing yet;
# - once it has removed everything, and has committed nothing yet.
DELETION_STOPS = {
    "holds the account": (auth_service, "_lock_account_if_unchanged"),
    "has removed everything": (erasure, "erase_account"),
}


def remove(client: TestClient, password: str = PASSWORD) -> Response:
    return client.post("/auth/delete-account", json={"password": password})


def deletion_stopped(monkeypatch: pytest.MonkeyPatch, where: str) -> Gate:
    return stopped_after(monkeypatch, *DELETION_STOPS[where])


def stored(account: Account) -> dict | None:
    """The account's row as committed, or None if there is none."""
    with SessionLocal() as db:
        found = db.execute(
            select(User.__table__).where(User.id == account.id)
        ).one_or_none()
        return dict(found._mapping) if found is not None else None


def rows(model: type, *conditions: object) -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(model).where(*conditions))


def post_row(post_id: str) -> dict | None:
    with SessionLocal() as db:
        found = db.execute(
            select(Post.__table__).where(Post.id == uuid.UUID(post_id))
        ).one_or_none()
        return dict(found._mapping) if found is not None else None


def assert_gone(
    account: Account, *written: str, deleted_from: int = 0, counters: bool = True
) -> None:
    """Nothing of the account is left: not its id in any column of any
    table, not its names, and nothing it wrote.

    ``deleted_from`` is the number of the address the deletion was asked
    from. ``counters`` is off where logins by the name went on after the
    account was gone: those are failed logins by a name that nobody has, and
    are counted like any other.
    """
    names = [account.name, account.email]
    with SessionLocal() as db:
        assert what_is_left_of(db, user_id=account.id, texts=[*names, *written]) == []
        # What was counted under its names, as far as it could be found by
        # the request that deleted it.
        address = account.address(deleted_from)
        named = [
            key_hash(scope, str(account.id))
            for scope in ("password-confirmation:account", "data-export:account")
        ]
        for name in names:
            named.append(key_hash("login:identifier", name))
            named.append(key_hash("login:identifier+address", f"{name}\x00{address}"))
        left = db.scalar(
            select(func.count())
            .select_from(RateLimit)
            .where(RateLimit.key_hash.in_(named))
        )
        assert not counters or left == 0
        # A post without an author is the place of one in a thread, and
        # something has to be answering it.
        for (tombstone,) in db.execute(select(Post.id).where(Post.author_id.is_(None))):
            assert db.scalar(
                select(func.count())
                .select_from(Post)
                .where(Post.parent_post_id == tombstone)
            ), "a tombstone nobody needs"


def assert_intact(account: Account, password: str = PASSWORD) -> None:
    """The account is there and can be logged into with ``password``."""
    assert stored(account) is not None
    assert log_in(account.client(9), account.name, password).status_code == 200


def create(client: TestClient, content: str, parent: str | None = None) -> str:
    body = {"content": content}
    if parent is not None:
        body["parent_post_id"] = parent
    response = client.post("/posts", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def create_story(client: TestClient, name: str) -> str:
    response = client.post("/stories", json={"media_url": f"{MEDIA}/{name}.jpg"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --- a login -------------------------------------------------------------
#
# Somebody who knows the password logs in while the account is being
# deleted. Wherever the two meet, no session is left that the login opened.


def test_login_that_had_checked_the_password_opens_no_session(
    account, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = account()
    owning, intruder = signed_in(owner), owner.client(1)
    # The login has found the password right, and has done nothing yet.
    gate = stopped_after(monkeypatch, auth_service, "verify_password")

    logged_in, deleted, waited = one_after_the_other(
        lambda: log_in(intruder, owner.name), lambda: remove(owning), gate
    )

    # Nothing held the deletion up. The login then finds that the account
    # whose password it checked is not there to open a session for.
    assert not waited
    assert deleted.status_code == 204
    assert logged_in.status_code == 401
    assert logged_in.json() == INVALID_CREDENTIALS
    assert "set-cookie" not in logged_in.headers
    assert rows(UserSession, UserSession.user_id == owner.id) == 0
    assert_gone(owner)


def test_login_that_holds_the_account_is_waited_for_and_its_session_removed(
    account, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = account()
    owning, intruder = signed_in(owner), owner.client(1)
    # The login holds the account and is about to write its session.
    gate = stopped_after(monkeypatch, auth_service, "generate_token")

    logged_in, deleted, waited = one_after_the_other(
        lambda: log_in(intruder, owner.name), lambda: remove(owning), gate
    )

    # The deletion waited for the login to finish, and took the session it
    # had just opened along with all the others. The login is answered from
    # the account it opened the session for, if that was still to be read;
    # let in or not, nothing that it opened is alive.
    assert waited
    assert deleted.status_code == 204
    assert logged_in.status_code in (200, 401)
    assert intruder.get("/auth/me").status_code == 401
    assert intruder.get("/auth/me").json() == NOT_AUTHENTICATED
    assert rows(UserSession, UserSession.user_id == owner.id) == 0
    assert_gone(owner)


@pytest.mark.parametrize("stopped", DELETION_STOPS)
def test_login_during_the_deletion_waits_and_finds_no_account(
    account, monkeypatch: pytest.MonkeyPatch, stopped: str
) -> None:
    owner = account()
    owning, intruder = signed_in(owner), owner.client(1)
    gate = deletion_stopped(monkeypatch, stopped)

    deleted, logged_in, waited = one_after_the_other(
        lambda: remove(owning), lambda: log_in(intruder, owner.name), gate
    )

    # The account's row was still to be read, and the password was right.
    # But a session is only opened with the account held, and by the time
    # the login held it there was none.
    assert waited
    assert deleted.status_code == 204
    assert logged_in.status_code == 401
    assert logged_in.json() == INVALID_CREDENTIALS
    assert "set-cookie" not in logged_in.headers
    assert intruder.get("/auth/me").status_code == 401
    assert rows(UserSession, UserSession.user_id == owner.id) == 0
    assert_gone(owner)


def test_logins_and_a_deletion_all_at_once_leave_no_session(account) -> None:
    for _ in range(3):
        owner = account()
        owning = signed_in(owner)
        clients = [owner.client(number) for number in range(1, 5)]

        def request(number: int) -> int:
            if number == 0:
                return remove(owning).status_code
            return log_in(clients[number - 1], owner.name).status_code

        deleted, *logins = at_once(5, request)

        assert deleted == 204
        assert set(logins) <= {200, 401}
        for client in clients:
            assert client.get("/auth/me").status_code == 401
        assert rows(UserSession, UserSession.user_id == owner.id) == 0
        assert_gone(owner, counters=False)


# --- an account that goes between two looks at it ------------------------
#
# A request reads its session and its account again whenever something has
# been committed in between. Those are two statements. An account can be
# removed after the first has found the session and before the second looks
# for the account.


@contextmanager
def stopped_between_its_session_and_its_account() -> Iterator[Gate]:
    """A gate at which the next request stops right after it has read its
    session's row again, with the account's row still to be read."""
    gate = Gate()
    read_again = gate.wrap(lambda: None)

    def after(conn, cursor, statement, parameters, context, executemany) -> None:
        if "FROM sessions WHERE sessions.id = " in " ".join(statement.split()):
            read_again()

    event.listen(engine, "after_cursor_execute", after)
    try:
        yield gate
    finally:
        event.remove(engine, "after_cursor_execute", after)


def test_login_whose_account_goes_while_it_is_being_answered_is_not_let_in(
    account,
) -> None:
    owner = account()
    owning, intruder = signed_in(owner), owner.client(1)

    # The login has created its session and committed it, and is reading
    # back whom to answer with.
    with stopped_between_its_session_and_its_account() as gate:
        logged_in, deleted, waited = one_after_the_other(
            lambda: log_in(intruder, owner.name), lambda: remove(owning), gate
        )

    # Nothing held the deletion up, and it took the new session with it.
    # The login found its session and then no account: that is a login that
    # came to nothing, not a fault of the server and not a cookie.
    assert not waited
    assert deleted.status_code == 204
    assert logged_in.status_code == 401
    assert logged_in.json() == NOT_AUTHENTICATED
    assert "set-cookie" not in logged_in.headers
    assert rows(UserSession, UserSession.user_id == owner.id) == 0
    assert_gone(owner)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/auth/me", None),
        ("GET", "/users/me", None),
        ("GET", "/auth/sessions", None),
        ("GET", "/feed/following", None),
        ("POST", "/posts", {"content": "Written at the last moment"}),
        ("PATCH", "/users/me", {"bio": "Changed at the last moment"}),
        ("POST", "/users/me/export", {"password": PASSWORD}),
        ("POST", "/auth/delete-account", {"password": PASSWORD}),
        (
            "POST",
            "/auth/change-password",
            {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_request_whose_account_goes_between_two_looks_at_it_is_not_authenticated(
    account, method: str, path: str, body: dict | None
) -> None:
    owner = account()
    asking, deleting = signed_in(owner, 0), signed_in(owner, 1)
    # A session that was last used a while ago: using it now is recorded,
    # and that is a commit, after which the request reads its rows again.
    a_day = func.make_interval(0, 0, 0, 1)
    with SessionLocal() as db:
        db.execute(
            update(UserSession)
            .where(UserSession.user_id == owner.id)
            .values(last_used_at=UserSession.created_at - a_day)
        )
        db.commit()

    with stopped_between_its_session_and_its_account() as gate:
        answered, deleted, waited = one_after_the_other(
            lambda: asking.request(method, path, json=body),
            lambda: remove(deleting),
            gate,
        )

    # Whatever it asked for, by now it is a request without a session. It
    # is told so, it wrote nothing, and nothing was counted for it that
    # would stay behind under the id of an account that is gone.
    assert not waited
    assert deleted.status_code == 204
    assert answered.status_code == 401
    assert answered.json() == NOT_AUTHENTICATED
    assert "set-cookie" not in answered.headers
    assert_gone(owner, "at the last moment", deleted_from=1)


# --- a password change ---------------------------------------------------


def change(client: TestClient) -> Response:
    return client.post(
        "/auth/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
    )


def test_deletion_after_a_change_of_password_is_refused_and_removes_nothing(
    account, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = account()
    changing, deleting = signed_in(owner, 0), signed_in(owner, 1)
    create(deleting, "Written before any of this")
    # The change holds the account and is about to write the new password.
    gate = stopped_after(monkeypatch, auth_service, "_lock_account_if_unchanged")

    changed, deleted, waited = one_after_the_other(
        lambda: change(changing), lambda: remove(deleting), gate
    )

    # The deletion was asked for with the password as it was. By the time it
    # held the account, that was not the account's password any more: what
    # it had been shown proves nothing, and nothing is removed for it.
    assert waited
    assert changed.status_code == 200
    assert deleted.status_code == 400
    assert deleted.json() == WRONG_PASSWORD
    assert rows(Post, Post.author_id == owner.id) == 1
    assert_intact(owner, NEW_PASSWORD)
    # The session that asked was ended by the change, like every other.
    assert deleting.get("/auth/me").status_code == 401
    assert changing.get("/auth/me").status_code == 200
    assert log_in(owner.client(8), owner.name, PASSWORD).status_code == 401


@pytest.mark.parametrize("stopped", DELETION_STOPS)
def test_change_of_password_during_the_deletion_brings_nothing_back(
    account, monkeypatch: pytest.MonkeyPatch, stopped: str
) -> None:
    owner = account()
    changing, deleting = signed_in(owner, 0), signed_in(owner, 1)
    gate = deletion_stopped(monkeypatch, stopped)

    deleted, changed, waited = one_after_the_other(
        lambda: remove(deleting), lambda: change(changing), gate
    )

    # The change had the right password and a session that was good when it
    # arrived. It waited for the account, and there was none to change.
    assert waited
    assert deleted.status_code == 204
    assert changed.status_code == 401
    assert changed.json() == NOT_AUTHENTICATED
    assert "set-cookie" not in changed.headers
    # No row with a new password, and no session under a new token.
    assert stored(owner) is None
    assert rows(UserSession, UserSession.user_id == owner.id) == 0
    assert_gone(owner, deleted_from=1)
    for password in (PASSWORD, NEW_PASSWORD):
        assert log_in(owner.client(8), owner.name, password).status_code == 401


def test_change_that_had_only_checked_the_password_finds_no_account(
    account, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = account()
    changing, deleting = signed_in(owner, 0), signed_in(owner, 1)
    # The change has checked the old password and hashed the new one. It
    # holds nothing yet.
    gate = stopped_after(monkeypatch, auth_service, "hash_password")

    changed, deleted, waited = one_after_the_other(
        lambda: change(changing), lambda: remove(deleting), gate
    )

    assert not waited
    assert deleted.status_code == 204
    assert changed.status_code == 401
    assert changed.json() == NOT_AUTHENTICATED
    assert stored(owner) is None
    assert_gone(owner, deleted_from=1)


# --- a password reset ----------------------------------------------------


def reset_link(owner: Account, issued: list[str]) -> str:
    """Ask for a reset link for the account. Its token, as it was emailed."""
    asked = owner.client(2).post("/auth/forgot-password", json={"email": owner.email})
    assert asked.status_code == 202
    return issued[-1]


def reset(owner: Account, token: str) -> Response:
    return owner.client(2).post(
        "/auth/reset-password", json={"token": token, "new_password": NEW_PASSWORD}
    )


def test_deletion_after_a_reset_of_the_password_is_refused_and_removes_nothing(
    account, issued: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = account()
    deleting = signed_in(owner)
    create(deleting, "Written before any of this")
    token = reset_link(owner, issued)
    # The reset holds the account and the link it was asked with.
    gate = stopped_after(monkeypatch, auth_service, "_usable_token")

    was_reset, deleted, waited = one_after_the_other(
        lambda: reset(owner, token), lambda: remove(deleting), gate
    )

    # Whoever reset the password had the mailbox. Whoever asked for the
    # deletion had the old password and a session, and after the reset has
    # neither.
    assert waited
    assert was_reset.status_code == 200
    assert deleted.status_code == 400
    assert deleted.json() == WRONG_PASSWORD
    assert rows(Post, Post.author_id == owner.id) == 1
    assert deleting.get("/auth/me").status_code == 401
    assert_intact(owner, NEW_PASSWORD)


@pytest.mark.parametrize("stopped", DELETION_STOPS)
def test_reset_of_the_password_during_the_deletion_brings_nothing_back(
    account, issued: list[str], monkeypatch: pytest.MonkeyPatch, stopped: str
) -> None:
    owner = account()
    deleting = signed_in(owner)
    token = reset_link(owner, issued)
    gate = deletion_stopped(monkeypatch, stopped)

    deleted, was_reset, waited = one_after_the_other(
        lambda: remove(deleting), lambda: reset(owner, token), gate
    )

    # The link was good when the reset arrived. It waited for the account,
    # and with the account the link was gone.
    assert waited
    assert deleted.status_code == 204
    assert was_reset.status_code == 400
    assert was_reset.json() == {"detail": "Invalid or expired password reset token."}
    assert stored(owner) is None
    assert rows(PasswordResetToken, PasswordResetToken.user_id == owner.id) == 0
    assert_gone(owner)
    for password in (PASSWORD, NEW_PASSWORD):
        assert log_in(owner.client(8), owner.name, password).status_code == 401


# --- an email verification -----------------------------------------------


@pytest.fixture
def unverified(account, issued: list[str]) -> tuple[Account, TestClient, str]:
    """An account that is not verified, a session of it all the same, and
    the token of the verification link that is in its mailbox."""
    owner = account()
    client = signed_in(owner)
    with SessionLocal() as db:
        db.execute(
            update(User).where(User.id == owner.id).values(email_verified_at=None)
        )
        db.commit()
    asked = owner.client(2).post(
        "/auth/resend-verification", json={"email": owner.email}
    )
    assert asked.status_code == 202
    assert rows(EmailVerificationToken, EmailVerificationToken.user_id == owner.id)
    return owner, client, issued[-1]


def verify(owner: Account, token: str) -> Response:
    return owner.client(2).post("/auth/verify-email", json={"token": token})


def test_verification_that_came_first_is_waited_for_and_the_account_removed(
    unverified: tuple[Account, TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, deleting, token = unverified
    # The verification holds the account and the link.
    gate = stopped_after(monkeypatch, auth_service, "_usable_token")

    verified, deleted, waited = one_after_the_other(
        lambda: verify(owner, token), lambda: remove(deleting), gate
    )

    # Being verified changes nothing about whose password it is.
    assert waited
    assert (verified.status_code, deleted.status_code) == (200, 204)
    assert rows(EmailVerificationToken) == 0
    assert_gone(owner)


@pytest.mark.parametrize("stopped", DELETION_STOPS)
def test_verification_during_the_deletion_brings_nothing_back(
    unverified: tuple[Account, TestClient, str],
    monkeypatch: pytest.MonkeyPatch,
    stopped: str,
) -> None:
    owner, deleting, token = unverified
    gate = deletion_stopped(monkeypatch, stopped)

    deleted, verified, waited = one_after_the_other(
        lambda: remove(deleting), lambda: verify(owner, token), gate
    )

    # No account is left behind for having been verified at the last moment.
    assert waited
    assert deleted.status_code == 204
    assert verified.status_code == 400
    assert verified.json() == {"detail": "Invalid or expired verification token."}
    assert stored(owner) is None
    assert rows(EmailVerificationToken) == 0
    assert_gone(owner)


# --- a link being asked for ----------------------------------------------

LINKS = {
    "forgot-password": PasswordResetToken,
    "resend-verification": EmailVerificationToken,
}


def ask_for_a_link(owner: Account, kind: str) -> Response:
    return owner.client(3).post(f"/auth/{kind}", json={"email": owner.email})


def unverify(owner: Account) -> None:
    with SessionLocal() as db:
        db.execute(
            update(User).where(User.id == owner.id).values(email_verified_at=None)
        )
        db.commit()


@pytest.mark.parametrize("kind", LINKS)
def test_link_that_is_being_issued_is_waited_for_and_removed_with_the_account(
    account, issued: list[str], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    owner = account()
    deleting = signed_in(owner)
    if kind == "resend-verification":
        unverify(owner)
    # The link's token has been made, with the account held for it.
    gate = stopped_after(monkeypatch, auth_service, "generate_token")

    asked, deleted, waited = one_after_the_other(
        lambda: ask_for_a_link(owner, kind), lambda: remove(deleting), gate
    )

    assert waited
    assert (asked.status_code, deleted.status_code) == (202, 204)
    assert rows(LINKS[kind]) == 0
    assert_gone(owner)


@pytest.mark.parametrize("kind", LINKS)
def test_link_asked_for_during_the_deletion_is_not_issued(
    account, issued: list[str], monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    owner = account()
    deleting = signed_in(owner)
    if kind == "resend-verification":
        unverify(owner)
    gate = deletion_stopped(monkeypatch, "has removed everything")
    made_before = len(issued)

    deleted, asked, waited = one_after_the_other(
        lambda: remove(deleting), lambda: ask_for_a_link(owner, kind), gate
    )

    # The answer is the one every such request gets. No link was made for
    # an address that has no account any more, so none was sent either.
    assert waited
    assert (deleted.status_code, asked.status_code) == (204, 202)
    assert len(issued) == made_before
    assert rows(LINKS[kind]) == 0
    assert_gone(owner)


# --- the same deletion twice ---------------------------------------------


@pytest.mark.parametrize("stopped", DELETION_STOPS)
def test_second_deletion_waits_for_the_first_and_finds_no_account(
    account, monkeypatch: pytest.MonkeyPatch, stopped: str
) -> None:
    owner = account()
    here, there = signed_in(owner, 0), signed_in(owner, 1)
    create(here, "Written before any of this")
    gate = deletion_stopped(monkeypatch, stopped)

    first, second, waited = one_after_the_other(
        lambda: remove(here), lambda: remove(there), gate
    )

    assert waited
    assert first.status_code == 204
    # Not a second success, and not an error of the server's: by now it is
    # a request without a session.
    assert second.status_code == 401
    assert second.json() == NOT_AUTHENTICATED
    assert_gone(owner, "Written before any of this")


def test_deletions_from_every_session_at_once_remove_the_account_once(
    account,
) -> None:
    for _ in range(3):
        owner = account()
        clients = [signed_in(owner, number) for number in range(4)]
        create(clients[0], "Written before any of this")

        answers = at_once(4, lambda number: remove(clients[number]).status_code)

        assert sorted(answers) == [204, 401, 401, 401]
        assert stored(owner) is None
        assert rows(Post) == 0


# --- what the account itself is writing ----------------------------------
#
# Another session of the same account writes something while the account is
# being deleted. If the write comes first, the deletion waits for it and
# removes what it wrote. If the deletion comes first, the write waits and
# then has no account to write for.


class Scene:
    """Two accounts and what they have to do with each other.

    ``owner`` is the one that is deleted, and it has two sessions: one that
    writes and one that deletes.
    """

    def __init__(self, account: Callable[[], Account]) -> None:
        self.owner, self.other = account(), account()
        self.writing = signed_in(self.owner, 0)
        self.deleting = signed_in(self.owner, 1)
        self.others = signed_in(self.other)
        self.own_post = create(self.writing, "The owner's own post")
        self.others_post = create(self.others, "Somebody else's post")
        # The owner follows the other, and so may see their stories.
        assert self.writing.post(f"/users/{self.other.name}/follow").status_code == 200
        self.others_story = create_story(self.others, "somebody-elses")
        assert self.writing.post(f"/posts/{self.others_post}/like").status_code == 200

    def assert_owner_gone(self) -> None:
        assert_gone(
            self.owner,
            "The owner's own post",
            "Written at the last moment",
            "written-at-the-last-moment",
            deleted_from=1,
        )
        # What was the other's is as it was, with nothing of the owner on it.
        assert post_row(self.others_post)["content"] == "Somebody else's post"
        assert rows(Story) == 1
        assert rows(Like) == rows(Repost) == rows(Follow) == rows(StoryView) == 0
        assert rows(Post) == 1


def _post(scene: Scene) -> Response:
    return scene.writing.post("/posts", json={"content": "Written at the last moment"})


def _reply_to(post: str) -> Callable[[Scene], Response]:
    return lambda scene: scene.writing.post(
        "/posts",
        json={
            "content": "Written at the last moment",
            "parent_post_id": getattr(scene, post),
        },
    )


def _interaction(method: str, kind: str, post: str) -> Callable[[Scene], Response]:
    return lambda scene: scene.writing.request(
        method, f"/posts/{getattr(scene, post)}/{kind}"
    )


def _follow(method: str) -> Callable[[Scene], Response]:
    return lambda scene: scene.writing.request(
        method, f"/users/{scene.other.name}/follow"
    )


def _story(scene: Scene) -> Response:
    return scene.writing.post(
        "/stories", json={"media_url": f"{MEDIA}/written-at-the-last-moment.jpg"}
    )


def _view(scene: Scene) -> Response:
    return scene.writing.post(f"/stories/{scene.others_story}/view")


# What is written, by the service that writes it, and what it is answered
# with when it is written.
OWN_WRITES = {
    "a post": (posts_service, _post, 201),
    "a reply to somebody else": (posts_service, _reply_to("others_post"), 201),
    "a reply to a post of its own": (posts_service, _reply_to("own_post"), 201),
    "a like": (posts_service, _interaction("POST", "like", "others_post"), 200),
    "a like of a post of its own": (
        posts_service,
        _interaction("POST", "like", "own_post"),
        200,
    ),
    "a like taken back": (
        posts_service,
        _interaction("DELETE", "like", "others_post"),
        200,
    ),
    "a repost": (posts_service, _interaction("POST", "repost", "others_post"), 200),
    "a repost of a post of its own": (
        posts_service,
        _interaction("POST", "repost", "own_post"),
        200,
    ),
    "a follow": (users_service, _follow("POST"), 200),
    "an unfollow": (users_service, _follow("DELETE"), 200),
    "a story": (stories_service, _story, 201),
    "a view of a story": (stories_service, _view, 200),
}


@pytest.mark.parametrize("kind", OWN_WRITES)
def test_what_the_account_was_writing_is_waited_for_and_removed_with_it(
    account, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    scene = Scene(account)
    service, write, written = OWN_WRITES[kind]
    # The write holds its own account, and has written nothing yet.
    gate = stopped_after(monkeypatch, service, "hold_account")

    wrote, deleted, waited = one_after_the_other(
        lambda: write(scene), lambda: remove(scene.deleting), gate
    )

    # The deletion could not have the account while something was being
    # written in its name. It waited, and then removed that as well.
    assert waited
    assert deleted.status_code == 204
    # The write was done. What has been created is read once more to be
    # answered with, and by then it may be gone already, with its account.
    assert wrote.status_code in ({written, 401, 404} if written == 201 else {written})
    scene.assert_owner_gone()


@pytest.mark.parametrize("kind", OWN_WRITES)
@pytest.mark.parametrize("stopped", DELETION_STOPS)
def test_write_by_the_account_during_its_deletion_is_refused(
    account, monkeypatch: pytest.MonkeyPatch, stopped: str, kind: str
) -> None:
    scene = Scene(account)
    _, write, _ = OWN_WRITES[kind]
    gate = deletion_stopped(monkeypatch, stopped)

    deleted, wrote, waited = one_after_the_other(
        lambda: remove(scene.deleting), lambda: write(scene), gate
    )

    # The write waited for its account before it held or wrote anything,
    # and was then a request without a session.
    assert waited
    assert deleted.status_code == 204
    assert wrote.status_code == 401
    assert wrote.json() == NOT_AUTHENTICATED
    scene.assert_owner_gone()


def change_profile(client: TestClient) -> Response:
    return client.patch(
        "/users/me",
        json={"display_name": "Changed at the last moment", "bio": "And a new bio"},
    )


@pytest.mark.parametrize("stopped", DELETION_STOPS)
def test_profile_change_during_the_deletion_is_refused(
    account, monkeypatch: pytest.MonkeyPatch, stopped: str
) -> None:
    owner = account()
    changing, deleting = signed_in(owner, 0), signed_in(owner, 1)
    gate = deletion_stopped(monkeypatch, stopped)

    deleted, changed, waited = one_after_the_other(
        lambda: remove(deleting), lambda: change_profile(changing), gate
    )

    # The change is of the account's own row. It waited for the row and
    # found none to change.
    assert waited
    assert deleted.status_code == 204
    assert changed.status_code == 401
    assert changed.json() == NOT_AUTHENTICATED
    assert_gone(
        owner, "Changed at the last moment", "And a new bio", deleted_from=1
    )


def test_profile_change_that_came_first_is_waited_for_and_removed_with_the_account(
    account,
) -> None:
    owner = account()
    changing, deleting = signed_in(owner, 0), signed_in(owner, 1)
    # The change has been sent to the database and is not committed yet.
    gate = Gate()
    flushed = gate.wrap(lambda session, context: None)
    event.listen(SessionLocal, "after_flush", flushed)
    try:
        changed, deleted, waited = one_after_the_other(
            lambda: change_profile(changing), lambda: remove(deleting), gate
        )
    finally:
        event.remove(SessionLocal, "after_flush", flushed)

    assert waited
    assert deleted.status_code == 204
    # Changed, and answered from the row if it was still there to be read.
    assert changed.status_code in (200, 401)
    assert_gone(
        owner, "Changed at the last moment", "And a new bio", deleted_from=1
    )


@pytest.mark.parametrize("attempt", range(3))
def test_writes_of_every_kind_and_a_deletion_all_at_once(
    account, attempt: int
) -> None:
    scene = Scene(account)
    writes = [write for _, write, _ in OWN_WRITES.values()]
    # Each write from a session of its own, as from so many browsers.
    scenes = []
    for number in range(len(writes)):
        scenes.append(copy.copy(scene))
        scenes[-1].writing = signed_in(scene.owner, number + 2)

    def request(number: int) -> int:
        if number == 0:
            return remove(scene.deleting).status_code
        return writes[number - 1](scenes[number - 1]).status_code

    deleted, *wrote = at_once(len(writes) + 1, request)

    # Each write either got in before the deletion, and was removed by it,
    # or came after it and was refused. None failed.
    assert deleted == 204
    assert set(wrote) <= {200, 201, 401, 404}
    scene.assert_owner_gone()


# --- what others are writing about the account ----------------------------


def test_follow_that_came_first_is_waited_for_and_removed_with_the_account(
    account, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, follower = account(), account()
    deleting, following = signed_in(owner), signed_in(follower)
    # The follow holds the account it is about.
    gate = stopped_after(monkeypatch, users_service, "_shown_user_id")

    followed, deleted, waited = one_after_the_other(
        lambda: following.post(f"/users/{owner.name}/follow"),
        lambda: remove(deleting),
        gate,
    )

    assert waited
    assert (followed.status_code, deleted.status_code) == (200, 204)
    assert followed.json() == {"following": True}
    # Nobody is left following an account that is not there.
    assert rows(Follow) == 0
    assert following.get("/users/me").json()["following_count"] == 0
    assert_gone(owner)


@pytest.mark.parametrize("method", ["POST", "DELETE"])
@pytest.mark.parametrize("stopped", DELETION_STOPS)
def test_follow_during_the_deletion_waits_and_finds_no_account(
    account, monkeypatch: pytest.MonkeyPatch, stopped: str, method: str
) -> None:
    owner, follower = account(), account()
    deleting, following = signed_in(owner), signed_in(follower)
    gate = deletion_stopped(monkeypatch, stopped)

    deleted, followed, waited = one_after_the_other(
        lambda: remove(deleting),
        lambda: following.request(method, f"/users/{owner.name}/follow"),
        gate,
    )

    # The ordinary answer for an account that is not there. Not a failed
    # foreign key, and no follow left pointing at nothing.
    assert waited
    assert (deleted.status_code, followed.status_code) == (204, 404)
    assert followed.json() == {"detail": "User not found."}
    assert rows(Follow) == 0
    assert_gone(owner)


def test_reply_that_came_first_keeps_the_place_of_the_post_it_answers(
    account, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, replier = account(), account()
    deleting, replying = signed_in(owner), signed_in(replier)
    post = create(deleting, "About to be gone with its author")
    # The reply holds the post it answers.
    gate = stopped_after(monkeypatch, posts_service, "_referred_to")

    replied, deleted, waited = one_after_the_other(
        lambda: replying.post(
            "/posts", json={"content": "A reply", "parent_post_id": post}
        ),
        lambda: remove(deleting),
        gate,
    )

    assert waited
    assert (replied.status_code, deleted.status_code) == (201, 204)
    # The deletion saw the reply. The post is kept for it, as a place in the
    # thread without an author or a text.
    left = post_row(post)
    assert (left["author_id"], left["content"]) == (None, None)
    assert post_row(replied.json()["id"])["parent_post_id"] == uuid.UUID(post)
    assert replying.get(f"/posts/{replied.json()['id']}").status_code == 200
    assert replying.get(f"/posts/{post}").status_code == 404
    assert rows(Post) == 2
    assert_gone(owner, "About to be gone with its author")


def test_reply_during_the_deletion_waits_and_finds_no_post(
    account, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, replier = account(), account()
    deleting, replying = signed_in(owner), signed_in(replier)
    post = create(deleting, "About to be gone with its author")
    gate = deletion_stopped(monkeypatch, "has removed everything")

    deleted, replied, waited = one_after_the_other(
        lambda: remove(deleting),
        lambda: replying.post(
            "/posts", json={"content": "A reply", "parent_post_id": post}
        ),
        gate,
    )

    # The ordinary answer for a post that is not there. Not a failed foreign
    # key, and no reply left answering nothing.
    assert waited
    assert (deleted.status_code, replied.status_code) == (204, 404)
    assert replied.json() == {"detail": "Parent post not found."}
    assert rows(Post) == 0
    assert_gone(owner, "About to be gone with its author")


@pytest.mark.parametrize("kind", ["like", "repost"])
def test_interaction_that_came_first_is_removed_with_the_post(
    account, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    owner, other = account(), account()
    deleting, interacting = signed_in(owner), signed_in(other)
    post = create(deleting, "About to be gone with its author")
    gate = stopped_after(monkeypatch, posts_service, "_referred_to")

    interacted, deleted, waited = one_after_the_other(
        lambda: interacting.post(f"/posts/{post}/{kind}"),
        lambda: remove(deleting),
        gate,
    )

    assert waited
    assert (interacted.status_code, deleted.status_code) == (200, 204)
    assert rows(Like) == rows(Repost) == rows(Post) == 0
    assert_gone(owner, "About to be gone with its author")


@pytest.mark.parametrize("kind", ["like", "repost"])
@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_interaction_during_the_deletion_waits_and_finds_no_post(
    account, monkeypatch: pytest.MonkeyPatch, kind: str, method: str
) -> None:
    owner, other = account(), account()
    deleting, interacting = signed_in(owner), signed_in(other)
    post = create(deleting, "About to be gone with its author")
    gate = deletion_stopped(monkeypatch, "has removed everything")

    deleted, interacted, waited = one_after_the_other(
        lambda: remove(deleting),
        lambda: interacting.request(method, f"/posts/{post}/{kind}"),
        gate,
    )

    assert waited
    assert (deleted.status_code, interacted.status_code) == (204, 404)
    assert interacted.json() == {"detail": "Post not found."}
    assert rows(Like) == rows(Repost) == rows(Post) == 0
    assert_gone(owner, "About to be gone with its author")


def test_view_that_came_first_is_removed_with_the_story(
    account, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, viewer = account(), account()
    deleting, viewing = signed_in(owner), signed_in(viewer)
    assert viewing.post(f"/users/{owner.name}/follow").status_code == 200
    story = create_story(deleting, "about-to-be-gone")
    # The view holds the story it is of.
    gate = stopped_after(monkeypatch, stories_service, "_referred_to")

    viewed, deleted, waited = one_after_the_other(
        lambda: viewing.post(f"/stories/{story}/view"),
        lambda: remove(deleting),
        gate,
    )

    assert waited
    assert (viewed.status_code, deleted.status_code) == (200, 204)
    assert rows(Story) == rows(StoryView) == 0
    assert_gone(owner, "about-to-be-gone")


def test_view_during_the_deletion_waits_and_finds_no_story(
    account, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, viewer = account(), account()
    deleting, viewing = signed_in(owner), signed_in(viewer)
    assert viewing.post(f"/users/{owner.name}/follow").status_code == 200
    story = create_story(deleting, "about-to-be-gone")
    gate = deletion_stopped(monkeypatch, "has removed everything")

    deleted, viewed, waited = one_after_the_other(
        lambda: remove(deleting),
        lambda: viewing.post(f"/stories/{story}/view"),
        gate,
    )

    assert waited
    assert (deleted.status_code, viewed.status_code) == (204, 404)
    assert viewed.json() == {"detail": "Story not found."}
    assert rows(Story) == rows(StoryView) == 0
    assert_gone(owner, "about-to-be-gone")


# --- other erasures ------------------------------------------------------


def test_deletion_of_a_post_that_came_first_is_waited_for(
    account, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = account()
    here, there = signed_in(owner, 0), signed_in(owner, 1)
    post = create(here, "Deleted on its own, a moment earlier")
    create(here, "Gone with the account")
    # The deletion of the post holds the erasure lock and the post.
    gate = stopped_after(monkeypatch, posts_service, "_own_post_for_change")

    post_deleted, account_deleted, waited = one_after_the_other(
        lambda: here.delete(f"/posts/{post}"), lambda: remove(there), gate
    )

    assert waited
    assert (post_deleted.status_code, account_deleted.status_code) == (204, 204)
    assert rows(Post) == 0
    assert_gone(
        owner,
        "Deleted on its own, a moment earlier",
        "Gone with the account",
        deleted_from=1,
    )


def test_deletion_of_a_post_during_the_deletion_of_its_account_finds_no_post(
    account, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = account()
    here, there = signed_in(owner, 0), signed_in(owner, 1)
    post = create(here, "Gone with the account")
    gate = deletion_stopped(monkeypatch, "has removed everything")

    account_deleted, post_deleted, waited = one_after_the_other(
        lambda: remove(there), lambda: here.delete(f"/posts/{post}"), gate
    )

    # Erasures happen one at a time. This one waited its turn, and what it
    # was to erase had been erased.
    assert waited
    assert account_deleted.status_code == 204
    assert post_deleted.status_code == 404
    assert post_deleted.json() == {"detail": "Post not found."}
    assert rows(Post) == 0
    assert_gone(owner, "Gone with the account", deleted_from=1)


def crossing_threads(first: TestClient, second: TestClient) -> None:
    """Two threads in which the two take turns, each starting one."""
    for starts, answers in ((first, second), (second, first)):
        parent = None
        for depth in range(6):
            writer = starts if depth % 2 == 0 else answers
            parent = create(writer, f"A turn at depth {depth}", parent)


def test_two_accounts_in_each_others_threads_are_deleted_one_after_the_other(
    account, monkeypatch: pytest.MonkeyPatch
) -> None:
    one, two = account(), account()
    first, second = signed_in(one), signed_in(two)
    crossing_threads(first, second)
    assert rows(Post) == 12
    # The first has erased its posts, and holds the erasure lock.
    gate = stopped_after(monkeypatch, erasure, "erase_posts")

    first_deleted, second_deleted, waited = one_after_the_other(
        lambda: remove(first), lambda: remove(second), gate
    )

    # Each would have had to lock posts in the other's threads, from
    # opposite ends. They do not meet: the second waits for the first.
    assert waited
    assert (first_deleted.status_code, second_deleted.status_code) == (204, 204)
    # With both gone there is nobody whose reply a place had to be kept for.
    assert rows(Post) == 0
    assert_gone(one, "A turn at depth")
    assert_gone(two, "A turn at depth")


def test_accounts_in_each_others_threads_deleted_all_at_once(account) -> None:
    for _ in range(3):
        accounts = [account() for _ in range(3)]
        clients = [signed_in(each) for each in accounts]
        for left, right in ((0, 1), (1, 2), (2, 0)):
            crossing_threads(clients[left], clients[right])
        bystander = signed_in(account())
        answered = create(clients[0], "Answered by a bystander")
        kept = create(bystander, "A bystander's reply", answered)

        answers = at_once(3, lambda number: remove(clients[number]).status_code)

        assert answers == [204, 204, 204]
        for each in accounts:
            assert_gone(each, "A turn at depth", "Answered by a bystander")
        # What is left is the bystander's reply and the place it answers.
        assert post_row(kept)["content"] == "A bystander's reply"
        assert post_row(kept)["parent_post_id"] == uuid.UUID(answered)
        assert rows(Post) == 2
        assert bystander.delete(f"/posts/{kept}").status_code == 204
        assert rows(Post) == 0


def test_view_by_one_account_and_the_deletions_of_both_do_not_wait_in_a_circle(
    account, monkeypatch: pytest.MonkeyPatch
) -> None:
    author, viewer = account(), account()
    authoring = signed_in(author)
    viewing, viewer_deleting = signed_in(viewer, 0), signed_in(viewer, 1)
    assert viewing.post(f"/users/{author.name}/follow").status_code == 200
    story = create_story(authoring, "about-to-be-gone")
    # The author's deletion holds the erasure lock, and has the story still
    # to remove.
    gate = stopped_after(monkeypatch, erasure, "erase_posts")

    with ThreadPoolExecutor(3) as pool:
        gate.close()
        author_deleted = pool.submit(lambda: remove(authoring))
        assert gate.reached.wait(TIMEOUT)
        # The viewer's deletion takes the viewer's account and then waits
        # for the erasure lock.
        viewer_deleted = pool.submit(lambda: remove(viewer_deleting))
        until_waiting(1)
        # The view is the viewer's and of the author's story. It asks for
        # the viewer's account first, like everything else, and so waits
        # there, holding nothing that the author's deletion needs.
        viewed = pool.submit(lambda: viewing.post(f"/stories/{story}/view"))
        until_waiting(2)
        gate.open()
        answers = [
            future.result(TIMEOUT)
            for future in (author_deleted, viewer_deleted, viewed)
        ]

    assert [answer.status_code for answer in answers] == [204, 204, 401]
    assert rows(Story) == rows(StoryView) == rows(Follow) == 0
    assert_gone(author, "about-to-be-gone")
    assert_gone(viewer, deleted_from=1)


# --- the names --------------------------------------------------------------


def test_names_are_taken_until_the_deletion_is_committed_and_free_from_then_on(
    account, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = account()
    deleting, newcomer = signed_in(owner), owner.client(5)
    again = registration(username=owner.name, email=owner.email)
    gate = deletion_stopped(monkeypatch, "has removed everything")

    deleted, refused, waited = one_after_the_other(
        lambda: remove(deleting),
        lambda: newcomer.post("/auth/register", json=again),
        gate,
    )

    # A deletion that has not been committed has not happened: it may yet
    # fail, and then the names are still somebody's.
    assert not waited
    assert refused.status_code == 409
    assert deleted.status_code == 204
    # Committed, they are free. Nothing was kept that remembers them.
    created = newcomer.post("/auth/register", json=again)
    try:
        assert created.status_code == 201
        assert created.json()["id"] != str(owner.id)
        assert stored(owner) is None
    finally:
        # The account that was just made is not the fixture's to remove.
        with SessionLocal() as db:
            db.execute(User.__table__.delete().where(User.username == owner.name))
            db.commit()


# --- a session found a moment before its account goes ---------------------
#
# A request is authenticated by finding its session, and noting that the
# session was used ends a transaction. The account can be removed right
# after that, before the request has read anything else.


def session_found_and_noted(monkeypatch: pytest.MonkeyPatch) -> Gate:
    # Every request notes the use of its session, whenever the last one was.
    monkeypatch.setattr(settings, "session_last_used_interval_seconds", 0)
    return stopped_after(monkeypatch, auth_service, "authenticate_session")


@pytest.mark.parametrize(
    "path", ["/users/{name}", "/users/{name}/follow-status", "/posts/{post}", "/feed"]
)
def test_public_request_whose_account_goes_meanwhile_is_answered_as_anonymous(
    account, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    owner, other = account(), account()
    reading, owning = signed_in(owner), signed_in(owner, 1)
    post = create(signed_in(other), "Somebody else's post")
    anonymous = other.client(5).get(path.format(name=other.name, post=post))
    gate = session_found_and_noted(monkeypatch)

    read, deleted, waited = one_after_the_other(
        lambda: reading.get(path.format(name=other.name, post=post)),
        lambda: remove(owning),
        gate,
    )

    assert not waited
    assert deleted.status_code == 204
    # What anybody gets who is not signed in: the cookie belongs to nobody.
    assert read.status_code == anonymous.status_code == 200
    assert read.json() == anonymous.json()
    assert_gone(owner, deleted_from=1)


@pytest.mark.parametrize("path", ["/auth/me", "/users/me", "/stories", "/feed/following"])
def test_request_that_needs_an_account_that_goes_meanwhile_is_not_authenticated(
    account, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    owner = account()
    reading, owning = signed_in(owner), signed_in(owner, 1)
    gate = session_found_and_noted(monkeypatch)

    read, deleted, waited = one_after_the_other(
        lambda: reading.get(path), lambda: remove(owning), gate
    )

    assert not waited
    assert deleted.status_code == 204
    assert read.status_code == 401
    assert read.json() == NOT_AUTHENTICATED
    assert_gone(owner, deleted_from=1)

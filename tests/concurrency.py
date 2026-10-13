"""What the tests of concurrent requests share.

Most tests run in one transaction that is rolled back (``conftest.session``).
What happens between transactions cannot be tested there. The tests that are
about that use the application as it really runs, with a database session
per request, on accounts that are really committed, and they remove what
they wrote. Everything they need for that is here:

- ``Account``: an account that really exists, with the addresses used
  against it, and its removal;
- ``Gate``: stopping one request at a known point, so that an interleaving
  is forced and not hoped for;
- ``let_run``: letting another request go on until it has finished or is
  seen to be waiting for a lock;
- ``one_after_the_other``: the two together, for two requests;
- ``at_once``: sending several requests at the same moment.

The fixtures that hand these out (``account``, ``issued``) are in
``conftest.py``.
"""

import threading
import time
import uuid
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from types import ModuleType
from typing import TypeVar

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import delete, exists, select, text
from sqlalchemy.orm import Session, aliased

from app.db.session import SessionLocal
from app.main import app
from app.models import Post, RateLimit, Story, User, UserSession
from app.services.rate_limit import key_hash
from helpers import PASSWORD_HASH, log_in

# Long enough for anything in these tests to happen, and never waited out
# when things work: every wait ends as soon as what it waits for has
# happened.
TIMEOUT = 15

# The counters of the rate limiter, by what they are counted for. An account
# has to know them to remove the ones it caused.
LOGIN_SCOPES = ("login:identifier+address", "login:address", "login:identifier")
ADDRESS_SCOPES = (
    "login:address",
    "register",
    "forgot-password",
    "resend-verification",
    "verify-email",
    "reset-password",
)
ACCOUNT_SCOPES = ("password-confirmation:account", "data-export:account")

_Result = TypeVar("_Result")


class Account:
    """An account that really exists, with the addresses used against it.

    Its password is ``helpers.PASSWORD``. Requests are made from addresses
    that no other test uses, one per ``number``, so that the limits per
    address of one test are not another's.
    """

    def __init__(self, *, verified: bool = True) -> None:
        self.name = f"race_{uuid.uuid4().hex[:12]}"
        self.email = f"{self.name}@example.com"
        self.run = uuid.uuid4().hex
        self.addresses: set[str] = set()
        with SessionLocal() as db:
            user = User(
                username=self.name,
                email=self.email,
                password_hash=PASSWORD_HASH,
                display_name="Race",
                email_verified_at=datetime.now(timezone.utc) if verified else None,
            )
            db.add(user)
            db.commit()
            self.id = user.id

    def address(self, number: int = 0) -> str:
        address = f"race-{self.run}-{number}"
        self.addresses.add(address)
        return address

    def client(self, number: int = 0) -> TestClient:
        """A browser at one of the account's addresses.

        It talks to the application as it really runs: every request gets a
        database session of its own.
        """
        return TestClient(
            app, base_url="https://testserver", client=(self.address(number), 50000)
        )

    def sessions(self, db: Session) -> list[UserSession]:
        return list(
            db.scalars(select(UserSession).where(UserSession.user_id == self.id))
        )

    def unused_tokens(self, model: type) -> list[str]:
        with SessionLocal() as db:
            return list(
                db.scalars(
                    select(model.token_hash).where(
                        model.user_id == self.id, model.used_at.is_(None)
                    )
                )
            )

    def login_counters(self, number: int = 0) -> list[int | None]:
        """The three counters of failed logins by name from one address."""
        address = self.address(number)
        subjects = (f"{self.name}\x00{address}", address, self.name)
        with SessionLocal() as db:
            return [
                db.scalar(
                    select(RateLimit.count).where(
                        RateLimit.key_hash == key_hash(scope, subject)
                    )
                )
                for scope, subject in zip(LOGIN_SCOPES, subjects)
            ]

    def keys(self) -> list[str]:
        """The key of every rate-limit counter this account can have caused."""
        keys = [key_hash(scope, str(self.id)) for scope in ACCOUNT_SCOPES]
        for identifier in (self.name, self.email):
            keys.append(key_hash("login:identifier", identifier))
            keys += [
                key_hash("login:identifier+address", f"{identifier}\x00{address}")
                for address in self.addresses
            ]
        for address in self.addresses:
            keys += [key_hash(scope, address) for scope in ADDRESS_SCOPES]
        return keys

    def remove(self) -> None:
        remove_accounts([self])


def remove_accounts(accounts: Iterable[Account]) -> None:
    """Remove the accounts and everything that was written for them.

    Their posts go first, replies before what they answer, since the
    database keeps a post that is answered and an account that has posts.
    Tombstones go the same way once nothing answers them: they name no
    author, so they cannot be told apart by account, and in a database that
    held nothing before the test they can only be the test's own.
    Their sessions, tokens, follows, likes and views go with the accounts.
    An account that the test has already removed is simply not there.
    """
    accounts = list(accounts)
    if not accounts:
        return
    ids = [account.id for account in accounts]
    keys = [key for account in accounts for key in account.keys()]
    reply = aliased(Post)
    with SessionLocal() as db:
        db.execute(delete(RateLimit).where(RateLimit.key_hash.in_(keys)))
        db.execute(delete(Story).where(Story.author_id.in_(ids)))
        while db.execute(
            delete(Post).where(
                Post.author_id.in_(ids) | Post.deleted_at.is_not(None),
                ~exists().where(reply.parent_post_id == Post.id),
            )
        ).rowcount:
            pass
        db.execute(delete(User).where(User.id.in_(ids)))
        db.commit()


class Gate:
    """Stops one call of a function, right after it has returned.

    Once ``close`` has been called, the next call of the wrapped function
    does its work and then waits at the gate until ``open`` is called. Every
    other call passes straight through.
    """

    def __init__(self) -> None:
        self._closed = False
        self._lock = threading.Lock()
        self.reached = threading.Event()
        self._opened = threading.Event()

    def wrap(self, function: Callable) -> Callable:
        def wrapped(*args: object, **kwargs: object) -> object:
            result = function(*args, **kwargs)
            with self._lock:
                stop_here, self._closed = self._closed, False
            if stop_here:
                self.reached.set()
                assert self._opened.wait(TIMEOUT), "the gate was never opened"
            return result

        return wrapped

    def close(self) -> None:
        self._closed = True

    def open(self) -> None:
        self._opened.set()


def signed_in(account: Account, number: int = 0) -> TestClient:
    """A browser at one of the account's addresses, logged in to it."""
    client = account.client(number)
    assert log_in(client, account.name).status_code == 200
    return client


def stopped_after(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType, function: str
) -> Gate:
    """A gate at which the next call of a function stops, once it has done
    its work."""
    gate = Gate()
    monkeypatch.setattr(module, function, gate.wrap(getattr(module, function)))
    return gate


def requests_waiting_for_a_lock() -> int:
    """How many statements the database is holding back for a lock."""
    with SessionLocal() as db:
        return db.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        )


def let_run(other: Future) -> None:
    """Wait until ``other`` has finished, or is waiting for a lock to be released."""
    deadline = time.monotonic() + TIMEOUT
    while not other.done() and requests_waiting_for_a_lock() == 0:
        assert time.monotonic() < deadline, "neither finished nor waiting"
        time.sleep(0.01)


def until_waiting(count: int) -> None:
    """Wait until ``count`` requests are held back by the database for a lock."""
    deadline = time.monotonic() + TIMEOUT
    while requests_waiting_for_a_lock() < count:
        assert time.monotonic() < deadline, f"fewer than {count} requests waiting"
        time.sleep(0.01)


def one_after_the_other(
    first: Callable[[], Response], second: Callable[[], Response], gate: Gate
) -> tuple[Response, Response, bool]:
    """Start ``first``, which stops at the gate; then start ``second`` and
    let it get as far as it can; then let ``first`` go on.

    Returns both answers, and whether ``second`` had to wait for ``first``.
    """
    with ThreadPoolExecutor(2) as pool:
        gate.close()
        earlier = pool.submit(first)
        assert gate.reached.wait(TIMEOUT), "the first request never got that far"
        later = pool.submit(second)
        let_run(later)
        waited = not later.done()
        gate.open()
        return earlier.result(TIMEOUT), later.result(TIMEOUT), waited


def at_once(count: int, request: Callable[[int], _Result]) -> list[_Result]:
    """The answers to ``count`` requests that are all sent at the same moment."""
    together = threading.Barrier(count)

    def run(number: int) -> _Result:
        together.wait()
        return request(number)

    with ThreadPoolExecutor(count) as pool:
        return list(pool.map(run, range(count)))

"""Fixtures for the tests.

The tests run against the database from DATABASE_URL, with the schema created
by ``alembic upgrade head``, so they exercise the real migrated schema. Each
test runs inside a transaction that is rolled back afterwards, so no test data
is ever committed.

The API tests use the same transaction: the application's database dependency
is replaced with the test's session, so requests made through ``client`` see
the test's data and their writes are rolled back with everything else.
"""

import os
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone

# The settings are read when the application is imported, below, and a
# production without its required settings does not load at all. The tests
# need no deployment's configuration: they load as a development setup and
# then switch to the production behaviour themselves (``_production_settings``).
os.environ.setdefault("ENVIRONMENT", "development")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import settings
from app.db.session import engine
from app.main import app
from app.models import Post, Story, User
from app.services import auth as auth_service
from app.services import posts as posts_service
from app.services import stories as stories_service
from app.services.email import get_email_sender
from concurrency import Account, remove_accounts
from database_guard import leak_report, refusal, tables_with_rows
from helpers import add_user, log_in

# --- the database the tests run against ----------------------------------
#
# See ``database_guard``: the tests are not run against a database that has
# anything in it, and they must leave it as empty as they found it.

_FOUND_EMPTY = pytest.StashKey[bool]()


def pytest_sessionstart(session: pytest.Session) -> None:
    if session.config.option.collectonly:
        return
    try:
        with engine.connect() as connection:
            found = tables_with_rows(connection)
    except SQLAlchemyError as error:
        # Only what kind of error it was: its text can hold the connection
        # string, and with it the password.
        pytest.exit(
            "\nThe tests were not run: the database could not be checked "
            f"({type(error).__name__}). Is it running, and is its schema "
            "created (alembic upgrade head)?\n",
            returncode=2,
        )
    if found:
        pytest.exit(refusal(found, engine.url), returncode=2)
    session.config.stash[_FOUND_EMPTY] = True


def pytest_sessionfinish(session: pytest.Session) -> None:
    if not session.config.stash.get(_FOUND_EMPTY, False):
        return
    with engine.connect() as connection:
        found = tables_with_rows(connection)
    if not found:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("=", "the database was not left empty", red=True)
        reporter.write_line(leak_report(found), red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture
def session() -> Iterator[Session]:
    with engine.connect() as connection:
        transaction = connection.begin()
        # The session works in savepoints inside the outer transaction, so a
        # test can provoke an IntegrityError and the rollback stays contained.
        with Session(
            bind=connection,
            join_transaction_mode="create_savepoint",
        ) as session:
            yield session
        transaction.rollback()


def make_user(session: Session, username: str) -> User:
    user = User(
        username=username,
        email=f"{username}@example.com",
        password_hash="not-a-real-hash",
        display_name=username.title(),
    )
    session.add(user)
    session.flush()
    return user


@pytest.fixture
def alice(session: Session) -> User:
    return make_user(session, "alice")


@pytest.fixture
def bob(session: Session) -> User:
    return make_user(session, "bob")


@pytest.fixture
def post(session: Session, alice: User) -> Post:
    post = Post(author=alice, content="Hello, Hopsnop!")
    session.add(post)
    session.flush()
    return post


@pytest.fixture
def story(session: Session, alice: User) -> Story:
    story = Story(
        author=alice,
        media_url="https://media.example.com/stories/1.jpg",
        media_type="image",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    session.add(story)
    session.flush()
    return story


# --- authentication ------------------------------------------------------


@pytest.fixture(autouse=True)
def _production_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # The tests run with the production behaviour whatever the local .env
    # says. The few tests about development behaviour switch it themselves.
    monkeypatch.setattr(settings, "environment", "production")


class Outbox:
    """Stands in for the email provider and keeps what would have been sent."""

    def __init__(self) -> None:
        self.verification: list[tuple[str, str]] = []
        self.password_reset: list[tuple[str, str]] = []

    def send_email_verification(self, *, to: str, url: str) -> None:
        self.verification.append((to, url))

    def send_password_reset(self, *, to: str, url: str) -> None:
        self.password_reset.append((to, url))


@pytest.fixture
def outbox() -> Outbox:
    return Outbox()


@pytest.fixture
def make_client(
    session: Session, outbox: Outbox
) -> Iterator[Callable[..., TestClient]]:
    """Creates API clients. Each has its own cookie jar, like a separate browser."""

    def use_test_session() -> Iterator[Session]:
        # Mirrors a real request, which starts from committed data and loses
        # whatever it leaves uncommitted. Inside the test's transaction a
        # commit only releases a savepoint, so nothing is committed for real.
        session.commit()
        try:
            yield session
        finally:
            session.rollback()

    app.dependency_overrides[get_db] = use_test_session
    app.dependency_overrides[get_email_sender] = lambda: outbox
    clients: list[TestClient] = []

    def make(**kwargs: object) -> TestClient:
        # HTTPS, because the production session cookie is Secure.
        client = TestClient(app, base_url="https://testserver", **kwargs)
        clients.append(client)
        return client

    yield make

    for client in clients:
        client.close()
    app.dependency_overrides.clear()


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


@pytest.fixture
def alice_account(session: Session) -> User:
    """A verified, active account that logs in with ``helpers.PASSWORD``."""
    return add_user(session, "alice")


@pytest.fixture
def bob_account(session: Session) -> User:
    return add_user(session, "bob")


@pytest.fixture
def alice_client(client: TestClient, alice_account: User) -> TestClient:
    """A client that is logged in as alice."""
    assert log_in(client, "alice").status_code == 200
    return client


@pytest.fixture
def bob_client(make_client: Callable[..., TestClient], bob_account: User) -> TestClient:
    """A second browser, logged in as bob."""
    client = make_client()
    assert log_in(client, "bob").status_code == 200
    return client


# --- posts ---------------------------------------------------------------


class Clock:
    """Stands in for the clock of the posts and stories services.

    It only moves when told to.
    """

    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **duration: float) -> None:
        self.now += timedelta(**duration)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Puts the time that the post and story rules see under the test's control."""
    clock = Clock()
    monkeypatch.setattr(posts_service, "_now", clock)
    monkeypatch.setattr(stories_service, "_now", clock)
    return clock


# --- requests that arrive together ----------------------------------------
#
# For the tests that cannot run in the one transaction above, because what
# they are about is what happens between transactions. See ``concurrency``.


@pytest.fixture
def account() -> Iterator[Callable[..., Account]]:
    """Creates accounts that really exist, and removes them afterwards."""
    made: list[Account] = []

    def make(**kwargs: bool) -> Account:
        made.append(Account(**kwargs))
        return made[-1]

    yield make
    remove_accounts(made)


@pytest.fixture
def issued(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every raw token the application generates, in the order it does."""
    tokens: list[str] = []
    generate = auth_service.generate_token

    def recording_generate() -> str:
        tokens.append(generate())
        return tokens[-1]

    monkeypatch.setattr(auth_service, "generate_token", recording_generate)
    return tokens

"""The retention purge while something else is going on.

These run against real transactions (see ``concurrency``): the purge on a
database session of its own, as the command runs it, beside requests that
each have theirs. One of the two is stopped at a known point, the other is
started, and the first is let go once the other has finished or is seen to
be waiting for it.

Whatever the order, nobody is answered with a server error and the purge
does not fail; nothing that was erased comes back; what is in use is not
removed; and the database is consistent afterwards.
"""

import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.orm import ORMExecuteState, Session

from app.core.config import settings
from app.db.session import SessionLocal, engine
from app.models import (
    EmailVerificationToken,
    PasswordResetToken,
    Post,
    RateLimit,
    Story,
    StoryView,
    User,
    UserSession,
)
from app.services import auth as auth_service
from app.services import erasure, rate_limit, retention
from app.services import stories as stories_service
from app.services.rate_limit import key_hash
from concurrency import (
    LOGIN_SCOPES,
    TIMEOUT,
    Account,
    Gate,
    at_once,
    one_after_the_other,
    signed_in,
    stopped_after,
)
from database_guard import tables_with_rows
from helpers import PASSWORD, log_in, registration, what_is_left_of

REPOSITORY = Path(__file__).resolve().parent.parent
LONG = timedelta(days=400)
MEDIA = "https://media.example.com/stories"


def now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _accounts_are_purged(monkeypatch: pytest.MonkeyPatch) -> None:
    # Off unless it is switched on, which it is for these tests.
    monkeypatch.setattr(settings, "purge_unverified_accounts", True)


def run_purge(at: datetime | None = None) -> retention.Report:
    """The purge, the way the command runs it."""
    with SessionLocal() as db:
        return retention.purge(db, at or now())


def rows(model: type, *conditions: object) -> int:
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(model).where(*conditions))


def there(model: type, key: object) -> bool:
    with SessionLocal() as db:
        return db.get(model, key) is not None


class Before(Gate):
    """A gate that stops a call before it does its work, not after."""

    def wrap(self, function: Callable) -> Callable:
        stop_if_closed = super().wrap(lambda: None)

        def wrapped(*args: object, **kwargs: object) -> object:
            stop_if_closed()
            return function(*args, **kwargs)

        return wrapped


class BeforeCommit:
    """Stops a database session just before it commits what one statement
    did: the rows that statement touched are locked, and nothing is
    committed. Used like a ``Gate``."""

    def __init__(
        self, db: Session, statement: Callable[[ORMExecuteState], bool]
    ) -> None:
        self.reached = threading.Event()
        self._opened = threading.Event()
        self._due = False
        commit = db.commit

        def note(state: ORMExecuteState) -> None:
            self._due = self._due or statement(state)

        def stop_and_commit() -> None:
            if self._due and not self.reached.is_set():
                self.reached.set()
                assert self._opened.wait(TIMEOUT), "the gate was never opened"
            commit()

        event.listen(db, "do_orm_execute", note)
        db.commit = stop_and_commit

    def close(self) -> None:
        pass

    def open(self) -> None:
        self._opened.set()


def writes_a_counter(state: ORMExecuteState) -> bool:
    return state.is_insert and state.bind_mapper.class_ is RateLimit


def removes_counters(state: ORMExecuteState) -> bool:
    return state.is_delete and state.bind_mapper.class_ is RateLimit


# --- rows that are of no use, really committed ----------------------------


def add_story(
    author: Account, *, expired: bool, viewers: list[Account] = ()
) -> uuid.UUID:
    created = now() - (LONG if expired else timedelta(hours=1))
    with SessionLocal() as db:
        story = Story(
            author_id=author.id,
            media_url=f"{MEDIA}/{uuid.uuid4().hex}.jpg",
            media_type="image",
            created_at=created,
            expires_at=created + timedelta(hours=24),
        )
        db.add(story)
        db.flush()
        db.add_all(
            StoryView(story_id=story.id, viewer_id=viewer.id) for viewer in viewers
        )
        db.commit()
        return story.id


def add_ended_session(account: Account) -> uuid.UUID:
    with SessionLocal() as db:
        row = UserSession(
            user_id=account.id,
            token_hash=uuid.uuid4().hex,
            created_at=now() - LONG,
            expires_at=now() - LONG + timedelta(days=30),
            last_used_at=now() - LONG,
        )
        db.add(row)
        db.commit()
        return row.id


def add_spent_tokens(account: Account) -> None:
    with SessionLocal() as db:
        for model in (EmailVerificationToken, PasswordResetToken):
            db.add(
                model(
                    user_id=account.id,
                    token_hash=uuid.uuid4().hex,
                    created_at=now() - LONG,
                    expires_at=now() - LONG + timedelta(hours=1),
                )
            )
        db.commit()


@pytest.fixture
def counters() -> Iterator[Callable[..., str]]:
    """Writes rate-limit counters whose window is long over, and removes
    whichever of them are left."""
    keys: list[str] = []

    def add(key: str | None = None, count: int = 3) -> str:
        keys.append(key or key_hash("test", uuid.uuid4().hex))
        with SessionLocal() as db:
            db.add(RateLimit(key_hash=keys[-1], window_start=now() - LONG, count=count))
            db.commit()
        return keys[-1]

    yield add
    with SessionLocal() as db:
        db.execute(delete(RateLimit).where(RateLimit.key_hash.in_(keys)))
        db.commit()


@pytest.fixture
def abandoned(account) -> Callable[[], Account]:
    """Accounts that were registered long ago and never verified."""

    def make() -> Account:
        ghost = account(verified=False)
        with SessionLocal() as db:
            db.execute(
                update(User).where(User.id == ghost.id).values(created_at=now() - LONG)
            )
            db.commit()
        return ghost

    return make


@pytest.fixture
def registered_again() -> Iterator[list[str]]:
    """Usernames that a test registers through the API; removed afterwards."""
    names: list[str] = []
    yield names
    with SessionLocal() as db:
        db.execute(delete(User).where(User.username.in_(names)))
        db.commit()


def remove(client: TestClient) -> Response:
    return client.post("/auth/delete-account", json={"password": PASSWORD})


def create(client: TestClient, content: str, parent: str | None = None) -> str:
    body = {"content": content}
    if parent is not None:
        body["parent_post_id"] = parent
    response = client.post("/posts", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def assert_gone(account: Account, *written: str) -> None:
    with SessionLocal() as db:
        assert (
            what_is_left_of(
                db, user_id=account.id, texts=[account.name, account.email, *written]
            )
            == []
        )


def assert_consistent() -> None:
    """No tombstone that nothing answers, and no reference to a row that is
    not there."""
    reply = Post.__table__.alias("reply")
    with SessionLocal() as db:
        unanswered = db.scalar(
            select(func.count())
            .select_from(Post)
            .where(
                Post.deleted_at.is_not(None),
                ~select(reply.c.id).where(reply.c.parent_post_id == Post.id).exists(),
            )
        )
        assert unanswered == 0
        for table in User.metadata.tables.values():
            for key in table.foreign_keys:
                referred = key.column.table.alias("referred")
                dangling = db.scalar(
                    select(func.count())
                    .select_from(table)
                    .where(
                        key.parent.is_not(None),
                        ~select(referred.c[key.column.name])
                        .where(referred.c[key.column.name] == key.parent)
                        .exists(),
                    )
                )
                assert dangling == 0, f"{table.name}.{key.parent.name}"


def assert_nothing_of_no_use_is_left() -> None:
    assert run_purge().total == 0


# --- an account being deleted ---------------------------------------------
#
# Its expired stories, ended sessions and spent tokens are the purge's to
# remove and the deletion's. Whoever comes first removes them.


@pytest.fixture
def two(account) -> tuple[Account, Account]:
    """An account with rows of no use of every kind, and another whose rows
    are tied up with them."""
    owner, other = account(), account()
    add_story(owner, expired=True, viewers=[other])
    add_story(other, expired=True, viewers=[owner])
    for each in (owner, other):
        add_ended_session(each)
        add_spent_tokens(each)
    return owner, other


@pytest.mark.parametrize(
    "stopped",
    [(auth_service, "_lock_account_if_unchanged"), (erasure, "erase_account")],
    ids=["holds the account", "has removed everything"],
)
def test_purge_beside_a_deletion_that_is_under_way(
    two, monkeypatch: pytest.MonkeyPatch, stopped: tuple
) -> None:
    owner, other = two
    owning = signed_in(owner)
    kept = add_story(other, expired=False, viewers=[owner])
    gate = stopped_after(monkeypatch, *stopped)

    deleted, report, waited = one_after_the_other(
        lambda: remove(owning), run_purge, gate
    )

    assert deleted.status_code == 204
    # A deletion that has got as far as erasing holds the erasure lock, and
    # the purge waits for it before it removes stories. One that only holds
    # its account holds nothing the purge wants.
    assert waited == (stopped[1] == "erase_account")
    assert_gone(owner)
    # Between them they removed everything of no use, each row once.
    assert rows(Story) == 1 and there(Story, kept)
    assert rows(StoryView) == 0
    assert rows(UserSession, UserSession.user_id == other.id) == 0
    assert rows(EmailVerificationToken) == rows(PasswordResetToken) == 0
    assert 1 <= report.expired_stories <= 2
    assert 1 <= report.ended_sessions <= 2
    assert there(User, other.id)
    assert_consistent()
    assert_nothing_of_no_use_is_left()


def test_deletion_waits_for_a_batch_of_stories_and_no_longer(
    two, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, other = two
    owning = signed_in(owner)
    # The purge holds the erasure lock, and has removed nothing yet.
    gate = stopped_after(monkeypatch, erasure, "lock_erasure")

    report, deleted, waited = one_after_the_other(
        run_purge, lambda: remove(owning), gate
    )

    assert waited
    assert deleted.status_code == 204
    # The stories went with the batch, the owner's too: the deletion had
    # not come to them.
    assert report.expired_stories == 2
    assert_gone(owner)
    assert there(User, other.id)
    assert rows(Story) == rows(StoryView) == 0
    assert_consistent()
    assert_nothing_of_no_use_is_left()


def test_viewers_deleting_their_accounts_while_the_stories_they_viewed_are_purged(
    account,
) -> None:
    # Removing a story removes its views, and removing an account removes
    # the views it made. Without the erasure lock the two take those rows in
    # orders of their own, and now and then the database ends one of them
    # as a deadlock. Which order that is, is the database's to choose, so
    # the deadlock itself cannot be forced, and this test passes without
    # the lock more often than not. What is tested for certain is that the
    # two are kept apart: a batch of stories waits for a deletion that is
    # erasing, and a deletion for a batch
    # (``test_purge_beside_a_deletion_that_is_under_way`` and
    # ``test_deletion_waits_for_a_batch_of_stories_and_no_longer`` above,
    # which both fail if the lock is not taken). This one adds that many of
    # them at once all go through.
    for _ in range(3):
        author = account()
        viewers = [account() for _ in range(4)]
        clients = [signed_in(viewer) for viewer in viewers]
        for _ in range(60):
            add_story(author, expired=True, viewers=viewers)

        def request(number: int) -> object:
            if number == 0:
                return run_purge()
            return remove(clients[number - 1]).status_code

        report, *deleted = at_once(5, request)

        assert deleted == [204] * 4
        assert report.expired_stories == 60
        assert rows(Story) == rows(StoryView) == 0
        for viewer in viewers:
            assert_gone(viewer)
        assert there(User, author.id)
        assert_consistent()


def test_purges_deletions_and_writes_all_at_once(account, counters) -> None:
    for _ in range(3):
        owner, other = account(), account()
        owning, othering = signed_in(owner), signed_in(other)
        post = create(owning, "A post of the owner")
        shown = add_story(owner, expired=False)
        add_story(owner, expired=True, viewers=[other])
        add_story(other, expired=True, viewers=[owner])
        add_ended_session(owner)
        add_ended_session(other)
        add_spent_tokens(owner)
        counters()
        with SessionLocal() as db:
            db.execute(delete(RateLimit).where(RateLimit.key_hash.in_(owner.keys())))
            db.commit()

        requests = [
            run_purge,
            run_purge,
            lambda: remove(owning).status_code,
            lambda: othering.post(
                "/posts", json={"content": "A reply", "parent_post_id": post}
            ).status_code,
            lambda: othering.post(f"/stories/{shown}/view").status_code,
            lambda: othering.post(f"/posts/{post}/like").status_code,
        ]
        first, second, deleted, replied, viewed, liked = at_once(
            len(requests), lambda number: requests[number]()
        )

        assert deleted == 204
        assert replied in (201, 404)
        # Nobody follows the owner, so the story is not the other's to view.
        assert viewed == 404
        assert liked in (200, 404)
        # Each row of no use was removed once, by one of the three.
        assert first.expired_stories + second.expired_stories in (1, 2)
        assert first.rate_limit_counters + second.rate_limit_counters == 1
        assert_gone(owner, "A post of the owner")
        assert rows(Story) == rows(StoryView) == 0
        assert rows(UserSession, UserSession.user_id == other.id) == 1
        assert rows(Post, Post.author_id == other.id) == (replied == 201)
        assert_consistent()
        assert_nothing_of_no_use_is_left()


# --- a post being deleted -------------------------------------------------


def test_purge_waits_for_a_post_being_erased(account, monkeypatch) -> None:
    owner, other = account(), account()
    owning, othering = signed_in(owner), signed_in(other)
    add_story(owner, expired=True, viewers=[other])
    answered = create(owning, "Answered, and about to be erased")
    reply = create(othering, "The reply", parent=answered)
    # The post is a tombstone, and nothing is committed.
    gate = stopped_after(monkeypatch, erasure, "erase_posts")

    deleted, report, waited = one_after_the_other(
        lambda: owning.delete(f"/posts/{answered}"), run_purge, gate
    )

    assert waited
    assert deleted.status_code == 204
    assert report.expired_stories == 1
    with SessionLocal() as db:
        tombstone = db.get(Post, uuid.UUID(answered))
        assert tombstone.author_id is None and tombstone.content is None
        assert db.get(Post, uuid.UUID(reply)).parent_post_id == tombstone.id
    assert rows(Post) == 2
    assert_consistent()


def test_post_deletion_waits_for_a_batch_of_stories(account, monkeypatch) -> None:
    owner, other = account(), account()
    owning, othering = signed_in(owner), signed_in(other)
    add_story(owner, expired=True, viewers=[other])
    answered = create(owning, "Answered, and about to be erased")
    create(othering, "The reply", parent=answered)
    alone = create(owning, "Not answered")
    gate = stopped_after(monkeypatch, erasure, "lock_erasure")

    def delete_both() -> list[int]:
        return [
            owning.delete(f"/posts/{post}").status_code for post in (alone, answered)
        ]

    report, deleted, waited = one_after_the_other(run_purge, delete_both, gate)

    assert waited
    assert deleted == [204, 204]
    assert report.expired_stories == 1
    assert not there(Post, uuid.UUID(alone))
    assert rows(Post, Post.deleted_at.is_not(None)) == 1
    assert rows(Post) == 2
    assert_consistent()


def test_threads_taken_apart_while_purges_run_leave_no_tombstone_behind(
    account, counters
) -> None:
    for _ in range(3):
        owner, other = account(), account()
        owning, othering = signed_in(owner), signed_in(other)
        add_story(owner, expired=True, viewers=[other])
        add_ended_session(other)
        counters()
        # A thread three deep, the two authors taking turns, and a post of
        # the other's that nobody deletes.
        root = create(owning, "Root")
        middle = create(othering, "Middle", parent=root)
        leaf = create(owning, "Leaf", parent=middle)
        untouched = create(othering, "Untouched")

        requests = [
            run_purge,
            run_purge,
            lambda: owning.delete(f"/posts/{root}").status_code,
            lambda: othering.delete(f"/posts/{middle}").status_code,
            lambda: owning.delete(f"/posts/{leaf}").status_code,
        ]
        first, second, *deleted = at_once(
            len(requests), lambda number: requests[number]()
        )

        assert deleted == [204, 204, 204]
        assert first.total + second.total == 3
        # The whole thread is gone, tombstones and all, and nothing else is.
        assert rows(Post) == 1 and there(Post, uuid.UUID(untouched))
        assert_consistent()
        assert_nothing_of_no_use_is_left()
        with SessionLocal() as db:
            db.execute(delete(Post).where(Post.id == uuid.UUID(untouched)))
            db.commit()


# --- an account that was never verified -----------------------------------


def resend(account: Account, number: int = 1) -> Response:
    return account.client(number).post(
        "/auth/resend-verification", json={"email": account.email}
    )


def verify(account: Account, token: str, number: int = 2) -> Response:
    return account.client(number).post("/auth/verify-email", json={"token": token})


def test_link_asked_for_after_the_account_was_found_keeps_it(
    abandoned, issued: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ghost = abandoned()
    # The purge has found the account abandoned, and has not locked it yet.
    gate = Before()
    monkeypatch.setattr(
        retention, "_remove_account", gate.wrap(retention._remove_account)
    )

    report, asked, waited = one_after_the_other(run_purge, lambda: resend(ghost), gate)

    assert not waited
    assert asked.status_code == 202 and len(issued) == 1
    # Asked again with the account held, it is not abandoned any more.
    assert report.unverified_accounts == 0
    assert verify(ghost, issued[0]).status_code == 200
    assert log_in(ghost.client(3), ghost.name).status_code == 200


def test_account_that_a_link_is_being_issued_for_is_passed_over(
    abandoned, issued: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ghost = abandoned()
    # The request holds the account and has written the link, uncommitted.
    gate = stopped_after(monkeypatch, auth_service, "_issue_token")

    asked, report, waited = one_after_the_other(lambda: resend(ghost), run_purge, gate)

    # The purge did not wait for the account: it went on without it.
    assert not waited
    assert report.unverified_accounts == 0
    assert asked.status_code == 202
    assert verify(ghost, issued[0]).status_code == 200


def test_link_being_opened_as_it_expires_is_honoured(
    abandoned, issued: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ghost = abandoned()
    assert resend(ghost).status_code == 202
    # For the purge the link has run out; the request that opens it came a
    # moment before that and holds the account.
    later = now() + settings.email_verification_token_lifetime
    gate = stopped_after(monkeypatch, auth_service, "_usable_token")

    verified, report, waited = one_after_the_other(
        lambda: verify(ghost, issued[0]), lambda: run_purge(later), gate
    )

    assert not waited
    assert verified.status_code == 200
    assert report.unverified_accounts == 0
    # Verified, it is never removed for this again.
    assert run_purge(later + LONG).unverified_accounts == 0
    assert log_in(ghost.client(3), ghost.name).status_code == 200


def test_link_opened_while_the_account_is_being_purged_finds_nothing(
    abandoned, issued: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ghost = abandoned()
    assert resend(ghost).status_code == 202
    later = now() + settings.email_verification_token_lifetime
    # The purge has removed the account, and has committed nothing.
    gate = stopped_after(monkeypatch, erasure, "erase_account")

    report, verified, waited = one_after_the_other(
        lambda: run_purge(later), lambda: verify(ghost, issued[0]), gate
    )

    assert waited
    assert report.unverified_accounts == 1
    assert verified.status_code == 400
    assert verified.json() == {"detail": "Invalid or expired verification token."}
    assert_gone(ghost)
    assert resend(ghost, 4).status_code == 202 and len(issued) == 1


def test_name_of_an_account_being_purged_is_free_once_that_is_committed(
    abandoned, registered_again: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ghost = abandoned()
    registered_again.append(ghost.name)
    gate = stopped_after(monkeypatch, erasure, "erase_account")
    again = registration(username=ghost.name, email=ghost.email)

    report, registered, waited = one_after_the_other(
        run_purge, lambda: ghost.client(1).post("/auth/register", json=again), gate
    )

    # Told that the name is taken, as it still was, or made to wait for the
    # purge and then given it: never anything else.
    assert report.unverified_accounts == 1
    assert registered.status_code in (201, 409)
    assert waited == (registered.status_code == 201)
    assert not there(User, ghost.id)
    assert ghost.client(2).post("/auth/register", json=again).status_code in (201, 409)
    assert rows(User, User.username == ghost.name) == 1


# --- purges among themselves ----------------------------------------------


def test_purges_at_once_remove_every_row_once(
    account, abandoned, counters, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "purge_batch_size", 7)
    keeper = account()
    kept_session = signed_in(keeper)
    kept_story = add_story(keeper, expired=False)
    for _ in range(30):
        add_ended_session(keeper)
        counters()
    for _ in range(10):
        add_spent_tokens(keeper)
        add_story(keeper, expired=True, viewers=[keeper])
        abandoned()

    reports = at_once(4, lambda _: run_purge())

    assert {
        category: sum(report.as_dict()[category] for report in reports)
        for category in reports[0].as_dict()
    } == {
        "unverified_accounts": 10,
        "expired_stories": 10,
        "ended_sessions": 30,
        "email_verification_tokens": 10,
        "password_reset_tokens": 10,
        "rate_limit_counters": 30,
    }
    assert rows(User) == 1
    assert rows(Story) == 1 and there(Story, kept_story)
    assert rows(UserSession) == 1
    assert kept_session.get("/auth/me").status_code == 200
    assert_consistent()
    assert_nothing_of_no_use_is_left()


def test_run_after_run_changes_nothing(account, counters) -> None:
    keeper = account()
    signed_in(keeper)
    add_story(keeper, expired=False)
    add_ended_session(keeper)
    counters()
    assert run_purge().total == 2
    with SessionLocal() as db:
        left = tables_with_rows(db.connection())

    for _ in range(3):
        assert run_purge().total == 0
        with SessionLocal() as db:
            assert tables_with_rows(db.connection()) == left


# --- counters that are counted again --------------------------------------


def stored_counter(key: str) -> tuple[int, datetime] | None:
    with SessionLocal() as db:
        found = db.execute(
            select(RateLimit.count, RateLimit.window_start).where(
                RateLimit.key_hash == key
            )
        ).one_or_none()
        return tuple(found) if found is not None else None


def test_counter_that_a_request_is_starting_again_is_passed_over(counters) -> None:
    subject = uuid.uuid4().hex
    key = counters(key_hash("test", subject), count=5)
    limit = rate_limit.Limit("test", subject, 5)

    with SessionLocal() as db:
        # The request has started the counter again, from one, in the row
        # whose window was long over. It has not committed.
        held = BeforeCommit(db, writes_a_counter)
        reservation, report, waited = one_after_the_other(
            lambda: rate_limit.count(db, limit), run_purge, held
        )

    # The purge neither waited for the row nor removed it, and the count
    # that was taken stands.
    assert not waited
    assert report.rate_limit_counters == 0
    count, window_start = stored_counter(key)
    assert count == 1
    assert window_start == reservation.window_start > now() - timedelta(minutes=1)


def test_request_that_meets_a_counter_being_purged_waits_and_counts_from_one(
    account, counters
) -> None:
    owner = account()
    client, address = owner.client(1), owner.address(1)
    subjects = (f"{owner.name}\x00{address}", address, owner.name)
    # All three counters of failed logins, full, their windows long over.
    keys = [
        counters(key_hash(scope, subject), count=5)
        for scope, subject in zip(LOGIN_SCOPES, subjects)
    ]

    with SessionLocal() as db:
        # The purge has removed the three rows, and has not committed.
        held = BeforeCommit(db, removes_counters)
        report, answer, waited = one_after_the_other(
            lambda: retention.purge(db, now()),
            lambda: log_in(client, owner.name, "wrong"),
            held,
        )

    # The attempt waited for the purge and then found no counter, which is
    # what a counter whose window is over comes to: it is not refused, and
    # it is the first of a new window. No count that still counted is lost.
    assert waited
    assert report.rate_limit_counters == 3
    assert answer.status_code == 401
    assert [stored_counter(key)[0] for key in keys] == [1, 1, 1]
    assert owner.login_counters(1) == [1, 1, 1]


# The third way the two can meet is not forced by any test: a request that
# starts the counter again and commits after the purge's statement has begun
# and before it reaches the row. The purge then locks the row as it now is
# and tests it again, which is what PostgreSQL does for a locking read at
# READ COMMITTED; nothing in the application can be stopped between those
# two moments of one statement. The test below sends both at once, several
# times, and has only ever seen the right outcome. That is evidence for it,
# not a proof.


def test_counter_started_again_while_it_is_purged_counts_from_one(
    account, counters
) -> None:
    for _ in range(5):
        owner = account()
        client = owner.client(1)
        address = owner.address(1)
        subjects = (f"{owner.name}\x00{address}", address, owner.name)
        # All three counters of failed logins, full, their windows long over.
        for scope, subject in zip(LOGIN_SCOPES, subjects):
            counters(key_hash(scope, subject), count=5)

        answers = at_once(
            2,
            lambda number: (
                run_purge() if number else log_in(client, owner.name, "wrong")
            ),
        )

        # The window was over either way: the attempt is not refused, and
        # it is the first of a new window, whether it found the old row or
        # none.
        assert answers[0].status_code == 401
        assert owner.login_counters(1) == [1, 1, 1]
        assert answers[1].rate_limit_counters <= 3
        with SessionLocal() as db:
            started = db.scalars(select(RateLimit.window_start)).all()
        assert all(start > now() - timedelta(minutes=1) for start in started)


# --- a story being viewed as it expires -----------------------------------


def test_story_being_viewed_is_passed_over_and_removed_by_a_later_run(
    account, monkeypatch: pytest.MonkeyPatch
) -> None:
    author, viewer = account(), account()
    viewing = signed_in(viewer)
    assert viewing.post(f"/users/{author.name}/follow").status_code == 200
    story = add_story(author, expired=False)
    # For the purge it is long expired; the view came a moment before that
    # and holds the story.
    later = now() + timedelta(days=3)
    gate = stopped_after(monkeypatch, stories_service, "_referred_to")

    viewed, report, waited = one_after_the_other(
        lambda: viewing.post(f"/stories/{story}/view"), lambda: run_purge(later), gate
    )

    assert not waited
    assert viewed.status_code == 200 and viewed.json() == {"viewed": True}
    assert report.expired_stories == 0
    assert rows(StoryView, StoryView.story_id == story) == 1

    assert run_purge(later).expired_stories == 1
    assert rows(Story) == rows(StoryView) == 0
    assert_consistent()


# --- failing, with real transactions --------------------------------------


def test_failure_leaves_committed_what_was_done_and_holds_nothing(
    account, counters
) -> None:
    keeper = account()
    add_ended_session(keeper)
    add_story(keeper, expired=True)
    key = counters()

    def lost(conn, cursor, statement, parameters, context, executemany) -> None:
        if statement.startswith("DELETE FROM rate_limits "):
            raise ConnectionError("the connection was lost")

    event.listen(engine, "before_cursor_execute", lost)
    try:
        with pytest.raises(retention.PurgeError) as failure:
            run_purge()
    finally:
        event.remove(engine, "before_cursor_execute", lost)

    assert failure.value.category == "rate_limit_counters"
    assert failure.value.report.total == 2
    # Seen from another connection: committed, not merely done.
    assert rows(UserSession) == rows(Story) == 0
    assert there(RateLimit, key)
    # Nothing is still held by the run that failed: the next one, and a
    # deletion, go through without waiting.
    assert signed_in(keeper).delete(f"/posts/{uuid.uuid4()}").status_code == 404
    assert run_purge().as_dict()["rate_limit_counters"] == 1


def test_account_whose_removal_fails_is_still_there_and_can_be_removed(
    abandoned, monkeypatch: pytest.MonkeyPatch
) -> None:
    ghost = abandoned()
    add_spent_tokens(ghost)
    erase_account = erasure.erase_account

    def erase_and_fail(*args: object) -> None:
        erase_account(*args)
        raise ConnectionError("the connection was lost")

    with monkeypatch.context() as patched:
        patched.setattr(erasure, "erase_account", erase_and_fail)
        with pytest.raises(retention.PurgeError) as failure:
            run_purge()

    assert failure.value.category == "unverified_accounts"
    assert failure.value.report.total == 0
    assert there(User, ghost.id)
    assert rows(EmailVerificationToken, EmailVerificationToken.user_id == ghost.id) == 1

    assert run_purge().unverified_accounts == 1
    assert_gone(ghost)


# --- the command, as cron runs it -------------------------------------------

DATABASE = engine.url.database
SERVER = f"{engine.url.host}:{engine.url.port}"
EXECUTE = ("--execute", "--database", DATABASE, "--host", SERVER)


def command(*arguments: str, **environment: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "app.purge", *arguments],
        cwd=REPOSITORY,
        env={
            **os.environ,
            "DATABASE_URL": engine.url.render_as_string(hide_password=False),
            "ENVIRONMENT": "development",
            **environment,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def of_no_use(account, abandoned, counters) -> tuple[Account, Account]:
    keeper, ghost = account(), abandoned()
    add_ended_session(keeper)
    add_ended_session(keeper)
    counters()
    return keeper, ghost


def test_command_as_it_is_removes_nothing(of_no_use) -> None:
    run = command()

    assert run.returncode == 0, run.stderr
    assert "(dry run: nothing is removed)" in run.stdout
    assert "ended sessions                       2" in run.stdout
    assert "rate limit counters                  1" in run.stdout
    assert f"run with --execute --database {DATABASE} --host {SERVER}" in run.stdout
    assert rows(UserSession) == 2 and rows(RateLimit) == 1 and rows(User) == 2


def test_command_told_to_removes_and_leaves_accounts_alone_by_default(
    of_no_use,
) -> None:
    keeper, ghost = of_no_use

    first = command(*EXECUTE)
    second = command(*EXECUTE)

    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert first.stderr == second.stderr == ""
    assert "not removed (PURGE_UNVERIFIED_ACCOUNTS is off)" in first.stdout
    assert "ended sessions                       2" in first.stdout
    assert "total                                3" in first.stdout
    assert "total                                0" in second.stdout
    assert rows(UserSession) == rows(RateLimit) == 0
    # As the settings are when nothing is said: the account nobody verified
    # is still there.
    assert there(User, keeper.id) and there(User, ghost.id)


def test_command_removes_accounts_when_that_is_switched_on(of_no_use) -> None:
    keeper, ghost = of_no_use

    run = command(*EXECUTE, PURGE_UNVERIFIED_ACCOUNTS="true")

    assert run.returncode == 0, run.stderr
    assert "unverified accounts           removed after 7 days" in run.stdout
    assert "unverified accounts                  1" in run.stdout
    assert there(User, keeper.id) and not there(User, ghost.id)


@pytest.mark.parametrize(
    "meant",
    [
        ("--database", "hopsnop_production", "--host", SERVER),
        # A database of the same name, on another server.
        ("--database", DATABASE, "--host", "db.production.example:5432"),
    ],
    ids=["another name", "another host"],
)
def test_command_refuses_any_database_but_the_one_it_is_told(
    of_no_use, meant: tuple[str, ...]
) -> None:
    run = command("--execute", *meant)

    assert run.returncode == 2
    assert f"Refused: this is the database {DATABASE!r} on {SERVER}" in run.stderr
    assert "Nothing was changed." in run.stderr
    assert rows(UserSession) == 2 and rows(RateLimit) == 1 and rows(User) == 2


@pytest.mark.parametrize(
    "told",
    [(), ("--database", DATABASE), ("--host", SERVER)],
    ids=["neither", "no host", "no database"],
)
def test_command_does_not_remove_without_database_and_host_being_named(
    of_no_use, told: tuple[str, ...]
) -> None:
    run = command("--execute", *told)

    assert run.returncode == 2
    assert "--execute needs --database NAME and --host HOST:PORT" in run.stderr
    assert rows(UserSession) == 2 and rows(RateLimit) == 1


@pytest.mark.parametrize("arguments", [(), EXECUTE], ids=["dry run", "execute"])
def test_command_that_cannot_reach_the_database_ends_with_1(arguments) -> None:
    unreachable = engine.url.set(port=1, password="a-secret-password")

    run = command(
        *arguments, DATABASE_URL=unreachable.render_as_string(hide_password=False)
    )

    assert run.returncode == 1
    assert "Failed before anything was removed (OperationalError)." in run.stderr
    assert "a-secret-password" not in run.stdout + run.stderr
    assert "Traceback" not in run.stderr

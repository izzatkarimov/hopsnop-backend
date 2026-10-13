"""The migration that brings deleted posts under the rule of erasure.

Before it, a deleted post was a row with ``deleted_at`` set that still had
its author, its text, its likes and its reposts. These tests build such
rows as they were, run the migration over them, and look at what is left.

That cannot be done in the database the other tests use, whose schema is the
current one. Each of these gets a database of its own, created for it and
dropped again, and Alembic is run against it the way it is run by hand.
"""

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db.session import engine as application_engine

REPOSITORY = Path(__file__).resolve().parent.parent
BEFORE = "7f984c985213"
AFTER = "f55b0cf7f446"
WRITTEN = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
EDITED = WRITTEN + timedelta(minutes=10)
DELETED = WRITTEN + timedelta(days=2)


@dataclass
class Scratch:
    """A database that exists for one test, and how to reach it."""

    url: URL
    engine: Engine
    ids: dict[str, uuid.UUID] = field(default_factory=dict)

    def alembic(self, *arguments: str) -> subprocess.CompletedProcess:
        environment = {
            **os.environ,
            "DATABASE_URL": self.url.render_as_string(hide_password=False),
            "ENVIRONMENT": "development",
        }
        return subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=REPOSITORY,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def migrate(self, *arguments: str) -> None:
        done = self.alembic(*arguments)
        assert done.returncode == 0, done.stdout + done.stderr

    def revision(self) -> str:
        with self.engine.connect() as connection:
            return connection.scalar(text("SELECT version_num FROM alembic_version"))

    def one(self, sql: str, **parameters: object) -> object:
        with self.engine.connect() as connection:
            return connection.scalar(text(sql), parameters)

    def post(self, name: str) -> dict[str, object] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                text("SELECT * FROM posts WHERE id = :id"), {"id": self.ids[name]}
            ).one_or_none()
        return dict(row._mapping) if row is not None else None

    def interactions(self, name: str) -> tuple[int, int]:
        return tuple(
            self.one(
                f"SELECT count(*) FROM {table} WHERE post_id = :id", id=self.ids[name]
            )
            for table in ("likes", "reposts")
        )


def create_scratch() -> Iterator[Scratch]:
    name = f"hopsnop_migration_{uuid.uuid4().hex[:12]}"
    administration = create_engine(
        application_engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with administration.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    except DBAPIError as error:
        administration.dispose()
        pytest.skip(f"cannot create a database to migrate ({type(error).__name__})")
    url = application_engine.url.set(database=name)
    scratch = Scratch(url=url, engine=create_engine(url))
    try:
        yield scratch
    finally:
        scratch.engine.dispose()
        with administration.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        administration.dispose()


def add_account(scratch: Scratch, name: str) -> None:
    scratch.ids[name] = uuid.uuid4()
    with scratch.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, username, email, password_hash, "
                "display_name, email_verified_at) "
                "VALUES (:id, :name, :email, 'x', :name, now())"
            ),
            {"id": scratch.ids[name], "name": name, "email": f"{name}@example.com"},
        )


def add_old_post(
    scratch: Scratch,
    name: str,
    author: str,
    *,
    deleted: bool = False,
    answers: str | None = None,
    edited: bool = False,
    liked_by: str | None = None,
) -> None:
    """A post as the old schema held it: deleted or not, it has its author
    and its text. Its text is its name, so it can be looked for afterwards."""
    scratch.ids[name] = uuid.uuid4()
    with scratch.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO posts (id, author_id, content, created_at, updated_at, "
                "parent_post_id, deleted_at) VALUES "
                "(:id, :author, :content, :created, :updated, :parent, :deleted)"
            ),
            {
                "id": scratch.ids[name],
                "author": scratch.ids[author],
                "content": name,
                "created": WRITTEN,
                "updated": EDITED if edited else WRITTEN,
                "parent": scratch.ids[answers] if answers else None,
                "deleted": DELETED if deleted else None,
            },
        )
        if liked_by:
            for table in ("likes", "reposts"):
                connection.execute(
                    text(f"INSERT INTO {table} (user_id, post_id) VALUES (:u, :p)"),
                    {"u": scratch.ids[liked_by], "p": scratch.ids[name]},
                )


# Posts as they were before the migration, each named by its own text, in
# every position a deleted post can be in. A reply comes after what it
# answers.
LEGACY_POSTS: list[tuple[str, str, dict[str, object]]] = [
    ("live: never deleted", "alice", {"liked_by": "bob"}),
    ("deleted: unanswered", "alice", {"deleted": True, "liked_by": "bob"}),
    (
        "deleted: answered",
        "alice",
        {"deleted": True, "edited": True, "liked_by": "bob"},
    ),
    (
        "live: its answer",
        "bob",
        {"answers": "deleted: answered", "liked_by": "alice"},
    ),
    # Deleted all the way down: nothing below holds any of it.
    ("deleted: top of a dead thread", "alice", {"deleted": True}),
    (
        "deleted: middle of a dead thread",
        "bob",
        {"deleted": True, "answers": "deleted: top of a dead thread"},
    ),
    (
        "deleted: bottom of a dead thread",
        "alice",
        {
            "deleted": True,
            "answers": "deleted: middle of a dead thread",
            "liked_by": "bob",
        },
    ),
    # Deleted above a post that is still there.
    (
        "deleted: top of a living thread",
        "alice",
        {"deleted": True, "liked_by": "bob"},
    ),
    (
        "deleted: middle of a living thread",
        "bob",
        {"deleted": True, "answers": "deleted: top of a living thread"},
    ),
    (
        "live: bottom of a living thread",
        "alice",
        {"answers": "deleted: middle of a living thread"},
    ),
    ("live: answered by a deleted reply", "bob", {}),
    (
        "deleted: reply to a live post",
        "alice",
        {"deleted": True, "answers": "live: answered by a deleted reply"},
    ),
]


@pytest.fixture(scope="module")
def migrated() -> Iterator[Scratch]:
    """A database with posts deleted the old way, in every position a deleted
    post can be in, brought to the new schema by the migration."""
    for scratch in create_scratch():
        scratch.migrate("upgrade", BEFORE)
        add_account(scratch, "alice")
        add_account(scratch, "bob")
        for name, author, how in LEGACY_POSTS:
            add_old_post(scratch, name, author, **how)

        scratch.migrate("upgrade", "head")
        yield scratch


@pytest.fixture
def scratch() -> Iterator[Scratch]:
    yield from create_scratch()


# --- what the migration does to posts that were deleted the old way ------


def test_migration_arrives_at_the_new_revision(migrated: Scratch) -> None:
    assert migrated.revision() == AFTER


@pytest.mark.parametrize(
    "name",
    [
        "deleted: unanswered",
        "deleted: top of a dead thread",
        "deleted: middle of a dead thread",
        "deleted: bottom of a dead thread",
        "deleted: reply to a live post",
    ],
)
def test_deleted_post_that_nothing_answers_is_removed(
    migrated: Scratch, name: str
) -> None:
    # Also the ones that only other deleted posts answered: when those went,
    # nothing held them any more.
    assert migrated.post(name) is None
    assert migrated.interactions(name) == (0, 0)


@pytest.mark.parametrize(
    "name",
    [
        "deleted: answered",
        "deleted: top of a living thread",
        "deleted: middle of a living thread",
    ],
)
def test_deleted_post_that_something_answers_becomes_a_tombstone(
    migrated: Scratch, name: str
) -> None:
    tombstone = migrated.post(name)

    assert tombstone["author_id"] is None
    assert tombstone["content"] is None
    # When it was deleted is what it was, not when it was migrated.
    assert tombstone["deleted_at"] == DELETED
    assert tombstone["created_at"] == WRITTEN
    assert migrated.interactions(name) == (0, 0)


def test_tombstone_keeps_its_place_in_the_thread_and_its_times(
    migrated: Scratch,
) -> None:
    edited = migrated.post("deleted: answered")
    middle = migrated.post("deleted: middle of a living thread")
    bottom = migrated.post("live: bottom of a living thread")

    assert edited["updated_at"] == EDITED
    assert middle["parent_post_id"] == migrated.ids["deleted: top of a living thread"]
    assert (
        bottom["parent_post_id"] == migrated.ids["deleted: middle of a living thread"]
    )


@pytest.mark.parametrize(
    ("name", "author", "interactions"),
    [
        ("live: never deleted", "alice", (1, 1)),
        ("live: its answer", "bob", (1, 1)),
        ("live: bottom of a living thread", "alice", (0, 0)),
        ("live: answered by a deleted reply", "bob", (0, 0)),
    ],
)
def test_post_that_was_not_deleted_is_as_it_was(
    migrated: Scratch, name: str, author: str, interactions: tuple[int, int]
) -> None:
    post = migrated.post(name)

    assert post["author_id"] == migrated.ids[author]
    assert post["content"] == name
    assert post["deleted_at"] is None
    assert (post["created_at"], post["updated_at"]) == (WRITTEN, WRITTEN)
    assert migrated.interactions(name) == interactions


def test_no_text_of_a_deleted_post_is_left_in_the_database(migrated: Scratch) -> None:
    count = "SELECT count(*) FROM posts WHERE content LIKE "

    assert migrated.one(count + "'deleted:%'") == 0
    assert migrated.one(count + "'live:%'") == 4
    assert migrated.one("SELECT count(*) FROM posts") == 7


def test_every_row_is_a_post_or_a_tombstone_afterwards(migrated: Scratch) -> None:
    half_erased = migrated.one(
        "SELECT count(*) FROM posts WHERE "
        "(deleted_at IS NULL) <> (author_id IS NOT NULL) "
        "OR (deleted_at IS NULL) <> (content IS NOT NULL)"
    )
    unanswered_tombstones = migrated.one(
        "SELECT count(*) FROM posts AS post WHERE post.deleted_at IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM posts AS reply "
        "WHERE reply.parent_post_id = post.id)"
    )

    assert half_erased == 0
    assert unanswered_tombstones == 0
    assert migrated.one("SELECT count(*) FROM posts WHERE deleted_at IS NOT NULL") == 3


def test_accounts_are_not_touched(migrated: Scratch) -> None:
    assert migrated.one("SELECT count(*) FROM users") == 2


def test_migrated_schema_refuses_a_post_deleted_the_old_way(migrated: Scratch) -> None:
    with pytest.raises(IntegrityError, match="ck_posts_live_or_erased"):
        with migrated.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO posts (id, author_id, content, deleted_at) "
                    "VALUES (:id, :author, 'Still has its text', now())"
                ),
                {"id": uuid.uuid4(), "author": migrated.ids["alice"]},
            )


def test_migrated_schema_is_the_one_the_models_describe(migrated: Scratch) -> None:
    columns = {
        column["name"]: column["nullable"]
        for column in inspect(migrated.engine).get_columns("posts")
    }

    assert (columns["author_id"], columns["content"]) == (True, True)
    assert migrated.alembic("check").returncode == 0


# --- going back ----------------------------------------------------------


def test_downgrade_is_refused_while_there_is_a_tombstone(migrated: Scratch) -> None:
    before = migrated.one("SELECT count(*) FROM posts")

    refused = migrated.alembic("downgrade", "-1")

    output = refused.stdout + refused.stderr
    assert refused.returncode != 0
    assert "Cannot downgrade" in output
    assert "3 erased post(s)" in output
    assert "Erasure cannot be undone" in output
    # Said plainly, not as a traceback of the database refusing a NOT NULL.
    assert "Traceback" not in output
    # And nothing was changed by the attempt: not the revision, not the
    # schema, not a row.
    assert migrated.revision() == AFTER
    assert migrated.one("SELECT count(*) FROM posts") == before
    constraints = {
        check["name"]
        for check in inspect(migrated.engine).get_check_constraints("posts")
    }
    assert "ck_posts_live_or_erased" in constraints


def test_downgrade_without_tombstones_restores_the_old_schema_and_can_be_redone(
    scratch: Scratch,
) -> None:
    scratch.migrate("upgrade", BEFORE)
    add_account(scratch, "alice")
    add_old_post(scratch, "live: kept through it all", "alice", liked_by="alice")
    add_old_post(scratch, "deleted: unanswered", "alice", deleted=True)
    scratch.migrate("upgrade", "head")
    # Nothing answered the deleted post, so it left no tombstone.
    assert scratch.one("SELECT count(*) FROM posts") == 1

    scratch.migrate("downgrade", "-1")

    assert scratch.revision() == BEFORE
    inspector = inspect(scratch.engine)
    columns = {c["name"]: c["nullable"] for c in inspector.get_columns("posts")}
    checks = {check["name"] for check in inspector.get_check_constraints("posts")}
    assert (columns["author_id"], columns["content"]) == (False, False)
    assert "ck_posts_live_or_erased" not in checks
    assert scratch.post("live: kept through it all")["content"] == (
        "live: kept through it all"
    )

    scratch.migrate("upgrade", "head")

    assert scratch.revision() == AFTER
    assert scratch.alembic("check").returncode == 0
    assert scratch.interactions("live: kept through it all") == (1, 1)


def test_upgrade_of_a_database_with_no_deleted_posts_changes_no_row(
    scratch: Scratch,
) -> None:
    scratch.migrate("upgrade", BEFORE)
    add_account(scratch, "alice")
    add_old_post(scratch, "live: a post", "alice", liked_by="alice")
    add_old_post(scratch, "live: a reply", "alice", answers="live: a post", edited=True)
    before = [scratch.post(name) for name in ("live: a post", "live: a reply")]

    scratch.migrate("upgrade", "head")

    assert [scratch.post(name) for name in ("live: a post", "live: a reply")] == before
    assert scratch.interactions("live: a post") == (1, 1)


def test_upgrade_of_an_empty_database_works(scratch: Scratch) -> None:
    scratch.migrate("upgrade", "head")

    assert scratch.revision() == AFTER
    assert scratch.one("SELECT count(*) FROM posts") == 0
    assert scratch.alembic("check").returncode == 0

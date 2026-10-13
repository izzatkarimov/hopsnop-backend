"""Erasing posts, below the API: what the database allows a row to be, what
``app.services.erasure`` does to rows, and how the writes that refer to a
post keep it in place.

What a client sees of a deletion is in ``test_posts_delete.py``; what happens
when requests arrive together is in ``test_post_erasure_concurrency.py``.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, insert, inspect, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import engine
from app.models import Like, Post, Repost, User
from app.services import erasure
from app.services import posts as posts_service
from helpers import (
    add_post,
    add_user,
    erase,
    log_in,
    recorded_selects,
    stored_post,
    where_text_is_stored,
)

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
START = NOW - timedelta(days=1)
CONFLICT = {"detail": "The request could not be completed."}


@pytest.fixture
def alice(session: Session) -> User:
    return add_user(session, "alice")


@pytest.fixture
def bob(session: Session) -> User:
    return add_user(session, "bob")


@pytest.fixture
def carol(session: Session) -> User:
    return add_user(session, "carol")


def post_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Post))


def tombstone_count(session: Session) -> int:
    return session.scalar(select(func.count()).where(Post.deleted_at.is_not(None)))


def like(session: Session, user: User, post: Post) -> None:
    session.add_all(
        [
            Like(user_id=user.id, post_id=post.id),
            Repost(user_id=user.id, post_id=post.id),
        ]
    )
    session.flush()


def erase_through_the_service(session: Session, posts: list[Post]) -> list[uuid.UUID]:
    """Erase the posts the way an operation does: locks first, then the erasing."""
    post_ids = [post.id for post in posts]
    erasure.lock_erasure(session)
    session.scalars(
        select(Post.id).where(Post.id.in_(post_ids)).order_by(Post.id).with_for_update()
    ).all()
    erasure.erase_posts(session, post_ids, NOW)
    session.flush()
    session.expire_all()
    return post_ids


def statements_of(action) -> list[str]:
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statement = " ".join(statement.split())
        if not statement.upper().startswith(("SAVEPOINT", "RELEASE", "ROLLBACK")):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        action()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return statements


# --- the two states of a row ---------------------------------------------


def write_row(session: Session, **values: object) -> None:
    """Write a row of posts as given, past the models and the application."""
    session.execute(insert(Post.__table__).values(id=uuid.uuid4(), **values))


def test_row_with_an_author_and_a_text_and_no_deletion_is_a_post(
    session: Session, alice: User
) -> None:
    write_row(session, author_id=alice.id, content="A post", deleted_at=None)

    assert post_count(session) == 1


def test_row_without_author_and_text_and_with_a_deletion_is_a_tombstone(
    session: Session,
) -> None:
    write_row(session, author_id=None, content=None, deleted_at=NOW)

    assert tombstone_count(session) == 1


HALF_ERASED = {
    "deleted, with its author and text (how deletion used to be)": (True, True, True),
    "deleted, with its text": (False, True, True),
    "deleted, with its author": (True, False, True),
    "not deleted, without an author": (False, True, False),
    "not deleted, without a text": (True, False, False),
    "nothing at all": (False, False, False),
}


@pytest.mark.parametrize("state", HALF_ERASED)
def test_database_refuses_a_row_that_is_neither(
    session: Session, alice: User, state: str
) -> None:
    has_author, has_text, is_deleted = HALF_ERASED[state]

    with pytest.raises(IntegrityError, match="ck_posts_live_or_erased"):
        write_row(
            session,
            author_id=alice.id if has_author else None,
            content="Some text" if has_text else None,
            deleted_at=NOW if is_deleted else None,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"deleted_at": NOW},
        {"content": None},
        {"author_id": None},
        {"content": None, "deleted_at": NOW},
        {"author_id": None, "deleted_at": NOW},
        {"author_id": None, "content": None},
    ],
)
def test_post_cannot_be_left_half_erased_by_an_update(
    session: Session, alice: User, change: dict
) -> None:
    post = add_post(session, alice)

    # Any one or two of the three steps, without the rest.
    with pytest.raises(IntegrityError, match="ck_posts_live_or_erased"):
        session.execute(update(Post).where(Post.id == post.id).values(**change))


def test_tombstone_cannot_be_given_a_text_or_an_author_again(
    session: Session, alice: User
) -> None:
    tombstone = add_post(session, alice, deleted=True)

    for change in ({"content": "Back"}, {"author_id": alice.id}, {"deleted_at": None}):
        savepoint = session.begin_nested()
        with pytest.raises(IntegrityError, match="ck_posts_live_or_erased"):
            session.execute(
                update(Post).where(Post.id == tombstone.id).values(**change)
            )
        savepoint.rollback()


def test_text_that_is_there_is_still_held_to_its_rules(
    session: Session, alice: User
) -> None:
    # Allowing no text at all did not loosen what a text may be.
    for content, constraint in (
        ("", "ck_posts_content"),
        ("   ", "ck_posts_content_not_blank"),
        ("x" * 301, "ck_posts_content_length"),
    ):
        savepoint = session.begin_nested()
        with pytest.raises(IntegrityError, match=constraint):
            write_row(session, author_id=alice.id, content=content, deleted_at=None)
        savepoint.rollback()


def test_migrated_table_is_what_the_model_says(session: Session) -> None:
    inspector = inspect(session.connection())
    columns = {column["name"]: column for column in inspector.get_columns("posts")}
    checks = {check["name"] for check in inspector.get_check_constraints("posts")}
    foreign_keys = {
        key["constrained_columns"][0]: key["options"].get("ondelete")
        for key in inspector.get_foreign_keys("posts")
    }

    assert columns["author_id"]["nullable"] is True
    assert columns["content"]["nullable"] is True
    assert columns["deleted_at"]["nullable"] is True
    assert "ck_posts_live_or_erased" in checks
    # Being allowed to have no author is not being detached from one: an
    # account still cannot be removed from under its posts, nor a post from
    # under its replies.
    assert foreign_keys == {"author_id": "RESTRICT", "parent_post_id": "RESTRICT"}


def test_account_cannot_be_removed_while_a_post_names_it(
    session: Session, alice: User
) -> None:
    add_post(session, alice)

    with pytest.raises(IntegrityError, match="fk_posts_author_id_users"):
        session.execute(delete(User).where(User.id == alice.id))


def test_tombstone_does_not_hold_on_to_the_account_that_wrote_it(
    session: Session, alice: User, bob: User
) -> None:
    post = add_post(session, alice, "Alice's")
    reply = add_post(session, bob, "Bob's reply", parent=post)
    post_id, reply_id = post.id, reply.id
    erase(session, post)

    session.execute(delete(User).where(User.id == alice.id))
    session.expire_all()

    # The account is gone; the thread it was part of is in one piece.
    assert session.get(User, alice.id) is None
    assert stored_post(session, post_id)["author_id"] is None
    assert stored_post(session, reply_id)["parent_post_id"] == post_id


# --- erasing posts -------------------------------------------------------


def test_post_without_replies_is_removed_with_its_likes_and_reposts(
    session: Session, alice: User, bob: User
) -> None:
    post = add_post(session, alice, "Unanswered")
    like(session, bob, post)

    [post_id] = erase_through_the_service(session, [post])

    assert stored_post(session, post_id) is None
    assert session.scalar(select(func.count()).select_from(Like)) == 0
    assert session.scalar(select(func.count()).select_from(Repost)) == 0


def test_post_with_replies_is_left_as_a_tombstone_without_likes_and_reposts(
    session: Session, alice: User, bob: User
) -> None:
    post = add_post(session, alice, "Answered", created_at=START)
    reply = add_post(session, bob, "Reply", parent=post)
    like(session, bob, post)
    like(session, alice, reply)
    reply_id = reply.id

    [post_id] = erase_through_the_service(session, [post])

    assert stored_post(session, post_id) == {
        "id": post_id,
        "author_id": None,
        "content": None,
        "parent_post_id": None,
        "created_at": START,
        "updated_at": START,
        "deleted_at": NOW,
    }
    assert session.scalars(select(Like.post_id)).all() == [reply_id]
    assert session.scalars(select(Repost.post_id)).all() == [reply_id]


def test_everything_one_author_wrote_can_be_erased_at_once(
    session: Session, alice: User, bob: User, carol: User
) -> None:
    """What removing an account will ask for: every post of one author, in
    whatever threads they are."""
    unanswered = add_post(session, alice, "alice: unanswered")
    answered = add_post(session, alice, "alice: answered by bob")
    bobs_answer = add_post(session, bob, "bob: answer", parent=answered)
    own_thread = add_post(session, alice, "alice: start of her own thread")
    own_reply = add_post(session, alice, "alice: reply to herself", parent=own_thread)
    bobs_post = add_post(session, bob, "bob: a post of his own")
    reply_to_bob = add_post(session, alice, "alice: reply to bob", parent=bobs_post)
    answered_reply = add_post(
        session, alice, "alice: reply that carol answers", parent=bobs_post
    )
    carols_answer = add_post(session, carol, "carol: answer", parent=answered_reply)
    for post in (unanswered, answered, own_reply, reply_to_bob, bobs_post, bobs_answer):
        like(session, carol, post)
    alices = [unanswered, answered, own_thread, own_reply, reply_to_bob, answered_reply]
    kept = {
        post.id: stored_post(session, post.id)
        for post in (bobs_answer, bobs_post, carols_answer)
    }
    tombstones = {answered.id, answered_reply.id}

    erased = erase_through_the_service(session, alices)

    for post_id in erased:
        row = stored_post(session, post_id)
        if post_id in tombstones:
            # Kept for what answers it, and nothing of alice's left in it.
            assert (row["author_id"], row["content"]) == (None, None)
            assert row["deleted_at"] == NOW
        else:
            # Her own thread too: its reply went, and then nothing held it.
            assert row is None
    # What others wrote is as it was, to the last column.
    for post_id, before in kept.items():
        assert stored_post(session, post_id) == before
    assert where_text_is_stored(session, "alice:") == []
    assert post_count(session) == 5
    assert session.scalar(select(func.count()).where(Post.author_id == alice.id)) == 0
    # Of the likes, those of what others wrote.
    assert set(session.scalars(select(Like.post_id))) == {bobs_post.id, bobs_answer.id}


def test_erasing_no_posts_does_nothing(session: Session) -> None:
    statements = statements_of(lambda: erasure.erase_posts(session, [], NOW))

    assert statements == []


def test_erasing_what_is_already_erased_changes_nothing(
    session: Session, alice: User, bob: User
) -> None:
    gone = add_post(session, alice, "Unanswered")
    post = add_post(session, alice, "Answered")
    add_post(session, bob, "Reply", parent=post)
    erased = erase_through_the_service(session, [gone, post])
    before = stored_post(session, post.id)

    erasure.erase_posts(session, erased, NOW + timedelta(hours=1))
    erasure.erase_posts(session, [uuid.uuid4()], NOW)

    # The moment of the erasure is the first one, and what is gone is gone.
    assert stored_post(session, erased[1]) == before
    assert before["deleted_at"] == NOW
    assert post_count(session) == 2


def test_erasing_takes_the_same_statements_however_many_posts_there_are(
    session: Session, alice: User, bob: User
) -> None:
    def erasing(count: int) -> list[str]:
        posts = [add_post(session, alice, f"Post {number}") for number in range(count)]
        for post in posts:
            like(session, bob, post)
        post_ids = [post.id for post in posts]
        return statements_of(lambda: erasure.erase_posts(session, post_ids, NOW))

    few, many = erasing(2), erasing(60)

    # Likes, reposts, the erasing itself, and one round of removal: no
    # statement per post. And no parameter per post either: the very same
    # statements, to the letter.
    assert len(few) == len(many) == 5
    assert few == many
    assert post_count(session) == 0


def test_more_posts_can_be_erased_at_once_than_a_statement_takes_parameters(
    session: Session, alice: User, bob: User
) -> None:
    # 65,535 is the most parameters a statement can have. An account can
    # have written more than that.
    count = 70_000
    session.execute(
        text(
            "INSERT INTO posts (id, author_id, content) "
            "SELECT gen_random_uuid(), :author, 'One of very many' "
            "FROM generate_series(1, :count)"
        ),
        {"author": alice.id, "count": count},
    )
    # As a table of that size is in practice: with statistics. Without them
    # this connection would go on using the plans it made for the handful of
    # rows the tests before this one had, and check every removed row's
    # replies by reading the whole table.
    session.execute(text("ANALYZE posts"))
    answered = add_post(session, alice, "One that is answered")
    add_post(session, bob, "Its answer", parent=answered)
    post_ids = session.scalars(select(Post.id).where(Post.author_id == alice.id)).all()
    assert len(post_ids) == count + 1

    erasure.erase_posts(session, post_ids, NOW)

    assert post_count(session) == 2
    assert stored_post(session, answered.id)["content"] is None
    assert where_text_is_stored(session, "One of very many") == []


# --- removing tombstones that nothing answers any more -------------------


def test_pruning_never_removes_a_post_that_is_not_a_tombstone(
    session: Session, alice: User
) -> None:
    posts = [add_post(session, alice, f"Post {number}") for number in range(3)]

    removed = erasure.prune_tombstones(session, [post.id for post in posts])

    assert removed == 0
    assert post_count(session) == 3


def test_pruning_leaves_a_tombstone_that_something_answers(
    session: Session, alice: User, bob: User
) -> None:
    tombstone = add_post(session, alice, deleted=True)
    add_post(session, bob, "Still here", parent=tombstone)

    assert erasure.prune_tombstones(session, [tombstone.id]) == 0
    assert post_count(session) == 2


def test_pruning_removes_a_tombstone_that_nothing_answers(
    session: Session, alice: User
) -> None:
    tombstone = add_post(session, alice, deleted=True)

    assert erasure.prune_tombstones(session, [tombstone.id]) == 1
    assert post_count(session) == 0


def chain_of_tombstones(session: Session, author: User, length: int) -> list[Post]:
    chain: list[Post] = []
    for _ in range(length):
        parent = chain[-1] if chain else None
        chain.append(add_post(session, author, parent=parent, deleted=True))
    return chain


@pytest.mark.parametrize("depth", [1, 2, 5, 40])
def test_pruning_goes_up_through_every_tombstone_it_empties(
    session: Session, alice: User, depth: int
) -> None:
    chain = chain_of_tombstones(session, alice, depth)

    # Asked only about the bottom one.
    removed = erasure.prune_tombstones(session, [chain[-1].id])

    assert removed == depth
    assert post_count(session) == 0


def test_pruning_stops_below_a_post_and_below_an_answered_tombstone(
    session: Session, alice: User, bob: User
) -> None:
    live = add_post(session, alice, "A post, not deleted")
    under_live = add_post(session, alice, parent=live, deleted=True)
    answered = add_post(session, alice, deleted=True)
    add_post(session, bob, "Another answer", parent=answered)
    under_answered = add_post(session, alice, parent=answered, deleted=True)

    removed = erasure.prune_tombstones(session, [under_live.id, under_answered.id])

    assert removed == 2
    assert stored_post(session, live.id)["content"] == "A post, not deleted"
    assert stored_post(session, answered.id) is not None
    assert post_count(session) == 3


def test_pruning_handles_a_tombstone_and_the_one_above_it_in_one_call(
    session: Session, alice: User
) -> None:
    chain = chain_of_tombstones(session, alice, 4)

    removed = erasure.prune_tombstones(session, [post.id for post in chain])

    assert removed == 4
    assert post_count(session) == 0


def test_pruning_takes_two_statements_for_each_level(
    session: Session, alice: User
) -> None:
    shallow = chain_of_tombstones(session, alice, 2)
    deep = chain_of_tombstones(session, alice, 12)

    two = statements_of(lambda: erasure.prune_tombstones(session, [shallow[-1].id]))
    twelve = statements_of(lambda: erasure.prune_tombstones(session, [deep[-1].id]))

    # A lock and a removal for every level that goes.
    assert (len(two), len(twelve)) == (2 * 2, 2 * 12)
    assert all("FOR UPDATE" in sql for sql in twelve[::2])
    assert all(sql.startswith("DELETE FROM posts") for sql in twelve[1::2])


def test_pruning_locks_what_it_looks_at_in_the_order_of_ids(
    session: Session, alice: User
) -> None:
    tombstones = [add_post(session, alice, deleted=True) for _ in range(3)]

    with recorded_selects() as statements:
        erasure.prune_tombstones(session, [post.id for post in tombstones])

    [locking] = statements
    assert " ".join(locking.split()).endswith("ORDER BY posts.id FOR UPDATE")


# --- what a deletion asks the service for --------------------------------


def test_service_refuses_what_the_caller_may_not_delete(
    session: Session, alice: User, bob: User
) -> None:
    post = add_post(session, alice)
    session.commit()

    with pytest.raises(posts_service.NotPostAuthorError):
        posts_service.delete_post(session, bob, post.id)
    with pytest.raises(posts_service.PostNotFoundError):
        posts_service.delete_post(session, alice, uuid.uuid4())

    assert stored_post(session, post.id)["content"] == "Hello, Hopsnop!"


def test_service_does_not_find_a_tombstone_to_delete(
    session: Session, alice: User, bob: User
) -> None:
    tombstone = add_post(session, alice, deleted=True)
    add_post(session, bob, "Reply", parent=tombstone)
    session.commit()

    # Not even "not yours": to everyone it is not there.
    for user in (alice, bob):
        with pytest.raises(posts_service.PostNotFoundError):
            posts_service.delete_post(session, user, tombstone.id)


# --- writes that refer to a post keep it in place ------------------------


def lookups(statements: list[str]) -> list[str]:
    return [" ".join(sql.split()) for sql in statements if "FROM posts" in sql]


def test_reply_holds_the_post_it_answers_until_it_is_written(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    parent = add_post(session, alice_account)

    with recorded_selects() as statements:
        response = alice_client.post(
            "/posts", json={"content": "Reply", "parent_post_id": str(parent.id)}
        )

    assert response.status_code == 201
    # The first thing asked of the database about posts, before the reply
    # is written: is the parent there, and it is to stay.
    finding = lookups(statements)[0]
    assert finding.endswith("FOR KEY SHARE OF posts")
    for rule in (
        "posts.deleted_at IS NULL",
        "users.is_active IS true",
        "users.email_verified_at IS NOT NULL",
    ):
        assert rule in finding


def test_post_that_answers_nothing_holds_no_post(alice_client: TestClient) -> None:
    with recorded_selects() as statements:
        created = alice_client.post("/posts", json={"content": "On its own"})
    assert created.status_code == 201

    # Its author's account is held in place, as for everything written in a
    # user's name, and that is all: there is no post that it refers to.
    locking = [" ".join(sql.split()) for sql in statements if "FOR " in sql]
    assert len(locking) == 1
    assert locking[0].startswith("SELECT users.id FROM users WHERE users.id = ")
    assert locking[0].endswith("FOR KEY SHARE")


@pytest.mark.parametrize("kind", ["like", "repost"])
@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_like_and_repost_hold_the_post_while_they_are_changed(
    alice_client: TestClient,
    session: Session,
    alice_account: User,
    kind: str,
    method: str,
) -> None:
    post = add_post(session, alice_account)

    with recorded_selects() as statements:
        response = alice_client.request(method, f"/posts/{post.id}/{kind}")

    assert response.status_code == 200
    assert lookups(statements)[0].endswith("FOR KEY SHARE OF posts")


def test_reading_holds_nothing(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account)
    add_post(session, alice_account, "Reply", parent=post)

    with recorded_selects() as statements:
        for path in (
            f"/posts/{post.id}",
            f"/posts/{post.id}/replies",
            "/feed",
            "/feed/following",
            "/users/alice/posts",
        ):
            assert alice_client.get(path).status_code == 200

    # Readers never wait for a deletion, and a deletion never for them.
    assert not any("FOR " in sql for sql in statements)


def test_hidden_post_is_found_neither_to_answer_nor_to_like(
    alice_client: TestClient, session: Session, alice_account: User, bob: User
) -> None:
    tombstone = add_post(session, bob, deleted=True)
    add_post(session, alice_account, "Reply", parent=tombstone)
    hidden = add_post(session, add_user(session, "gone", active=False))

    for post in (tombstone, hidden):
        reply = alice_client.post(
            "/posts", json={"content": "Reply", "parent_post_id": str(post.id)}
        )
        liked = alice_client.post(f"/posts/{post.id}/like")
        assert (reply.status_code, liked.status_code) == (404, 404)
        assert reply.json() == {"detail": "Parent post not found."}
        assert liked.json() == {"detail": "Post not found."}
    assert session.scalar(select(func.count()).select_from(Like)) == 0


# --- a write that lost what it refers to ---------------------------------


def test_write_that_refers_to_something_removed_is_a_conflict(
    make_client, session: Session, alice_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    def like_what_is_gone(db: Session, user: User, post_id: uuid.UUID, **kwargs):
        # What a write does that was let through just before its post, or
        # its own account, was removed: it names a row that is not there.
        db.execute(insert(Like).values(user_id=user.id, post_id=uuid.uuid4()))

    monkeypatch.setattr(posts_service, "set_like", like_what_is_gone)
    client = make_client(raise_server_exceptions=False)
    log_in(client)
    post = add_post(session, alice_account)

    response = client.post(f"/posts/{post.id}/like")

    assert response.status_code == 409
    assert response.json() == CONFLICT
    assert session.scalar(select(func.count()).select_from(Like)) == 0


def test_conflict_names_nothing_of_the_database(
    make_client, session: Session, alice_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(db: Session, user: User, post_id: uuid.UUID, **kwargs):
        db.execute(insert(Like).values(user_id=uuid.uuid4(), post_id=post_id))

    monkeypatch.setattr(posts_service, "set_like", fail)
    client = make_client(raise_server_exceptions=False)
    log_in(client)
    post = add_post(session, alice_account)

    response = client.post(f"/posts/{post.id}/like")

    assert response.status_code == 409
    assert set(response.json()) == {"detail"}
    internals = ("foreign", "constraint", "fk_", "likes", "users", "psycopg", "sql")
    for internal in internals:
        assert internal not in response.text.lower()
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


def test_any_other_integrity_error_is_still_a_server_error(
    make_client, session: Session, alice_account: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    def like_twice(db: Session, user: User, post_id: uuid.UUID, **kwargs):
        # A fault of the application's own, not a deletion it lost against.
        db.execute(
            text("INSERT INTO likes (user_id, post_id) VALUES (:u, :p), (:u, :p)"),
            {"u": user.id, "p": post_id},
        )

    monkeypatch.setattr(posts_service, "set_like", like_twice)
    client = make_client(raise_server_exceptions=False)
    log_in(client)
    post = add_post(session, alice_account)

    response = client.post(f"/posts/{post.id}/like")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}

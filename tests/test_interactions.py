"""Likes and reposts: making them, taking them back, and what posts show of them.

A like and a repost follow the same rules, so nearly every test here runs
twice, once for each (the ``kind`` fixture).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.db.session import engine
from app.models import Like, Post, Repost, User, UserSession
from helpers import (
    add_post,
    add_user,
    expire,
    follow,
    log_in,
    plant_session_cookie,
    recorded_selects,
)

NOT_FOUND = {"detail": "Post not found."}
NOT_AUTHENTICATED = {"detail": "Not authenticated."}
NOT_VERIFIED = {"detail": "Email address is not verified."}
INTERACTION_FIELDS = {"like_count", "liked_by_me", "repost_count", "reposted_by_me"}
START = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)
# Every reason a post cannot be read, and so cannot be liked or reposted.
HIDDEN = ["nonexistent", "deleted", "inactive", "unverified"]
# Every endpoint that returns existing posts.
READS = ["single post", "user's posts", "feed"]


@dataclass(frozen=True)
class Kind:
    """One of the two interactions, and what the API calls its parts."""

    name: str  # the last segment of the path
    model: type[Like] | type[Repost]
    state: str  # in the answer to a change: is it there now?
    count: str  # in that answer and in a post: how many are there?
    by_me: str  # in a post: is the reader's own there?


LIKE = Kind("like", Like, "liked", "like_count", "liked_by_me")
REPOST = Kind("repost", Repost, "reposted", "repost_count", "reposted_by_me")


@pytest.fixture(params=[LIKE, REPOST], ids=["like", "repost"])
def kind(request: pytest.FixtureRequest) -> Kind:
    return request.param


@pytest.fixture(params=["POST", "DELETE"])
def method(request: pytest.FixtureRequest) -> str:
    """Making an interaction, or taking it back."""
    return request.param


def other(kind: Kind) -> Kind:
    return REPOST if kind is LIKE else LIKE


def change(client: TestClient, method: str, kind: Kind, post_id: object):
    return client.request(method, f"/posts/{post_id}/{kind.name}")


def do(client: TestClient, kind: Kind, post_id: object):
    return change(client, "POST", kind, post_id)


def undo(client: TestClient, kind: Kind, post_id: object):
    return change(client, "DELETE", kind, post_id)


def state(kind: Kind, active: bool, count: int) -> dict:
    """The answer to a change that left things this way."""
    return {kind.state: active, kind.count: count}


def rows(session: Session, kind: Kind) -> int:
    """How many likes, or reposts, the database holds in all."""
    return session.scalar(select(func.count()).select_from(kind.model))


def add_interaction(session: Session, kind: Kind, user: User, post: Post) -> None:
    """A like or repost written directly to the database."""
    session.add(kind.model(user_id=user.id, post_id=post.id))
    session.flush()


def browser(make_client, username: str) -> TestClient:
    """A separate browser, logged in to an account that exists."""
    client = make_client()
    assert log_in(client, username).status_code == 200
    return client


def browsers(make_client, *usernames: str) -> list[TestClient]:
    return [browser(make_client, username) for username in usernames]


def add_fans(session: Session, count: int) -> list[User]:
    return [add_user(session, f"fan{number}") for number in range(count)]


def add_posts(
    session: Session, count: int, *authors: User, start: datetime = START
) -> list[Post]:
    """``count`` posts one minute apart, by the authors in turn, newest first."""
    posts = [
        add_post(
            session,
            authors[number % len(authors)],
            f"Post {number}",
            created_at=start + number * MINUTE,
        )
        for number in range(count)
    ]
    return posts[::-1]


def hidden_post_id(session: Session, reason: str) -> uuid.UUID:
    """The id of a post that nobody can read, its author included."""
    if reason == "nonexistent":
        return uuid.uuid4()
    author = add_user(
        session,
        f"{reason}_author",
        verified=reason != "unverified",
        active=reason != "inactive",
    )
    return add_post(session, author, "Hidden", deleted=reason == "deleted").id


def read(client: TestClient, where: str, post_id: object, author: str = "alice"):
    """The post as one of the endpoints in READS returns it."""
    if where == "single post":
        return client.get(f"/posts/{post_id}").json()
    path = "/feed" if where == "feed" else f"/users/{author}/posts"
    items = client.get(path, params={"limit": 50}).json()["items"]
    return next(item for item in items if item["id"] == str(post_id))


def shown(post: dict) -> tuple[int, bool, int, bool]:
    """The four interaction fields of a post, in their usual order."""
    return (
        post["like_count"],
        post["liked_by_me"],
        post["repost_count"],
        post["reposted_by_me"],
    )


def feed(client: TestClient, **params: object) -> dict:
    response = client.get("/feed", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def walk(client: TestClient, *, limit: int, cursor: str | None = None) -> list[dict]:
    """Every page of the feed from ``cursor`` on, as returned."""
    pages = []
    while True:
        extra = {"cursor": cursor} if cursor else {}
        pages.append(feed(client, limit=limit, **extra))
        cursor = pages[-1]["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) < 200, "pagination does not terminate"


def ids(posts: list) -> list[str]:
    return [str(post["id"] if isinstance(post, dict) else post.id) for post in posts]


# --- making an interaction and taking it back ----------------------------


def test_user_can_interact_with_another_users_post(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: Kind,
) -> None:
    post = add_post(session, alice_account)

    response = do(bob_client, kind, post.id)

    assert response.status_code == 200
    assert response.json() == state(kind, True, 1)
    [row] = session.scalars(select(kind.model)).all()
    assert (row.user_id, row.post_id) == (bob_account.id, post.id)
    assert row.created_at.tzinfo is not None


def test_user_can_take_an_interaction_back(
    bob_client: TestClient, session: Session, alice_account: User, kind: Kind
) -> None:
    post = add_post(session, alice_account)
    do(bob_client, kind, post.id)

    response = undo(bob_client, kind, post.id)

    assert response.status_code == 200
    assert response.json() == state(kind, False, 0)
    assert rows(session, kind) == 0


def test_answer_holds_the_state_the_count_and_nothing_else(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    kind: Kind,
    method: str,
) -> None:
    post = add_post(session, alice_account)

    body = change(bob_client, method, kind, post.id).json()

    assert set(body) == {kind.state, kind.count}
    assert type(body[kind.state]) is bool
    assert type(body[kind.count]) is int


def test_user_can_interact_with_their_own_post(
    alice_client: TestClient, session: Session, alice_account: User, kind: Kind
) -> None:
    post = add_post(session, alice_account)

    assert do(alice_client, kind, post.id).json() == state(kind, True, 1)
    assert undo(alice_client, kind, post.id).json() == state(kind, False, 0)


def test_reply_is_interacted_with_like_any_post(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: Kind,
) -> None:
    parent = add_post(session, alice_account, "Parent", created_at=START)
    reply = add_post(
        session, bob_account, "Reply", parent=parent, created_at=START + MINUTE
    )

    response = do(bob_client, kind, reply.id)

    assert response.status_code == 200
    assert response.json() == state(kind, True, 1)
    # It is the reply's, not the parent's.
    assert bob_client.get(f"/posts/{reply.id}").json()[kind.count] == 1
    assert bob_client.get(f"/posts/{parent.id}").json()[kind.count] == 0
    assert undo(bob_client, kind, reply.id).json() == state(kind, False, 0)


def test_reply_can_be_interacted_with_when_its_parent_no_longer_can(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: Kind,
) -> None:
    parent = add_post(session, alice_account, "Parent", created_at=START)
    reply = add_post(
        session, bob_account, "Reply", parent=parent, created_at=START + MINUTE
    )
    alice_account.is_active = False
    session.flush()

    assert do(bob_client, kind, parent.id).status_code == 404
    assert do(bob_client, kind, reply.id).json() == state(kind, True, 1)


def test_author_and_anyone_else_can_interact_with_the_same_post(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    kind: Kind,
) -> None:
    post = add_post(session, alice_account)

    # Whoever can read a post can like and repost it, and everyone can read
    # it: no account keeps its posts, or what is done with them, to itself.
    assert do(alice_client, kind, post.id).json() == state(kind, True, 1)
    assert do(bob_client, kind, post.id).json() == state(kind, True, 2)
    assert alice_client.get(f"/posts/{post.id}").json()[kind.by_me] is True
    assert undo(alice_client, kind, post.id).json() == state(kind, False, 1)


def test_like_and_repost_are_independent(
    bob_client: TestClient, session: Session, alice_account: User, kind: Kind
) -> None:
    post = add_post(session, alice_account)
    assert do(bob_client, kind, post.id).json() == state(kind, True, 1)

    # One says nothing about the other, and changing one leaves the other.
    after_one = bob_client.get(f"/posts/{post.id}").json()
    assert (after_one[kind.count], after_one[kind.by_me]) == (1, True)
    assert (after_one[other(kind).count], after_one[other(kind).by_me]) == (0, False)

    assert do(bob_client, other(kind), post.id).json() == state(other(kind), True, 1)
    assert undo(bob_client, kind, post.id).json() == state(kind, False, 0)
    after_both = bob_client.get(f"/posts/{post.id}").json()
    assert (after_both[kind.count], after_both[kind.by_me]) == (0, False)
    assert (after_both[other(kind).count], after_both[other(kind).by_me]) == (1, True)
    assert (rows(session, kind), rows(session, other(kind))) == (0, 1)


def test_interaction_answers_are_not_to_be_cached(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    kind: Kind,
    method: str,
) -> None:
    post = add_post(session, alice_account)

    response = change(bob_client, method, kind, post.id)

    assert response.headers["cache-control"] == "no-store"


# --- repeating a request -------------------------------------------------


def test_making_it_twice_is_the_same_as_making_it_once(
    bob_client: TestClient, session: Session, alice_account: User, kind: Kind
) -> None:
    post = add_post(session, alice_account)

    first = do(bob_client, kind, post.id)
    second = do(bob_client, kind, post.id)

    # Not a conflict: the second request is answered with how things are.
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == state(kind, True, 1)
    assert rows(session, kind) == 1


def test_taking_it_back_twice_is_the_same_as_taking_it_back_once(
    bob_client: TestClient, session: Session, alice_account: User, kind: Kind
) -> None:
    post = add_post(session, alice_account)
    do(bob_client, kind, post.id)

    first = undo(bob_client, kind, post.id)
    second = undo(bob_client, kind, post.id)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == state(kind, False, 0)
    assert rows(session, kind) == 0


def test_taking_back_what_was_never_made_is_not_an_error(
    bob_client: TestClient, session: Session, alice_account: User, kind: Kind
) -> None:
    post = add_post(session, alice_account)

    response = undo(bob_client, kind, post.id)

    assert response.status_code == 200
    assert response.json() == state(kind, False, 0)
    assert rows(session, kind) == 0


def test_make_make_take_back_take_back(
    bob_client: TestClient, session: Session, alice_account: User, kind: Kind
) -> None:
    post = add_post(session, alice_account)

    answers = [
        change(bob_client, method, kind, post.id)
        for method in ("POST", "POST", "DELETE", "DELETE")
    ]

    assert [response.status_code for response in answers] == [200] * 4
    assert [response.json() for response in answers] == [
        state(kind, True, 1),
        state(kind, True, 1),
        state(kind, False, 0),
        state(kind, False, 0),
    ]
    assert rows(session, kind) == 0
    assert bob_client.get(f"/posts/{post.id}").json()[kind.by_me] is False


def test_it_can_be_made_again_after_being_taken_back(
    bob_client: TestClient, session: Session, alice_account: User, kind: Kind
) -> None:
    post = add_post(session, alice_account)

    answers = [
        change(bob_client, method, kind, post.id)
        for method in ("POST", "DELETE", "POST")
    ]

    assert [response.json() for response in answers] == [
        state(kind, True, 1),
        state(kind, False, 0),
        state(kind, True, 1),
    ]
    assert rows(session, kind) == 1


def test_repeating_a_request_never_affects_another_users_interaction(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: Kind,
) -> None:
    post = add_post(session, alice_account)
    do(bob_client, kind, post.id)

    assert do(alice_client, kind, post.id).json() == state(kind, True, 2)
    assert do(alice_client, kind, post.id).json() == state(kind, True, 2)
    assert undo(alice_client, kind, post.id).json() == state(kind, False, 1)
    assert undo(alice_client, kind, post.id).json() == state(kind, False, 1)

    # Bob's is still the one that is there.
    [row] = session.scalars(select(kind.model)).all()
    assert row.user_id == bob_account.id
    assert bob_client.get(f"/posts/{post.id}").json()[kind.by_me] is True


def test_row_that_is_already_there_is_not_an_error(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: Kind,
) -> None:
    # What a request finds when another one, sent at the same moment, was
    # the first to write.
    post = add_post(session, alice_account)
    add_interaction(session, kind, bob_account, post)

    response = do(bob_client, kind, post.id)

    assert response.status_code == 200
    assert response.json() == state(kind, True, 1)
    assert rows(session, kind) == 1


def test_duplicate_is_left_to_the_database_to_refuse(
    bob_client: TestClient, session: Session, alice_account: User, kind: Kind
) -> None:
    post = add_post(session, alice_account)
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        do(bob_client, kind, post.id)
        do(bob_client, kind, post.id)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    # Not "look, then insert", which two simultaneous requests would both
    # get past. Every request inserts, and the primary key settles it.
    table = kind.model.__tablename__
    inserts = [statement for statement in statements if statement.startswith("INSERT")]
    assert len(inserts) == 2
    for statement in inserts:
        assert statement.startswith(f"INSERT INTO {table} (user_id, post_id) VALUES")
        assert statement.endswith("ON CONFLICT DO NOTHING")


# --- counts --------------------------------------------------------------


def test_count_is_the_number_of_users_who_interacted(
    make_client, session: Session, alice_account: User, bob_account: User, kind: Kind
) -> None:
    post = add_post(session, alice_account)
    add_user(session, "carol")
    alice, bob, carol = browsers(make_client, "alice", "bob", "carol")

    answers = [do(user, kind, post.id).json() for user in (alice, bob, carol)]
    assert [answer[kind.count] for answer in answers] == [1, 2, 3]
    assert make_client().get(f"/posts/{post.id}").json()[kind.count] == 3

    assert undo(bob, kind, post.id).json() == state(kind, False, 2)
    assert make_client().get(f"/posts/{post.id}").json()[kind.count] == 2
    assert rows(session, kind) == 2


def test_count_never_goes_below_zero(
    bob_client: TestClient, session: Session, alice_account: User, kind: Kind
) -> None:
    post = add_post(session, alice_account)

    for _ in range(3):
        assert undo(bob_client, kind, post.id).json() == state(kind, False, 0)

    assert bob_client.get(f"/posts/{post.id}").json()[kind.count] == 0


def test_taking_back_does_not_reduce_what_others_made(
    bob_client: TestClient, session: Session, alice_account: User, kind: Kind
) -> None:
    post = add_post(session, alice_account)
    for fan in add_fans(session, 3):
        add_interaction(session, kind, fan, post)

    for _ in range(3):
        assert undo(bob_client, kind, post.id).json() == state(kind, False, 3)

    assert rows(session, kind) == 3


def test_count_belongs_to_one_post(
    bob_client: TestClient, session: Session, alice_account: User, kind: Kind
) -> None:
    first, second = add_posts(session, 2, alice_account)
    for fan in add_fans(session, 4):
        add_interaction(session, kind, fan, first)

    assert do(bob_client, kind, second.id).json() == state(kind, True, 1)
    assert bob_client.get(f"/posts/{first.id}").json()[kind.count] == 4
    assert bob_client.get(f"/posts/{first.id}").json()[kind.by_me] is False


def test_count_is_taken_from_the_rows_that_exist(
    bob_client: TestClient, session: Session, alice_account: User, kind: Kind
) -> None:
    # Nothing is stored that could drift: rows that appear or disappear by
    # any other means are counted, or no longer counted, at the next request.
    post = add_post(session, alice_account)
    fans = add_fans(session, 5)
    for fan in fans:
        add_interaction(session, kind, fan, post)
    assert bob_client.get(f"/posts/{post.id}").json()[kind.count] == 5

    for fan in fans[:2]:
        session.delete(session.get(kind.model, (fan.id, post.id)))
    session.flush()

    assert bob_client.get(f"/posts/{post.id}").json()[kind.count] == 3
    assert do(bob_client, kind, post.id).json() == state(kind, True, 4)


# --- what a post shows ---------------------------------------------------


def test_new_post_starts_with_no_interactions(alice_client: TestClient) -> None:
    created = alice_client.post("/posts", json={"content": "Hello Hopsnop!"})

    assert created.status_code == 201
    body = created.json()
    assert INTERACTION_FIELDS <= set(body)
    assert shown(body) == (0, False, 0, False)
    assert body == alice_client.get(f"/posts/{body['id']}").json()


def test_new_reply_starts_with_no_interactions_whatever_its_parent_has(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    parent = alice_client.post("/posts", json={"content": "Parent"}).json()
    do(bob_client, LIKE, parent["id"])
    do(bob_client, REPOST, parent["id"])

    reply = bob_client.post(
        "/posts", json={"content": "Reply", "parent_post_id": parent["id"]}
    ).json()

    assert shown(reply) == (0, False, 0, False)
    assert shown(bob_client.get(f"/posts/{parent['id']}").json()) == (1, True, 1, True)


@pytest.mark.parametrize("where", READS)
def test_every_post_carries_the_interaction_fields(
    client: TestClient, session: Session, alice_account: User, where: str
) -> None:
    post = add_post(session, alice_account)

    item = read(client, where, post.id)

    assert INTERACTION_FIELDS <= set(item)
    assert type(item["like_count"]) is int
    assert type(item["repost_count"]) is int
    assert type(item["liked_by_me"]) is bool
    assert type(item["reposted_by_me"]) is bool
    assert shown(item) == (0, False, 0, False)


@pytest.mark.parametrize("where", READS)
def test_post_shows_its_counts_and_the_readers_own_interactions(
    make_client, session: Session, alice_account: User, bob_account: User, where: str
) -> None:
    post = add_post(session, alice_account)
    add_user(session, "carol")
    alice, bob, carol = browsers(make_client, "alice", "bob", "carol")
    do(bob, LIKE, post.id)
    do(carol, LIKE, post.id)
    do(carol, REPOST, post.id)

    # The counts are the same for everyone. The rest is each reader's own.
    assert shown(read(make_client(), where, post.id)) == (2, False, 1, False)
    assert shown(read(alice, where, post.id)) == (2, False, 1, False)
    assert shown(read(bob, where, post.id)) == (2, True, 1, False)
    assert shown(read(carol, where, post.id)) == (2, True, 1, True)


@pytest.mark.parametrize("where", READS)
def test_anonymous_reader_gets_the_counts_and_no_interactions_of_their_own(
    client: TestClient, session: Session, alice_account: User, where: str, kind: Kind
) -> None:
    post = add_post(session, alice_account)
    for fan in add_fans(session, 3):
        add_interaction(session, kind, fan, post)

    item = read(client, where, post.id)

    assert client.cookies.get("hopsnop_session") is None
    assert item[kind.count] == 3
    assert item[kind.by_me] is False
    assert item[other(kind).count] == 0
    assert item[other(kind).by_me] is False


@pytest.mark.parametrize("where", READS)
def test_taking_it_back_shows_wherever_the_post_is_returned(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    where: str,
    kind: Kind,
) -> None:
    post = add_post(session, alice_account)

    do(bob_client, kind, post.id)
    made = read(bob_client, where, post.id)
    undo(bob_client, kind, post.id)
    taken_back = read(bob_client, where, post.id)

    assert (made[kind.count], made[kind.by_me]) == (1, True)
    assert (taken_back[kind.count], taken_back[kind.by_me]) == (0, False)


def test_post_is_the_same_object_wherever_it_is_returned(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account)
    do(bob_client, LIKE, post.id)
    do(bob_client, REPOST, post.id)
    add_interaction(session, LIKE, add_user(session, "carol"), post)

    answers = [read(bob_client, where, post.id) for where in READS]

    assert answers[0] == answers[1] == answers[2]
    assert shown(answers[0]) == (2, True, 1, True)


def test_edit_returns_the_posts_counts_and_the_authors_own_interactions(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    post = alice_client.post("/posts", json={"content": "First"}).json()
    do(alice_client, LIKE, post["id"])
    do(bob_client, LIKE, post["id"])
    do(bob_client, REPOST, post["id"])

    edited = alice_client.patch(f"/posts/{post['id']}", json={"content": "Second"})

    assert edited.status_code == 200
    assert edited.json()["content"] == "Second"
    assert shown(edited.json()) == (2, True, 1, False)
    assert edited.json() == alice_client.get(f"/posts/{post['id']}").json()
    # Editing a post leaves what others made of it alone.
    assert (rows(session, LIKE), rows(session, REPOST)) == (2, 1)


def test_interactions_shown_are_those_of_the_session_and_end_with_it(
    bob_client: TestClient, session: Session, alice_account: User, kind: Kind
) -> None:
    post = add_post(session, alice_account)
    do(bob_client, kind, post.id)
    assert bob_client.get(f"/posts/{post.id}").json()[kind.by_me] is True

    bob_client.post("/auth/logout")
    after_logout = bob_client.get(f"/posts/{post.id}").json()

    # The interaction is still there and still counted; the reader is no
    # longer known to be the one who made it.
    assert (after_logout[kind.count], after_logout[kind.by_me]) == (1, False)
    assert rows(session, kind) == 1


def test_reader_cannot_be_named_by_the_request(
    client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: Kind,
) -> None:
    post = add_post(session, alice_account)
    add_interaction(session, kind, bob_account, post)

    bob_id = bob_account.id
    answers = [
        client.get(f"/posts/{post.id}?viewer_id={bob_id}&user_id={bob_id}"),
        client.get(f"/posts/{post.id}?{kind.by_me}=true&as=bob&username=bob"),
        client.get(f"/posts/{post.id}", headers={"X-User-Id": str(bob_id)}),
    ]

    for response in answers:
        assert response.json()[kind.by_me] is False
        assert response.json()[kind.count] == 1


def test_stale_cookie_reads_as_anonymous(
    client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: Kind,
) -> None:
    post = add_post(session, alice_account)
    add_interaction(session, kind, bob_account, post)
    plant_session_cookie(client, "left-over-from-an-old-login")

    item = client.get(f"/posts/{post.id}").json()

    assert (item[kind.count], item[kind.by_me]) == (1, False)


# --- the feed ------------------------------------------------------------


def test_likes_and_reposts_do_not_change_the_order_of_the_feed(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    posts = add_posts(session, 5, alice_account, bob_account)
    reader = make_client()
    before = feed(reader)["items"]

    # The oldest post becomes by far the most liked and reposted.
    for fan in add_fans(session, 6):
        add_interaction(session, LIKE, fan, posts[-1])
        add_interaction(session, REPOST, fan, posts[-1])
    after = feed(reader)["items"]

    # Still newest first. Nothing is ranked by how popular it is.
    assert ids(after) == ids(before) == ids(posts)
    assert shown(after[-1]) == (6, False, 6, False)
    assert all(shown(item) == (0, False, 0, False) for item in after[:-1])
    times = [datetime.fromisoformat(item["created_at"]) for item in after]
    assert times == sorted(times, reverse=True)


def test_readers_own_interactions_do_not_change_the_order_either(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, 4, alice_account)

    do(bob_client, LIKE, posts[3].id)
    do(bob_client, REPOST, posts[2].id)
    items = feed(bob_client)["items"]

    assert ids(items) == ids(posts)
    assert [item["liked_by_me"] for item in items] == [False, False, False, True]
    assert [item["reposted_by_me"] for item in items] == [False, False, True, False]


def test_pages_and_cursors_are_the_same_with_and_without_interactions(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    posts = add_posts(session, 7, alice_account, bob_account)
    before = walk(client, limit=3)

    fans = add_fans(session, 5)
    for number, post in enumerate(posts):
        for fan in fans[: number % 6]:
            add_interaction(session, LIKE, fan, post)
        for fan in fans[: (number * 2) % 5]:
            add_interaction(session, REPOST, fan, post)
    after = walk(client, limit=3)

    # The same posts on the same pages, reached by the same cursors.
    assert [len(page["items"]) for page in after] == [3, 3, 1]
    assert [ids(page["items"]) for page in after] == [
        ids(page["items"]) for page in before
    ]
    assert [page["next_cursor"] for page in after] == [
        page["next_cursor"] for page in before
    ]
    like_counts = [item["like_count"] for page in after for item in page["items"]]
    assert like_counts == [number % 6 for number in range(7)]


def test_interacting_between_pages_causes_no_duplicates_or_gaps(
    bob_client: TestClient, session: Session, alice_account: User, kind: Kind
) -> None:
    posts = add_posts(session, 6, alice_account)
    first = feed(bob_client, limit=2)

    # A post already seen, the very post the cursor points at, and two that
    # are still to come.
    for post in (posts[0], posts[1], posts[3], posts[5]):
        assert do(bob_client, kind, post.id).status_code == 200
    rest = [
        item
        for page in walk(bob_client, limit=2, cursor=first["next_cursor"])
        for item in page["items"]
    ]

    assert ids(rest) == ids(posts[2:])
    assert [item[kind.by_me] for item in rest] == [False, True, False, True]
    assert [item[kind.count] for item in rest] == [0, 1, 0, 1]


def test_interactions_do_not_decide_what_is_in_the_feed(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    shown = add_post(session, alice_account, "Shown", created_at=START)
    deleted = add_post(
        session, alice_account, "Deleted", created_at=START + MINUTE, deleted=True
    )
    gone = add_user(session, "gone", active=False)
    hidden = add_post(session, gone, "Hidden", created_at=START + 2 * MINUTE)
    # A deleted post and one of a deactivated account are the most popular
    # there are.
    for fan in add_fans(session, 3):
        add_interaction(session, LIKE, fan, deleted)
        add_interaction(session, REPOST, fan, deleted)
        add_interaction(session, LIKE, fan, hidden)
        add_interaction(session, REPOST, fan, hidden)

    response = bob_client.get("/feed")

    assert ids(response.json()["items"]) == [str(shown.id)]
    assert "Hidden" not in response.text
    assert "Deleted" not in response.text


def test_feed_item_is_the_post_as_the_same_reader_gets_it_alone(
    bob_client: TestClient, make_client, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, 3, alice_account)
    do(bob_client, LIKE, posts[0].id)
    do(bob_client, REPOST, posts[1].id)

    for reader in (bob_client, make_client()):
        for item in feed(reader)["items"]:
            assert item == reader.get(f"/posts/{item['id']}").json()


# --- who may make or take back an interaction ----------------------------


def test_anonymous_user_cannot_change_an_interaction(
    client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: Kind,
    method: str,
) -> None:
    post = add_post(session, alice_account)
    add_interaction(session, kind, bob_account, post)

    response = change(client, method, kind, post.id)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert rows(session, kind) == 1


def test_anonymous_user_gets_the_answer_every_write_gives(
    client: TestClient,
    session: Session,
    alice_account: User,
    kind: Kind,
    method: str,
) -> None:
    post = add_post(session, alice_account)

    refused = change(client, method, kind, post.id)
    other_write = client.patch(f"/posts/{post.id}", json={"content": "x"})

    assert refused.status_code == other_write.status_code == 401
    assert refused.text == other_write.text
    assert sorted(refused.headers) == sorted(other_write.headers)


def test_anonymous_user_can_still_read_what_they_cannot_change(
    client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: Kind,
) -> None:
    post = add_post(session, alice_account)
    add_interaction(session, kind, bob_account, post)

    assert do(client, kind, post.id).status_code == 401
    assert client.get(f"/posts/{post.id}").json()[kind.count] == 1


def test_interaction_after_logout_is_rejected(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    kind: Kind,
    method: str,
) -> None:
    post = add_post(session, alice_account)
    do(bob_client, kind, post.id)

    bob_client.post("/auth/logout")
    response = change(bob_client, method, kind, post.id)

    assert response.status_code == 401
    assert rows(session, kind) == 1


def test_interaction_with_an_expired_or_made_up_session_is_rejected(
    bob_client: TestClient,
    make_client,
    session: Session,
    alice_account: User,
    kind: Kind,
    method: str,
) -> None:
    post = add_post(session, alice_account)
    expire(session, session.scalars(select(UserSession)).one())
    stranger = make_client()
    plant_session_cookie(stranger, "not-a-session-token")

    for client in (bob_client, stranger):
        response = change(client, method, kind, post.id)
        assert response.status_code == 401
        assert response.json() == NOT_AUTHENTICATED
    assert rows(session, kind) == 0


def test_unverified_user_cannot_change_an_interaction(
    client: TestClient,
    session: Session,
    alice_account: User,
    kind: Kind,
    method: str,
) -> None:
    # The ordinary case: an unverified account never gets a session.
    post = add_post(session, alice_account)
    add_user(session, "unverified", verified=False)

    assert log_in(client, "unverified").status_code == 403
    response = change(client, method, kind, post.id)

    assert response.status_code == 401
    assert rows(session, kind) == 0


def test_session_of_an_unverified_account_cannot_change_an_interaction(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: Kind,
    method: str,
) -> None:
    # Not reachable by logging in. Should a session ever belong to an
    # unverified account all the same, the change is still refused.
    post = add_post(session, alice_account)
    add_interaction(session, kind, bob_account, post)
    bob_account.email_verified_at = None
    session.flush()

    response = change(bob_client, method, kind, post.id)

    assert response.status_code == 403
    assert response.json() == NOT_VERIFIED
    assert rows(session, kind) == 1


def test_inactive_user_cannot_change_an_interaction(
    client: TestClient,
    session: Session,
    alice_account: User,
    kind: Kind,
    method: str,
) -> None:
    post = add_post(session, alice_account)
    add_user(session, "inactive", active=False)

    assert log_in(client, "inactive").status_code == 401
    response = change(client, method, kind, post.id)

    assert response.status_code == 401
    assert rows(session, kind) == 0


def test_user_deactivated_while_logged_in_cannot_change_an_interaction(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: Kind,
    method: str,
) -> None:
    post = add_post(session, alice_account)
    assert do(bob_client, kind, post.id).status_code == 200

    bob_account.is_active = False
    session.flush()
    response = change(bob_client, method, kind, post.id)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert rows(session, kind) == 1


# --- which posts ---------------------------------------------------------


@pytest.mark.parametrize("reason", HIDDEN)
def test_post_that_cannot_be_read_cannot_be_interacted_with(
    bob_client: TestClient, session: Session, kind: Kind, method: str, reason: str
) -> None:
    post_id = hidden_post_id(session, reason)

    response = change(bob_client, method, kind, post_id)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert rows(session, kind) == 0


def test_following_plays_no_part_in_interacting_with_a_post(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: Kind,
) -> None:
    post = add_post(session, alice_account)

    # Without following the author, and no differently once he does.
    assert do(bob_client, kind, post.id).json() == state(kind, True, 1)
    follow(session, bob_account, alice_account)
    assert undo(bob_client, kind, post.id).json() == state(kind, False, 0)
    assert do(bob_client, kind, post.id).json() == state(kind, True, 1)
    assert rows(session, kind) == 1


def test_no_profile_change_closes_a_post_to_interactions(
    alice_client: TestClient, bob_client: TestClient, kind: Kind
) -> None:
    post = alice_client.post("/posts", json={"content": "Hello Hopsnop!"}).json()

    # There is no privacy setting to switch on, alone or next to a real change.
    alice_client.patch("/users/me", json={"is_private": True})
    alice_client.patch("/users/me", json={"is_private": True, "bio": "Hi"})

    assert do(bob_client, kind, post["id"]).json() == state(kind, True, 1)
    assert bob_client.get(f"/posts/{post['id']}").json()[kind.by_me] is True
    assert undo(bob_client, kind, post["id"]).json() == state(kind, False, 0)


def test_deleted_post_cannot_be_interacted_with_by_its_own_author(
    alice_client: TestClient, session: Session, kind: Kind, method: str
) -> None:
    post = alice_client.post("/posts", json={"content": "Short-lived"}).json()
    assert alice_client.delete(f"/posts/{post['id']}").status_code == 204

    response = change(alice_client, method, kind, post["id"])

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert rows(session, kind) == 0


@pytest.mark.parametrize("how", ["deleted", "deactivated"])
def test_interaction_cannot_be_changed_once_the_post_is_hidden(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    kind: Kind,
    method: str,
    how: str,
) -> None:
    post = add_post(session, alice_account)
    assert do(bob_client, kind, post.id).json() == state(kind, True, 1)

    if how == "deleted":
        post.deleted_at = datetime.now(timezone.utc)
    else:
        alice_account.is_active = False
    session.flush()
    response = change(bob_client, method, kind, post.id)

    # Not found, like the post itself, and the attempt changes nothing: the
    # row stays with the post it belongs to, as it does when a post is
    # deleted.
    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert rows(session, kind) == 1


def test_deleted_post_reports_nothing_about_its_interactions(
    alice_client: TestClient,
    bob_client: TestClient,
    make_client,
    session: Session,
    kind: Kind,
) -> None:
    post = alice_client.post("/posts", json={"content": "Short-lived"}).json()
    do(bob_client, kind, post["id"])
    do(alice_client, kind, post["id"])
    assert alice_client.delete(f"/posts/{post['id']}").status_code == 204

    for reader in (make_client(), alice_client, bob_client):
        single = reader.get(f"/posts/{post['id']}")
        assert single.status_code == 404
        assert single.json() == NOT_FOUND
        assert reader.get("/feed").json() == {"items": [], "next_cursor": None}
        assert reader.get("/users/alice/posts").json()["items"] == []
    # The rows are kept with the post, as they were before this phase.
    assert rows(session, kind) == 2


def test_interaction_is_shown_again_when_the_post_can_be_read_again(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    kind: Kind,
) -> None:
    post = alice_client.post("/posts", json={"content": "Now you see me"}).json()
    do(bob_client, kind, post["id"])

    alice_account.is_active = False
    session.flush()
    assert bob_client.get(f"/posts/{post['id']}").status_code == 404
    assert undo(bob_client, kind, post["id"]).status_code == 404

    alice_account.is_active = True
    session.flush()
    visible_again = bob_client.get(f"/posts/{post['id']}").json()

    assert (visible_again[kind.count], visible_again[kind.by_me]) == (1, True)
    assert undo(bob_client, kind, post["id"]).json() == state(kind, False, 0)


@pytest.mark.parametrize("post_id", ["1", "abc", "me", "00000000-0000-0000-0000"])
def test_malformed_post_id_is_rejected(
    bob_client: TestClient, session: Session, kind: Kind, method: str, post_id: str
) -> None:
    response = change(bob_client, method, kind, post_id)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["path", "post_id"]
    assert rows(session, kind) == 0


def test_a_users_id_is_not_a_post_to_interact_with(
    bob_client: TestClient, session: Session, alice_account: User, kind: Kind
) -> None:
    assert do(bob_client, kind, alice_account.id).status_code == 404
    assert rows(session, kind) == 0


# --- cost ----------------------------------------------------------------


def popular_posts(session: Session, count: int) -> list[Post]:
    """``count`` posts by several authors, with likes and reposts on most."""
    authors = [add_user(session, f"author{number}") for number in range(5)]
    fans = add_fans(session, 6)
    posts = add_posts(session, count, *authors)
    for number, post in enumerate(posts):
        for fan in fans[: number % 7]:
            add_interaction(session, LIKE, fan, post)
        for fan in fans[: number % 4]:
            add_interaction(session, REPOST, fan, post)
    return posts


def test_feed_takes_one_query_however_many_posts_likes_and_reposts_there_are(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_post(session, alice_account)
    # Like a real request, which starts with nothing loaded.
    session.expunge_all()
    with recorded_selects() as with_one:
        assert len(feed(client, limit=50)["items"]) == 1

    popular_posts(session, 20)
    session.expunge_all()
    with recorded_selects() as with_many:
        items = feed(client, limit=50)["items"]

    assert len(items) == 21
    assert sum(item["like_count"] for item in items) == 57
    assert sum(item["repost_count"] for item in items) == 30
    # Posts, authors, counts: one statement. No query per post, per like or
    # per repost.
    assert len(with_one) == len(with_many) == 1


def test_feed_takes_no_more_queries_for_a_signed_in_reader(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    bob_id = bob_account.id
    lone = add_post(session, alice_account)
    add_interaction(session, LIKE, bob_account, lone)
    session.expunge_all()
    with recorded_selects() as with_one:
        [item] = feed(bob_client, limit=50)["items"]
    assert shown(item) == (1, True, 0, False)

    bob = session.get(User, bob_id)
    for number, post in enumerate(popular_posts(session, 20)):
        if number % 2:
            add_interaction(session, LIKE, bob, post)
        if number % 3 == 0:
            add_interaction(session, REPOST, bob, post)
    session.expunge_all()
    with recorded_selects() as with_many:
        items = feed(bob_client, limit=50)["items"]

    assert len(items) == 21
    assert sum(item["liked_by_me"] for item in items) == 11
    assert sum(item["reposted_by_me"] for item in items) == 7
    # One to find the session and its user, one for the page with everything
    # on it, the reader's own likes and reposts included.
    assert len(with_one) == len(with_many) == 2


def test_a_users_posts_take_the_same_queries_with_and_without_interactions(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    alice_id, bob_id = alice_account.id, bob_account.id
    add_post(session, alice_account)
    session.expunge_all()
    with recorded_selects() as with_one:
        assert len(bob_client.get("/users/alice/posts").json()["items"]) == 1

    alice = session.get(User, alice_id)
    bob = session.get(User, bob_id)
    fans = add_fans(session, 4)
    for number, post in enumerate(add_posts(session, 15, alice)):
        for fan in fans[: number % 5]:
            add_interaction(session, LIKE, fan, post)
        add_interaction(session, REPOST, bob, post)
    session.expunge_all()
    with recorded_selects() as with_many:
        items = bob_client.get("/users/alice/posts").json()["items"]

    assert len(items) == 16
    assert sum(item["reposted_by_me"] for item in items) == 15
    # The session, the account, the page.
    assert len(with_one) == len(with_many) == 3


def test_single_post_takes_one_query_however_many_interactions_it_has(
    client: TestClient, session: Session, alice_account: User
) -> None:
    quiet = add_post(session, alice_account).id
    popular = add_post(session, alice_account).id
    for fan in add_fans(session, 12):
        session.add(Like(user_id=fan.id, post_id=popular))
        session.add(Repost(user_id=fan.id, post_id=popular))
    session.flush()
    session.expunge_all()

    with recorded_selects() as for_quiet:
        assert shown(client.get(f"/posts/{quiet}").json()) == (0, False, 0, False)
    with recorded_selects() as for_popular:
        assert shown(client.get(f"/posts/{popular}").json()) == (12, False, 12, False)

    assert len(for_quiet) == len(for_popular) == 1
    assert for_quiet == for_popular


def test_changing_an_interaction_takes_the_same_queries_however_many_there_are(
    bob_client: TestClient, session: Session, alice_account: User, kind: Kind
) -> None:
    quiet = add_post(session, alice_account).id
    popular = add_post(session, alice_account).id
    for fan in add_fans(session, 12):
        session.add(kind.model(user_id=fan.id, post_id=popular))
    session.flush()
    session.expunge_all()

    with recorded_selects() as for_quiet:
        assert do(bob_client, kind, quiet).json() == state(kind, True, 1)
    with recorded_selects() as for_popular:
        assert do(bob_client, kind, popular).json() == state(kind, True, 13)

    # The session, the post, and how things stand after the change.
    assert len(for_quiet) == len(for_popular) == 3


def test_written_post_is_returned_by_the_query_that_every_read_uses(
    alice_client: TestClient, session: Session
) -> None:
    session.expunge_all()

    with recorded_selects() as creating:
        post = alice_client.post("/posts", json={"content": "First"}).json()
    with recorded_selects() as editing:
        edited = alice_client.patch(f"/posts/{post['id']}", json={"content": "Edit"})

    assert edited.status_code == 200
    # What a write answers with is not what is left of the object it wrote:
    # the post is read again, whole, with its author and its interactions,
    # by the one statement that also decides whether it may be seen.
    for statements in (creating, editing):
        returned = " ".join(statements[-1].split())
        assert "FROM posts JOIN users ON users.id = posts.author_id" in returned
        assert "FROM likes WHERE likes.post_id = posts.id" in returned
        assert "FROM reposts WHERE reposts.post_id = posts.id" in returned
        assert "posts.deleted_at IS NULL" in returned
    # The same number of statements as before posts had interactions.
    assert (len(creating), len(editing)) == (3, 4)


def test_interactions_are_counted_by_the_statement_that_loads_the_posts(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, 3, alice_account)
    session.expunge_all()

    with recorded_selects() as statements:
        assert len(feed(bob_client)["items"]) == 3

    page = " ".join(statements[-1].split())
    assert "FROM posts JOIN users" in page
    assert "count(*)" in page
    assert "FROM likes WHERE likes.post_id = posts.id" in page
    assert "FROM reposts WHERE reposts.post_id = posts.id" in page
    # The reader's own are looked up by primary key, in that same statement.
    assert "EXISTS (SELECT * FROM likes WHERE likes.user_id =" in page
    assert "EXISTS (SELECT * FROM reposts WHERE reposts.user_id =" in page
    # And the order is still the chronological one, with nothing added to it.
    assert page.count("ORDER BY") == 1
    assert " ORDER BY posts.created_at DESC, posts.id DESC LIMIT " in page

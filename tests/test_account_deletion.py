"""POST /auth/delete-account: removing one's own account.

What the endpoint asks for and answers, what is removed with an account and
what is left, and that nothing which is left says whose it was. What happens
when a deletion and another request for the same account arrive together is
in ``test_account_deletion_concurrency``.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session, aliased

from app.core.config import settings
from app.core.security import PASSWORD_MAX_LENGTH, hash_password, hash_token
from app.db.base import Base
from app.db.session import engine
from app.main import app
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
from app.services import erasure, rate_limit
from app.services import users as users_service
from helpers import (
    PASSWORD,
    SENSITIVE_KEYS,
    add_post,
    add_story,
    add_user,
    follow,
    keys_in,
    log_in,
    plant_session_cookie,
    registration,
    session_token,
    set_cookie,
    stored_post,
    token_from,
    what_is_left_of,
)

NOT_AUTHENTICATED = {"detail": "Not authenticated."}
WRONG_PASSWORD = {"detail": "Current password is incorrect."}
TOO_MANY = {"detail": "Too many requests. Try again later."}
INVALID_CREDENTIALS = {"detail": "Invalid username/email or password."}
FOREIGN_ORIGIN = "https://evil.example"
# The address the test client's requests come from.
ADDRESS = "testclient"
MEDIA = "https://media.example.com"
HOUR = timedelta(hours=1)
# The accounts of the tests share one hash of the one password, to save the
# time hashing takes. An account whose hash is looked for in the database
# after it is gone needs one that nobody else has.
OWN_HASH = hash_password(PASSWORD)


def remove(client: TestClient, password: object = PASSWORD, **kwargs: object):
    return client.post("/auth/delete-account", json={"password": password}, **kwargs)


def count(session: Session, model: type, *conditions: object) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*conditions))


def row_counts(session: Session) -> dict[str, int]:
    """How many rows every table of the application holds."""
    return {
        name: session.scalar(select(func.count()).select_from(table))
        for name, table in sorted(Base.metadata.tables.items())
    }


def user_row(session: Session, user_id: object) -> dict | None:
    """The account's row as stored, or None if there is none."""
    found = session.execute(
        select(User.__table__).where(User.id == user_id)
    ).one_or_none()
    return dict(found._mapping) if found is not None else None


def tombstones(session: Session) -> list[dict]:
    """Every post that has no author, as stored."""
    return [
        dict(row._mapping)
        for row in session.execute(
            select(Post.__table__).where(Post.author_id.is_(None))
        )
    ]


def replies_to(session: Session, post_id: object) -> int:
    return count(session, Post, Post.parent_post_id == post_id)


def counter(session: Session, scope: str, subject: str) -> int | None:
    """What one counter of the rate limiter holds, if it exists."""
    return session.scalar(
        select(RateLimit.count).where(
            RateLimit.key_hash == rate_limit.key_hash(scope, subject)
        )
    )


def counters_named_after(user_id: object, username: str, email: str) -> list[str]:
    """The key of every counter that is kept under one of the account's
    names, as far as requests from the test client's address go."""
    keys = [
        rate_limit.key_hash(scope, str(user_id))
        for scope in ("password-confirmation:account", "data-export:account")
    ]
    for identifier in (username, email):
        keys.append(rate_limit.key_hash("login:identifier", identifier))
        keys.append(
            rate_limit.key_hash(
                "login:identifier+address", f"{identifier}\x00{ADDRESS}"
            )
        )
    return keys


# --- the acceptance criterion --------------------------------------------
#
# After an account is deleted, its id is in no column of any table, and
# neither is anything that names it or that it wrote. What may be left is the
# place of a post in a thread: a row with no author and no text, and only
# for as long as something answers it.


@dataclass(frozen=True)
class Traces:
    """What would give an account away if it were left in the database."""

    user_id: uuid.UUID
    # Its names, its secrets, and everything it wrote.
    texts: list[str]
    # The rate-limit counters kept under its names.
    counters: list[str]


def traces_of(session: Session, user: User) -> Traces:
    """Everything of the account that the database holds at this moment."""
    owned = lambda model: model.user_id == user.id  # noqa: E731
    stories = session.execute(
        select(Story.media_url, Story.caption).where(Story.author_id == user.id)
    ).all()
    texts = [
        user.username,
        user.email,
        user.display_name,
        user.password_hash,
        user.bio,
        user.avatar_url,
        *session.scalars(select(Post.content).where(Post.author_id == user.id)),
        *(media_url for media_url, _ in stories),
        *(caption for _, caption in stories),
        *session.scalars(select(UserSession.token_hash).where(owned(UserSession))),
        *session.scalars(
            select(EmailVerificationToken.token_hash).where(
                owned(EmailVerificationToken)
            )
        ),
        *session.scalars(
            select(PasswordResetToken.token_hash).where(owned(PasswordResetToken))
        ),
    ]
    return Traces(
        user_id=user.id,
        texts=[text for text in texts if text],
        counters=counters_named_after(user.id, user.username, user.email),
    )


def assert_nothing_left(session: Session, traces: Traces) -> None:
    """The acceptance criterion, in full."""
    # The id in no column that holds ids, and none of the texts in any
    # column that holds text, in any table.
    assert what_is_left_of(session, user_id=traces.user_id, texts=traces.texts) == []
    assert count(session, RateLimit, RateLimit.key_hash.in_(traces.counters)) == 0
    # A post without an author is a place in a thread and nothing else, and
    # it is there for a reply: one that nothing answers has no reason to be.
    for tombstone in tombstones(session):
        assert tombstone["content"] is None
        assert tombstone["deleted_at"] is not None
        assert replies_to(session, tombstone["id"]) > 0, "a tombstone nobody needs"


def others(session: Session, user_id: object) -> dict[str, list]:
    """Everything in the database that is not the account's, row by row.

    Posts are told by their author, and a like, a repost or a view by who
    made it and what it is of. Tombstones belong to nobody and are left out.
    """
    post, story = aliased(Post), aliased(Story)
    of_others_post = select(post.id).where(post.author_id != user_id)
    of_others_story = select(story.id).where(story.author_id != user_id)

    def rows(model: type, *conditions: object) -> list:
        table = model.__table__
        return sorted(
            tuple(row)
            for row in session.execute(select(table).where(*conditions))
        )

    return {
        "users": rows(User, User.id != user_id),
        "sessions": rows(UserSession, UserSession.user_id != user_id),
        "posts": rows(Post, Post.author_id != user_id),
        "stories": rows(Story, Story.author_id != user_id),
        "follows": rows(
            Follow, Follow.follower_id != user_id, Follow.following_id != user_id
        ),
        "likes": rows(
            Like, Like.user_id != user_id, Like.post_id.in_(of_others_post)
        ),
        "reposts": rows(
            Repost, Repost.user_id != user_id, Repost.post_id.in_(of_others_post)
        ),
        "story_views": rows(
            StoryView,
            StoryView.viewer_id != user_id,
            StoryView.story_id.in_(of_others_story),
        ),
    }


@dataclass
class World:
    """Alice, who is about to leave, and two people who stay."""

    session: Session
    alice: User
    bob: User
    carol: User

    def post(self, author: User, text: str, parent: Post | None = None) -> Post:
        return add_post(self.session, author, text, parent=parent)

    def add(self, *rows: object) -> None:
        self.session.add_all(rows)
        self.session.flush()


@pytest.fixture
def world(session: Session, alice_account: User, bob_account: User) -> World:
    alice_account.display_name = "Alice Liddell"
    alice_account.bio = "Curiouser and curiouser."
    alice_account.avatar_url = f"{MEDIA}/avatars/liddell.png"
    alice_account.password_hash = OWN_HASH
    session.flush()
    return World(session, alice_account, bob_account, add_user(session, "carol"))


# What an account can consist of. Each of these adds one kind of thing to
# Alice's account, together with what others have to do with it.


def no_content(world: World) -> None:
    pass


def posts(world: World) -> None:
    for number in range(5):
        world.post(world.alice, f"Alice wrote {number}")


def posts_with_replies(world: World) -> None:
    for number in range(3):
        asked = world.post(world.alice, f"Alice asked {number}")
        world.post(world.bob, f"Bob answered {number}", asked)
        world.post(world.carol, f"Carol answered {number}", asked)


def replies_in_the_threads_of_others(world: World) -> None:
    started = world.post(world.bob, "Bob started")
    # One that nothing answers, and one that the thread goes on from.
    world.post(world.alice, "Alice had the last word", started)
    between = world.post(world.alice, "Alice said something in between", started)
    world.post(world.bob, "Bob went on", between)


def deeply_nested_threads(world: World) -> None:
    # The two take turns, forty deep, and Alice has the last word.
    parent = None
    for depth in range(41):
        author = world.alice if depth % 2 == 0 else world.bob
        parent = world.post(author, f"{author.username} at depth {depth}", parent)
    # A thread that is Alice's alone, from top to bottom.
    parent = None
    for depth in range(30):
        parent = world.post(world.alice, f"Alice alone at depth {depth}", parent)
    # And one under what somebody else deleted, which only her replies keep.
    parent = add_post(world.session, world.bob, deleted=True)
    for depth in range(5):
        parent = world.post(world.alice, f"Alice under a deleted post {depth}", parent)


def stories_and_views(world: World) -> None:
    now = datetime.now(timezone.utc)
    shown = add_story(
        world.session,
        world.alice,
        "Alice's caption",
        media_url=f"{MEDIA}/stories/liddell-1.jpg",
    )
    expired = add_story(
        world.session,
        world.alice,
        "Alice's caption of yesterday",
        created_at=now - 30 * HOUR,
        media_url=f"{MEDIA}/stories/liddell-0.jpg",
    )
    of_bob = add_story(world.session, world.bob, "Bob's caption")
    world.add(
        StoryView(story_id=shown.id, viewer_id=world.bob.id),
        StoryView(story_id=shown.id, viewer_id=world.carol.id),
        StoryView(story_id=expired.id, viewer_id=world.bob.id),
        StoryView(story_id=of_bob.id, viewer_id=world.alice.id),
        StoryView(story_id=of_bob.id, viewer_id=world.carol.id),
    )


def likes_and_reposts(world: World) -> None:
    mine = world.post(world.alice, "Alice's much liked post")
    answered = world.post(world.alice, "Alice's liked and answered post")
    world.post(world.bob, "Bob's reply to a liked post", answered)
    of_bob = world.post(world.bob, "Bob's post")
    world.add(
        *(Like(user_id=world.alice.id, post_id=post.id) for post in (mine, of_bob)),
        *(Repost(user_id=world.alice.id, post_id=post.id) for post in (mine, of_bob)),
        *(Like(user_id=world.bob.id, post_id=post.id) for post in (mine, answered)),
        *(Repost(user_id=world.carol.id, post_id=post.id) for post in (mine, answered)),
        Like(user_id=world.carol.id, post_id=of_bob.id),
        Repost(user_id=world.carol.id, post_id=of_bob.id),
    )


def followers_and_following(world: World) -> None:
    for follower, following in (
        (world.alice, world.bob),
        (world.alice, world.carol),
        (world.bob, world.alice),
        (world.carol, world.alice),
        (world.bob, world.carol),
    ):
        follow(world.session, follower, following)


def several_sessions(world: World) -> None:
    now = datetime.now(timezone.utc)

    def a_session(user: User, **state: datetime) -> UserSession:
        values = {"created_at": now - 2 * HOUR, "expires_at": now + HOUR} | state
        return UserSession(
            user_id=user.id,
            token_hash=hash_token(auth_service.generate_token()),
            last_used_at=now - HOUR,
            **values,
        )

    world.add(
        a_session(world.alice),
        a_session(world.alice),
        a_session(world.alice, revoked_at=now - HOUR),
        a_session(world.alice, expires_at=now - HOUR),
        a_session(world.bob),
    )


def outstanding_links(world: World) -> None:
    now = datetime.now(timezone.utc)

    def a_link(model: type, user: User, **state: datetime) -> object:
        values = {"created_at": now - 2 * HOUR, "expires_at": now + HOUR} | state
        return model(
            user_id=user.id,
            token_hash=hash_token(auth_service.generate_token()),
            **values,
        )

    for model in (EmailVerificationToken, PasswordResetToken):
        world.add(
            a_link(model, world.alice),
            a_link(model, world.alice, used_at=now - HOUR),
            a_link(model, world.alice, expires_at=now - HOUR),
            a_link(model, world.bob),
        )


def counted_failures(world: World) -> None:
    now = datetime.now(timezone.utc)
    alice = world.alice
    keys = counters_named_after(alice.id, alice.username, alice.email)
    # Logging in has left some of them behind already, standing at none.
    world.session.execute(delete(RateLimit).where(RateLimit.key_hash.in_(keys)))
    world.add(*(RateLimit(key_hash=key, window_start=now, count=3) for key in keys))


def everything(world: World) -> None:
    for kind in KINDS[:-1]:
        kind(world)


KINDS = [
    no_content,
    posts,
    posts_with_replies,
    replies_in_the_threads_of_others,
    deeply_nested_threads,
    stories_and_views,
    likes_and_reposts,
    followers_and_following,
    several_sessions,
    outstanding_links,
    counted_failures,
    everything,
]


@pytest.mark.parametrize("consisting_of", KINDS, ids=lambda kind: kind.__name__)
def test_nothing_of_a_deleted_account_is_left_anywhere(
    alice_client: TestClient, world: World, consisting_of: Callable[[World], None]
) -> None:
    session = world.session
    consisting_of(world)
    traces = traces_of(session, world.alice)
    # The scan below looks for what was really there.
    assert user_row(session, traces.user_id) is not None
    assert what_is_left_of(session, user_id=traces.user_id, texts=traces.texts)

    response = remove(alice_client)

    assert response.status_code == 204
    assert_nothing_left(session, traces)


@pytest.mark.parametrize("consisting_of", KINDS, ids=lambda kind: kind.__name__)
def test_nothing_of_anybody_else_is_touched(
    alice_client: TestClient, world: World, consisting_of: Callable[[World], None]
) -> None:
    session = world.session
    consisting_of(world)
    alice_id = world.alice.id
    before = others(session, alice_id)
    assert before["users"]

    assert remove(alice_client).status_code == 204

    # Every row of theirs as it was, to the column: their accounts and
    # sessions, their posts with the posts they answer, their stories, and
    # what they follow, like, repost and have viewed among themselves.
    assert others(session, alice_id) == before


# The check is only worth something if it looks everywhere and fails when it
# should. These two are about the check itself.


def test_scan_looks_into_every_column_that_can_refer_to_an_account(
    alice_client: TestClient, world: World
) -> None:
    session = world.session
    everything(world)
    traces = traces_of(session, world.alice)

    found = what_is_left_of(session, user_id=traces.user_id, texts=["Alice asked 1"])

    # Every column of the schema that refers to an account, whichever there
    # are, and the account's own key. A column added later is searched as
    # well, and has to be given something here for this to keep passing.
    referring = {
        f"{table.name}.{column.name} (the id)"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if any(key.column.table.name == "users" for key in column.foreign_keys)
    }
    assert len(referring) == 10
    assert set(found) == referring | {
        "users.id (the id)",
        "posts.content ('Alice asked 1')",
    }


def test_check_fails_for_a_deletion_that_leaves_more_than_it_has_to(
    alice_client: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = world.session
    everything(world)
    traces = traces_of(session, world.alice)
    # A deletion that empties the account's posts but does not clear away
    # the ones that nothing answers.
    monkeypatch.setattr(erasure, "prune_tombstones", lambda db, post_ids: 0)

    assert remove(alice_client).status_code == 204

    with pytest.raises(AssertionError, match="a tombstone nobody needs"):
        assert_nothing_left(session, traces)


# --- who may ask ---------------------------------------------------------


def test_unauthenticated_request_is_refused(
    client: TestClient, session: Session, alice_account: User
) -> None:
    response = remove(client)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert user_row(session, alice_account.id) is not None


def test_unauthenticated_request_is_refused_before_it_is_validated(
    client: TestClient,
) -> None:
    assert client.post("/auth/delete-account", json={}).status_code == 401
    assert client.post("/auth/delete-account").status_code == 401


def test_unknown_session_cookie_is_refused(
    client: TestClient, session: Session, alice_account: User
) -> None:
    plant_session_cookie(client, auth_service.generate_token())

    assert remove(client).status_code == 401
    assert user_row(session, alice_account.id) is not None


@pytest.mark.parametrize("ended", ["revoked", "expired"])
def test_session_that_has_ended_cannot_delete_the_account(
    alice_client: TestClient, session: Session, alice_account: User, ended: str
) -> None:
    now = datetime.now(timezone.utc)
    own = session.scalar(select(UserSession))
    if ended == "revoked":
        own.revoked_at = now
    else:
        own.created_at, own.expires_at = now - 2 * HOUR, now - HOUR
    session.flush()

    response = remove(alice_client)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert user_row(session, alice_account.id) is not None


def test_deactivated_account_is_refused_like_no_session_at_all(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    alice_account.is_active = False
    session.flush()

    response = remove(alice_client)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert user_row(session, alice_account.id) is not None


def test_account_that_is_not_verified_can_be_deleted_by_its_session(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    # Not reachable by logging in. Should a session belong to an unverified
    # account all the same, leaving is not among the things it is kept from.
    alice_id = alice_account.id
    alice_account.email_verified_at = None
    session.flush()

    assert remove(alice_client).status_code == 204
    assert user_row(session, alice_id) is None


def test_cross_site_request_is_refused_and_removes_nothing(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    response = remove(alice_client, headers={"Origin": FOREIGN_ORIGIN})

    assert response.status_code == 403
    assert response.json() == {"detail": "Cross-origin request rejected."}
    assert user_row(session, alice_account.id) is not None
    assert alice_client.get("/auth/me").status_code == 200


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
def test_account_can_only_be_deleted_with_post(
    alice_client: TestClient, session: Session, alice_account: User, method: str
) -> None:
    response = alice_client.request(
        method, "/auth/delete-account", json={"password": PASSWORD}
    )

    assert response.status_code == 405
    assert user_row(session, alice_account.id) is not None


def test_whose_account_it_is_cannot_be_said_by_the_request(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    alice_id = alice_account.id
    bob = add_user(session, "bob")

    # Whatever a body or a query says about who is meant, it is not read.
    response = alice_client.post(
        f"/auth/delete-account?user_id={bob.id}&username=bob",
        json={
            "password": PASSWORD,
            "user_id": str(bob.id),
            "username": "bob",
            "email": "bob@example.com",
        },
    )

    assert response.status_code == 204
    assert user_row(session, bob.id) is not None
    assert user_row(session, alice_id) is None


def test_endpoint_takes_a_password_and_nothing_else() -> None:
    operation = app.openapi()["paths"]["/auth/delete-account"]
    body = app.openapi()["components"]["schemas"]["DeleteAccountRequest"]

    assert list(operation) == ["post"]
    assert "parameters" not in operation["post"]
    assert list(body["properties"]) == ["password"]
    assert body["required"] == ["password"]
    assert body["properties"]["password"]["writeOnly"] is True
    assert "204" in operation["post"]["responses"]


# --- the password --------------------------------------------------------


def test_wrong_password_is_refused(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    before = user_row(session, alice_account.id)

    response = remove(alice_client, "not my password at all")

    assert response.status_code == 400
    assert response.json() == WRONG_PASSWORD
    assert user_row(session, alice_account.id) == before


def test_wrong_password_ends_no_session_and_keeps_the_cookie(
    make_client, alice_client: TestClient, session: Session, alice_account: User
) -> None:
    elsewhere = make_client()
    assert log_in(elsewhere).status_code == 200
    token = session_token(alice_client)

    response = remove(alice_client, "not my password at all")

    assert "set-cookie" not in response.headers
    assert session_token(alice_client) == token
    assert alice_client.get("/auth/me").status_code == 200
    assert elsewhere.get("/auth/me").status_code == 200
    assert count(session, UserSession, UserSession.revoked_at.is_(None)) == 2


def test_wrong_password_removes_nothing_of_the_account(
    alice_client: TestClient, world: World
) -> None:
    everything(world)
    before = row_counts(world.session)

    assert remove(alice_client, "not my password at all").status_code == 400

    # Apart from the attempt, which is counted.
    after = row_counts(world.session)
    assert after.pop("rate_limits") == before.pop("rate_limits")
    assert after == before


def test_password_of_another_account_is_a_wrong_password(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    bob = add_user(session, "bob")
    bob.password_hash = hash_password("what bob logs in with")
    session.flush()

    response = remove(alice_client, "what bob logs in with")

    assert response.status_code == 400
    assert user_row(session, alice_account.id) is not None
    assert user_row(session, bob.id) is not None


@pytest.mark.parametrize(
    "almost", [PASSWORD.upper(), PASSWORD + " ", " " + PASSWORD, PASSWORD[:-1]]
)
def test_password_is_taken_exactly_as_typed(
    alice_client: TestClient, session: Session, alice_account: User, almost: str
) -> None:
    assert remove(alice_client, almost).status_code == 400
    assert user_row(session, alice_account.id) is not None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"password": ""},
        {"password": None},
        {"password": 12345678},
        {"password": ["correct horse battery staple"]},
        {"password": "x" * (PASSWORD_MAX_LENGTH + 1)},
        {"current_password": PASSWORD},
        [],
        "correct horse battery staple",
    ],
    ids=[
        "empty",
        "blank",
        "null",
        "number",
        "list",
        "too long",
        "wrong name",
        "not an object",
        "bare string",
    ],
)
def test_request_without_a_usable_password_is_invalid(
    alice_client: TestClient, session: Session, alice_account: User, body: object
) -> None:
    response = alice_client.post("/auth/delete-account", json=body)

    assert response.status_code == 422
    assert user_row(session, alice_account.id) is not None
    assert alice_client.get("/auth/me").status_code == 200


def test_invalid_request_is_not_counted_as_a_wrong_password(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    for _ in range(settings.password_confirmation_failures + 2):
        assert alice_client.post("/auth/delete-account", json={}).status_code == 422

    assert (
        counter(session, "password-confirmation:account", str(alice_account.id))
        is None
    )
    assert remove(alice_client).status_code == 204


def test_rejected_password_is_not_echoed(alice_client: TestClient) -> None:
    secret = "x" * (PASSWORD_MAX_LENGTH + 1)

    response = remove(alice_client, secret)

    assert response.status_code == 422
    assert "xxxx" not in response.text
    assert "input" not in keys_in(response.json())


def test_longest_password_there_can_be_is_checked_like_any_other(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    longest = "x" * PASSWORD_MAX_LENGTH
    assert remove(alice_client, longest).status_code == 400

    alice_account.password_hash = hash_password(longest)
    session.flush()

    assert remove(alice_client, longest).status_code == 204


# --- the answer ----------------------------------------------------------


def test_deleting_an_account_answers_with_no_content(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    alice_id = alice_account.id

    response = remove(alice_client)

    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"
    assert user_row(session, alice_id) is None


def test_session_cookie_is_cleared(alice_client: TestClient) -> None:
    response = remove(alice_client)

    cookie = set_cookie(response)
    assert cookie.key == settings.session_cookie
    assert cookie.key.startswith("__Host-")
    assert cookie.value == ""
    assert cookie["max-age"] == "0"
    assert cookie["path"] == "/"
    assert cookie["domain"] == ""
    assert cookie["httponly"] is True
    assert cookie["secure"] is True
    assert cookie["samesite"] == "lax"
    assert session_token(alice_client) is None


@pytest.mark.parametrize("password", [PASSWORD, "not my password at all", None])
def test_no_answer_carries_anything_of_the_account(
    alice_client: TestClient,
    session: Session,
    alice_account: User,
    password: str | None,
) -> None:
    token = session_token(alice_client)
    secrets = [PASSWORD, alice_account.password_hash, token, hash_token(token)]
    names = [alice_account.email, str(alice_account.id)]

    response = remove(alice_client, password)

    assert response.status_code in (204, 400, 422)
    if response.content:
        assert not keys_in(response.json()) & SENSITIVE_KEYS
    answer = response.text + repr(sorted(response.headers.items()))
    for value in secrets + names:
        assert value not in answer


def test_password_does_not_appear_in_logs(
    alice_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    token = session_token(alice_client)

    remove(alice_client, "not my password at all")
    remove(alice_client)

    assert "not my password at all" not in caplog.text
    assert PASSWORD not in caplog.text
    assert token not in caplog.text
    assert "alice@example.com" not in caplog.text


# --- what goes with the account ------------------------------------------


def test_account_with_nothing_but_itself_is_removed(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    assert remove(alice_client).status_code == 204

    # Not deactivated, not marked: no row. The one thing in the database is
    # the count of failed logins from the address, which stands at none and
    # is the address's, not the account's.
    assert row_counts(session) == dict.fromkeys(Base.metadata.tables, 0) | {
        "rate_limits": 1
    }
    assert counter(session, "login:address", ADDRESS) == 0


def test_every_session_of_the_account_is_removed(
    make_client, alice_client: TestClient, session: Session, alice_account: User
) -> None:
    alice_id = alice_account.id
    browsers = [alice_client, make_client(), make_client()]
    for browser in browsers[1:]:
        assert log_in(browser).status_code == 200
    tokens = [session_token(browser) for browser in browsers]
    assert count(session, UserSession, UserSession.user_id == alice_id) == 3

    assert remove(alice_client).status_code == 204

    # Removed, not revoked: there is no row left that says there was one.
    assert count(session, UserSession) == 0
    for token in tokens:
        replayed = make_client()
        plant_session_cookie(replayed, token)
        assert replayed.get("/auth/me").status_code == 401
        assert replayed.get("/auth/me").json() == NOT_AUTHENTICATED
    assert browsers[1].get("/auth/sessions").status_code == 401


def test_sessions_of_others_stay(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    assert remove(alice_client).status_code == 204

    assert bob_client.get("/auth/me").json()["username"] == "bob"
    assert len(bob_client.get("/auth/sessions").json()) == 1


def test_links_that_were_outstanding_are_removed_and_open_nothing(
    make_client,
    alice_client: TestClient,
    session: Session,
    alice_account: User,
    outbox,
) -> None:
    alice_id = alice_account.id
    elsewhere = make_client()
    assert (
        elsewhere.post(
            "/auth/forgot-password", json={"email": "alice@example.com"}
        ).status_code
        == 202
    )
    reset_token = token_from(outbox.password_reset[-1][1])
    verification_token = auth_service.generate_token()
    session.add(
        EmailVerificationToken(
            user_id=alice_id,
            token_hash=hash_token(verification_token),
            expires_at=datetime.now(timezone.utc) + HOUR,
        )
    )
    session.flush()

    assert remove(alice_client).status_code == 204

    assert count(session, PasswordResetToken) == 0
    assert count(session, EmailVerificationToken) == 0
    # The link in the mailbox is a link to nothing: it neither sets a
    # password nor brings anything back.
    reset = elsewhere.post(
        "/auth/reset-password",
        json={"token": reset_token, "new_password": "an entirely new passphrase"},
    )
    verified = elsewhere.post("/auth/verify-email", json={"token": verification_token})
    assert reset.status_code == 400
    assert reset.json() == {"detail": "Invalid or expired password reset token."}
    assert verified.status_code == 400
    assert verified.json() == {"detail": "Invalid or expired verification token."}
    assert count(session, User) == 0


def test_posts_that_nothing_answers_are_removed_with_what_was_on_them(
    alice_client: TestClient, world: World
) -> None:
    session = world.session
    post = world.post(world.alice, "Alice wrote this")
    world.add(
        Like(user_id=world.bob.id, post_id=post.id),
        Repost(user_id=world.carol.id, post_id=post.id),
    )
    post_id = post.id

    assert remove(alice_client).status_code == 204

    assert stored_post(session, post_id) is None
    assert count(session, Post) == count(session, Like) == count(session, Repost) == 0


def test_post_that_was_answered_leaves_its_place_in_the_thread_and_no_more(
    alice_client: TestClient, world: World
) -> None:
    session = world.session
    root = world.post(world.bob, "Bob started")
    answered = world.post(world.alice, "Alice said something in between", root)
    reply = world.post(world.bob, "Bob went on", answered)
    world.add(
        Like(user_id=world.bob.id, post_id=answered.id),
        Repost(user_id=world.carol.id, post_id=answered.id),
    )
    before = stored_post(session, answered.id)
    root_id, reply_id = root.id, reply.id

    assert remove(alice_client).status_code == 204

    left = stored_post(session, before["id"])
    # No author and no text: nothing in the row says who was here or what
    # they said. The likes and reposts it had are gone as well.
    assert left["author_id"] is None
    assert left["content"] is None
    assert left["deleted_at"] is not None
    assert count(session, Like) == count(session, Repost) == 0
    # What is kept is what the thread needs: which post this was, what it
    # answered, and when.
    for column in ("id", "parent_post_id", "created_at"):
        assert left[column] == before[column]
    assert left["parent_post_id"] == root_id
    assert set(left) == {
        "id",
        "author_id",
        "parent_post_id",
        "content",
        "created_at",
        "updated_at",
        "deleted_at",
    }
    assert stored_post(session, reply_id)["parent_post_id"] == before["id"]


def test_replies_of_others_stay_where_they_were(
    alice_client: TestClient, bob_client: TestClient, world: World
) -> None:
    asked = world.post(world.alice, "Alice asked")
    reply = world.post(world.bob, "Bob answered", asked)
    further = world.post(world.carol, "Carol answered Bob", reply)
    asked_id, reply_id, further_id = str(asked.id), str(reply.id), str(further.id)

    assert remove(alice_client).status_code == 204

    # The replies are read as before and still say what they answer.
    read = bob_client.get(f"/posts/{reply_id}")
    assert read.status_code == 200
    assert read.json()["content"] == "Bob answered"
    assert read.json()["parent_post_id"] == asked_id
    answers = bob_client.get(f"/posts/{reply_id}/replies").json()["items"]
    assert [answer["id"] for answer in answers] == [further_id]
    in_the_feed = bob_client.get("/feed").json()["items"]
    assert sorted(item["id"] for item in in_the_feed) == sorted([reply_id, further_id])
    # What they answer is, to everyone, not there.
    for path in (f"/posts/{asked_id}", f"/posts/{asked_id}/replies"):
        assert bob_client.get(path).status_code == 404
    for method, path in (
        ("POST", f"/posts/{asked_id}/like"),
        ("POST", f"/posts/{asked_id}/repost"),
        ("PATCH", f"/posts/{asked_id}"),
        ("DELETE", f"/posts/{asked_id}"),
    ):
        body = {"content": "Mine now"} if method == "PATCH" else None
        assert bob_client.request(method, path, json=body).status_code == 404
    answering = bob_client.post(
        "/posts", json={"content": "Too late", "parent_post_id": asked_id}
    )
    assert answering.status_code == 404


def test_thread_the_two_took_turns_in_keeps_exactly_what_the_other_wrote(
    alice_client: TestClient, world: World
) -> None:
    session = world.session
    parent, chain = None, []
    for depth in range(41):
        author = world.alice if depth % 2 == 0 else world.bob
        parent = world.post(author, f"{author.username} at depth {depth}", parent)
        chain.append(parent.id)

    assert remove(alice_client).status_code == 204

    stored = [stored_post(session, post_id) for post_id in chain]
    # Alice had the last word, and nothing answers it: that row is gone.
    assert stored[40] is None
    # Bob's twenty posts are all there, each still answering the one above.
    for depth in range(1, 40, 2):
        assert stored[depth]["content"] == f"bob at depth {depth}"
        assert stored[depth]["parent_post_id"] == chain[depth - 1]
    # And between them, twenty places where something was.
    for depth in range(0, 40, 2):
        assert (stored[depth]["author_id"], stored[depth]["content"]) == (None, None)
    assert count(session, Post) == 40


def test_thread_that_was_the_accounts_alone_is_removed_from_top_to_bottom(
    alice_client: TestClient, world: World
) -> None:
    parent = None
    for depth in range(60):
        parent = world.post(world.alice, f"Alice alone at depth {depth}", parent)

    assert remove(alice_client).status_code == 204

    assert count(world.session, Post) == 0


def test_tombstones_that_only_the_account_kept_are_removed_with_it(
    alice_client: TestClient, world: World
) -> None:
    session = world.session
    # Two posts that were deleted before, one above the other, and under
    # them nothing but what Alice wrote.
    top = add_post(session, world.bob, deleted=True)
    below = add_post(session, world.bob, parent=top, deleted=True)
    world.post(world.alice, "Alice under two deleted posts", below)
    # Another, which Carol answered as well: that one is still needed.
    shared = add_post(session, world.bob, deleted=True)
    world.post(world.alice, "Alice under a deleted post", shared)
    kept = world.post(world.carol, "Carol under the same one", shared)
    shared_id, kept_id = shared.id, kept.id

    assert remove(alice_client).status_code == 204

    assert [row["id"] for row in tombstones(session)] == [shared_id]
    assert stored_post(session, kept_id)["parent_post_id"] == shared_id
    assert count(session, Post) == 2


def test_stories_are_removed_with_the_record_of_who_viewed_them(
    alice_client: TestClient, bob_client: TestClient, world: World
) -> None:
    session = world.session
    follow(session, world.bob, world.alice)
    stories_and_views(world)
    assert count(session, Story) == 3 and count(session, StoryView) == 5

    assert remove(alice_client).status_code == 204

    # Hers are gone, the one that had expired too, and every view of them;
    # so is the view she made of Bob's.
    [left] = session.execute(select(Story.author_id, Story.caption)).all()
    assert tuple(left) == (world.bob.id, "Bob's caption")
    assert session.scalars(select(StoryView.viewer_id)).all() == [world.carol.id]
    shown = bob_client.get("/stories").json()["items"]
    assert [item["caption"] for item in shown] == ["Bob's caption"]


def test_likes_and_reposts_are_removed_in_both_directions(
    alice_client: TestClient, bob_client: TestClient, world: World
) -> None:
    session = world.session
    likes_and_reposts(world)
    of_bob = session.scalar(select(Post.id).where(Post.content == "Bob's post"))

    assert remove(alice_client).status_code == 204

    # What she liked and reposted no longer counts her, and what was liked
    # and reposted of hers is gone with the posts.
    read = bob_client.get(f"/posts/{of_bob}").json()
    assert (read["like_count"], read["repost_count"]) == (1, 1)
    assert session.execute(select(Like.user_id, Like.post_id)).all() == [
        (world.carol.id, of_bob)
    ]
    assert session.execute(select(Repost.user_id, Repost.post_id)).all() == [
        (world.carol.id, of_bob)
    ]


def test_follows_are_removed_in_both_directions(
    alice_client: TestClient, bob_client: TestClient, world: World
) -> None:
    session = world.session
    followers_and_following(world)
    assert bob_client.get("/users/me").json()["followers_count"] == 1
    assert bob_client.get("/users/me").json()["following_count"] == 2

    assert remove(alice_client).status_code == 204

    # Bob follows Carol, and that is all there is.
    assert session.execute(select(Follow.follower_id, Follow.following_id)).all() == [
        (world.bob.id, world.carol.id)
    ]
    profile = bob_client.get("/users/me").json()
    assert (profile["followers_count"], profile["following_count"]) == (0, 1)
    following = bob_client.get("/users/bob/following").json()["items"]
    assert [user["username"] for user in following] == ["carol"]
    assert bob_client.get("/users/bob/followers").json()["items"] == []


# --- afterwards ----------------------------------------------------------


def test_account_is_gone_for_everyone(
    client: TestClient, alice_client: TestClient, bob_client: TestClient, world: World
) -> None:
    world.post(world.alice, "Alice wrote this")

    assert remove(alice_client).status_code == 204

    for reader in (client, bob_client):
        for path in (
            "/users/alice",
            "/users/alice/posts",
            "/users/alice/followers",
            "/users/alice/following",
        ):
            response = reader.get(path)
            assert response.status_code == 404, path
            assert response.json() == {"detail": "User not found."}
    assert bob_client.get("/users/alice/follow-status").status_code == 404
    assert bob_client.post("/users/alice/follow").status_code == 404
    assert bob_client.get("/feed").json()["items"] == []


@pytest.mark.parametrize("identifier", ["alice", "alice@example.com"])
def test_deleted_account_cannot_be_logged_into(
    make_client, alice_client: TestClient, identifier: str
) -> None:
    assert remove(alice_client).status_code == 204

    refused = log_in(make_client(), identifier)
    unknown = log_in(make_client(), "nobody")

    # As for a name that was never there.
    assert refused.status_code == unknown.status_code == 401
    assert refused.json() == unknown.json() == INVALID_CREDENTIALS
    assert "set-cookie" not in refused.headers


def test_its_cookie_opens_nothing_any_more(
    make_client, alice_client: TestClient, world: World
) -> None:
    token = session_token(alice_client)
    of_bob = world.post(world.bob, "Bob's post").id
    assert remove(alice_client).status_code == 204
    stale = make_client()
    plant_session_cookie(stale, token)

    for method, path, body in (
        ("GET", "/auth/me", None),
        ("GET", "/users/me", None),
        ("GET", "/auth/sessions", None),
        ("GET", "/feed/following", None),
        ("GET", "/stories", None),
        ("PATCH", "/users/me", {"display_name": "Back again"}),
        ("POST", "/posts", {"content": "Back again"}),
        ("POST", f"/posts/{of_bob}/like", None),
        ("POST", "/users/bob/follow", None),
        ("POST", "/stories", {"media_url": f"{MEDIA}/stories/2.jpg"}),
        ("POST", "/auth/change-password", {
            "current_password": PASSWORD,
            "new_password": "an entirely new passphrase",
        }),
        ("POST", "/auth/sessions/revoke-others", None),
    ):
        response = stale.request(method, path, json=body)
        assert response.status_code == 401, (method, path)
        assert response.json() == NOT_AUTHENTICATED
    assert count(world.session, User, User.username == "alice") == 0
    assert count(world.session, Post) == 1


def test_deleting_again_is_refused_like_any_request_without_a_session(
    make_client, alice_client: TestClient, session: Session, alice_account: User
) -> None:
    token = session_token(alice_client)
    assert remove(alice_client).status_code == 204

    # The browser has dropped the cookie...
    without = remove(alice_client)
    # ...and a copy of it is worth nothing.
    stale = make_client()
    plant_session_cookie(stale, token)
    with_a_copy = remove(stale)

    for response in (without, with_a_copy):
        assert response.status_code == 401
        assert response.json() == NOT_AUTHENTICATED
    assert count(session, User) == 0


def test_deleting_again_is_not_counted_against_anything(
    make_client, alice_client: TestClient, session: Session, alice_account: User
) -> None:
    token = session_token(alice_client)
    assert remove(alice_client).status_code == 204
    stale = make_client()
    plant_session_cookie(stale, token)

    for _ in range(settings.password_confirmation_failures + 2):
        assert remove(stale).status_code == 401

    assert session.scalars(select(RateLimit.key_hash)).all() == [
        rate_limit.key_hash("login:address", ADDRESS)
    ]


# --- the names are free again --------------------------------------------


def sign_up(client: TestClient, outbox, **overrides: object) -> dict:
    """Register, follow the emailed link and log in. The account, as told."""
    created = client.post("/auth/register", json=registration(**overrides))
    assert created.status_code == 201, created.text
    token = token_from(outbox.verification[-1][1])
    assert client.post("/auth/verify-email", json={"token": token}).status_code == 200
    account = created.json()
    assert log_in(client, account["username"]).status_code == 200
    return account


@pytest.mark.parametrize(
    "taking",
    [
        {"email": "somebody.else@example.com"},
        {"username": "somebody_else"},
        {},
    ],
    ids=["the username", "the email address", "both"],
)
def test_username_and_email_can_be_registered_again_at_once(
    make_client,
    alice_client: TestClient,
    session: Session,
    alice_account: User,
    taking: dict,
) -> None:
    alice_id = alice_account.id
    newcomer = make_client()
    refused = newcomer.post("/auth/register", json=registration(**taking))
    assert refused.status_code == 409

    assert remove(alice_client).status_code == 204
    created = newcomer.post("/auth/register", json=registration(**taking))

    assert created.status_code == 201
    assert created.json()["id"] != str(alice_id)
    # No trace of having been somebody's before: nothing was kept that could
    # have reserved the name or the address.
    assert count(session, User) == 1
    assert user_row(session, alice_id) is None


def test_whoever_takes_the_name_next_inherits_nothing(
    make_client, alice_client: TestClient, bob_client: TestClient, world: World, outbox
) -> None:
    session = world.session
    everything(world)
    alice_id = world.alice.id
    old_token = session_token(alice_client)
    assert remove(alice_client).status_code == 204
    tombstone_ids = sorted(str(row["id"]) for row in tombstones(session))
    assert tombstone_ids

    newcomer = make_client()
    account = sign_up(newcomer, outbox, display_name="Another Alice")

    # A new account with the old name and the old address, and nothing else
    # of the old one.
    assert account["id"] != str(alice_id)
    profile = newcomer.get("/users/me").json()
    assert profile["display_name"] == "Another Alice"
    assert profile["bio"] is None and profile["avatar_url"] is None
    assert (profile["followers_count"], profile["following_count"]) == (0, 0)
    assert newcomer.get("/users/alice/posts").json()["items"] == []
    assert newcomer.get("/feed/following").json()["items"] == []
    assert len(newcomer.get("/auth/sessions").json()) == 1
    # Who followed the old account does not follow this one, and what stands
    # where the old one's posts stood is not this one's to see or to change.
    assert bob_client.get("/users/alice/follow-status").json() == {"following": False}
    for post_id in tombstone_ids:
        assert newcomer.get(f"/posts/{post_id}").status_code == 404
        assert newcomer.delete(f"/posts/{post_id}").status_code == 404
    # The old cookie is not the new account's either.
    stale = make_client()
    plant_session_cookie(stale, old_token)
    assert stale.get("/auth/me").status_code == 401


def test_failed_logins_counted_under_the_name_do_not_pass_to_the_next_owner(
    make_client, alice_client: TestClient, session: Session, alice_account: User, outbox
) -> None:
    guesser = make_client()
    names = ("alice", "alice@example.com")

    def counted(identifier: str) -> tuple[int | None, int | None]:
        """By the name from anywhere, and by the name from this address."""
        pair = f"{identifier}\x00{ADDRESS}"
        return (
            counter(session, "login:identifier", identifier),
            counter(session, "login:identifier+address", pair),
        )

    for identifier in names:
        for _ in range(3):
            assert log_in(guesser, identifier, "a wrong guess").status_code == 401
        assert counted(identifier) == (3, 3)

    assert remove(alice_client).status_code == 204

    for identifier in names:
        assert counted(identifier) == (None, None)
    # The next owner of the name starts with a clean slate: one wrong
    # password is one, not the fourth.
    sign_up(make_client(), outbox)
    assert log_in(guesser, "alice", "a wrong guess").status_code == 401
    assert counter(session, "login:identifier", "alice") == 1


def test_guesses_made_from_an_address_stay_counted_for_the_address(
    make_client, alice_client: TestClient, session: Session, alice_account: User
) -> None:
    # What an address has used up of its guesses is the address's, whatever
    # becomes of the accounts it guessed at. Deleting an account of one's
    # own does not buy more guesses at other people's.
    add_user(session, "bob")
    guesser = make_client()
    for identifier in ("alice", "bob", "bob"):
        assert log_in(guesser, identifier, "a wrong guess").status_code == 401
    assert counter(session, "login:address", ADDRESS) == 3

    assert remove(alice_client).status_code == 204

    assert counter(session, "login:address", ADDRESS) == 3
    assert counter(session, "login:identifier", "bob") == 2
    assert counter(session, "login:identifier+address", f"bob\x00{ADDRESS}") == 2


# --- guessing the password -----------------------------------------------


def confirmations_counted(session: Session, user_id: object) -> int | None:
    return counter(session, "password-confirmation:account", str(user_id))


def test_guessing_the_password_is_limited(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    allowed = settings.password_confirmation_failures

    guesses = [
        remove(alice_client, f"guess number {number}") for number in range(allowed)
    ]
    refused = remove(alice_client)

    assert [response.status_code for response in guesses] == [400] * allowed
    # Also with the right password, like a login.
    assert refused.status_code == 429
    assert refused.json() == TOO_MANY
    assert refused.headers["retry-after"].isdigit()
    assert user_row(session, alice_account.id) is not None
    assert alice_client.get("/auth/me").status_code == 200


def test_refusal_for_the_limit_is_decided_before_the_password_is_looked_at(
    alice_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(settings.password_confirmation_failures):
        remove(alice_client, f"guess number {number}")
    checked = []
    monkeypatch.setattr(
        auth_service, "verify_password", lambda *args: checked.append(args) or True
    )

    assert remove(alice_client).status_code == 429
    assert checked == []


@pytest.mark.parametrize("first", ["change", "deletion"])
def test_limit_is_one_for_everything_that_asks_for_the_password(
    alice_client: TestClient, session: Session, alice_account: User, first: str
) -> None:
    def change(password: str):
        return alice_client.post(
            "/auth/change-password",
            json={
                "current_password": password,
                "new_password": "an entirely new passphrase",
            },
        )

    guess = {"change": change, "deletion": lambda p: remove(alice_client, p)}
    second = "deletion" if first == "change" else "change"
    allowed = settings.password_confirmation_failures

    # A session that guesses has that many guesses, not that many for each
    # thing that can be asked for.
    for number in range(allowed - 2):
        assert guess[first](f"guess number {number}").status_code == 400
    for number in range(2):
        assert guess[second](f"another guess {number}").status_code == 400

    assert confirmations_counted(session, alice_account.id) == allowed
    assert guess["deletion"](PASSWORD).status_code == 429
    assert guess["change"](PASSWORD).status_code == 429
    assert user_row(session, alice_account.id) is not None


def test_limit_follows_the_account_to_every_session(
    make_client, alice_client: TestClient, session: Session, alice_account: User
) -> None:
    elsewhere = make_client(client=("203.0.113.9", 50000))
    assert log_in(elsewhere).status_code == 200
    for number in range(settings.password_confirmation_failures):
        assert remove(alice_client, f"guess number {number}").status_code == 400

    assert remove(elsewhere).status_code == 429
    assert user_row(session, alice_account.id) is not None


def test_limit_is_each_accounts_own(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    bob_account: User,
) -> None:
    bob_id = bob_account.id
    for number in range(settings.password_confirmation_failures):
        assert remove(alice_client, f"guess number {number}").status_code == 400

    assert remove(bob_client).status_code == 204
    assert user_row(session, bob_id) is None
    assert count(session, User) == 1


def test_account_can_be_deleted_when_the_window_is_over(
    alice_client: TestClient,
    session: Session,
    alice_account: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice_id = alice_account.id
    for number in range(settings.password_confirmation_failures):
        remove(alice_client, f"guess number {number}")
    assert remove(alice_client).status_code == 429

    later = datetime.now(timezone.utc) + settings.rate_limit_window
    monkeypatch.setattr(rate_limit, "_now", lambda: later)

    assert remove(alice_client).status_code == 204
    assert user_row(session, alice_id) is None


def test_deletion_after_some_wrong_guesses_leaves_no_counter(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    alice_id = alice_account.id
    for number in range(settings.password_confirmation_failures - 1):
        assert remove(alice_client, f"guess number {number}").status_code == 400
    assert confirmations_counted(session, alice_id) is not None

    assert remove(alice_client).status_code == 204

    # A counter under the id of an account that is gone would be the last
    # thing in the database that was about it.
    assert confirmations_counted(session, alice_id) is None
    assert session.scalars(select(RateLimit.key_hash)).all() == [
        rate_limit.key_hash("login:address", ADDRESS)
    ]


# --- how it is done ------------------------------------------------------


class Statements(list):
    """The statements sent to the database, each on one line."""

    def where(self, *fragments: str) -> list[int]:
        """The positions of the statements that hold all of ``fragments``."""
        return [
            position
            for position, statement in enumerate(self)
            if all(fragment in statement for fragment in fragments)
        ]

    def first(self, *fragments: str) -> int:
        found = self.where(*fragments)
        assert found, f"no statement with {fragments}"
        return found[0]


@pytest.fixture
def statements() -> Statements:
    """Everything sent to the database from here on, savepoints aside."""
    recorded = Statements()

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        if "SAVEPOINT" not in statement:
            recorded.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", record)
    yield recorded
    event.remove(engine, "before_cursor_execute", record)


def test_everything_is_locked_and_removed_in_the_one_order(
    alice_client: TestClient, world: World, statements: Statements
) -> None:
    everything(world)
    statements.clear()

    assert remove(alice_client).status_code == 204

    in_order = [
        # The attempt is counted first, in a transaction of its own.
        statements.first("INSERT INTO rate_limits"),
        # Then the account is locked, and by the lock that keeps out
        # everything, also what would only refer to the account.
        statements.first("FROM users", "FOR UPDATE"),
        # Then the erasure lock, and only then any post.
        statements.first("pg_advisory_xact_lock"),
        statements.first("FROM posts", "ORDER BY posts.id FOR UPDATE"),
        statements.first("DELETE FROM likes"),
        statements.first("DELETE FROM reposts"),
        statements.first("UPDATE posts SET"),
        statements.first("DELETE FROM posts"),
        statements.first("DELETE FROM stories"),
        # The account's own row goes last, when nothing refers to it that
        # the database would not remove with it.
        statements.first("DELETE FROM users"),
        # And once that is committed, what was counted under its names.
        statements.first("DELETE FROM rate_limits"),
    ]
    assert in_order == sorted(in_order)
    assert len(statements.where("DELETE FROM users")) == 1
    account_lock = statements[statements.first("FROM users", "FOR UPDATE")]
    assert account_lock.endswith("FOR UPDATE")
    assert "NO KEY" not in account_lock


def test_nothing_is_locked_until_the_password_is_known_to_be_right(
    alice_client: TestClient, world: World, statements: Statements
) -> None:
    everything(world)
    statements.clear()

    assert remove(alice_client, "not my password at all").status_code == 400

    # Checking a password takes a while, on purpose. No lock is held for
    # it, and none is taken for a password that was wrong.
    assert statements.where("FOR UPDATE") == []
    assert statements.where("pg_advisory_xact_lock") == []
    assert not [s for s in statements if s.startswith(("DELETE", "UPDATE posts"))]


def test_deletion_takes_the_same_statements_however_much_there_is_to_delete(
    make_client, session: Session, statements: Statements
) -> None:
    def deletion_of(username: str, size: int) -> list[str]:
        user = add_user(session, username)
        reader = add_user(session, f"reader_of_{username}")
        follow(session, reader, user)
        for number in range(size):
            post = add_post(session, user, f"{username} wrote {number}")
            add_post(session, reader, f"an answer to number {number}", parent=post)
            add_post(session, user, f"{username} went on {number}", parent=post)
            session.add(Like(user_id=reader.id, post_id=post.id))
            add_story(session, user)
        session.flush()
        client = make_client()
        assert log_in(client, username).status_code == 200
        statements.clear()
        assert remove(client).status_code == 204
        return list(statements)

    small, large = deletion_of("small", 2), deletion_of("large", 60)

    # Sets of rows, not row by row: a larger account is more work for the
    # database but not one statement more.
    assert small == large
    assert len(small) < 25


def test_deletion_is_all_or_nothing(
    alice_client: TestClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = world.session
    everything(world)
    traces = traces_of(session, world.alice)
    before = row_counts(session)
    hers_before = count(session, Post, Post.author_id == traces.user_id)
    tombstones_before = tombstones(session)
    remove_rows = erasure.delete

    def fail_at_the_account(model: type) -> object:
        if model is User:
            raise RuntimeError("the database went away")
        return remove_rows(model)

    # Her posts, likes and stories have been erased and the account's own
    # row is about to go, when it fails.
    monkeypatch.setattr(erasure, "delete", fail_at_the_account)
    with pytest.raises(RuntimeError):
        remove(alice_client)

    # Then nothing was erased: not a post, not a like, not a session.
    assert row_counts(session) == before
    assert count(session, Post, Post.author_id == traces.user_id) == hers_before
    assert tombstones(session) == tombstones_before
    assert alice_client.get("/auth/me").status_code == 200
    # And it can be asked for again.
    monkeypatch.setattr(erasure, "delete", remove_rows)
    assert remove(alice_client).status_code == 204
    assert_nothing_left(session, traces)


def test_erasing_an_account_commits_nothing_by_itself(world: World) -> None:
    session = world.session
    everything(world)
    session.commit()
    before = row_counts(session)
    alice_id = world.alice.id

    erasure.erase_account(session, alice_id, datetime.now(timezone.utc))
    assert user_row(session, alice_id) is None
    session.rollback()

    # Whoever calls it decides, and here that was "no".
    assert row_counts(session) == before
    assert user_row(session, alice_id) is not None


def test_erasing_an_account_that_is_not_there_does_nothing(world: World) -> None:
    session = world.session
    everything(world)
    before = row_counts(session)

    erasure.erase_account(session, uuid.uuid4(), datetime.now(timezone.utc))

    assert row_counts(session) == before


def test_database_still_refuses_to_drop_an_account_that_has_posts(
    world: World,
) -> None:
    # The foreign key from posts is not one that removes or detaches them.
    # An account can only go once its posts were erased by the rule for
    # posts; whatever forgets that is stopped, not waved through.
    from sqlalchemy.exc import IntegrityError

    session = world.session
    world.post(world.alice, "Alice wrote this")
    session.commit()

    with pytest.raises(IntegrityError):
        session.execute(delete(User).where(User.id == world.alice.id))
    session.rollback()

    assert count(session, Post) == 1


# --- a request whose account goes while it is under way ------------------
#
# A request is authenticated when it arrives. Its account may be deleted by
# another request before it gets to do what it came for. Here that is made
# to happen at the worst moment: right after the session was found good.

WRITES = {
    "post": ("POST", "/posts", {"content": "One more thing"}),
    "reply": ("POST", "/posts", {"content": "A reply", "parent_post_id": "{post}"}),
    "like": ("POST", "/posts/{post}/like", None),
    "unlike": ("DELETE", "/posts/{post}/like", None),
    "repost": ("POST", "/posts/{post}/repost", None),
    "unrepost": ("DELETE", "/posts/{post}/repost", None),
    "follow": ("POST", "/users/bob/follow", None),
    "unfollow": ("DELETE", "/users/bob/follow", None),
    "story": ("POST", "/stories", {"media_url": f"{MEDIA}/stories/2.jpg"}),
    "view": ("POST", "/stories/{story}/view", None),
    "profile": ("PATCH", "/users/me", {"display_name": "Somebody new"}),
}


def write(client: TestClient, kind: str, post: object, story: object):
    method, path, body = WRITES[kind]
    fill = lambda text: text.format(post=post, story=story)  # noqa: E731
    if body is not None:
        body = {key: fill(value) for key, value in body.items()}
    return client.request(method, fill(path), json=body)


@pytest.fixture
def removed_once_authenticated(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """From now on, an account is removed the moment a request of its own has
    been authenticated. Holds the ids of the accounts this happened to."""
    removed: list[uuid.UUID] = []
    authenticate = auth_service.authenticate_session

    def authenticate_then_remove(db: Session, raw_token: str) -> UserSession | None:
        found = authenticate(db, raw_token)
        if found is not None:
            removed.append(found.user_id)
            erasure.erase_account(db, found.user_id, datetime.now(timezone.utc))
            # Committed, as it would be by the request that removed it. What
            # this request has read of the account stays as it was read: it
            # is all the request has, and it is of rows that are gone.
            expire, db.expire_on_commit = db.expire_on_commit, False
            db.commit()
            db.expire_on_commit = expire
        return found

    monkeypatch.setattr(auth_service, "authenticate_session", authenticate_then_remove)
    return removed


@pytest.mark.parametrize("kind", WRITES)
def test_write_by_an_account_that_was_just_removed_is_refused_as_not_authenticated(
    alice_client: TestClient,
    world: World,
    removed_once_authenticated: list[uuid.UUID],
    kind: str,
) -> None:
    session = world.session
    follow(session, world.alice, world.bob)
    post = world.post(world.bob, "Bob's post")
    story = add_story(session, world.bob)
    world.add(
        Like(user_id=world.alice.id, post_id=post.id),
        Repost(user_id=world.alice.id, post_id=post.id),
    )
    alice_id = world.alice.id
    before = others(session, alice_id)

    response = write(alice_client, kind, post.id, story.id)

    assert removed_once_authenticated == [alice_id]
    # Not a server error and not a half-told "conflict": the request has no
    # session any more, and is told so.
    assert response.status_code == 401, response.text
    assert response.json() == NOT_AUTHENTICATED
    # And it wrote nothing: there is no post, like, follow, story or view in
    # the name of an account that is not there.
    assert others(session, alice_id) == before
    written = ["One more thing", "A reply", "Somebody new", "/stories/2.jpg"]
    assert what_is_left_of(session, user_id=alice_id, texts=written) == []
    assert count(session, Post) == 1 and count(session, Story) == 1
    assert count(session, StoryView) == count(session, Follow) == 0


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/auth/me"),
        ("GET", "/users/me"),
        ("GET", "/auth/sessions"),
        ("POST", "/auth/sessions/revoke-others"),
    ],
)
def test_other_requests_of_an_account_that_was_just_removed_are_no_server_errors(
    alice_client: TestClient,
    session: Session,
    alice_account: User,
    removed_once_authenticated: list[uuid.UUID],
    method: str,
    path: str,
) -> None:
    response = alice_client.request(method, path)

    assert removed_once_authenticated
    # What was read when the request arrived may still be answered from; a
    # request that goes back to the rows finds none and is not authenticated.
    assert response.status_code in (200, 204, 401), response.text
    assert count(session, User) == 0


@pytest.mark.parametrize("kind", [k for k in WRITES if k != "profile"])
def test_everything_written_in_a_users_name_holds_their_account_first(
    alice_client: TestClient, world: World, statements: Statements, kind: str
) -> None:
    session = world.session
    post = world.post(world.bob, "Bob's post")
    story = add_story(session, world.bob)
    follow(session, world.alice, world.bob)
    statements.clear()

    assert write(alice_client, kind, post.id, story.id).status_code in (200, 201)

    # The same lock the removal of the account takes first, in the form that
    # only a removal has to wait for. Taken before any other row is held
    # and before anything is written.
    locking = statements.where(" FOR ")
    writing = [
        position
        for position, statement in enumerate(statements)
        if statement.startswith(("INSERT", "DELETE", "UPDATE"))
    ]
    held = statements[locking[0]]
    assert held.startswith("SELECT users.id FROM users WHERE users.id = ")
    assert held.endswith("FOR KEY SHARE")
    assert not writing or locking[0] < writing[0]


def test_profile_change_needs_no_lock_of_its_own(
    alice_client: TestClient, statements: Statements
) -> None:
    # It changes the account's row, which is a lock on that row already,
    # and it holds nothing else.
    assert write(alice_client, "profile", None, None).status_code == 200

    assert statements.where(" FOR ") == []
    assert len(statements.where("UPDATE users SET")) == 1


def test_session_whose_account_cannot_be_read_any_more_has_no_account() -> None:
    # What a request holds when its session's row was still found and its
    # account's row, read a moment later, was not. See
    # ``test_account_deletion_concurrency`` for how that comes about.
    class FoundWithoutItsAccount:
        user = None

    with pytest.raises(auth_service.AccountNoLongerExistsError):
        auth_service.account_of(FoundWithoutItsAccount())


def test_account_that_goes_under_a_request_is_answered_as_not_authenticated(
    alice_client: TestClient,
    session: Session,
    alice_account: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The two ways the rows of a request can turn out to be gone: when one
    # is read again, and when one is changed. Neither is the server's fault.
    read_profile = users_service.get_own_profile

    def removed_and_forgotten(db: Session, user: User):
        erasure.erase_account(db, user.id, datetime.now(timezone.utc))
        db.commit()
        return read_profile(db, user)

    monkeypatch.setattr(users_service, "get_own_profile", removed_and_forgotten)

    response = alice_client.get("/users/me")

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert count(session, User) == 0

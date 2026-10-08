"""Following: following and unfollowing a user, and what the API shows of it.

The lists of followers and of followed users work the same way, so the tests
about lists run twice, once for each (the ``side`` fixture).
"""

import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Cursor,
    decode_cursor,
    encode_cursor,
)
from app.db.session import engine
from app.main import app
from app.models import Follow, User, UserSession
from helpers import (
    add_post,
    add_user,
    expire,
    log_in,
    plant_session_cookie,
    recorded_selects,
)

FOLLOWING = {"following": True}
NOT_FOLLOWING = {"following": False}
EMPTY = {"items": [], "next_cursor": None}
USER_NOT_FOUND = {"detail": "User not found."}
NOT_AUTHENTICATED = {"detail": "Not authenticated."}
NOT_VERIFIED = {"detail": "Email address is not verified."}
SELF_FOLLOW = {"detail": "You cannot follow yourself."}
INVALID_CURSOR = {"detail": "Invalid cursor."}
SUMMARY_FIELDS = {"username", "display_name", "avatar_url"}
START = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)
# Every reason there is no account to follow or to list anything of.
UNAVAILABLE = ["nonexistent", "inactive", "unverified"]


@pytest.fixture(params=["followers", "following"])
def side(request: pytest.FixtureRequest) -> str:
    """Which of a user's two lists a test is about."""
    return request.param


@pytest.fixture(params=["POST", "DELETE"])
def method(request: pytest.FixtureRequest) -> str:
    """Following, or unfollowing."""
    return request.param


def change(client: TestClient, method: str, username: str):
    return client.request(method, f"/users/{username}/follow")


def follow(client: TestClient, username: str):
    return change(client, "POST", username)


def unfollow(client: TestClient, username: str):
    return change(client, "DELETE", username)


def status(client: TestClient, username: str):
    return client.get(f"/users/{username}/follow-status")


def listing(client: TestClient, side: str, username: str = "alice", **params: object):
    return client.get(f"/users/{username}/{side}", params=params)


def usernames(response_or_items: object) -> list[str]:
    items = response_or_items
    if not isinstance(items, list):
        assert items.status_code == 200, items.text
        items = items.json()["items"]
    return [item["username"] for item in items]


def profile(client: TestClient, username: str) -> dict:
    response = client.get(f"/users/{username}")
    assert response.status_code == 200, response.text
    return response.json()


def counts(client: TestClient, username: str) -> tuple[int, int]:
    """A user's (followers, following) counts, as the profile shows them."""
    body = profile(client, username)
    return body["followers_count"], body["following_count"]


def rows(session: Session) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """Every follow in the database, as (follower, followed)."""
    found = session.execute(select(Follow.follower_id, Follow.following_id)).all()
    return {(row.follower_id, row.following_id) for row in found}


def add_follow(
    session: Session, follower: User, followed: User, *, at: datetime | None = None
) -> None:
    """A follow written directly to the database, as if made at ``at``."""
    when = {"created_at": at} if at is not None else {}
    session.add(Follow(follower_id=follower.id, following_id=followed.id, **when))
    session.flush()


def connect(
    session: Session, side: str, account: User, other: User, *, at: datetime | None
) -> None:
    """Put ``other`` on ``account``'s list of followers, or of followed users."""
    if side == "followers":
        add_follow(session, other, account, at=at)
    else:
        add_follow(session, account, other, at=at)


def add_listed(
    session: Session,
    side: str,
    account: User,
    count: int,
    *,
    start: datetime = START,
    prefix: str = "user",
) -> list[User]:
    """``count`` new users on ``account``'s list, connected one minute apart.

    Returned as the list shows them: the most recent first.
    """
    users = []
    for number in range(count):
        user = add_user(session, f"{prefix}{number:02d}")
        connect(session, side, account, user, at=start + number * MINUTE)
        users.append(user)
    return users[::-1]


def names(users: list[User]) -> list[str]:
    return [user.username for user in users]


def walk(
    client: TestClient,
    side: str,
    username: str = "alice",
    *,
    limit: int,
    cursor: str | None = None,
) -> list[list[dict]]:
    """Every page from ``cursor`` on, following next_cursor until there is none."""
    pages = []
    while True:
        extra = {"cursor": cursor} if cursor else {}
        response = listing(client, side, username, limit=limit, **extra)
        assert response.status_code == 200, response.text
        body = response.json()
        pages.append(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) < 200, "pagination does not terminate"


def browser(make_client, username: str) -> TestClient:
    """A separate browser, logged in to an account that exists."""
    client = make_client()
    assert log_in(client, username).status_code == 200
    return client


def browsers(make_client, *usernames: str) -> list[TestClient]:
    return [browser(make_client, username) for username in usernames]


def unavailable(session: Session, reason: str) -> str:
    """The username of an account that is not there for anyone to follow."""
    if reason != "nonexistent":
        add_user(
            session,
            reason,
            verified=reason != "unverified",
            active=reason != "inactive",
        )
    return reason


# --- following and unfollowing -------------------------------------------


def test_user_can_follow_another_user(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    response = follow(bob_client, "alice")

    assert response.status_code == 200
    assert response.json() == FOLLOWING
    assert rows(session) == {(bob_account.id, alice_account.id)}
    assert session.scalars(select(Follow)).one().created_at.tzinfo is not None


def test_user_can_unfollow(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    follow(bob_client, "alice")

    response = unfollow(bob_client, "alice")

    assert response.status_code == 200
    assert response.json() == NOT_FOLLOWING
    assert rows(session) == set()


def test_answer_holds_the_state_and_nothing_else(
    bob_client: TestClient, alice_account: User, method: str
) -> None:
    body = change(bob_client, method, "alice").json()

    assert set(body) == {"following"}
    assert type(body["following"]) is bool


def test_follow_takes_effect_at_once(
    bob_client: TestClient, make_client, alice_account: User
) -> None:
    response = follow(bob_client, "alice")

    # No request to approve and no state in between: it is simply so.
    assert response.status_code == 200
    assert response.json() == FOLLOWING
    assert status(bob_client, "alice").json() == FOLLOWING
    assert profile(bob_client, "alice")["following"] is True
    anyone = make_client()
    assert usernames(listing(anyone, "followers", "alice")) == ["bob"]
    assert usernames(listing(anyone, "following", "bob")) == ["alice"]
    assert counts(anyone, "alice") == (1, 0)
    assert counts(anyone, "bob") == (0, 1)


def test_following_goes_one_way(
    alice_client: TestClient, bob_client: TestClient, session: Session
) -> None:
    follow(bob_client, "alice")

    # Bob follows alice. That says nothing about alice following bob.
    assert status(alice_client, "bob").json() == NOT_FOLLOWING
    assert profile(alice_client, "bob")["following"] is False
    assert usernames(listing(alice_client, "followers", "bob")) == []
    assert usernames(listing(alice_client, "following", "alice")) == []
    assert len(rows(session)) == 1


def test_two_users_can_follow_each_other(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    assert follow(bob_client, "alice").json() == FOLLOWING
    assert follow(alice_client, "bob").json() == FOLLOWING

    assert rows(session) == {
        (bob_account.id, alice_account.id),
        (alice_account.id, bob_account.id),
    }
    assert counts(alice_client, "alice") == counts(alice_client, "bob") == (1, 1)

    # And one of them ending it leaves the other's follow alone.
    assert unfollow(bob_client, "alice").json() == NOT_FOLLOWING
    assert rows(session) == {(alice_account.id, bob_account.id)}


def test_user_can_follow_many_and_be_followed_by_many(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    for name in ("carol", "dave"):
        add_user(session, name)
    alice = browser(make_client, "alice")

    for name in ("bob", "carol", "dave"):
        assert follow(alice, name).json() == FOLLOWING
    for name in ("bob", "carol"):
        assert follow(browser(make_client, name), "alice").json() == FOLLOWING

    assert counts(alice, "alice") == (2, 3)
    assert sorted(usernames(listing(alice, "following"))) == ["bob", "carol", "dave"]
    assert sorted(usernames(listing(alice, "followers"))) == ["bob", "carol"]


@pytest.mark.parametrize("username", ["alice", "Alice", "ALICE", " alice "])
def test_username_is_matched_in_its_canonical_form(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    username: str,
) -> None:
    path = quote(username)

    assert follow(bob_client, path).json() == FOLLOWING
    assert rows(session) == {(bob_account.id, alice_account.id)}
    assert status(bob_client, path).json() == FOLLOWING
    assert usernames(listing(bob_client, "followers", path)) == ["bob"]
    assert unfollow(bob_client, path).json() == NOT_FOLLOWING


def test_no_profile_change_puts_an_account_out_of_reach_of_a_follow(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    # There is no privacy setting to switch on, alone or next to a real
    # change, so there is nothing that could ask a follower to wait.
    alone = alice_client.patch("/users/me", json={"is_private": True})
    beside = alice_client.patch("/users/me", json={"is_private": True, "bio": "Hi"})

    assert follow(bob_client, "alice").json() == FOLLOWING
    assert status(bob_client, "alice").json() == FOLLOWING
    assert usernames(listing(bob_client, "followers", "alice")) == ["bob"]
    assert counts(bob_client, "alice") == (1, 0)
    assert (alone.status_code, beside.status_code) == (422, 200)


def test_follow_answers_are_not_to_be_cached(
    bob_client: TestClient, alice_account: User
) -> None:
    answers = [
        follow(bob_client, "alice"),
        status(bob_client, "alice"),
        listing(bob_client, "followers"),
        listing(bob_client, "following"),
        bob_client.get("/users/alice"),
        unfollow(bob_client, "alice"),
    ]

    for response in answers:
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"


# --- repeating a request -------------------------------------------------


def test_following_twice_is_the_same_as_following_once(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    first = follow(bob_client, "alice")
    second = follow(bob_client, "alice")

    # Not a conflict: the second request is answered with how things are.
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == FOLLOWING
    assert len(rows(session)) == 1


def test_unfollowing_twice_is_the_same_as_unfollowing_once(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    follow(bob_client, "alice")

    first = unfollow(bob_client, "alice")
    second = unfollow(bob_client, "alice")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == NOT_FOLLOWING
    assert rows(session) == set()


def test_unfollowing_someone_never_followed_is_not_an_error(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    response = unfollow(bob_client, "alice")

    assert response.status_code == 200
    assert response.json() == NOT_FOLLOWING
    assert rows(session) == set()


def test_follow_follow_unfollow_unfollow(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    answers = [
        change(bob_client, method, "alice")
        for method in ("POST", "POST", "DELETE", "DELETE")
    ]

    assert [response.status_code for response in answers] == [200] * 4
    assert [response.json() for response in answers] == [
        FOLLOWING,
        FOLLOWING,
        NOT_FOLLOWING,
        NOT_FOLLOWING,
    ]
    assert rows(session) == set()
    assert counts(bob_client, "alice") == (0, 0)


def test_user_can_follow_again_after_unfollowing(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    answers = [
        change(bob_client, method, "alice") for method in ("POST", "DELETE", "POST")
    ]

    assert [response.json() for response in answers] == [
        FOLLOWING,
        NOT_FOLLOWING,
        FOLLOWING,
    ]
    assert rows(session) == {(bob_account.id, alice_account.id)}


def test_repeating_a_request_never_affects_another_users_follow(
    bob_client: TestClient,
    make_client,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    carol = add_user(session, "carol")
    add_follow(session, carol, alice_account)

    for method in ("POST", "POST", "DELETE", "DELETE"):
        assert change(bob_client, method, "alice").status_code == 200

    # Carol's follow is still the one that is there.
    assert rows(session) == {(carol.id, alice_account.id)}
    assert status(browser(make_client, "carol"), "alice").json() == FOLLOWING


def test_row_that_is_already_there_is_not_an_error(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    # What a request finds when another one, sent at the same moment, was
    # the first to write.
    add_follow(session, bob_account, alice_account)

    response = follow(bob_client, "alice")

    assert response.status_code == 200
    assert response.json() == FOLLOWING
    assert len(rows(session)) == 1


def test_duplicate_is_left_to_the_database_to_refuse(
    bob_client: TestClient, alice_account: User
) -> None:
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        follow(bob_client, "alice")
        follow(bob_client, "alice")
    finally:
        event.remove(engine, "before_cursor_execute", record)

    # Not "look, then insert", which two simultaneous requests would both
    # get past. Every request inserts, and the primary key settles it.
    inserts = [statement for statement in statements if statement.startswith("INSERT")]
    assert len(inserts) == 2
    for statement in inserts:
        assert statement.startswith(
            "INSERT INTO follows (follower_id, following_id) VALUES"
        )
        assert statement.endswith("ON CONFLICT DO NOTHING")


# --- following oneself ---------------------------------------------------


@pytest.mark.parametrize("username", ["bob", "Bob", "BOB", " bob "])
def test_user_cannot_follow_themselves(
    bob_client: TestClient, session: Session, username: str
) -> None:
    response = follow(bob_client, quote(username))

    assert response.status_code == 400
    assert response.json() == SELF_FOLLOW
    assert rows(session) == set()
    assert counts(bob_client, "bob") == (0, 0)


def test_self_follow_is_refused_before_it_reaches_the_database(
    bob_client: TestClient,
) -> None:
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement.lstrip().upper())

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = follow(bob_client, "bob")
    finally:
        event.remove(engine, "before_cursor_execute", record)

    # An answer to the client, not a constraint violation turned into one.
    assert response.status_code == 400
    assert not any(statement.startswith("INSERT") for statement in statements)


def test_database_refuses_a_self_follow_on_its_own(
    session: Session, alice_account: User
) -> None:
    # The service check is the first line. This stays the last one.
    session.add(Follow(follower_id=alice_account.id, following_id=alice_account.id))

    with pytest.raises(IntegrityError, match="ck_follows_no_self_follow"):
        session.flush()


def test_database_refuses_a_second_row_for_the_same_follow(
    session: Session, alice_account: User, bob_account: User
) -> None:
    add_follow(session, bob_account, alice_account)
    session.expunge_all()

    session.add(Follow(follower_id=bob_account.id, following_id=alice_account.id))
    with pytest.raises(IntegrityError, match="pk_follows"):
        session.flush()


def test_user_does_not_follow_themselves_and_unfollowing_says_so(
    bob_client: TestClient, session: Session
) -> None:
    # Nothing to refuse: the result asked for is how things already are.
    assert unfollow(bob_client, "bob").status_code == 200
    assert unfollow(bob_client, "bob").json() == NOT_FOLLOWING
    assert status(bob_client, "bob").json() == NOT_FOLLOWING
    assert profile(bob_client, "bob")["following"] is False
    assert rows(session) == set()


# --- who can be followed -------------------------------------------------


@pytest.mark.parametrize("reason", UNAVAILABLE)
def test_account_that_is_not_shown_cannot_be_followed_or_unfollowed(
    bob_client: TestClient, session: Session, method: str, reason: str
) -> None:
    username = unavailable(session, reason)

    response = change(bob_client, method, username)

    assert response.status_code == 404
    assert response.json() == USER_NOT_FOUND
    assert rows(session) == set()


@pytest.mark.parametrize(
    "username",
    ["al", "a" * 31, "alice-smith", "álice", "alice@example.com", "' OR '1'='1", "%"],
)
def test_impossible_username_is_not_found(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    method: str,
    username: str,
) -> None:
    response = change(bob_client, method, quote(username, safe=""))

    assert response.status_code == 404
    assert response.json() == USER_NOT_FOUND
    assert rows(session) == set()


def test_user_cannot_be_followed_by_id_or_email(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    by_id = follow(bob_client, str(alice_account.id))
    by_email = follow(bob_client, quote(alice_account.email, safe=""))

    assert by_id.status_code == by_email.status_code == 404
    assert rows(session) == set()


def test_me_is_not_a_username(bob_client: TestClient, session: Session) -> None:
    # /users/me is the caller's profile; there is no such alias for follows.
    assert follow(bob_client, "me").status_code == 404
    assert status(bob_client, "me").status_code == 404
    assert listing(bob_client, "followers", "me").status_code == 404
    assert rows(session) == set()


def test_follow_of_an_account_that_stops_being_shown_is_kept_but_not_reported(
    bob_client: TestClient,
    make_client,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    follow(bob_client, "alice")
    alice_account.is_active = False
    session.flush()

    # The account is not there for anyone, so there is nothing to unfollow
    # or to ask about, and it is on nobody's list.
    assert unfollow(bob_client, "alice").status_code == 404
    assert status(bob_client, "alice").status_code == 404
    assert usernames(listing(bob_client, "following", "bob")) == []
    assert rows(session) == {(bob_account.id, alice_account.id)}

    # Nothing was destroyed by the deactivation.
    alice_account.is_active = True
    session.flush()
    assert status(bob_client, "alice").json() == FOLLOWING
    assert usernames(listing(make_client(), "following", "bob")) == ["alice"]
    assert unfollow(bob_client, "alice").json() == NOT_FOLLOWING


# --- who can follow ------------------------------------------------------


def test_anonymous_user_cannot_follow_or_unfollow(
    client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    method: str,
) -> None:
    add_follow(session, bob_account, alice_account)
    before = rows(session)

    response = change(client, method, "alice")

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert rows(session) == before


def test_anonymous_user_gets_the_answer_every_write_gives(
    client: TestClient, alice_account: User, method: str
) -> None:
    refused = change(client, method, "alice")
    other_write = client.patch("/users/me", json={"bio": "x"})

    assert refused.status_code == other_write.status_code == 401
    assert refused.text == other_write.text
    assert sorted(refused.headers) == sorted(other_write.headers)


def test_follow_after_logout_is_rejected(
    bob_client: TestClient, session: Session, alice_account: User, method: str
) -> None:
    follow(bob_client, "alice")

    bob_client.post("/auth/logout")
    response = change(bob_client, method, "alice")

    assert response.status_code == 401
    assert len(rows(session)) == 1


def test_follow_with_an_expired_or_made_up_session_is_rejected(
    bob_client: TestClient,
    make_client,
    session: Session,
    alice_account: User,
    method: str,
) -> None:
    expire(session, session.scalars(select(UserSession)).one())
    stranger = make_client()
    plant_session_cookie(stranger, "not-a-session-token")

    for client in (bob_client, stranger):
        response = change(client, method, "alice")
        assert response.status_code == 401
        assert response.json() == NOT_AUTHENTICATED
    assert rows(session) == set()


def test_unverified_user_cannot_follow_or_unfollow(
    client: TestClient, session: Session, alice_account: User, method: str
) -> None:
    # The ordinary case: an unverified account never gets a session.
    add_user(session, "unverified", verified=False)

    assert log_in(client, "unverified").status_code == 403
    response = change(client, method, "alice")

    assert response.status_code == 401
    assert rows(session) == set()


def test_session_of_an_unverified_account_cannot_follow_or_unfollow(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    method: str,
) -> None:
    # Not reachable by logging in. Should a session ever belong to an
    # unverified account all the same, the change is still refused.
    add_follow(session, bob_account, alice_account)
    bob_account.email_verified_at = None
    session.flush()

    response = change(bob_client, method, "alice")

    assert response.status_code == 403
    assert response.json() == NOT_VERIFIED
    assert len(rows(session)) == 1


def test_inactive_user_cannot_follow_or_unfollow(
    client: TestClient, session: Session, alice_account: User, method: str
) -> None:
    add_user(session, "inactive", active=False)

    assert log_in(client, "inactive").status_code == 401
    response = change(client, method, "alice")

    assert response.status_code == 401
    assert rows(session) == set()


def test_user_deactivated_while_logged_in_cannot_follow_or_unfollow(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    method: str,
) -> None:
    assert follow(bob_client, "alice").status_code == 200

    bob_account.is_active = False
    session.flush()
    response = change(bob_client, method, "alice")

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert len(rows(session)) == 1


# --- follow status -------------------------------------------------------


def test_status_follows_what_the_caller_does(
    bob_client: TestClient, alice_account: User
) -> None:
    before = status(bob_client, "alice")
    follow(bob_client, "alice")
    followed = status(bob_client, "alice")
    unfollow(bob_client, "alice")
    unfollowed = status(bob_client, "alice")

    assert before.status_code == followed.status_code == unfollowed.status_code == 200
    assert before.json() == NOT_FOLLOWING
    assert followed.json() == FOLLOWING
    assert unfollowed.json() == NOT_FOLLOWING
    assert set(followed.json()) == {"following"}


def test_anonymous_caller_follows_nobody(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    add_follow(session, bob_account, alice_account)

    response = status(client, "alice")

    # Not refused: the answer is public, and for nobody in particular it is no.
    assert client.cookies.get("__Host-hopsnop_session") is None
    assert response.status_code == 200
    assert response.json() == NOT_FOLLOWING


def test_status_is_the_callers_own_and_nobody_elses(
    bob_client: TestClient,
    make_client,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    carol = add_user(session, "carol")
    add_follow(session, carol, alice_account)
    add_follow(session, alice_account, bob_account)

    # Carol follows alice, and alice follows bob. Bob follows neither.
    assert status(bob_client, "alice").json() == NOT_FOLLOWING
    assert status(bob_client, "carol").json() == NOT_FOLLOWING
    assert status(browser(make_client, "carol"), "alice").json() == FOLLOWING
    assert status(browser(make_client, "alice"), "bob").json() == FOLLOWING


@pytest.mark.parametrize("reason", UNAVAILABLE)
def test_status_of_an_account_that_is_not_shown_is_the_profiles_404(
    make_client, session: Session, bob_account: User, reason: str
) -> None:
    username = unavailable(session, reason)

    for caller in (make_client(), browser(make_client, "bob")):
        response = status(caller, username)
        shown = caller.get(f"/users/{username}")

        assert response.status_code == shown.status_code == 404
        assert response.json() == USER_NOT_FOUND
        assert response.text == shown.text


def test_status_with_a_stale_cookie_is_answered_as_anonymous(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    add_follow(session, bob_account, alice_account)
    plant_session_cookie(client, "left-over-from-an-old-login")

    response = status(client, "alice")

    assert response.status_code == 200
    assert response.json() == NOT_FOLLOWING


def test_caller_cannot_be_named_by_the_request(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    add_follow(session, bob_account, alice_account)
    bob_id = bob_account.id

    asked = "/users/alice/follow-status"
    answers = [
        client.get(f"{asked}?follower_id={bob_id}&user_id={bob_id}"),
        client.get(f"{asked}?follower=bob&as=bob&following=true"),
        client.get(asked, headers={"X-User-Id": str(bob_id)}),
        client.get(f"/users/alice?viewer_id={bob_id}&following=true"),
    ]

    for response in answers:
        assert response.status_code == 200
        assert response.json()["following"] is False


def test_status_ends_with_the_session(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    follow(bob_client, "alice")
    assert status(bob_client, "alice").json() == FOLLOWING

    bob_client.post("/auth/logout")

    # The follow is still there; the caller is no longer known to be bob.
    assert status(bob_client, "alice").json() == NOT_FOLLOWING
    assert profile(bob_client, "alice")["following"] is False
    assert len(rows(session)) == 1


# --- counts --------------------------------------------------------------


def test_following_and_unfollowing_move_both_counts(
    bob_client: TestClient, make_client, alice_account: User
) -> None:
    anyone = make_client()
    assert counts(anyone, "alice") == counts(anyone, "bob") == (0, 0)

    follow(bob_client, "alice")
    assert counts(anyone, "alice") == (1, 0)
    assert counts(anyone, "bob") == (0, 1)

    unfollow(bob_client, "alice")
    assert counts(anyone, "alice") == counts(anyone, "bob") == (0, 0)


def test_repeated_requests_do_not_move_the_counts_again(
    bob_client: TestClient, alice_account: User
) -> None:
    for _ in range(3):
        follow(bob_client, "alice")
    assert counts(bob_client, "alice") == (1, 0)
    assert counts(bob_client, "bob") == (0, 1)

    for _ in range(3):
        unfollow(bob_client, "alice")
    # Never below zero.
    assert counts(bob_client, "alice") == (0, 0)
    assert counts(bob_client, "bob") == (0, 0)


def test_counts_are_the_number_of_follows_in_each_direction(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    for name in ("carol", "dave"):
        add_user(session, name)
    alice, bob, carol = browsers(make_client, "alice", "bob", "carol")

    for name in ("bob", "carol", "dave"):
        follow(alice, name)
    follow(bob, "alice")
    follow(carol, "alice")

    anyone = make_client()
    assert counts(anyone, "alice") == (2, 3)
    assert counts(anyone, "bob") == (1, 1)
    assert counts(anyone, "carol") == (1, 1)
    assert counts(anyone, "dave") == (1, 0)

    unfollow(bob, "alice")
    unfollow(alice, "dave")
    assert counts(anyone, "alice") == (1, 2)
    assert counts(anyone, "dave") == (0, 0)


def test_own_profile_shows_the_same_counts(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    follow(bob_client, "alice")
    follow(alice_client, "bob")
    unfollow(alice_client, "bob")

    own = alice_client.get("/users/me").json()

    assert (own["followers_count"], own["following_count"]) == (1, 0)
    assert counts(alice_client, "alice") == (1, 0)


# --- follow state on a profile -------------------------------------------


def test_profile_says_whether_the_reader_follows_the_user(
    bob_client: TestClient, alice_account: User
) -> None:
    before = profile(bob_client, "alice")
    follow(bob_client, "alice")
    followed = profile(bob_client, "alice")
    unfollow(bob_client, "alice")
    unfollowed = profile(bob_client, "alice")

    assert before["following"] is False
    assert followed["following"] is True
    assert unfollowed["following"] is False
    assert (followed["followers_count"], followed["following_count"]) == (1, 0)


def test_profile_follow_state_is_each_readers_own(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    add_user(session, "carol")
    alice, bob, carol = browsers(make_client, "alice", "bob", "carol")
    follow(bob, "alice")

    seen = {
        "anonymous": profile(make_client(), "alice"),
        "bob": profile(bob, "alice"),
        "carol": profile(carol, "alice"),
        "alice": profile(alice, "alice"),
    }

    assert {name: body["following"] for name, body in seen.items()} == {
        "anonymous": False,
        "bob": True,
        "carol": False,
        "alice": False,
    }
    # Everything else on the profile is the same for all of them.
    for body in seen.values():
        body.pop("following")
    assert seen["anonymous"] == seen["bob"] == seen["carol"] == seen["alice"]
    assert seen["anonymous"]["followers_count"] == 1


def test_profile_does_not_say_whether_the_user_follows_the_reader(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    follow(bob_client, "alice")

    # Alice is reading the profile of someone who follows her.
    assert profile(alice_client, "bob")["following"] is False


def test_own_profile_has_no_follow_state(alice_client: TestClient) -> None:
    assert "following" not in alice_client.get("/users/me").json()


# --- the lists -----------------------------------------------------------


def test_followers_are_the_users_who_follow_the_user(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    carol = add_user(session, "carol")
    add_follow(session, bob_account, alice_account, at=START)
    add_follow(session, carol, alice_account, at=START + MINUTE)
    add_follow(session, alice_account, carol, at=START + 2 * MINUTE)

    response = listing(client, "followers", "alice")

    assert response.status_code == 200
    assert set(response.json()) == {"items", "next_cursor"}
    assert usernames(response) == ["carol", "bob"]
    assert response.json()["next_cursor"] is None


def test_following_are_the_users_the_user_follows(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    carol = add_user(session, "carol")
    add_follow(session, alice_account, bob_account, at=START)
    add_follow(session, alice_account, carol, at=START + MINUTE)
    add_follow(session, bob_account, alice_account, at=START + 2 * MINUTE)

    response = listing(client, "following", "alice")

    assert response.status_code == 200
    assert usernames(response) == ["carol", "bob"]
    assert response.json()["next_cursor"] is None


def test_item_shows_what_a_list_needs_and_nothing_else(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    carol = add_user(session, "carol")
    carol.display_name = "Carol Jones"
    carol.avatar_url = "https://cdn.example.com/avatars/carol.png"
    carol.bio = "Not part of a list"
    dave = add_user(session, "dave")
    connect(session, side, alice_account, carol, at=START + MINUTE)
    connect(session, side, alice_account, dave, at=START)

    items = listing(client, side).json()["items"]

    assert items == [
        {
            "username": "carol",
            "display_name": "Carol Jones",
            "avatar_url": "https://cdn.example.com/avatars/carol.png",
        },
        {"username": "dave", "display_name": "Dave", "avatar_url": None},
    ]
    assert all(set(item) == SUMMARY_FIELDS for item in items)


def test_user_with_no_one_on_a_list_has_an_empty_list(
    client: TestClient, alice_account: User, side: str
) -> None:
    response = listing(client, side)

    assert response.status_code == 200
    assert response.json() == EMPTY


def test_lists_need_no_authentication_and_are_the_same_for_every_reader(
    make_client, session: Session, alice_account: User, bob_account: User, side: str
) -> None:
    listed = add_listed(session, side, alice_account, 3)
    connect(session, side, alice_account, bob_account, at=START - MINUTE)
    anonymous, alice, bob = make_client(), make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")

    seen = [listing(reader, side).json() for reader in (anonymous, alice, bob)]

    assert seen[0] == seen[1] == seen[2]
    assert usernames(seen[0]["items"]) == [*names(listed), "bob"]


def test_each_user_has_their_own_lists(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    carol = add_user(session, "carol")
    add_follow(session, carol, alice_account)
    add_follow(session, bob_account, carol)

    assert usernames(listing(client, "followers", "alice")) == ["carol"]
    assert usernames(listing(client, "following", "alice")) == []
    assert usernames(listing(client, "followers", "bob")) == []
    assert usernames(listing(client, "following", "bob")) == ["carol"]
    assert usernames(listing(client, "followers", "carol")) == ["bob"]
    assert usernames(listing(client, "following", "carol")) == ["alice"]


def test_most_recent_comes_first(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    # Connected in an order that is neither alphabetical nor that of the ids.
    hour = timedelta(hours=1)
    middle, newest, oldest = (
        add_user(session, name) for name in ("middle", "newest", "oldest")
    )
    connect(session, side, alice_account, middle, at=START)
    connect(session, side, alice_account, newest, at=START + hour)
    connect(session, side, alice_account, oldest, at=START - hour)

    assert usernames(listing(client, side)) == ["newest", "middle", "oldest"]


def test_order_is_by_the_follow_not_by_the_account(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    # The first to sign up is the last to be connected.
    early = add_user(session, "early_account")
    early.created_at = START - timedelta(days=365)
    late = add_user(session, "late_account")
    connect(session, side, alice_account, late, at=START)
    connect(session, side, alice_account, early, at=START + MINUTE)

    assert usernames(listing(client, side)) == ["early_account", "late_account"]


def test_follows_made_at_the_same_instant_have_a_fixed_order(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    users = [add_user(session, f"user{number:02d}") for number in range(8)]
    for user in users:
        connect(session, side, alice_account, user, at=START)
    by_id = sorted(users, key=lambda user: user.id, reverse=True)

    first = usernames(listing(client, side))
    second = usernames(listing(client, side))

    assert first == second == names(by_id)


def test_unfollowing_takes_the_user_off_both_lists_at_once(
    bob_client: TestClient, make_client, alice_account: User
) -> None:
    follow(bob_client, "alice")
    anyone = make_client()
    assert usernames(listing(anyone, "followers", "alice")) == ["bob"]
    assert usernames(listing(anyone, "following", "bob")) == ["alice"]

    unfollow(bob_client, "alice")

    assert listing(anyone, "followers", "alice").json() == EMPTY
    assert listing(anyone, "following", "bob").json() == EMPTY


def test_lists_say_nothing_about_account_privacy(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    add_follow(session, bob_account, alice_account)
    bob = browser(make_client, "bob")

    # Every account is listed and has its lists read alike, by anyone.
    for viewer in (make_client(), bob):
        followers = listing(viewer, "followers", "alice")
        following = listing(viewer, "following", "bob")
        assert usernames(followers) == ["bob"]
        assert usernames(following) == ["alice"]
        for response in (followers, following):
            [item] = response.json()["items"]
            assert set(item) == SUMMARY_FIELDS
            assert "private" not in response.text


# --- accounts that are not shown, and the lists --------------------------


@pytest.mark.parametrize("reason", UNAVAILABLE)
def test_lists_of_an_account_that_is_not_shown_are_the_profiles_404(
    client: TestClient, session: Session, side: str, reason: str
) -> None:
    username = unavailable(session, reason)

    response = listing(client, side, username)
    shown = client.get(f"/users/{username}")

    assert response.status_code == shown.status_code == 404
    assert response.json() == USER_NOT_FOUND
    assert response.text == shown.text


@pytest.mark.parametrize("reason", ["inactive", "unverified"])
def test_account_that_is_not_shown_is_on_nobodys_list(
    client: TestClient, session: Session, alice_account: User, side: str, reason: str
) -> None:
    shown = add_listed(session, side, alice_account, 2)
    hidden = add_user(
        session,
        "hidden_one",
        verified=reason != "unverified",
        active=reason != "inactive",
    )
    connect(session, side, alice_account, hidden, at=START + 30 * MINUTE)

    response = listing(client, side)

    # Whatever GET /users/{username} hides, a list does not reveal.
    assert usernames(response) == names(shown)
    assert "hidden_one" not in response.text
    assert client.get("/users/hidden_one").status_code == 404


def test_account_is_listed_again_when_it_is_shown_again(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    [carol] = add_listed(session, side, alice_account, 1, prefix="carol")
    carol.is_active = False
    session.flush()
    assert listing(client, side).json() == EMPTY

    carol.is_active = True
    session.flush()

    assert usernames(listing(client, side)) == ["carol00"]


# --- cursor pagination ---------------------------------------------------


def test_first_page_returns_a_cursor_when_there_are_more(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    listed = add_listed(session, side, alice_account, 5)

    body = listing(client, side, limit=2).json()

    assert usernames(body["items"]) == names(listed[:2])
    assert isinstance(body["next_cursor"], str)


def test_cursor_returns_the_pages_that_follow(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    listed = add_listed(session, side, alice_account, 5)
    first = listing(client, side, limit=2).json()

    second = listing(client, side, limit=2, cursor=first["next_cursor"]).json()
    third = listing(client, side, limit=2, cursor=second["next_cursor"]).json()

    assert usernames(second["items"]) == names(listed[2:4])
    assert second["next_cursor"] is not None
    assert usernames(third["items"]) == names(listed[4:])
    assert third["next_cursor"] is None


def test_page_that_holds_everything_has_no_cursor(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    add_listed(session, side, alice_account, 3)

    assert listing(client, side, limit=5).json()["next_cursor"] is None
    # Nor a "next page" that then turns out to be empty.
    assert listing(client, side, limit=3).json()["next_cursor"] is None


@pytest.mark.parametrize(("count", "limit"), [(1, 1), (7, 3), (20, 7), (23, 5)])
def test_walking_the_pages_returns_every_user_exactly_once_in_order(
    client: TestClient,
    session: Session,
    alice_account: User,
    side: str,
    count: int,
    limit: int,
) -> None:
    listed = add_listed(session, side, alice_account, count)

    pages = walk(client, side, limit=limit)

    seen = [name for items in pages for name in usernames(items)]
    assert seen == names(listed)  # nobody skipped, nobody out of order
    assert len(set(seen)) == len(seen)  # nobody twice
    assert all(len(items) == limit for items in pages[:-1])
    assert len(pages) == -(-count // limit)


def test_no_duplicates_or_gaps_among_follows_made_at_the_same_instant(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    # The hard case for a cursor: the time alone cannot tell where the page
    # ended. Three groups of follows, each sharing one instant.
    users = []
    for group in range(3):
        for number in range(7):
            user = add_user(session, f"user{group}{number}")
            connect(session, side, alice_account, user, at=START + group * MINUTE)
            users.append((START + group * MINUTE, user.id, user.username))
    expected = [name for _, _, name in sorted(users, reverse=True)]

    for limit in (1, 2, 3, 5, 7, 8):
        pages = walk(client, side, limit=limit)
        seen = [name for items in pages for name in usernames(items)]
        assert seen == expected, limit


def test_account_that_is_not_shown_takes_up_no_room_on_a_page(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    # Every second account on the list is deactivated.
    shown = add_listed(session, side, alice_account, 6)
    for number in range(6):
        hidden = add_user(session, f"gone{number:02d}", active=False)
        at = START + number * MINUTE + timedelta(seconds=30)
        connect(session, side, alice_account, hidden, at=at)

    pages = walk(client, side, limit=2)

    assert [usernames(items) for items in pages] == [
        names(shown[0:2]),
        names(shown[2:4]),
        names(shown[4:6]),
    ]


def test_page_is_the_last_one_when_only_hidden_accounts_remain(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    shown = add_listed(session, side, alice_account, 2)
    for number in range(5):
        hidden = add_user(session, f"gone{number:02d}", active=False)
        at = START - (number + 1) * MINUTE
        connect(session, side, alice_account, hidden, at=at)

    body = listing(client, side, limit=2).json()

    assert usernames(body["items"]) == names(shown)
    # No cursor: not even that there are more is given away.
    assert body["next_cursor"] is None


def test_new_follow_between_requests_causes_no_duplicates_or_gaps(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    listed = add_listed(session, side, alice_account, 6)
    first = listing(client, side, limit=3).json()

    # While the reader is on page one, a new follow is made. With offset
    # pagination, page two would now begin with the last user of page one.
    new = add_user(session, "newcomer")
    connect(session, side, alice_account, new, at=START + timedelta(days=1))
    second = listing(client, side, limit=3, cursor=first["next_cursor"]).json()

    assert usernames(first["items"]) == names(listed[:3])
    assert usernames(second["items"]) == names(listed[3:])
    assert second["next_cursor"] is None
    # The new one is found by starting again from the top.
    top = listing(client, side, limit=3)
    assert usernames(top) == ["newcomer", *names(listed[:2])]


def test_unfollows_between_requests_cause_no_duplicates_or_gaps(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    listed = add_listed(session, side, alice_account, 8)
    first = listing(client, side, limit=3).json()

    # One already seen, the very one the cursor points at, and one not yet
    # seen are removed. With offset pagination, users would be skipped.
    gone = {user.id for user in (listed[0], listed[2], listed[4])}
    for row in session.scalars(select(Follow)).all():
        if {row.follower_id, row.following_id} & gone:
            session.delete(row)
    session.flush()
    rest = walk(client, side, limit=3, cursor=first["next_cursor"])

    assert [name for items in rest for name in usernames(items)] == names(
        [listed[3], listed[5], listed[6], listed[7]]
    )


def test_account_deactivated_between_requests_is_gone_from_later_pages(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    listed = add_listed(session, side, alice_account, 6)
    first = listing(client, side, limit=2).json()

    listed[3].is_active = False
    session.flush()
    rest = walk(client, side, limit=2, cursor=first["next_cursor"])

    assert [name for items in rest for name in usernames(items)] == names(
        [listed[2], listed[4], listed[5]]
    )


def test_cursor_is_the_one_every_list_uses(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    listed = add_listed(session, side, alice_account, 3)

    cursor = listing(client, side, limit=2).json()["next_cursor"]

    # The same format as for posts: a time and an id. Here they are those of
    # the last follow on the page: when it was made, and who the other
    # account is.
    assert re.fullmatch(r"[A-Za-z0-9_-]{32}", cursor)
    assert decode_cursor(cursor) == Cursor(
        created_at=START + MINUTE, id=listed[1].id
    )


def test_cursor_from_another_list_is_only_a_position(
    client: TestClient, session: Session, alice_account: User
) -> None:
    half_a_minute = timedelta(seconds=30)
    followers = add_listed(session, "followers", alice_account, 4)
    later = START + half_a_minute
    followed = add_listed(
        session, "following", alice_account, 4, start=later, prefix="star"
    )
    # After the second follower: two minutes past the start.
    from_followers = listing(client, "followers", limit=2).json()["next_cursor"]
    made_up = encode_cursor(Cursor(created_at=START + half_a_minute, id=uuid.uuid4()))

    # Neither opens anything: each is a point in time on whatever list it
    # is presented to, and the list decides what is on it.
    on_the_other_list = listing(client, "following", cursor=from_followers)
    assert usernames(on_the_other_list) == names(followed[2:])
    assert usernames(listing(client, "followers", cursor=made_up)) == names(
        followers[3:]
    )


def test_cursor_does_not_reach_an_account_that_is_not_shown(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    hidden = add_user(session, "gone", active=False)
    connect(session, side, alice_account, hidden, at=START)
    just_after = encode_cursor(Cursor(created_at=START + MINUTE, id=hidden.id))
    exactly_at = encode_cursor(Cursor(created_at=START, id=uuid.UUID(int=2**128 - 1)))

    for cursor in (just_after, exactly_at):
        response = listing(client, side, cursor=cursor)
        assert response.status_code == 200
        assert response.json() == EMPTY


@pytest.mark.parametrize(
    "cursor",
    ["", "abc", "null", "1", "A" * 31, "A" * 33, "!" * 32, str(uuid.uuid4())],
)
def test_malformed_cursor_is_rejected(
    client: TestClient, session: Session, alice_account: User, side: str, cursor: str
) -> None:
    add_listed(session, side, alice_account, 3)

    response = listing(client, side, cursor=cursor)

    assert response.status_code == 400
    assert response.json() == INVALID_CURSOR


def test_malformed_cursor_gets_the_answer_every_list_gives(
    client: TestClient, alice_account: User, side: str
) -> None:
    answers = [
        listing(client, side, cursor="abc"),
        client.get("/users/alice/posts?cursor=abc"),
        client.get("/feed?cursor=abc"),
    ]

    assert {response.status_code for response in answers} == {400}
    assert len({response.text for response in answers}) == 1


def test_default_limit(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    add_listed(session, side, alice_account, DEFAULT_PAGE_SIZE + 3)

    body = listing(client, side).json()

    assert DEFAULT_PAGE_SIZE == 20
    assert len(body["items"]) == DEFAULT_PAGE_SIZE
    assert body["next_cursor"] is not None


def test_smallest_and_largest_limit_are_accepted(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    listed = add_listed(session, side, alice_account, MAX_PAGE_SIZE + 2)

    one = listing(client, side, limit=1).json()
    most = listing(client, side, limit=MAX_PAGE_SIZE).json()

    assert MAX_PAGE_SIZE == 50
    assert usernames(one["items"]) == names(listed[:1])
    assert one["next_cursor"] is not None
    assert usernames(most["items"]) == names(listed[:MAX_PAGE_SIZE])
    assert most["next_cursor"] is not None


@pytest.mark.parametrize("limit", [0, -1, MAX_PAGE_SIZE + 1, 1000, "abc", "", "1.5"])
def test_invalid_limit_is_rejected(
    client: TestClient, alice_account: User, side: str, limit: object
) -> None:
    response = listing(client, side, limit=limit)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "limit"]


@pytest.mark.parametrize("param", ["offset", "page", "skip", "per_page"])
def test_there_is_no_offset_pagination(
    client: TestClient, session: Session, alice_account: User, side: str, param: str
) -> None:
    listed = add_listed(session, side, alice_account, 5)

    response = client.get(f"/users/alice/{side}?limit=2&{param}=2")

    # Ignored: the answer is the first page all the same.
    assert usernames(response) == names(listed[:2])


def test_lists_are_paged_like_every_other_list(side: str) -> None:
    paths = app.openapi()["paths"]
    operation = paths[f"/users/{{username}}/{side}"]["get"]
    posts = paths["/users/{username}/posts"]["get"]

    def page_parameters(documented: dict) -> list[dict]:
        return [param for param in documented["parameters"] if param["in"] == "query"]

    assert {param["name"] for param in operation["parameters"]} == {
        "username",
        "limit",
        "cursor",
    }
    # The same limits and the same cursor as the list of posts.
    assert page_parameters(operation) == page_parameters(posts)
    answer = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert answer == {"$ref": "#/components/schemas/UserPageResponse"}


# --- what following does not change --------------------------------------


def test_following_does_not_change_the_for_you_feed(
    bob_client: TestClient, make_client, session: Session, alice_account: User
) -> None:
    carol = add_user(session, "carol")
    for number in range(6):
        author = (alice_account, carol)[number % 2]
        add_post(session, author, f"Post {number}", created_at=START + number * MINUTE)
    anonymous = make_client()
    before = bob_client.get("/feed").json()

    follow(bob_client, "alice")
    after = bob_client.get("/feed").json()

    # Still everyone's posts, newest first: not only those of the followed,
    # and not those first.
    assert after == before == anonymous.get("/feed").json()
    authors = [item["author"]["username"] for item in after["items"]]
    assert authors == ["carol", "alice"] * 3


def test_posts_are_read_the_same_with_and_without_a_follow(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account, "For everyone")

    def reads() -> list:
        return [
            bob_client.get(f"/posts/{post.id}"),
            bob_client.get("/users/alice/posts"),
            bob_client.get("/feed"),
        ]

    # Readable before the follow: nothing waits for one.
    before = reads()
    for response in before:
        assert response.status_code == 200
        assert "For everyone" in response.text

    assert follow(bob_client, "alice").json() == FOLLOWING
    following = reads()
    assert unfollow(bob_client, "alice").json() == NOT_FOLLOWING
    after = reads()

    # Who may read a post is not decided by follows, in either direction.
    for was, now, then in zip(before, following, after):
        assert was.json() == now.json() == then.json()
    assert bob_client.post(f"/posts/{post.id}/like").status_code == 200


def test_following_does_not_touch_posts_or_their_interactions(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account)
    bob_client.post(f"/posts/{post.id}/like")
    before = bob_client.get(f"/posts/{post.id}").json()

    follow(bob_client, "alice")
    unfollow(bob_client, "alice")

    assert bob_client.get(f"/posts/{post.id}").json() == before


# --- cost ----------------------------------------------------------------


def add_crowd(session: Session, account_id: uuid.UUID, count: int) -> None:
    """``count`` new users who follow the account and whom it follows."""
    for number in range(count):
        fan = add_user(session, f"fan{number:02d}")
        at = START + number * MINUTE
        session.add(Follow(follower_id=fan.id, following_id=account_id, created_at=at))
        session.add(Follow(follower_id=account_id, following_id=fan.id, created_at=at))
    session.flush()


def test_profile_takes_the_same_queries_however_many_follows_there_are(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    alice_id = alice_account.id
    anonymous, bob = make_client(), browser(make_client, "bob")
    follow(bob, "alice")
    session.expunge_all()
    with recorded_selects() as anonymous_before:
        assert profile(anonymous, "alice")["followers_count"] == 1
    with recorded_selects() as bob_before:
        assert profile(bob, "alice")["following"] is True

    add_crowd(session, alice_id, 20)
    session.expunge_all()
    with recorded_selects() as anonymous_after:
        assert counts(anonymous, "alice") == (21, 20)
    with recorded_selects() as bob_after:
        assert profile(bob, "alice")["following"] is True

    # The profile, its two counts and the reader's follow are one statement.
    # A signed-in reader adds the lookup of the session, and nothing else.
    assert len(anonymous_before) == len(anonymous_after) == 1
    assert len(bob_before) == len(bob_after) == 2


def test_list_takes_the_same_queries_however_many_users_are_on_it(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    alice_id = alice_account.id
    add_crowd(session, alice_id, 1)
    session.expunge_all()
    with recorded_selects() as with_one:
        assert len(listing(client, side, limit=50).json()["items"]) == 1

    for number in range(20):
        user = add_user(session, f"user{number:02d}")
        ids = (user.id, alice_id) if side == "followers" else (alice_id, user.id)
        session.add(Follow(follower_id=ids[0], following_id=ids[1]))
    session.flush()
    session.expunge_all()
    with recorded_selects() as with_many:
        assert len(listing(client, side, limit=50).json()["items"]) == 21

    # One to find the account, one for the page with the users on it. No
    # query per user.
    assert len(with_one) == len(with_many) == 2


def test_status_takes_the_same_queries_however_many_follows_there_are(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    alice_id = alice_account.id
    anonymous, bob = make_client(), browser(make_client, "bob")
    session.expunge_all()
    with recorded_selects() as anonymous_before:
        status(anonymous, "alice")
    with recorded_selects() as bob_before:
        status(bob, "alice")

    add_crowd(session, alice_id, 20)
    session.expunge_all()
    with recorded_selects() as anonymous_after:
        assert status(anonymous, "alice").json() == NOT_FOLLOWING
    with recorded_selects() as bob_after:
        assert status(bob, "alice").json() == NOT_FOLLOWING

    assert len(anonymous_before) == len(anonymous_after) == 1
    assert len(bob_before) == len(bob_after) == 2


def test_following_takes_the_same_queries_however_many_follows_there_are(
    bob_client: TestClient, session: Session, alice_account: User, method: str
) -> None:
    alice_id = alice_account.id
    add_user(session, "carol")
    session.expunge_all()
    with recorded_selects() as for_quiet:
        assert change(bob_client, method, "carol").status_code == 200

    add_crowd(session, alice_id, 20)
    session.expunge_all()
    with recorded_selects() as for_popular:
        assert change(bob_client, method, "alice").status_code == 200

    # The session, the account, and how things stand after the change.
    assert len(for_quiet) == len(for_popular) == 3


def test_list_is_filtered_ordered_and_cut_by_the_database(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    add_listed(session, side, alice_account, 5)
    cursor = listing(client, side, limit=2).json()["next_cursor"]
    session.expunge_all()

    with recorded_selects() as statements:
        assert len(listing(client, side, limit=2, cursor=cursor).json()["items"]) == 2

    page = " ".join(statements[-1].split())
    other = "follower_id" if side == "followers" else "following_id"
    account = "following_id" if side == "followers" else "follower_id"
    # Whose list, who is left out, from where on, in which order and how
    # many: all of it is in the query.
    assert f"FROM follows JOIN users ON users.id = follows.{other}" in page
    assert f"follows.{account} = " in page
    assert "users.is_active IS true" in page
    assert "users.email_verified_at IS NOT NULL" in page
    assert f"(follows.created_at, follows.{other}) < (" in page
    assert f" ORDER BY follows.created_at DESC, follows.{other} DESC LIMIT " in page
    assert "OFFSET" not in page


def test_list_reads_only_what_it_shows_of_each_user(
    client: TestClient, session: Session, alice_account: User, side: str
) -> None:
    # The strongest form of "not exposed": the data is never fetched.
    add_listed(session, side, alice_account, 3)
    session.expunge_all()

    with recorded_selects() as statements:
        assert listing(client, side).status_code == 200

    for statement in statements:
        assert "password_hash" not in statement
        assert not re.search(r"users\.email\b(?!_)", statement)
        assert "users.bio" not in statement
    assert "users.username" in statements[-1]


def test_counts_and_follow_state_come_with_the_profile_in_one_statement(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    session.expunge_all()

    with recorded_selects() as statements:
        profile(bob_client, "alice")

    read = " ".join(statements[-1].split())
    assert read.count("count(*)") == 2
    assert "FROM follows WHERE follows.following_id = users.id" in read
    assert "FROM follows WHERE follows.follower_id = users.id" in read
    assert "EXISTS (SELECT * FROM follows WHERE follows.follower_id =" in read
    assert "password_hash" not in read

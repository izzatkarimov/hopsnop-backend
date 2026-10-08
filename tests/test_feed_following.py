"""GET /feed/following: the posts of the accounts the caller follows."""

import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Cursor,
    decode_cursor,
    encode_cursor,
)
from app.main import app
from app.models import Post, User, UserSession
from helpers import (
    add_post,
    add_user,
    expire,
    follow,
    log_in,
    plant_session_cookie,
    recorded_selects,
    token_from,
)

POST_FIELDS = {
    "id",
    "author",
    "content",
    "parent_post_id",
    "created_at",
    "updated_at",
    "is_reply",
    "like_count",
    "liked_by_me",
    "repost_count",
    "reposted_by_me",
}
AUTHOR_FIELDS = {"id", "username", "display_name", "avatar_url"}
EMPTY = {"items": [], "next_cursor": None}
NOT_AUTHENTICATED = {"detail": "Not authenticated."}
NOT_VERIFIED = {"detail": "Email address is not verified."}
INVALID_CURSOR = {"detail": "Invalid cursor."}
START = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)
# Every reason a post is not shown to anyone.
HIDDEN = ["deleted", "inactive", "unverified"]


def following(client: TestClient, **params: object):
    return client.get("/feed/following", params=params)


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


def add_hidden_author(session: Session, kind: str) -> User:
    """An account whose posts are hidden for the reason ``kind`` names.

    For "deleted" the account itself is shown; it is its posts that are not.
    """
    return add_user(
        session,
        f"{kind}_author",
        verified=kind != "unverified",
        active=kind != "inactive",
    )


def ids(posts: list) -> list[str]:
    return [str(post["id"] if isinstance(post, dict) else post.id) for post in posts]


def walk(
    client: TestClient, *, limit: int, cursor: str | None = None
) -> list[list[dict]]:
    """Every page from ``cursor`` on, following next_cursor until there is none."""
    pages = []
    while True:
        extra = {"cursor": cursor} if cursor else {}
        response = following(client, limit=limit, **extra)
        assert response.status_code == 200, response.text
        body = response.json()
        pages.append(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) < 200, "pagination does not terminate"


@pytest.fixture
def carol(session: Session) -> User:
    return add_user(session, "carol")


@pytest.fixture
def dave(session: Session) -> User:
    return add_user(session, "dave")


# --- who may ask ---------------------------------------------------------


def test_verified_user_gets_their_following_feed(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    posts = add_posts(session, 3, alice_account)

    response = following(bob_client)

    assert response.status_code == 200
    assert ids(response.json()["items"]) == ids(posts)
    assert response.json()["next_cursor"] is None


def test_anonymous_request_is_refused(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, 2, alice_account)

    response = following(client)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


def test_stale_cookie_is_refused(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, 2, alice_account)
    plant_session_cookie(client, "left-over-from-an-old-login")

    response = following(client)

    # Unlike For You, which answers such a request as an anonymous one.
    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert client.get("/feed").status_code == 200


def test_expired_session_is_refused(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    add_posts(session, 2, alice_account)
    expire(session, session.scalars(select(UserSession)).one())

    response = following(bob_client)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


def test_revoked_session_is_refused(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    add_posts(session, 2, alice_account)
    session.scalars(select(UserSession)).one().revoked_at = datetime.now(timezone.utc)
    session.flush()

    response = following(bob_client)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


def test_session_revoked_from_another_device_is_refused(
    bob_client: TestClient,
    make_client,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    follow(session, bob_account, alice_account)
    add_posts(session, 2, alice_account)
    other_device = make_client()
    log_in(other_device, "bob")
    assert following(bob_client).status_code == 200

    assert other_device.post("/auth/sessions/revoke-others").status_code == 200

    assert following(bob_client).status_code == 401
    assert following(other_device).status_code == 200


def test_deactivated_user_is_refused(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    add_posts(session, 2, alice_account)
    bob_account.is_active = False
    session.flush()

    response = following(bob_client)

    # The same answer as for no session at all.
    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


def test_session_of_an_unverified_account_is_refused(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    # Not reachable by logging in. Should a session ever belong to an
    # unverified account all the same, the feed is still refused.
    follow(session, bob_account, alice_account)
    add_posts(session, 2, alice_account)
    bob_account.email_verified_at = None
    session.flush()

    response = following(bob_client)

    assert response.status_code == 403
    assert response.json() == NOT_VERIFIED
    assert "Post" not in response.text


def test_logging_out_ends_access(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    add_posts(session, 2, alice_account)
    assert following(bob_client).status_code == 200

    bob_client.post("/auth/logout")

    assert following(bob_client).status_code == 401


def test_password_reset_ends_the_sessions_that_could_read_the_feed(
    bob_client: TestClient,
    make_client,
    session: Session,
    alice_account: User,
    bob_account: User,
    outbox,
) -> None:
    follow(session, bob_account, alice_account)
    add_posts(session, 2, alice_account)
    assert following(bob_client).status_code == 200
    anonymous = make_client()
    anonymous.post("/auth/forgot-password", json={"email": "bob@example.com"})
    reset = anonymous.post(
        "/auth/reset-password",
        json={
            "token": token_from(outbox.password_reset[0][1]),
            "new_password": "a completely new passphrase",
        },
    )
    assert reset.status_code == 200

    assert following(bob_client).status_code == 401


def test_refusal_comes_before_anything_about_the_request_is_looked_at(
    client: TestClient,
) -> None:
    # Who is asking is settled first: a request without a session is not
    # told whether its cursor or its limit would have been accepted.
    for params in ({"cursor": "abc"}, {"limit": 0}, {"limit": 10**9}):
        response = following(client, **params)

        assert response.status_code == 401, params
        assert response.json() == NOT_AUTHENTICATED


def test_following_feed_responses_are_not_to_be_cached(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    empty = following(bob_client)
    follow(session, bob_account, alice_account)
    add_posts(session, 3, alice_account)
    first = following(bob_client, limit=2)
    second = following(bob_client, limit=2, cursor=first.json()["next_cursor"])

    for response in (empty, first, second):
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"


# --- whose posts are in it -----------------------------------------------


def test_posts_of_one_followed_author(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, bob_account, alice_account)
    alices = add_posts(session, 3, alice_account)
    add_posts(session, 3, carol, start=START + timedelta(seconds=30))

    items = following(bob_client).json()["items"]

    assert ids(items) == ids(alices)
    assert {item["author"]["username"] for item in items} == {"alice"}


def test_posts_of_several_followed_authors_are_mixed_by_time(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
    dave: User,
) -> None:
    follow(session, bob_account, alice_account)
    follow(session, bob_account, carol)
    posts = add_posts(session, 9, alice_account, carol, dave)

    items = following(bob_client).json()["items"]

    expected = [post for post in posts if post.author_id != dave.id]
    assert ids(items) == ids(expected)
    authors = [item["author"]["username"] for item in items]
    assert authors == ["carol", "alice"] * 3


def test_user_who_follows_nobody_gets_an_empty_page(
    bob_client: TestClient, session: Session, alice_account: User, carol: User
) -> None:
    add_posts(session, 4, alice_account, carol)

    response = following(bob_client)

    assert response.status_code == 200
    assert response.json() == EMPTY


def test_empty_database_is_an_empty_page(bob_client: TestClient) -> None:
    response = following(bob_client)

    assert response.status_code == 200
    assert response.json() == EMPTY


def test_followed_authors_without_posts_give_an_empty_page(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
    dave: User,
) -> None:
    follow(session, bob_account, alice_account)
    follow(session, bob_account, carol)
    add_posts(session, 3, dave)

    assert following(bob_client).json() == EMPTY


def test_own_posts_are_not_in_it(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    add_posts(session, 3, bob_account)
    alices = add_posts(session, 2, alice_account, start=START + timedelta(seconds=30))

    assert ids(following(bob_client).json()["items"]) == ids(alices)


def test_user_cannot_put_their_own_posts_in_it_by_following_themselves(
    bob_client: TestClient, session: Session, bob_account: User
) -> None:
    add_posts(session, 2, bob_account)

    assert bob_client.post("/users/bob/follow").status_code == 400

    assert following(bob_client).json() == EMPTY


def test_post_published_through_the_api_reaches_the_followers_feed(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    follow(session, bob_account, alice_account)

    created = alice_client.post("/posts", json={"content": "Fresh"}).json()

    assert ids(following(bob_client).json()["items"]) == [created["id"]]
    # Not in the author's own: she does not follow herself.
    assert following(alice_client).json() == EMPTY


def test_following_goes_one_way(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    follow(session, bob_account, alice_account)
    alices = add_posts(session, 2, alice_account)
    add_posts(session, 2, bob_account, start=START + timedelta(seconds=30))

    # Bob follows alice. That shows alice nothing of bob.
    assert ids(following(bob_client).json()["items"]) == ids(alices)
    assert following(alice_client).json() == EMPTY


def test_followers_of_the_same_account_see_the_same_posts(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, alice_account, carol)
    follow(session, bob_account, carol)
    posts = add_posts(session, 4, carol)

    for viewer in (alice_client, bob_client):
        assert ids(following(viewer).json()["items"]) == ids(posts)


def test_each_viewers_own_follows_decide_and_no_one_elses(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
    dave: User,
) -> None:
    follow(session, alice_account, carol)
    follow(session, bob_account, dave)
    # Whom the followed account follows, and who follows the viewer, add
    # nothing.
    follow(session, carol, dave)
    follow(session, dave, alice_account)
    posts = add_posts(session, 6, carol, dave)
    carols = [post for post in posts if post.author_id == carol.id]
    daves = [post for post in posts if post.author_id == dave.id]

    assert ids(following(alice_client).json()["items"]) == ids(carols)
    assert ids(following(bob_client).json()["items"]) == ids(daves)


def test_repost_by_a_followed_account_puts_nothing_in_it(
    bob_client: TestClient,
    make_client,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, bob_account, alice_account)
    carols = add_post(session, carol, "By carol")
    alice = make_client()
    log_in(alice, "alice")
    assert alice.post(f"/posts/{carols.id}/repost").status_code == 200
    assert alice.post(f"/posts/{carols.id}/like").status_code == 200

    # A repost is counted on the post and nothing more.
    assert following(bob_client).json() == EMPTY


def test_items_are_posts_in_their_usual_shape(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    post = add_post(session, alice_account, "Hello", created_at=START)

    body = following(bob_client).json()

    assert set(body) == {"items", "next_cursor"}
    [item] = body["items"]
    assert set(item) == POST_FIELDS
    assert set(item["author"]) == AUTHOR_FIELDS
    assert item == bob_client.get(f"/posts/{post.id}").json()
    assert item == bob_client.get("/feed").json()["items"][0]


# --- follows are read at the moment of asking ----------------------------


def test_following_an_account_brings_in_the_posts_it_already_has(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, 3, alice_account)
    assert following(bob_client).json() == EMPTY

    assert bob_client.post("/users/alice/follow").json() == {"following": True}

    assert ids(following(bob_client).json()["items"]) == ids(posts)


def test_unfollowing_takes_out_old_and_new_posts_at_once(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, bob_account, alice_account)
    follow(session, bob_account, carol)
    add_posts(session, 3, alice_account)
    carols = add_posts(session, 2, carol, start=START + timedelta(seconds=30))
    assert len(following(bob_client).json()["items"]) == 5

    assert bob_client.delete("/users/alice/follow").json() == {"following": False}
    alice_client.post("/posts", json={"content": "Written after the unfollow"})

    assert ids(following(bob_client).json()["items"]) == ids(carols)


def test_following_again_restores_the_posts_that_are_shown_then(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    follow(session, bob_account, alice_account)
    kept, removed = add_posts(session, 2, alice_account)
    bob_client.delete("/users/alice/follow")
    # While not followed: one post is deleted and one is written.
    assert alice_client.delete(f"/posts/{removed.id}").status_code == 204
    written = alice_client.post("/posts", json={"content": "Meanwhile"}).json()
    assert following(bob_client).json() == EMPTY

    bob_client.post("/users/alice/follow")

    assert ids(following(bob_client).json()["items"]) == [written["id"], str(kept.id)]


def test_unfollowing_between_pages_ends_that_authors_posts(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, bob_account, alice_account)
    follow(session, bob_account, carol)
    posts = add_posts(session, 8, alice_account, carol)
    first = following(bob_client, limit=2).json()

    bob_client.delete("/users/alice/follow")
    pages = walk(bob_client, limit=2, cursor=first["next_cursor"])
    rest = [item for page in pages for item in page]

    carols_left = [post for post in posts[2:] if post.author_id == carol.id]
    assert ids(rest) == ids(carols_left)


def test_following_between_pages_causes_no_duplicates(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, bob_account, alice_account)
    posts = add_posts(session, 8, alice_account, carol)
    first = following(bob_client, limit=2).json()
    last_seen = decode_cursor(first["next_cursor"]).created_at

    bob_client.post("/users/carol/follow")
    pages = walk(bob_client, limit=2, cursor=first["next_cursor"])
    rest = [item for page in pages for item in page]

    # From the position reached on: everything older, carol's now included.
    assert ids(rest) == ids([post for post in posts if post.created_at < last_seen])
    assert not set(ids(rest)) & set(ids(first["items"]))


# --- posts that are not shown to anyone ----------------------------------


@pytest.mark.parametrize("kind", HIDDEN)
def test_hidden_post_of_a_followed_account_is_not_in_it(
    bob_client: TestClient, session: Session, bob_account: User, kind: str
) -> None:
    author = add_hidden_author(session, kind)
    follow(session, bob_account, author)
    add_post(session, author, "Hidden", deleted=kind == "deleted")

    response = following(bob_client)

    assert response.status_code == 200
    assert response.json() == EMPTY


def test_deleted_post_leaves_and_the_authors_other_posts_stay(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    follow(session, bob_account, alice_account)
    newest, middle, oldest = add_posts(session, 3, alice_account)

    assert alice_client.delete(f"/posts/{middle.id}").status_code == 204

    assert ids(following(bob_client).json()["items"]) == ids([newest, oldest])


def test_posts_leave_with_a_deactivation_and_return_with_a_reactivation(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    posts = add_posts(session, 2, alice_account)

    alice_account.is_active = False
    session.flush()
    assert following(bob_client).json() == EMPTY

    alice_account.is_active = True
    session.flush()
    assert ids(following(bob_client).json()["items"]) == ids(posts)


@pytest.mark.parametrize("kind", HIDDEN)
def test_hidden_post_does_not_take_up_room_on_a_page(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: str,
) -> None:
    author = add_hidden_author(session, kind)
    follow(session, bob_account, alice_account)
    follow(session, bob_account, author)
    posts = add_posts(session, 4, alice_account)
    for number in range(4):
        add_post(
            session,
            author,
            "Hidden",
            created_at=START + number * MINUTE + timedelta(seconds=30),
            deleted=kind == "deleted",
        )

    body = following(bob_client, limit=3).json()

    assert ids(body["items"]) == ids(posts[:3])
    rest = following(bob_client, limit=3, cursor=body["next_cursor"]).json()
    assert ids(rest["items"]) == ids(posts[3:])


def test_post_of_an_account_that_is_not_followed_takes_up_no_room_either(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, bob_account, alice_account)
    posts = add_posts(session, 12, alice_account, carol, carol)
    alices = [post for post in posts if post.author_id == alice_account.id]

    body = following(bob_client, limit=4).json()

    assert ids(body["items"]) == ids(alices)
    # The page was exactly filled by what there is: nothing more follows.
    assert body["next_cursor"] is None


@pytest.mark.parametrize("kind", HIDDEN)
def test_page_is_the_last_one_when_only_hidden_posts_remain(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: str,
) -> None:
    author = add_hidden_author(session, kind)
    follow(session, bob_account, alice_account)
    follow(session, bob_account, author)
    posts = add_posts(session, 2, alice_account)
    add_post(
        session, author, "Hidden", created_at=START - MINUTE, deleted=kind == "deleted"
    )

    body = following(bob_client, limit=2).json()

    assert ids(body["items"]) == ids(posts)
    assert body["next_cursor"] is None


def test_feed_holds_exactly_the_posts_that_pass_every_rule(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
    dave: User,
) -> None:
    inactive = add_hidden_author(session, "inactive")
    unverified = add_hidden_author(session, "unverified")
    for followed in (alice_account, carol, inactive, unverified):
        follow(session, bob_account, followed)
    shown = [
        add_post(session, alice_account, "A", created_at=START),
        add_post(session, carol, "C", created_at=START + MINUTE),
    ]
    reply = add_post(
        session, carol, "Reply", parent=shown[0], created_at=START + 2 * MINUTE
    )
    add_post(session, alice_account, "Deleted", deleted=True)
    add_post(session, inactive, "Inactive")
    add_post(session, unverified, "Unverified")
    add_post(session, dave, "Not followed")
    add_post(session, bob_account, "Own")

    items = following(bob_client).json()["items"]

    assert ids(items) == ids([reply, shown[1], shown[0]])


# --- replies -------------------------------------------------------------


def test_reply_by_a_followed_author_is_in_it_at_its_own_time(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    start = add_post(session, alice_account, "Start", created_at=START)
    later = add_post(session, alice_account, "Later", created_at=START + MINUTE)
    reply = add_post(
        session, alice_account, "Reply", parent=start, created_at=START + 2 * MINUTE
    )

    items = following(bob_client).json()["items"]

    assert ids(items) == ids([reply, later, start])
    assert items[0]["parent_post_id"] == str(start.id)
    assert items[0]["is_reply"] is True


def test_followed_authors_reply_to_someone_who_is_not_followed_is_in_it(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, bob_account, alice_account)
    carols = add_post(session, carol, "By carol", created_at=START)
    reply = add_post(
        session, alice_account, "Answer", parent=carols, created_at=START + MINUTE
    )

    items = following(bob_client).json()["items"]

    # The reply, by its own author. The post it answers is named and not
    # brought along.
    assert ids(items) == [str(reply.id)]
    assert items[0]["parent_post_id"] == str(carols.id)
    assert "By carol" not in str(items)


def test_reply_by_someone_who_is_not_followed_is_not_in_it(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, bob_account, alice_account)
    alices = add_post(session, alice_account, "By alice", created_at=START)
    add_post(session, carol, "Carol answers", parent=alices, created_at=START + MINUTE)
    add_post(session, bob_account, "Bob answers", parent=alices)

    # Answering a followed account's post does not make one followed.
    assert ids(following(bob_client).json()["items"]) == [str(alices.id)]


@pytest.mark.parametrize("kind", HIDDEN)
def test_hidden_reply_of_a_followed_account_is_not_in_it(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: str,
) -> None:
    author = add_hidden_author(session, kind)
    follow(session, bob_account, alice_account)
    follow(session, bob_account, author)
    alices = add_post(session, alice_account, "By alice", created_at=START)
    add_post(session, author, "Hidden", parent=alices, deleted=kind == "deleted")

    response = following(bob_client)

    assert ids(response.json()["items"]) == [str(alices.id)]
    assert "Hidden" not in response.text


def test_deleted_parent_is_not_exposed_through_its_reply(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, bob_account, alice_account)
    follow(session, bob_account, carol)
    parent = add_post(
        session, carol, "Deleted parent", created_at=START, deleted=True
    )
    reply = add_post(
        session, alice_account, "Reply", parent=parent, created_at=START + MINUTE
    )

    response = following(bob_client)

    # As in For You: the reply is a post of its own and stays. Of the post
    # it answers, only the id it always carried is shown.
    assert ids(response.json()["items"]) == [str(reply.id)]
    assert response.json()["items"][0]["parent_post_id"] == str(parent.id)
    assert "Deleted parent" not in response.text
    assert response.json()["items"] == bob_client.get("/feed").json()["items"]


@pytest.mark.parametrize("kind", ["inactive", "unverified"])
def test_reply_stays_when_its_parents_author_is_no_longer_shown(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    kind: str,
) -> None:
    author = add_hidden_author(session, kind)
    follow(session, bob_account, alice_account)
    follow(session, bob_account, author)
    parent = add_post(session, author, "Hidden parent", created_at=START)
    reply = add_post(session, alice_account, "Reply", parent=parent)

    response = following(bob_client)

    assert ids(response.json()["items"]) == [str(reply.id)]
    assert "Hidden parent" not in response.text


# --- likes and reposts ---------------------------------------------------


def test_counts_and_the_viewers_own_state_are_on_each_post(
    alice_client: TestClient,
    bob_client: TestClient,
    make_client,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, bob_account, alice_account)
    liked, reposted, untouched = add_posts(session, 3, alice_account)
    carol_client = make_client()
    log_in(carol_client, "carol")
    bob_client.post(f"/posts/{liked.id}/like")
    carol_client.post(f"/posts/{liked.id}/like")
    alice_client.post(f"/posts/{liked.id}/like")
    bob_client.post(f"/posts/{reposted.id}/repost")
    carol_client.post(f"/posts/{untouched.id}/repost")

    items = {item["id"]: item for item in following(bob_client).json()["items"]}

    def state(post: Post) -> tuple:
        item = items[str(post.id)]
        return (
            item["like_count"],
            item["liked_by_me"],
            item["repost_count"],
            item["reposted_by_me"],
        )

    assert state(liked) == (3, True, 0, False)
    assert state(reposted) == (0, False, 1, True)
    # Someone else's repost is counted and is not "mine".
    assert state(untouched) == (0, False, 1, False)


def test_interaction_changes_show_at_once(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    post = add_post(session, alice_account)

    def state() -> tuple:
        [item] = following(bob_client).json()["items"]
        return (
            item["like_count"],
            item["liked_by_me"],
            item["repost_count"],
            item["reposted_by_me"],
        )

    assert state() == (0, False, 0, False)
    bob_client.post(f"/posts/{post.id}/like")
    bob_client.post(f"/posts/{post.id}/repost")
    assert state() == (1, True, 1, True)
    bob_client.delete(f"/posts/{post.id}/like")
    assert state() == (0, False, 1, True)
    bob_client.delete(f"/posts/{post.id}/repost")
    assert state() == (0, False, 0, False)


def test_by_me_is_each_viewers_own(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, alice_account, carol)
    follow(session, bob_account, carol)
    post = add_post(session, carol)
    bob_client.post(f"/posts/{post.id}/like")

    [for_bob] = following(bob_client).json()["items"]
    [for_alice] = following(alice_client).json()["items"]

    assert (for_bob["like_count"], for_bob["liked_by_me"]) == (1, True)
    assert (for_alice["like_count"], for_alice["liked_by_me"]) == (1, False)


# --- order ---------------------------------------------------------------


def test_order_is_by_creation_not_by_insertion_or_last_edit(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    clock,
) -> None:
    follow(session, bob_account, alice_account)
    first = alice_client.post("/posts", json={"content": "First"}).json()
    clock.advance(minutes=1)
    second = alice_client.post("/posts", json={"content": "Second"}).json()
    clock.advance(minutes=1)
    alice_client.patch(f"/posts/{first['id']}", json={"content": "First, edited"})
    backdated = add_post(
        session, alice_account, "Backdated", created_at=clock.now - timedelta(days=1)
    )

    items = following(bob_client).json()["items"]

    assert ids(items) == [second["id"], first["id"], str(backdated.id)]


def test_posts_created_at_the_same_instant_are_ordered_by_id(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, bob_account, alice_account)
    follow(session, bob_account, carol)
    posts = [
        add_post(session, author, f"Tie {number}", created_at=START)
        for number, author in enumerate([alice_account, carol] * 4)
    ]
    expected = sorted((str(post.id) for post in posts), key=uuid.UUID, reverse=True)

    assert ids(following(bob_client).json()["items"]) == expected
    assert ids(following(bob_client).json()["items"]) == expected


# --- pagination ----------------------------------------------------------


def test_first_next_and_last_page(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    posts = add_posts(session, 5, alice_account)

    first = following(bob_client, limit=2).json()
    second = following(bob_client, limit=2, cursor=first["next_cursor"]).json()
    third = following(bob_client, limit=2, cursor=second["next_cursor"]).json()

    assert ids(first["items"]) == ids(posts[:2])
    assert ids(second["items"]) == ids(posts[2:4])
    assert ids(third["items"]) == ids(posts[4:])
    assert first["next_cursor"] and second["next_cursor"]
    assert third["next_cursor"] is None


def test_page_that_exactly_holds_the_rest_has_no_cursor(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    posts = add_posts(session, 4, alice_account)

    whole = following(bob_client, limit=4).json()
    first = following(bob_client, limit=2).json()
    second = following(bob_client, limit=2, cursor=first["next_cursor"]).json()

    assert ids(whole["items"]) == ids(posts)
    assert whole["next_cursor"] is None
    assert ids(second["items"]) == ids(posts[2:])
    assert second["next_cursor"] is None


@pytest.mark.parametrize("limit", [1, 2, 3, 5, 50])
def test_walking_the_pages_returns_every_followed_post_exactly_once_in_order(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
    dave: User,
    limit: int,
) -> None:
    inactive = add_hidden_author(session, "inactive")
    for followed in (alice_account, carol, inactive):
        follow(session, bob_account, followed)
    posts = add_posts(session, 24, alice_account, carol, dave, inactive)
    expected = [
        post for post in posts if post.author_id in (alice_account.id, carol.id)
    ]

    pages = walk(bob_client, limit=limit)

    walked = [item for page in pages for item in page]
    assert ids(walked) == ids(expected)
    assert len(set(ids(walked))) == 12
    assert all(len(page) == limit for page in pages[:-1])
    assert len(pages) == -(-12 // limit)


@pytest.mark.parametrize("limit", [1, 2, 3, 5])
def test_no_duplicates_or_gaps_among_posts_created_at_the_same_instant(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
    limit: int,
) -> None:
    follow(session, bob_account, alice_account)
    newer = add_post(session, alice_account, "Newer", created_at=START + MINUTE)
    tied = [
        add_post(session, alice_account, f"Tie {number}", created_at=START)
        for number in range(7)
    ]
    # At the same instant and not followed: in between, and never returned.
    for number in range(7):
        add_post(session, carol, f"Other {number}", created_at=START)
    older = add_post(session, alice_account, "Older", created_at=START - MINUTE)
    expected = [
        str(newer.id),
        *sorted((str(post.id) for post in tied), key=uuid.UUID, reverse=True),
        str(older.id),
    ]

    walked = [item for page in walk(bob_client, limit=limit) for item in page]

    assert ids(walked) == expected


def test_many_posts_by_one_followed_author(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, bob_account, alice_account)
    follow(session, bob_account, carol)
    alices = add_posts(session, 130, alice_account)
    carols = add_post(session, carol, "One", created_at=START - MINUTE)

    assert len(following(bob_client).json()["items"]) == DEFAULT_PAGE_SIZE
    pages = walk(bob_client, limit=MAX_PAGE_SIZE)

    assert [len(page) for page in pages] == [50, 50, 31]
    walked = [item for page in pages for item in page]
    assert ids(walked) == ids([*alices, carols])


def test_the_same_cursor_always_gives_the_same_page(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    add_posts(session, 6, alice_account)
    cursor = following(bob_client, limit=2).json()["next_cursor"]

    answers = [following(bob_client, limit=2, cursor=cursor).json() for _ in range(3)]

    assert answers[0] == answers[1] == answers[2]


def test_new_post_between_requests_causes_no_duplicates_or_gaps(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    follow(session, bob_account, alice_account)
    posts = add_posts(session, 5, alice_account)
    first = following(bob_client, limit=2).json()

    alice_client.post("/posts", json={"content": "Brand new"})
    pages = walk(bob_client, limit=2, cursor=first["next_cursor"])
    rest = [item for page in pages for item in page]

    assert ids([*first["items"], *rest]) == ids(posts)


def test_cursor_is_the_position_of_the_last_post_shown(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    posts = add_posts(session, 3, alice_account)

    cursor = following(bob_client, limit=2).json()["next_cursor"]

    # Nothing in it that the page did not show.
    assert decode_cursor(cursor) == Cursor(
        created_at=posts[1].created_at, id=posts[1].id
    )


def test_cursor_is_the_one_for_you_uses(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, bob_account, alice_account)
    posts = add_posts(session, 8, alice_account, carol)
    alices = [post for post in posts if post.author_id == alice_account.id]
    # From For You, where the first two posts are carol's and alice's.
    for_you = bob_client.get("/feed?limit=2").json()["next_cursor"]
    own = following(bob_client, limit=1).json()["next_cursor"]

    in_following = following(bob_client, cursor=for_you).json()
    in_for_you = bob_client.get("/feed", params={"cursor": own}).json()

    # A position in time in both, and each feed's own posts from there on.
    assert ids(in_following["items"]) == ids(alices[1:])
    assert ids(in_for_you["items"]) == ids(posts[2:])
    assert re.fullmatch(r"[A-Za-z0-9_-]{32}", own)


# --- a cursor is not a key -----------------------------------------------


def test_forged_cursor_cannot_reach_a_post_of_an_account_that_is_not_followed(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, bob_account, alice_account)
    alices = add_post(session, alice_account, "Followed", created_at=START - MINUTE)
    hidden = add_post(session, carol, "Not followed", created_at=START)
    cursors = [
        encode_cursor(Cursor(created_at=START + timedelta(seconds=1), id=hidden.id)),
        # The same instant, and an id that sorts after every other.
        encode_cursor(Cursor(created_at=START, id=uuid.UUID(int=2**128 - 1))),
        encode_cursor(Cursor(created_at=START, id=hidden.id)),
    ]

    for cursor in cursors:
        response = following(bob_client, cursor=cursor)
        assert response.status_code == 200
        assert ids(response.json()["items"]) == [str(alices.id)]
        assert "Not followed" not in response.text


@pytest.mark.parametrize("kind", HIDDEN)
def test_forged_cursor_cannot_reach_a_hidden_post(
    bob_client: TestClient, session: Session, bob_account: User, kind: str
) -> None:
    author = add_hidden_author(session, kind)
    follow(session, bob_account, author)
    hidden = add_post(
        session, author, "Hidden", created_at=START, deleted=kind == "deleted"
    )
    cursors = [
        encode_cursor(Cursor(created_at=START + timedelta(seconds=1), id=hidden.id)),
        encode_cursor(Cursor(created_at=START, id=uuid.UUID(int=2**128 - 1))),
    ]

    for cursor in cursors:
        response = following(bob_client, cursor=cursor)
        assert response.status_code == 200
        assert response.json() == EMPTY


def test_well_formed_cursor_that_was_never_issued_is_just_a_position(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    posts = add_posts(session, 4, alice_account)
    between = encode_cursor(
        Cursor(created_at=posts[1].created_at - timedelta(seconds=30), id=uuid.uuid4())
    )
    before_all = encode_cursor(Cursor(created_at=START - MINUTE, id=uuid.uuid4()))
    after_all = encode_cursor(
        Cursor(created_at=START + timedelta(days=365), id=uuid.uuid4())
    )

    assert ids(following(bob_client, cursor=between).json()["items"]) == ids(posts[2:])
    assert following(bob_client, cursor=before_all).json() == EMPTY
    assert ids(following(bob_client, cursor=after_all).json()["items"]) == ids(posts)


def test_another_viewers_cursor_gives_only_ones_own_feed(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
    dave: User,
) -> None:
    follow(session, alice_account, carol)
    follow(session, bob_account, dave)
    posts = add_posts(session, 8, carol, dave)
    alices_cursor = following(alice_client, limit=1).json()["next_cursor"]
    position = decode_cursor(alices_cursor).created_at

    items = following(bob_client, cursor=alices_cursor).json()["items"]

    assert {item["author"]["username"] for item in items} == {"dave"}
    assert ids(items) == ids(
        [p for p in posts if p.author_id == dave.id and p.created_at < position]
    )


# --- malformed requests --------------------------------------------------


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "abc",
        "null",
        "0",
        "A" * 31,
        "A" * 33,
        "!" * 32,
        "A" * 31 + "=",
        "A" * 31 + "+",
        "é" * 32,
        "2026-10-01T12:00:00Z",
        str(uuid.uuid4()),
        "' OR '1'='1",
        "A" * 5000,
    ],
)
def test_malformed_cursor_is_rejected(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    cursor: str,
) -> None:
    follow(session, bob_account, alice_account)
    add_posts(session, 3, alice_account)

    response = following(bob_client, cursor=cursor)

    assert response.status_code == 400
    assert response.json() == INVALID_CURSOR


def test_malformed_cursor_gets_the_answer_for_you_gives(
    bob_client: TestClient,
) -> None:
    here = following(bob_client, cursor="something-the-client-sent")
    there = bob_client.get("/feed?cursor=something-the-client-sent")

    assert here.status_code == there.status_code == 400
    assert here.text == there.text
    assert "something" not in here.text


def test_default_limit_and_maximum_are_those_of_for_you(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    add_posts(session, MAX_PAGE_SIZE + 5, alice_account)

    assert len(following(bob_client).json()["items"]) == DEFAULT_PAGE_SIZE
    assert len(bob_client.get("/feed").json()["items"]) == DEFAULT_PAGE_SIZE
    assert len(following(bob_client, limit=MAX_PAGE_SIZE).json()["items"]) == 50
    assert len(following(bob_client, limit=1).json()["items"]) == 1


@pytest.mark.parametrize(
    "limit", [0, -1, MAX_PAGE_SIZE + 1, 1000, 10**30, "abc", "", "1.5", "5; DROP"]
)
def test_invalid_limit_is_rejected_as_for_you_rejects_it(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    limit: object,
) -> None:
    follow(session, bob_account, alice_account)
    add_posts(session, 3, alice_account)

    here = following(bob_client, limit=limit)
    there = bob_client.get("/feed", params={"limit": limit})

    assert here.status_code == there.status_code == 422
    assert here.json() == there.json()
    assert "Post" not in here.text


@pytest.mark.parametrize("param", ["offset", "page", "skip", "page_size", "per_page"])
def test_there_is_no_offset_pagination(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    param: str,
) -> None:
    follow(session, bob_account, alice_account)
    posts = add_posts(session, 5, alice_account)

    response = following(bob_client, limit=2, **{param: 2})

    # Ignored: the answer is the first page all the same.
    assert ids(response.json()["items"]) == ids(posts[:2])


def test_feed_is_paged_and_answered_like_for_you() -> None:
    paths = app.openapi()["paths"]
    here = paths["/feed/following"]["get"]
    there = paths["/feed"]["get"]

    def answer(operation: dict) -> dict:
        return operation["responses"]["200"]["content"]["application/json"]["schema"]

    # The same limits and the same cursor, and the same page of the same
    # posts. A caller can send nothing else: there is no parameter to name
    # a user with.
    assert here["parameters"] == there["parameters"]
    assert {param["name"] for param in here["parameters"]} == {"limit", "cursor"}
    assert answer(here) == answer(there)
    assert answer(here) == {"$ref": "#/components/schemas/PostPageResponse"}
    assert "requestBody" not in here


# --- cost ----------------------------------------------------------------


def test_feed_takes_the_same_queries_however_much_there_is(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    add_post(session, alice_account)
    # Like a real request, which starts with nothing loaded.
    session.expunge_all()
    with recorded_selects() as with_one:
        assert len(following(bob_client, limit=50).json()["items"]) == 1

    bob = session.get(User, bob_account.id)
    authors = [add_user(session, f"author{number}") for number in range(8)]
    for author in authors:
        follow(session, bob, author)
    posts = add_posts(session, 40, *authors)
    for post in posts[:10]:
        bob_client.post(f"/posts/{post.id}/like")
    session.expunge_all()
    with recorded_selects() as with_many:
        items = following(bob_client, limit=50).json()["items"]

    assert len(items) == 41
    assert len({item["author"]["id"] for item in items}) == 9
    assert sum(item["liked_by_me"] for item in items) == 10
    # One to find the session and its user, one for the page with its
    # authors, counts and follows: no query per post, author or follow.
    assert len(with_one) == len(with_many) == 2


def test_empty_feed_takes_the_same_queries(
    bob_client: TestClient, session: Session
) -> None:
    session.expunge_all()
    with recorded_selects() as statements:
        assert following(bob_client).json() == EMPTY

    assert len(statements) == 2


def test_feed_is_filtered_ordered_and_cut_by_the_database(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    follow(session, bob_account, alice_account)
    add_posts(session, 10, alice_account, carol)
    cursor = following(bob_client, limit=2).json()["next_cursor"]
    session.expunge_all()

    with recorded_selects() as statements:
        assert len(following(bob_client, limit=2, cursor=cursor).json()["items"]) == 2

    _session_lookup, statement = statements
    sql = " ".join(statement.split())
    # Whose posts, which of them, from where on, in which order and how
    # many: all of it is in the query, so no row is fetched only to be
    # thrown away.
    for condition in (
        "posts.deleted_at IS NULL",
        "users.is_active IS true",
        "users.email_verified_at IS NOT NULL",
        "EXISTS (SELECT * FROM follows WHERE follows.follower_id = ",
        "AND follows.following_id = posts.author_id)",
        "(posts.created_at, posts.id) < (",
    ):
        assert condition in sql, condition
    assert "private" not in sql.lower()
    assert " ORDER BY posts.created_at DESC, posts.id DESC LIMIT " in sql
    assert "OFFSET" not in sql
    assert set(re.findall(r"(?:FROM|JOIN) (\w+)", sql)) == {
        "posts",
        "users",
        "likes",
        "reposts",
        "follows",
    }


def test_feed_does_not_read_the_authors_private_columns(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    follow(session, bob_account, alice_account)
    add_posts(session, 3, alice_account)
    session.expunge_all()

    with recorded_selects() as statements:
        assert following(bob_client).status_code == 200

    statement = statements[-1]
    assert "users.username" in statement
    assert "password_hash" not in statement
    assert not re.search(r"users\.email\b(?!_)", statement)
    assert "users.bio" not in statement


# --- For You is what it was ----------------------------------------------


def test_for_you_is_untouched_by_the_following_feed(
    bob_client: TestClient,
    client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    posts = add_posts(session, 6, alice_account, carol, bob_account)
    before = bob_client.get("/feed").json()

    follow(session, bob_account, alice_account)
    following(bob_client)

    # Everyone's posts, the viewer's own included, with or without a
    # session and whoever is followed.
    assert bob_client.get("/feed").json() == before
    assert ids(before["items"]) == ids(posts)
    assert ids(client.get("/feed").json()["items"]) == ids(posts)

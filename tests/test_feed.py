"""GET /feed: the For You feed, with cursor pagination."""

import base64
import re
import struct
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
INVALID_CURSOR = {"detail": "Invalid cursor."}
START = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)
# Well formed in every respect, but its time lies beyond the year 9999.
IMPOSSIBLE_TIME_CURSOR = base64.urlsafe_b64encode(
    struct.pack(">q16s", 2**63 - 1, bytes(16))
).decode("ascii")
# Every reason a post is kept out of the feed.
HIDDEN = ["deleted", "inactive", "unverified"]


def feed(client: TestClient, **params: object):
    return client.get("/feed", params=params)


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


def add_hidden_post(
    session: Session,
    kind: str,
    content: str = "Hidden",
    *,
    created_at: datetime | None = None,
    parent: Post | None = None,
) -> Post:
    """A post the feed must not show, for the reason ``kind`` names.

    The posts hidden for one reason share an author, who has no other posts.
    """
    username = f"{kind}_author"
    author = session.scalar(select(User).where(User.username == username))
    if author is None:
        author = add_user(
            session,
            username,
            verified=kind != "unverified",
            active=kind != "inactive",
        )
    return add_post(
        session,
        author,
        content,
        parent=parent,
        created_at=created_at,
        deleted=kind == "deleted",
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
        response = feed(client, limit=limit, **extra)
        assert response.status_code == 200, response.text
        body = response.json()
        pages.append(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) < 200, "pagination does not terminate"


# --- the feed ------------------------------------------------------------


def test_feed_returns_posts_newest_first(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    oldest = add_post(session, alice_account, "Oldest", created_at=START)
    middle = add_post(session, bob_account, "Middle", created_at=START + MINUTE)
    newest = add_post(session, alice_account, "Newest", created_at=START + 2 * MINUTE)

    response = feed(client)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "next_cursor"}
    assert ids(body["items"]) == ids([newest, middle, oldest])
    assert [item["content"] for item in body["items"]] == ["Newest", "Middle", "Oldest"]
    assert body["next_cursor"] is None


def test_empty_feed_is_an_empty_list(client: TestClient) -> None:
    response = feed(client)

    assert response.status_code == 200
    assert response.json() == EMPTY


def test_feed_needs_no_authentication(
    client: TestClient, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, 2, alice_account)
    assert client.cookies.get("__Host-hopsnop_session") is None

    response = feed(client)

    assert response.status_code == 200
    assert ids(response.json()["items"]) == ids(posts)


def test_authenticated_user_gets_the_feed(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, 2, alice_account)

    response = feed(bob_client)

    assert response.status_code == 200
    assert ids(response.json()["items"]) == ids(posts)


def test_feed_is_the_same_for_every_viewer(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    add_posts(session, 3, alice_account)
    add_posts(session, 2, bob_account, start=START + timedelta(seconds=30))
    carol = add_user(session, "carol")
    add_post(session, carol, "Carol's post", created_at=START - MINUTE)
    anonymous, alice, bob, carols = (make_client() for _ in range(4))
    for viewer, name in ((alice, "alice"), (bob, "bob"), (carols, "carol")):
        assert log_in(viewer, name).status_code == 200

    seen = [feed(viewer).json() for viewer in (anonymous, alice, bob, carols)]

    assert seen[0] == seen[1] == seen[2] == seen[3]
    assert len(seen[0]["items"]) == 6


def test_feed_mixes_the_posts_of_all_accounts_by_time(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    carol = add_user(session, "carol")
    posts = add_posts(session, 9, alice_account, bob_account, carol)

    items = feed(client).json()["items"]

    assert ids(items) == ids(posts)
    authors = [item["author"]["username"] for item in items]
    assert authors == ["carol", "bob", "alice"] * 3


def test_posts_are_ordered_by_when_they_were_created(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    # Written to the database in an order that is not chronological.
    hour = timedelta(hours=1)
    middle = add_post(session, alice_account, "Middle", created_at=START)
    newest = add_post(session, bob_account, "Newest", created_at=START + hour)
    oldest = add_post(session, alice_account, "Oldest", created_at=START - hour)

    items = feed(client).json()["items"]

    assert ids(items) == ids([newest, middle, oldest])
    times = [datetime.fromisoformat(item["created_at"]) for item in items]
    assert times == sorted(times, reverse=True)


def test_order_is_by_creation_not_by_last_edit(alice_client: TestClient, clock) -> None:
    first = alice_client.post("/posts", json={"content": "First"}).json()
    clock.advance(minutes=10)
    second = alice_client.post("/posts", json={"content": "Second"}).json()

    clock.advance(minutes=10)
    edit = alice_client.patch(f"/posts/{first['id']}", json={"content": "Edited"})
    assert edit.status_code == 200

    items = feed(alice_client).json()["items"]

    # Editing an old post does not bring it back to the top.
    assert ids(items) == [second["id"], first["id"]]
    assert items[1]["content"] == "Edited"


def test_posts_created_at_the_same_instant_have_a_fixed_order(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    authors = (alice_account, bob_account)
    posts = [
        add_post(session, authors[number % 2], created_at=START) for number in range(8)
    ]
    expected = [str(id) for id in sorted((post.id for post in posts), reverse=True)]

    first = ids(feed(client).json()["items"])
    second = ids(feed(client).json()["items"])

    assert first == second == expected


def test_items_are_posts_in_their_usual_shape(
    client: TestClient, session: Session, alice_account: User
) -> None:
    alice_account.avatar_url = "https://cdn.example.com/avatars/alice.png"
    post = add_post(session, alice_account, "Hello Hopsnop!")

    [item] = feed(client).json()["items"]

    assert set(item) == POST_FIELDS
    assert set(item["author"]) == AUTHOR_FIELDS
    # The representation every post endpoint gives, not one of the feed's own.
    assert item == client.get(f"/posts/{post.id}").json()
    assert item == client.get("/users/alice/posts").json()["items"][0]
    assert datetime.fromisoformat(item.pop("created_at")) == post.created_at
    assert datetime.fromisoformat(item.pop("updated_at")) == post.updated_at
    assert item == {
        "id": str(post.id),
        "author": {
            "id": str(alice_account.id),
            "username": "alice",
            "display_name": "Alice",
            "avatar_url": "https://cdn.example.com/avatars/alice.png",
        },
        "content": "Hello Hopsnop!",
        "parent_post_id": None,
        "is_reply": False,
        "like_count": 0,
        "liked_by_me": False,
        "repost_count": 0,
        "reposted_by_me": False,
    }


def test_post_published_through_the_api_is_at_the_top_of_the_feed(
    alice_client: TestClient, make_client, session: Session, bob_account: User, clock
) -> None:
    add_posts(session, 2, bob_account)
    clock.now = START + timedelta(days=1)

    created = alice_client.post("/posts", json={"content": "Just now"}).json()

    for viewer in (alice_client, make_client()):
        items = feed(viewer).json()["items"]
        assert len(items) == 3
        assert items[0] == created


def test_feed_responses_are_not_to_be_cached(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_post(session, alice_account)

    assert feed(client).headers["cache-control"] == "no-store"


# --- which posts are in it -----------------------------------------------


def test_post_of_an_active_verified_account_appears(
    client: TestClient, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account)
    assert alice_account.is_active is True
    assert alice_account.email_verified_at is not None

    assert ids(feed(client).json()["items"]) == [str(post.id)]


def test_every_shown_accounts_posts_are_in_the_feed_for_everyone(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    carol = add_user(session, "carol")
    posts = add_posts(session, 6, alice_account, bob_account, carol)
    anonymous, alice, bob = make_client(), make_client(), make_client()
    log_in(alice, "alice")
    log_in(bob, "bob")

    # No account is public or private: being shown is all it takes, and the
    # author of a post finds it here like anyone else.
    for viewer in (anonymous, alice, bob):
        response = feed(viewer)
        assert ids(response.json()["items"]) == ids(posts)
        assert "private" not in response.text


def test_no_profile_change_takes_an_accounts_posts_out_of_the_feed(
    alice_client: TestClient, make_client, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, 2, alice_account)
    anonymous = make_client()
    before = feed(anonymous).json()

    # There is no privacy setting to switch on, alone or next to a real change.
    alone = alice_client.patch("/users/me", json={"is_private": True})
    beside = alice_client.patch("/users/me", json={"is_private": True, "bio": "Hi"})

    assert feed(anonymous).json() == before
    assert ids(feed(alice_client).json()["items"]) == ids(before["items"]) == ids(posts)
    assert (alone.status_code, beside.status_code) == (422, 200)


def test_following_changes_nothing_about_the_feed(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    carol = add_user(session, "carol")
    posts = add_posts(session, 6, alice_account, carol)
    before = feed(bob_client).json()

    follow(session, bob_account, alice_account)

    # For You is not the Following feed: the followed are neither the only
    # ones in it nor the first.
    assert feed(bob_client).json() == before
    assert ids(before["items"]) == ids(posts)
    authors = [item["author"]["username"] for item in before["items"]]
    assert authors == ["carol", "alice"] * 3


def test_deleted_posts_do_not_appear(
    client: TestClient, session: Session, alice_account: User
) -> None:
    kept = add_post(session, alice_account, "Kept", created_at=START)
    add_post(
        session, alice_account, "Removed", created_at=START + MINUTE, deleted=True
    )

    response = feed(client)

    assert ids(response.json()["items"]) == [str(kept.id)]
    assert "Removed" not in response.text


def test_deleted_posts_do_not_appear_for_their_author(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    add_post(session, alice_account, "Removed", deleted=True)

    assert feed(alice_client).json() == EMPTY


def test_post_deleted_through_the_api_leaves_the_feed(
    alice_client: TestClient, make_client
) -> None:
    post = alice_client.post("/posts", json={"content": "Short-lived"}).json()
    anonymous = make_client()
    assert ids(feed(anonymous).json()["items"]) == [post["id"]]

    assert alice_client.delete(f"/posts/{post['id']}").status_code == 204

    assert feed(anonymous).json() == EMPTY
    assert feed(alice_client).json() == EMPTY


def test_posts_leave_with_a_deactivation_and_return_with_a_reactivation(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    alices = add_posts(session, 2, alice_account)
    bobs = add_post(session, bob_account, created_at=START - MINUTE)
    assert ids(feed(client).json()["items"]) == ids([*alices, bobs])

    alice_account.is_active = False
    session.flush()
    assert ids(feed(client).json()["items"]) == [str(bobs.id)]

    # Nothing was destroyed by the deactivation.
    alice_account.is_active = True
    session.flush()
    assert ids(feed(client).json()["items"]) == ids([*alices, bobs])


def test_posts_of_an_unverified_account_do_not_appear(
    client: TestClient, session: Session
) -> None:
    unverified = add_user(session, "unverified", verified=False)
    post = add_post(session, unverified, "Not yet")

    assert feed(client).json() == EMPTY

    unverified.email_verified_at = datetime.now(timezone.utc)
    session.flush()
    assert ids(feed(client).json()["items"]) == [str(post.id)]


def test_feed_holds_exactly_the_posts_that_pass_every_rule(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    shown = add_posts(session, 4, alice_account, bob_account)
    for number, kind in enumerate(HIDDEN):
        between = START + number * MINUTE + timedelta(seconds=30)
        add_hidden_post(session, kind, f"Hidden: {kind}", created_at=between)
    # Hidden for every reason at once.
    several = add_user(session, "several", verified=False, active=False)
    add_post(session, several, "Hidden: several", deleted=True)
    bob = make_client()
    log_in(bob, "bob")

    for viewer in (make_client(), bob):
        response = feed(viewer)

        assert ids(response.json()["items"]) == ids(shown)
        assert response.json()["next_cursor"] is None
        assert "Hidden" not in response.text
        assert "_author" not in response.text
        assert "several" not in response.text


# --- who is asking -------------------------------------------------------


def test_stale_cookie_is_answered_as_anonymous(
    client: TestClient, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, 2, alice_account)
    plant_session_cookie(client, "left-over-from-an-old-login")

    response = feed(client)

    # Not refused: the feed stays readable, as it is for anyone.
    assert response.status_code == 200
    assert ids(response.json()["items"]) == ids(posts)


def test_expired_session_is_answered_as_anonymous(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, 2, alice_account)
    expire(session, session.scalars(select(UserSession)).one())

    response = feed(bob_client)

    assert response.status_code == 200
    assert ids(response.json()["items"]) == ids(posts)


def test_deactivated_viewer_is_answered_as_anonymous(
    alice_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    add_post(session, alice_account, "Written while active")
    bobs = add_post(session, bob_account)
    alice_account.is_active = False
    session.flush()

    response = feed(alice_client)

    assert response.status_code == 200
    assert ids(response.json()["items"]) == [str(bobs.id)]


def test_logging_out_changes_nothing_about_the_feed(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, 3, alice_account)
    signed_in = feed(alice_client).json()

    alice_client.post("/auth/logout")

    assert feed(alice_client).json() == signed_in
    assert len(signed_in["items"]) == 3


def test_viewer_cannot_be_named_by_the_request(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
) -> None:
    post = add_post(session, alice_account)
    alice_client.post(f"/posts/{post.id}/like")

    response = bob_client.get(
        f"/feed?viewer_id={alice_account.id}&user_id={alice_account.id}"
        "&username=alice&as=alice"
    )

    # Whose "by me" it is, is decided by the session and by nothing else.
    assert response.status_code == 200
    [item] = response.json()["items"]
    assert item["like_count"] == 1
    assert item["liked_by_me"] is False


# --- replies -------------------------------------------------------------


def test_replies_appear_in_chronological_order_with_the_other_posts(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    post_a = add_post(session, alice_account, "A", created_at=START)
    post_b = add_post(session, bob_account, "B", created_at=START + 2 * MINUTE)
    reply = add_post(
        session,
        alice_account,
        "Reply to B",
        parent=post_b,
        created_at=START + 3 * MINUTE,
    )

    items = feed(client).json()["items"]

    assert ids(items) == ids([reply, post_b, post_a])
    assert [item["is_reply"] for item in items] == [True, False, False]
    assert [item["parent_post_id"] for item in items] == [str(post_b.id), None, None]


def test_reply_is_placed_by_its_own_time_not_next_to_its_parent(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    parent = add_post(session, alice_account, "Parent", created_at=START)
    unrelated = add_post(session, bob_account, "Unrelated", created_at=START + MINUTE)
    reply = add_post(
        session, bob_account, "Reply", parent=parent, created_at=START + 2 * MINUTE
    )
    latest = add_post(session, alice_account, "Latest", created_at=START + 3 * MINUTE)

    items = feed(client).json()["items"]

    # Chronological content, not conversations: nothing is grouped.
    assert ids(items) == ids([latest, reply, unrelated, parent])


def test_reply_is_returned_with_its_parent_post_id(
    alice_client: TestClient, bob_client: TestClient, clock
) -> None:
    parent = alice_client.post("/posts", json={"content": "Question"}).json()
    clock.advance(minutes=1)
    reply = bob_client.post(
        "/posts", json={"content": "Answer", "parent_post_id": parent["id"]}
    ).json()

    items = feed(alice_client).json()["items"]

    assert items == [reply, parent]
    assert items[0]["parent_post_id"] == parent["id"]
    assert items[0]["is_reply"] is True
    assert items[0]["author"]["username"] == "bob"


def test_nested_replies_each_point_at_their_direct_parent(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    top = add_post(session, alice_account, "Top", created_at=START)
    first = add_post(
        session, bob_account, "First", parent=top, created_at=START + MINUTE
    )
    second = add_post(
        session, alice_account, "Second", parent=first, created_at=START + 2 * MINUTE
    )

    items = feed(client).json()["items"]

    assert ids(items) == ids([second, first, top])
    assert [item["parent_post_id"] for item in items] == [
        str(first.id),
        str(top.id),
        None,
    ]


@pytest.mark.parametrize("kind", HIDDEN)
def test_reply_follows_its_own_authors_account(
    client: TestClient, session: Session, alice_account: User, kind: str
) -> None:
    parent = add_post(session, alice_account, "The post", created_at=START)
    add_hidden_post(
        session, kind, "Hidden reply", parent=parent, created_at=START + MINUTE
    )

    response = feed(client)

    # Answering a post that is shown does not make a reply shown.
    assert ids(response.json()["items"]) == [str(parent.id)]
    assert "Hidden reply" not in response.text


@pytest.mark.parametrize("kind", HIDDEN)
def test_hidden_reply_is_not_shown_even_to_the_author_it_answers(
    alice_client: TestClient, session: Session, alice_account: User, kind: str
) -> None:
    parent = add_post(session, alice_account, "The post", created_at=START)
    add_hidden_post(
        session, kind, "Hidden reply", parent=parent, created_at=START + MINUTE
    )

    response = feed(alice_client)

    assert ids(response.json()["items"]) == [str(parent.id)]
    assert "Hidden reply" not in response.text


def test_reply_stays_in_the_feed_when_its_parents_author_is_no_longer_shown(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    parent = add_post(session, alice_account, "Now hidden", created_at=START)
    reply = add_post(
        session, bob_account, "The reply", parent=parent, created_at=START + MINUTE
    )
    alice_account.is_active = False
    session.flush()

    response = feed(client)

    [item] = response.json()["items"]
    assert item["id"] == str(reply.id)
    # The id of the parent, which the reply had all along, and nothing of
    # what the parent says.
    assert item["parent_post_id"] == str(parent.id)
    assert "Now hidden" not in response.text


def test_deleting_the_parent_does_not_remove_a_reply_from_the_feed(
    alice_client: TestClient, bob_client: TestClient, make_client, clock
) -> None:
    parent = alice_client.post("/posts", json={"content": "Soon deleted"}).json()
    clock.advance(minutes=1)
    reply = bob_client.post(
        "/posts", json={"content": "Still here", "parent_post_id": parent["id"]}
    ).json()

    assert alice_client.delete(f"/posts/{parent['id']}").status_code == 204

    for viewer in (make_client(), alice_client, bob_client):
        response = feed(viewer)
        assert response.json()["items"] == [reply]
        assert response.json()["items"][0]["parent_post_id"] == parent["id"]
        assert "Soon deleted" not in response.text


# --- cursor pagination ---------------------------------------------------


def test_first_page_returns_a_cursor_when_there_are_more_posts(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    posts = add_posts(session, 5, alice_account, bob_account)

    body = feed(client, limit=2).json()

    assert ids(body["items"]) == ids(posts[:2])
    assert isinstance(body["next_cursor"], str)


def test_cursor_returns_the_pages_that_follow(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    posts = add_posts(session, 5, alice_account, bob_account)
    first = feed(client, limit=2).json()

    second = feed(client, limit=2, cursor=first["next_cursor"]).json()
    third = feed(client, limit=2, cursor=second["next_cursor"]).json()

    assert ids(second["items"]) == ids(posts[2:4])
    assert second["next_cursor"] is not None
    assert ids(third["items"]) == ids(posts[4:])
    assert third["next_cursor"] is None


def test_last_page_has_no_cursor(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, 3, alice_account)

    assert feed(client, limit=5).json()["next_cursor"] is None


def test_page_that_exactly_holds_the_rest_has_no_cursor(
    client: TestClient, session: Session, alice_account: User
) -> None:
    # No "next page" that then turns out to be empty.
    add_posts(session, 4, alice_account)

    first = feed(client, limit=2).json()
    second = feed(client, limit=2, cursor=first["next_cursor"]).json()

    assert first["next_cursor"] is not None
    assert len(second["items"]) == 2
    assert second["next_cursor"] is None


@pytest.mark.parametrize(
    ("count", "limit"), [(1, 1), (7, 3), (20, 7), (23, 5), (50, 50)]
)
def test_walking_the_pages_returns_every_post_exactly_once_in_order(
    client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    count: int,
    limit: int,
) -> None:
    posts = add_posts(session, count, alice_account, bob_account)

    pages = walk(client, limit=limit)

    seen = [id for items in pages for id in ids(items)]
    assert seen == ids(posts)  # nothing skipped, nothing out of order
    assert len(set(seen)) == len(seen)  # nothing twice
    assert all(len(items) == limit for items in pages[:-1])
    assert len(pages) == -(-count // limit)


def test_no_duplicates_or_gaps_among_posts_created_at_the_same_instant(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    # The hard case for a cursor: the timestamp alone cannot tell where the
    # page ended. Three groups of posts, each sharing one timestamp.
    authors = (alice_account, bob_account)
    posts = [
        add_post(session, authors[number % 2], created_at=START + group * MINUTE)
        for group in range(3)
        for number in range(7)
    ]
    expected = [
        str(post.id)
        for post in sorted(posts, key=lambda p: (p.created_at, p.id), reverse=True)
    ]

    for limit in (1, 2, 3, 5, 7, 8):
        seen = [id for items in walk(client, limit=limit) for id in ids(items)]
        assert seen == expected, limit


def test_the_same_cursor_always_gives_the_same_page(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, 6, alice_account)
    cursor = feed(client, limit=2).json()["next_cursor"]

    answers = [feed(client, limit=2, cursor=cursor).json() for _ in range(3)]

    assert answers[0] == answers[1] == answers[2]
    assert len(answers[0]["items"]) == 2


def test_limit_may_change_between_pages(
    client: TestClient, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, 6, alice_account)
    first = feed(client, limit=2).json()

    second = feed(client, limit=4, cursor=first["next_cursor"]).json()

    assert ids(second["items"]) == ids(posts[2:])


def test_cursor_gives_the_same_page_whoever_presents_it(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    posts = add_posts(session, 6, alice_account, bob_account)
    anonymous, bob = make_client(), make_client()
    log_in(bob, "bob")
    from_anonymous = feed(anonymous, limit=2).json()["next_cursor"]
    from_bob = feed(bob, limit=2).json()["next_cursor"]

    assert from_anonymous == from_bob
    for viewer in (anonymous, bob):
        page = feed(viewer, limit=2, cursor=from_anonymous).json()
        assert ids(page["items"]) == ids(posts[2:4])


def test_cursor_is_the_position_of_the_last_post_shown(
    client: TestClient, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, 3, alice_account)
    last_shown = posts[1]
    # The rows right before and after it in the table are not in the feed.
    second = timedelta(seconds=1)
    add_hidden_post(session, "inactive", created_at=last_shown.created_at + second)
    add_hidden_post(session, "deleted", created_at=last_shown.created_at - second)

    cursor = feed(client, limit=2).json()["next_cursor"]

    # Usable in a query string as it is, and it carries only what the page
    # already showed: the time and id of its last post.
    assert re.fullmatch(r"[A-Za-z0-9_-]{32}", cursor)
    assert decode_cursor(cursor) == Cursor(
        created_at=last_shown.created_at, id=last_shown.id
    )


def test_cursor_is_the_one_every_list_of_posts_uses(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    posts = add_posts(session, 6, alice_account, bob_account)
    # From alice's own list: the position after her newest post.
    from_alices_list = client.get("/users/alice/posts?limit=1").json()["next_cursor"]

    page = feed(client, cursor=from_alices_list).json()

    # In the feed it is that same position. There is one cursor format.
    assert posts[1].author_id == alice_account.id
    assert ids(page["items"]) == ids(posts[2:])


# --- posts that are not shown, and pages ---------------------------------


@pytest.mark.parametrize("kind", HIDDEN)
def test_hidden_post_does_not_take_up_room_on_a_page(
    client: TestClient, session: Session, alice_account: User, kind: str
) -> None:
    add_post(session, alice_account, "D", created_at=START)
    add_post(session, alice_account, "C", created_at=START + MINUTE)
    add_hidden_post(session, kind, "B", created_at=START + 2 * MINUTE)
    add_post(session, alice_account, "A", created_at=START + 3 * MINUTE)

    body = feed(client, limit=3).json()

    # Three were asked for and three are returned; the hidden one is left
    # out before the page is cut, not after.
    assert [item["content"] for item in body["items"]] == ["A", "C", "D"]
    assert body["next_cursor"] is None


@pytest.mark.parametrize("limit", [1, 2, 3, 5, 50])
def test_walking_the_pages_shows_every_visible_post_and_no_hidden_one(
    client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    limit: int,
) -> None:
    # Every second row in the order is hidden, each time for another reason.
    visible = add_posts(session, 12, alice_account, bob_account)
    for number in range(12):
        after = START + number * MINUTE + timedelta(seconds=30)
        add_hidden_post(session, HIDDEN[number % len(HIDDEN)], created_at=after)

    pages = walk(client, limit=limit)

    seen = [id for items in pages for id in ids(items)]
    assert seen == ids(visible)
    # Every page but the last is full: hidden rows use none of the room.
    assert all(len(items) == limit for items in pages[:-1])
    assert len(pages) == -(-12 // limit)


@pytest.mark.parametrize("kind", HIDDEN)
def test_page_is_the_last_one_when_only_hidden_posts_remain(
    client: TestClient, session: Session, alice_account: User, kind: str
) -> None:
    posts = add_posts(session, 2, alice_account)
    for number in range(5):
        add_hidden_post(session, kind, created_at=START - (number + 1) * MINUTE)

    body = feed(client, limit=2).json()

    assert ids(body["items"]) == ids(posts)
    # No cursor: not even the existence of the older posts is given away.
    assert body["next_cursor"] is None


def test_feed_of_only_hidden_posts_is_empty(
    client: TestClient, session: Session
) -> None:
    for number, kind in enumerate(HIDDEN * 3):
        add_hidden_post(session, kind, created_at=START + number * MINUTE)

    for limit in (1, 5, 50):
        assert feed(client, limit=limit).json() == EMPTY


# --- data that changes between pages -------------------------------------


def test_new_post_between_requests_causes_no_duplicates_or_gaps(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    posts = add_posts(session, 6, alice_account, bob_account)
    first = feed(client, limit=3).json()

    # While the reader is on page one, a new post is published. With offset
    # pagination, page two would now begin with the last post of page one.
    new = add_post(session, bob_account, "X", created_at=START + timedelta(days=1))
    second = feed(client, limit=3, cursor=first["next_cursor"]).json()

    assert ids(first["items"]) == ids(posts[:3])
    assert ids(second["items"]) == ids(posts[3:])
    assert second["next_cursor"] is None
    assert str(new.id) not in ids(second["items"])
    assert not set(ids(first["items"])) & set(ids(second["items"]))
    # The new post is found by starting again from the top.
    assert ids(feed(client, limit=3).json()["items"]) == ids([new, *posts[:2]])


def test_posts_published_while_paging_never_shift_the_pages(
    alice_client: TestClient, make_client, session: Session, bob_account: User, clock
) -> None:
    posts = add_posts(session, 9, bob_account)
    clock.now = START + timedelta(days=1)
    reader = make_client()

    seen: list[str] = []
    cursor = None
    while True:
        extra = {"cursor": cursor} if cursor else {}
        body = feed(reader, limit=2, **extra).json()
        seen += ids(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        # After every page, another post lands on top of the feed.
        clock.advance(minutes=1)
        published = alice_client.post("/posts", json={"content": "Breaking"})
        assert published.status_code == 201

    assert seen == ids(posts)
    assert len(feed(reader, limit=50).json()["items"]) == 9 + 4


def test_posts_deleted_between_requests_cause_no_duplicates_or_gaps(
    alice_client: TestClient, make_client, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, 8, alice_account)
    reader = make_client()
    first = feed(reader, limit=3).json()

    # One post already seen, the very post the cursor points at, and one not
    # yet seen are deleted. With offset pagination, posts would be skipped.
    for post in (posts[0], posts[2], posts[4]):
        assert alice_client.delete(f"/posts/{post.id}").status_code == 204
    rest = [
        id
        for items in walk(reader, limit=3, cursor=first["next_cursor"])
        for id in ids(items)
    ]

    assert rest == ids([posts[3], posts[5], posts[6], posts[7]])


def test_account_deactivated_between_requests_is_gone_from_the_pages_that_follow(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    posts = add_posts(session, 8, alice_account, bob_account)
    first = feed(client, limit=2).json()
    assert {item["author"]["username"] for item in first["items"]} == {"alice", "bob"}

    alice_account.is_active = False
    session.flush()
    pages = walk(client, limit=2, cursor=first["next_cursor"])

    # The cursor was issued while alice's posts were in the feed. It does
    # not keep them there, and bob's posts are neither skipped nor repeated.
    bobs = [post for post in posts[2:] if post.author_id == bob_account.id]
    assert [id for items in pages for id in ids(items)] == ids(bobs)
    assert [len(items) for items in pages] == [2, 1]


# --- a cursor is not a key -----------------------------------------------


@pytest.mark.parametrize("kind", HIDDEN)
def test_forged_cursor_cannot_reach_a_hidden_post(
    client: TestClient, session: Session, kind: str
) -> None:
    hidden = add_hidden_post(session, kind, created_at=START)
    just_after = encode_cursor(
        Cursor(created_at=hidden.created_at + timedelta(seconds=1), id=hidden.id)
    )
    # The same instant, and an id that sorts after every other.
    exactly_at = encode_cursor(
        Cursor(created_at=hidden.created_at, id=uuid.UUID(int=2**128 - 1))
    )

    for cursor in (just_after, exactly_at):
        response = feed(client, cursor=cursor)
        assert response.status_code == 200
        assert response.json() == EMPTY


def test_cursor_from_a_users_own_list_is_only_a_point_in_time_in_the_feed(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    alices = add_posts(session, 5, alice_account)
    bobs = add_posts(session, 2, bob_account, start=START - timedelta(days=1))
    # A genuine cursor into alice's list, as she received it.
    cursor = alice_client.get("/users/alice/posts?limit=2").json()["next_cursor"]
    assert cursor is not None

    for viewer in (alice_client, bob_client):
        response = feed(viewer, cursor=cursor)

        # Every post older than that point, whoever wrote it and whoever asks.
        assert response.status_code == 200
        assert ids(response.json()["items"]) == ids([*alices[2:], *bobs])


# --- malformed requests --------------------------------------------------


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "abc",
        "null",
        "0",
        "1",
        "2",
        "A" * 31,
        "A" * 33,
        "!" * 32,
        "A" * 31 + "=",
        "A" * 31 + "+",
        "é" * 32,
        IMPOSSIBLE_TIME_CURSOR,
        "2026-10-01T12:00:00Z",
        str(uuid.uuid4()),
        "' OR '1'='1",
        "A" * 5000,
    ],
)
def test_malformed_cursor_is_rejected(
    client: TestClient, session: Session, alice_account: User, cursor: str
) -> None:
    add_posts(session, 3, alice_account)

    response = feed(client, cursor=cursor)

    assert response.status_code == 400
    assert response.json() == INVALID_CURSOR


def test_malformed_cursor_gets_the_answer_every_list_gives(
    make_client, session: Session, alice_account: User
) -> None:
    add_posts(session, 3, alice_account)
    alice = make_client()
    log_in(alice, "alice")

    answers = [
        feed(make_client(), cursor="abc"),
        feed(alice, cursor="abc"),
        alice.get("/users/alice/posts?cursor=abc"),
    ]

    assert {response.status_code for response in answers} == {400}
    assert len({response.text for response in answers}) == 1
    assert len({tuple(sorted(response.headers)) for response in answers}) == 1


def test_rejected_cursor_is_not_echoed(client: TestClient) -> None:
    response = feed(client, cursor="something-the-client-sent")

    assert response.status_code == 400
    assert "something" not in response.text


def test_well_formed_cursor_that_was_never_issued_is_just_a_position(
    client: TestClient, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, 4, alice_account)
    half_a_minute = timedelta(seconds=30)
    # Between the second post and the third, with an id that no post has.
    between = encode_cursor(
        Cursor(created_at=posts[1].created_at - half_a_minute, id=uuid.uuid4())
    )
    before_all = encode_cursor(Cursor(created_at=START - MINUTE, id=uuid.uuid4()))
    after_all = encode_cursor(
        Cursor(created_at=START + timedelta(days=365), id=uuid.uuid4())
    )

    assert ids(feed(client, cursor=between).json()["items"]) == ids(posts[2:])
    assert feed(client, cursor=before_all).json() == EMPTY
    assert ids(feed(client, cursor=after_all).json()["items"]) == ids(posts)


def test_default_limit(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, DEFAULT_PAGE_SIZE + 5, alice_account)

    body = feed(client).json()

    assert DEFAULT_PAGE_SIZE == 20
    assert len(body["items"]) == DEFAULT_PAGE_SIZE
    assert body["next_cursor"] is not None


def test_limit_is_respected(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, 10, alice_account)

    for limit in (2, 9, 10):
        assert len(feed(client, limit=limit).json()["items"]) == limit


def test_minimum_limit_is_accepted(
    client: TestClient, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, 3, alice_account)

    body = feed(client, limit=1).json()

    assert ids(body["items"]) == ids(posts[:1])
    assert body["next_cursor"] is not None


def test_maximum_limit_is_accepted(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, MAX_PAGE_SIZE + 10, alice_account)

    body = feed(client, limit=MAX_PAGE_SIZE).json()

    assert MAX_PAGE_SIZE == 50
    assert len(body["items"]) == MAX_PAGE_SIZE
    assert body["next_cursor"] is not None


@pytest.mark.parametrize("limit", [MAX_PAGE_SIZE + 1, 100, 1000, 10**9, 10**30])
def test_limit_above_the_maximum_is_rejected(
    client: TestClient, session: Session, alice_account: User, limit: int
) -> None:
    add_posts(session, 3, alice_account)

    response = feed(client, limit=limit)

    # Refused outright; the server never loads more than the maximum.
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "limit"]


@pytest.mark.parametrize("limit", [0, -1, -50])
def test_limit_below_the_minimum_is_rejected(
    client: TestClient, session: Session, alice_account: User, limit: int
) -> None:
    add_posts(session, 3, alice_account)

    response = feed(client, limit=limit)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "limit"]


@pytest.mark.parametrize("limit", ["abc", "", "1.5", "ten", "1e3", "null", "5; DROP"])
def test_limit_that_is_not_a_whole_number_is_rejected(
    client: TestClient, limit: str
) -> None:
    response = feed(client, limit=limit)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "limit"]


@pytest.mark.parametrize("param", ["offset", "page", "skip", "page_size", "per_page"])
def test_there_is_no_offset_pagination(
    client: TestClient, session: Session, alice_account: User, param: str
) -> None:
    posts = add_posts(session, 5, alice_account)

    response = client.get(f"/feed?limit=2&{param}=2")

    # Ignored: the answer is the first page all the same.
    assert ids(response.json()["items"]) == ids(posts[:2])


def test_documented_parameters_are_limit_and_cursor() -> None:
    operation = app.openapi()["paths"]["/feed"]["get"]
    parameters = {param["name"]: param for param in operation["parameters"]}

    assert set(parameters) == {"limit", "cursor"}
    assert parameters["limit"]["schema"]["maximum"] == MAX_PAGE_SIZE
    assert parameters["limit"]["schema"]["minimum"] == 1
    assert parameters["limit"]["schema"]["default"] == DEFAULT_PAGE_SIZE


def test_feed_is_paged_and_answered_like_the_other_list_of_posts() -> None:
    paths = app.openapi()["paths"]
    feed_operation = paths["/feed"]["get"]
    list_operation = paths["/users/{username}/posts"]["get"]

    def page_parameters(operation: dict) -> list[dict]:
        return [param for param in operation["parameters"] if param["in"] == "query"]

    def answer(operation: dict) -> dict:
        return operation["responses"]["200"]["content"]["application/json"]["schema"]

    # The same limits and the same cursor, and the same page of the same
    # posts: nothing here is the feed's own.
    assert page_parameters(feed_operation) == page_parameters(list_operation)
    assert answer(feed_operation) == answer(list_operation)
    assert answer(feed_operation) == {"$ref": "#/components/schemas/PostPageResponse"}


# --- cost ----------------------------------------------------------------


def test_feed_takes_one_query_however_many_posts_and_authors_there_are(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_post(session, alice_account)
    # Like a real request, which starts with nothing loaded.
    session.expunge_all()
    with recorded_selects() as with_one:
        assert len(feed(client, limit=50).json()["items"]) == 1

    authors = [add_user(session, f"author{number}") for number in range(8)]
    add_posts(session, 40, *authors)
    session.expunge_all()
    with recorded_selects() as with_many:
        items = feed(client, limit=50).json()["items"]

    assert len(items) == 41
    assert len({item["author"]["id"] for item in items}) == 9
    # The page and its authors are one statement: no query per post, and
    # none per author.
    assert len(with_one) == len(with_many) == 1


def test_feed_takes_no_more_queries_per_post_for_a_signed_in_viewer(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    add_post(session, alice_account)
    session.expunge_all()
    with recorded_selects() as with_one:
        assert len(feed(bob_client, limit=50).json()["items"]) == 1

    authors = [add_user(session, f"author{number}") for number in range(8)]
    add_posts(session, 40, *authors)
    session.expunge_all()
    with recorded_selects() as with_many:
        assert len(feed(bob_client, limit=50).json()["items"]) == 41

    # One to find the session and its user, one for the page.
    assert len(with_one) == len(with_many) == 2


def test_feed_is_filtered_ordered_and_cut_by_the_database(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, 5, alice_account)
    cursor = feed(client, limit=2).json()["next_cursor"]
    session.expunge_all()

    with recorded_selects() as statements:
        assert len(feed(client, limit=2, cursor=cursor).json()["items"]) == 2

    [statement] = statements
    sql = " ".join(statement.split())
    # Which posts, from where on, in which order and how many: all of it is
    # in the query, so no row is fetched only to be thrown away.
    for condition in (
        "posts.deleted_at IS NULL",
        "users.is_active IS true",
        "users.email_verified_at IS NOT NULL",
        "(posts.created_at, posts.id) < (",
    ):
        assert condition in sql, condition
    # Those are all the rules there are: no account is public or private.
    assert "private" not in sql.lower()
    assert " ORDER BY posts.created_at DESC, posts.id DESC LIMIT " in sql
    assert "OFFSET" not in sql
    # Nothing but posts, their authors and their likes and reposts is
    # consulted, and the last two only to count them.
    assert set(re.findall(r"(?:FROM|JOIN) (\w+)", sql)) == {
        "posts",
        "users",
        "likes",
        "reposts",
    }


def test_feed_does_not_read_the_authors_private_columns(
    client: TestClient, session: Session, alice_account: User
) -> None:
    # The strongest form of "not exposed": the data is never fetched.
    add_posts(session, 3, alice_account)
    session.expunge_all()

    with recorded_selects() as statements:
        assert feed(client).status_code == 200

    [statement] = statements
    assert "users.username" in statement
    assert "password_hash" not in statement
    assert not re.search(r"users\.email\b(?!_)", statement)
    assert "users.bio" not in statement

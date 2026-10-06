"""GET /users/{username}/posts: a user's posts, with cursor pagination."""

import base64
import re
import struct
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Cursor,
    decode_cursor,
    encode_cursor,
)
from app.main import app
from app.models import Post, User
from helpers import (
    SENSITIVE_KEYS,
    add_post,
    add_user,
    follow,
    keys_in,
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
USER_NOT_FOUND = {"detail": "User not found."}
PRIVATE = {"detail": "This account's posts are private."}
INVALID_CURSOR = {"detail": "Invalid cursor."}
START = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
# Well formed in every respect, but its time lies beyond the year 9999.
IMPOSSIBLE_TIME_CURSOR = base64.urlsafe_b64encode(
    struct.pack(">q16s", 2**63 - 1, bytes(16))
).decode("ascii")


def page(client: TestClient, username: str = "alice", **params: object):
    return client.get(f"/users/{username}/posts", params=params)


def add_posts(
    session: Session, author: User, count: int, *, start: datetime = START
) -> list[Post]:
    """``count`` posts one minute apart, returned newest first."""
    posts = [
        add_post(
            session,
            author,
            f"Post {number}",
            created_at=start + timedelta(minutes=number),
        )
        for number in range(count)
    ]
    return posts[::-1]


def ids(posts: list) -> list[str]:
    return [str(post["id"] if isinstance(post, dict) else post.id) for post in posts]


def walk(
    client: TestClient,
    username: str = "alice",
    *,
    limit: int,
    cursor: str | None = None,
) -> list[list[dict]]:
    """Every page from ``cursor`` on, following next_cursor until there is none."""
    pages = []
    while True:
        extra = {"cursor": cursor} if cursor else {}
        response = page(client, username, limit=limit, **extra)
        assert response.status_code == 200, response.text
        body = response.json()
        pages.append(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) < 200, "pagination does not terminate"


def make_private(session: Session, user: User) -> None:
    user.is_private = True
    session.flush()


# --- a user's posts ------------------------------------------------------


def test_returns_the_users_posts(
    client: TestClient, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, alice_account, 3)

    response = page(client)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"items", "next_cursor"}
    assert ids(body["items"]) == ids(posts)
    assert body["next_cursor"] is None


def test_items_are_posts_in_their_usual_shape(
    client: TestClient, session: Session, alice_account: User
) -> None:
    post = add_post(session, alice_account, "Hello Hopsnop!")

    [item] = page(client).json()["items"]

    assert set(item) == POST_FIELDS
    assert item == client.get(f"/posts/{post.id}").json()
    assert item["author"]["username"] == "alice"


def test_only_that_users_posts_are_returned(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    alices = add_posts(session, alice_account, 2)
    bobs = add_posts(session, bob_account, 3)

    assert ids(page(client, "alice").json()["items"]) == ids(alices)
    assert ids(page(client, "bob").json()["items"]) == ids(bobs)


def test_posts_are_returned_newest_first(
    client: TestClient, session: Session, alice_account: User
) -> None:
    # Written to the database in an order that is not chronological.
    hour = timedelta(hours=1)
    middle = add_post(session, alice_account, "Middle", created_at=START)
    newest = add_post(session, alice_account, "Newest", created_at=START + hour)
    oldest = add_post(session, alice_account, "Oldest", created_at=START - hour)

    items = page(client).json()["items"]

    assert [item["content"] for item in items] == ["Newest", "Middle", "Oldest"]
    assert ids(items) == ids([newest, middle, oldest])
    times = [datetime.fromisoformat(item["created_at"]) for item in items]
    assert times == sorted(times, reverse=True)


def test_order_is_by_creation_not_by_last_edit(
    alice_client: TestClient, clock
) -> None:
    first = alice_client.post("/posts", json={"content": "First"}).json()
    clock.advance(minutes=10)
    second = alice_client.post("/posts", json={"content": "Second"}).json()

    clock.advance(minutes=10)
    alice_client.patch(f"/posts/{first['id']}", json={"content": "First, edited"})

    # Editing an old post does not move it to the top.
    assert ids(page(alice_client).json()["items"]) == [second["id"], first["id"]]


def test_posts_created_at_the_same_instant_have_a_fixed_order(
    client: TestClient, session: Session, alice_account: User
) -> None:
    posts = [add_post(session, alice_account, created_at=START) for _ in range(8)]
    expected = [str(id) for id in sorted((post.id for post in posts), reverse=True)]

    first = ids(page(client).json()["items"])
    second = ids(page(client).json()["items"])

    assert first == second == expected


def test_replies_are_included(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    minute = timedelta(minutes=1)
    bobs = add_post(session, bob_account, "Bob's post", created_at=START)
    reply = add_post(
        session, alice_account, "Reply", parent=bobs, created_at=START + minute
    )
    own = add_post(session, alice_account, "Own", created_at=START + 2 * minute)

    items = page(client).json()["items"]

    assert ids(items) == ids([own, reply])
    assert [item["is_reply"] for item in items] == [False, True]
    assert items[1]["parent_post_id"] == str(bobs.id)


def test_deleted_posts_are_excluded(
    client: TestClient, session: Session, alice_account: User
) -> None:
    kept = add_post(session, alice_account, "Kept", created_at=START)
    add_post(
        session,
        alice_account,
        "Removed",
        created_at=START + timedelta(minutes=1),
        deleted=True,
    )

    response = page(client)

    assert ids(response.json()["items"]) == [str(kept.id)]
    assert "Removed" not in response.text


def test_deleted_posts_are_excluded_for_their_author_too(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    add_post(session, alice_account, "Removed", deleted=True)

    assert page(alice_client).json() == {"items": [], "next_cursor": None}


def test_user_without_posts_has_an_empty_list(
    client: TestClient, alice_account: User
) -> None:
    response = page(client)

    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_user_whose_posts_are_all_deleted_looks_like_one_without_posts(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    for number in range(3):
        add_post(session, alice_account, f"Removed {number}", deleted=True)

    assert page(client, "alice").json() == page(client, "bob").json()


def test_user_posts_responses_are_not_to_be_cached(
    client: TestClient, alice_account: User
) -> None:
    assert page(client).headers["cache-control"] == "no-store"


# --- who may read them ---------------------------------------------------


def test_public_users_posts_need_no_authentication(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, alice_account, 2)
    assert client.cookies.get("hopsnop_session") is None

    assert page(client).status_code == 200


def test_public_users_posts_are_the_same_for_every_viewer(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    add_posts(session, alice_account, 3)
    anonymous, bob, alice = make_client(), make_client(), make_client()
    log_in(bob, "bob")
    log_in(alice, "alice")

    seen = [page(viewer).json() for viewer in (anonymous, bob, alice)]

    assert seen[0] == seen[1] == seen[2]
    assert len(seen[0]["items"]) == 3


def test_private_accounts_posts_are_refused_without_authentication(
    client: TestClient, session: Session, alice_account: User
) -> None:
    make_private(session, alice_account)
    add_post(session, alice_account, "For my eyes only")

    response = page(client)

    assert response.status_code == 403
    assert response.json() == PRIVATE
    assert "For my eyes only" not in response.text


def test_private_accounts_posts_are_refused_to_another_user(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    make_private(session, alice_account)
    add_post(session, alice_account, "For my eyes only")

    response = page(bob_client)

    assert response.status_code == 403
    assert response.json() == PRIVATE
    assert "For my eyes only" not in response.text


def test_owner_of_a_private_account_gets_their_own_posts(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    make_private(session, alice_account)
    posts = add_posts(session, alice_account, 3)

    response = page(alice_client)

    assert response.status_code == 200
    assert ids(response.json()["items"]) == ids(posts)


def test_following_a_private_account_does_not_open_its_posts_yet(
    bob_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    make_private(session, alice_account)
    add_post(session, alice_account)
    follow(session, bob_account, alice_account)

    assert page(bob_client).status_code == 403


def test_refusal_does_not_reveal_whether_the_private_account_has_posts(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    make_private(session, alice_account)
    without_posts = page(bob_client)

    add_posts(session, alice_account, 5)
    add_post(session, alice_account, deleted=True)
    with_posts = page(bob_client)

    assert without_posts.status_code == with_posts.status_code == 403
    assert without_posts.text == with_posts.text
    assert sorted(without_posts.headers) == sorted(with_posts.headers)


def test_private_accounts_profile_stays_visible_while_its_posts_do_not(
    client: TestClient, session: Session, alice_account: User
) -> None:
    # Phase 3 behaviour, unchanged: the profile says the account is private.
    make_private(session, alice_account)
    add_post(session, alice_account)

    profile = client.get("/users/alice")

    assert profile.status_code == 200
    assert profile.json()["is_private"] is True
    assert page(client).status_code == 403


def test_posts_open_up_and_close_with_the_privacy_setting(
    alice_client: TestClient, make_client, session: Session, alice_account: User
) -> None:
    add_posts(session, alice_account, 2)
    anonymous = make_client()
    assert page(anonymous).status_code == 200

    alice_client.patch("/users/me", json={"is_private": True})
    assert page(anonymous).status_code == 403
    assert len(page(alice_client).json()["items"]) == 2

    alice_client.patch("/users/me", json={"is_private": False})
    assert len(page(anonymous).json()["items"]) == 2


def test_stale_cookie_is_answered_as_anonymous(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    add_posts(session, alice_account, 2)
    make_private(session, bob_account)
    plant_session_cookie(client, "left-over-from-an-old-login")

    assert page(client, "alice").status_code == 200
    assert page(client, "bob").status_code == 403


def test_viewer_cannot_be_named_by_the_request(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    make_private(session, alice_account)
    add_post(session, alice_account)

    response = bob_client.get(
        f"/users/alice/posts?viewer_id={alice_account.id}&user_id={alice_account.id}"
        "&as=alice&is_private=false"
    )

    assert response.status_code == 403


# --- accounts that are not found -----------------------------------------


def test_nonexistent_user_returns_the_same_404_as_the_profile(
    client: TestClient,
) -> None:
    response = page(client, "nonexistent")

    assert response.status_code == 404
    assert response.json() == USER_NOT_FOUND
    assert response.json() == client.get("/users/nonexistent").json()


@pytest.mark.parametrize(
    "username",
    ["al", "a" * 31, "alice-smith", "álice", "alice@example.com", "' OR '1'='1", "%"],
)
def test_impossible_username_returns_the_same_404(
    client: TestClient, session: Session, alice_account: User, username: str
) -> None:
    add_post(session, alice_account)

    response = client.get(f"/users/{quote(username, safe='')}/posts")

    assert response.status_code == 404
    assert response.json() == USER_NOT_FOUND


def test_posts_cannot_be_listed_by_user_id_or_email(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_post(session, alice_account)

    by_id = client.get(f"/users/{alice_account.id}/posts")
    by_email = client.get(f"/users/{quote(alice_account.email, safe='')}/posts")

    assert by_id.status_code == by_email.status_code == 404


def test_me_is_not_a_username(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    add_post(session, alice_account)

    # /users/me is the caller's profile; there is no such alias for posts.
    assert page(alice_client, "me").status_code == 404
    assert page(alice_client, "alice").status_code == 200


@pytest.mark.parametrize("username", ["alice", "Alice", "ALICE", " alice "])
def test_username_is_matched_in_its_canonical_form(
    client: TestClient, session: Session, alice_account: User, username: str
) -> None:
    add_post(session, alice_account)

    response = client.get(f"/users/{quote(username)}/posts")

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


def test_posts_of_a_deactivated_account_are_not_found(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, alice_account, 2)
    assert page(client).status_code == 200

    alice_account.is_active = False
    session.flush()

    assert page(client).status_code == 404
    assert page(client).json() == USER_NOT_FOUND


def test_posts_of_an_unverified_account_are_not_found(
    client: TestClient, session: Session
) -> None:
    add_post(session, add_user(session, "unverified", verified=False))

    assert page(client, "unverified").status_code == 404


def test_hidden_account_looks_the_same_as_one_that_does_not_exist(
    client: TestClient, session: Session
) -> None:
    inactive = add_user(session, "inactive", active=False)
    inactive.is_private = True
    add_post(session, inactive)
    add_post(session, add_user(session, "unverified", verified=False))

    responses = [
        page(client, "nonexistent"),
        page(client, "inactive"),  # not "private": that would confirm it exists
        page(client, "unverified"),
    ]

    assert {response.status_code for response in responses} == {404}
    assert len({response.text for response in responses}) == 1
    assert len({tuple(sorted(response.headers)) for response in responses}) == 1


# --- cursor pagination ---------------------------------------------------


def test_first_page_returns_a_cursor_when_there_are_more_posts(
    client: TestClient, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, alice_account, 5)

    body = page(client, limit=2).json()

    assert ids(body["items"]) == ids(posts[:2])
    assert isinstance(body["next_cursor"], str)


def test_cursor_returns_the_next_batch(
    client: TestClient, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, alice_account, 5)
    first = page(client, limit=2).json()

    second = page(client, limit=2, cursor=first["next_cursor"]).json()
    third = page(client, limit=2, cursor=second["next_cursor"]).json()

    assert ids(second["items"]) == ids(posts[2:4])
    assert ids(third["items"]) == ids(posts[4:])
    assert third["next_cursor"] is None


def test_last_page_has_no_cursor(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, alice_account, 3)

    assert page(client, limit=5).json()["next_cursor"] is None


def test_page_that_exactly_holds_the_rest_has_no_cursor(
    client: TestClient, session: Session, alice_account: User
) -> None:
    # No "next page" that then turns out to be empty.
    add_posts(session, alice_account, 4)

    first = page(client, limit=2).json()
    second = page(client, limit=2, cursor=first["next_cursor"]).json()

    assert first["next_cursor"] is not None
    assert len(second["items"]) == 2
    assert second["next_cursor"] is None


@pytest.mark.parametrize(
    ("count", "limit"), [(1, 1), (7, 3), (20, 7), (23, 5), (50, 50)]
)
def test_walking_the_pages_returns_every_post_exactly_once_in_order(
    client: TestClient, session: Session, alice_account: User, count: int, limit: int
) -> None:
    posts = add_posts(session, alice_account, count)

    pages = walk(client, limit=limit)

    seen = [id for items in pages for id in ids(items)]
    assert seen == ids(posts)  # nothing skipped, nothing out of order
    assert len(set(seen)) == len(seen)  # nothing twice
    assert all(len(items) == limit for items in pages[:-1])
    assert len(pages) == -(-count // limit)


def test_no_duplicates_or_gaps_among_posts_created_at_the_same_instant(
    client: TestClient, session: Session, alice_account: User
) -> None:
    # The hard case for a cursor: the timestamp alone cannot tell where the
    # page ended. Three groups of posts, each sharing one timestamp.
    posts = [
        add_post(session, alice_account, created_at=START + timedelta(minutes=group))
        for group in range(3)
        for _ in range(7)
    ]
    expected = [
        str(post.id)
        for post in sorted(posts, key=lambda p: (p.created_at, p.id), reverse=True)
    ]

    for limit in (1, 2, 3, 5, 7, 8):
        seen = [id for items in walk(client, limit=limit) for id in ids(items)]
        assert seen == expected, limit


def test_new_posts_between_requests_cause_no_duplicates_or_gaps(
    client: TestClient, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, alice_account, 6)
    first = page(client, limit=3).json()

    # While the reader is on page one, three newer posts appear. With offset
    # pagination, page two would now repeat the last three posts of page one.
    newer = add_posts(session, alice_account, 3, start=START + timedelta(days=1))
    second = page(client, limit=3, cursor=first["next_cursor"]).json()

    assert ids(second["items"]) == ids(posts[3:])
    assert second["next_cursor"] is None
    # The new posts are found by starting again from the top.
    assert ids(page(client, limit=3).json()["items"]) == ids(newer)


def test_posts_deleted_between_requests_cause_no_duplicates_or_gaps(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, alice_account, 8)
    first = page(alice_client, limit=3).json()

    # One post already seen, the very post the cursor points at, and one not
    # yet seen are deleted. With offset pagination, posts would be skipped.
    for post in (posts[0], posts[2], posts[4]):
        assert alice_client.delete(f"/posts/{post.id}").status_code == 204
    rest = [
        id
        for items in walk(alice_client, limit=3, cursor=first["next_cursor"])
        for id in ids(items)
    ]

    assert rest == ids([posts[3], posts[5], posts[6], posts[7]])


def test_the_same_cursor_always_gives_the_same_page(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, alice_account, 6)
    cursor = page(client, limit=2).json()["next_cursor"]

    answers = [page(client, limit=2, cursor=cursor).json() for _ in range(3)]

    assert answers[0] == answers[1] == answers[2]


def test_limit_may_change_between_pages(
    client: TestClient, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, alice_account, 6)
    first = page(client, limit=2).json()

    second = page(client, limit=4, cursor=first["next_cursor"]).json()

    assert ids(second["items"]) == ids(posts[2:])


def test_cursor_is_an_opaque_position_and_nothing_more(
    client: TestClient, session: Session, alice_account: User
) -> None:
    posts = add_posts(session, alice_account, 3)

    cursor = page(client, limit=2).json()["next_cursor"]

    # Usable in a query string as it is, and it carries only what the page
    # already showed: the time and id of its last post.
    assert re.fullmatch(r"[A-Za-z0-9_-]{32}", cursor)
    last_shown = posts[1]
    assert decode_cursor(cursor) == Cursor(
        created_at=last_shown.created_at, id=last_shown.id
    )


def test_paging_works_for_the_owner_of_a_private_account(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    make_private(session, alice_account)
    posts = add_posts(session, alice_account, 5)

    seen = [id for items in walk(alice_client, limit=2) for id in ids(items)]

    assert seen == ids(posts)


# --- a cursor is not a key -----------------------------------------------


def test_cursor_does_not_open_a_private_accounts_posts(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
) -> None:
    make_private(session, alice_account)
    add_posts(session, alice_account, 5)
    # A genuine cursor into the private list, as its owner received it.
    cursor = page(alice_client, limit=2).json()["next_cursor"]

    response = page(bob_client, limit=2, cursor=cursor)

    assert response.status_code == 403
    assert response.json() == PRIVATE


def test_cursor_obtained_while_public_stops_working_once_the_account_is_private(
    alice_client: TestClient, make_client, session: Session, alice_account: User
) -> None:
    add_posts(session, alice_account, 5)
    anonymous = make_client()
    cursor = page(anonymous, limit=2).json()["next_cursor"]

    alice_client.patch("/users/me", json={"is_private": True})

    assert page(anonymous, limit=2, cursor=cursor).status_code == 403


def test_cursor_from_one_users_list_shows_nothing_of_that_user_elsewhere(
    client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    add_posts(session, alice_account, 5)
    bobs = add_posts(session, bob_account, 5, start=START - timedelta(days=1))
    alices_cursor = page(client, "alice", limit=2).json()["next_cursor"]

    response = page(client, "bob", limit=10, cursor=alices_cursor)

    # It is only a point in time: bob's posts older than it, and only bob's.
    assert response.status_code == 200
    assert ids(response.json()["items"]) == ids(bobs)


def test_forged_cursor_cannot_reach_deleted_posts(
    client: TestClient, session: Session, alice_account: User
) -> None:
    deleted = add_post(
        session, alice_account, "Removed", created_at=START, deleted=True
    )
    just_after = encode_cursor(
        Cursor(created_at=deleted.created_at + timedelta(seconds=1), id=deleted.id)
    )
    # The same instant, and an id that sorts after every other.
    exactly_at = encode_cursor(
        Cursor(created_at=deleted.created_at, id=uuid.UUID(int=2**128 - 1))
    )

    for cursor in (just_after, exactly_at):
        response = page(client, cursor=cursor)
        assert response.status_code == 200
        assert response.json() == {"items": [], "next_cursor": None}


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
    add_posts(session, alice_account, 3)

    response = page(client, cursor=cursor)

    assert response.status_code == 400
    assert response.json() == INVALID_CURSOR


def test_rejected_cursor_is_not_echoed(client: TestClient, alice_account: User) -> None:
    response = page(client, cursor="something-the-client-sent")

    assert response.status_code == 400
    assert "something" not in response.text


def test_malformed_cursor_is_rejected_before_the_account_is_looked_at(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    make_private(session, alice_account)

    # The same answer for every account, so it says nothing about any.
    answers = [
        page(bob_client, name, cursor="abc") for name in ("alice", "nonexistent")
    ]

    assert {response.status_code for response in answers} == {400}
    assert answers[0].text == answers[1].text


def test_default_limit(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, alice_account, DEFAULT_PAGE_SIZE + 5)

    body = page(client).json()

    assert DEFAULT_PAGE_SIZE == 20
    assert len(body["items"]) == DEFAULT_PAGE_SIZE
    assert body["next_cursor"] is not None


def test_limit_is_respected(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, alice_account, 10)

    for limit in (1, 2, 9, 10):
        assert len(page(client, limit=limit).json()["items"]) == limit


def test_maximum_limit_is_accepted(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, alice_account, MAX_PAGE_SIZE + 10)

    body = page(client, limit=MAX_PAGE_SIZE).json()

    assert MAX_PAGE_SIZE == 50
    assert len(body["items"]) == MAX_PAGE_SIZE
    assert body["next_cursor"] is not None


@pytest.mark.parametrize("limit", [MAX_PAGE_SIZE + 1, 100, 1000, 10**9, 10**30])
def test_limit_above_the_maximum_is_rejected(
    client: TestClient, session: Session, alice_account: User, limit: int
) -> None:
    add_posts(session, alice_account, 3)

    response = page(client, limit=limit)

    # Refused outright; the server never loads more than the maximum.
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "limit"]


@pytest.mark.parametrize(
    "limit", [0, -1, -50, "abc", "", "1.5", "ten", "1e3", "null", "5; DROP"]
)
def test_invalid_limit_is_rejected(
    client: TestClient, alice_account: User, limit: object
) -> None:
    response = page(client, limit=limit)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "limit"]


@pytest.mark.parametrize("param", ["offset", "page", "skip", "page_size", "per_page"])
def test_there_is_no_offset_pagination(
    client: TestClient, session: Session, alice_account: User, param: str
) -> None:
    posts = add_posts(session, alice_account, 5)

    response = client.get(f"/users/alice/posts?limit=2&{param}=2")

    # Ignored: the answer is the first page all the same.
    assert ids(response.json()["items"]) == ids(posts[:2])


def test_documented_parameters_are_limit_and_cursor() -> None:
    operation = app.openapi()["paths"]["/users/{username}/posts"]["get"]
    parameters = {param["name"]: param for param in operation["parameters"]}

    assert set(parameters) == {"username", "limit", "cursor"}
    assert parameters["limit"]["schema"]["maximum"] == MAX_PAGE_SIZE
    assert parameters["limit"]["schema"]["minimum"] == 1
    assert parameters["limit"]["schema"]["default"] == DEFAULT_PAGE_SIZE


# --- cost and contents ---------------------------------------------------


def test_listing_takes_the_same_queries_however_many_posts_there_are(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_post(session, alice_account)
    session.expunge_all()
    with recorded_selects() as with_one:
        page(client, limit=50)

    add_posts(session, add_user(session, "carol"), 3)
    author = session.get(User, alice_account.id)
    add_posts(session, author, 40)
    session.expunge_all()
    with recorded_selects() as with_many:
        assert len(page(client, limit=50).json()["items"]) == 41

    # One to find the account, one for the page with its authors.
    assert len(with_one) == len(with_many) == 2


def test_listing_does_not_read_the_authors_private_columns(
    client: TestClient, session: Session, alice_account: User
) -> None:
    add_posts(session, alice_account, 3)
    session.expunge_all()

    with recorded_selects() as statements:
        assert page(client).status_code == 200

    for statement in statements:
        assert "password_hash" not in statement
        assert not re.search(r"users\.email\b(?!_)", statement)


def test_listing_exposes_no_account_data(
    make_client, session: Session, alice_account: User
) -> None:
    add_posts(session, alice_account, 3)
    alice = make_client()
    log_in(alice, "alice")

    for viewer in (make_client(), alice):
        response = page(viewer)
        assert keys_in(response.json()).isdisjoint(
            SENSITIVE_KEYS | {"email", "is_active", "email_verified_at", "deleted_at"}
        )
        assert alice_account.email not in response.text
        assert alice_account.password_hash not in response.text
        assert alice.cookies.get("hopsnop_session") not in response.text

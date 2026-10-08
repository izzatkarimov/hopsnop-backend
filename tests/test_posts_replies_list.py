"""GET /posts/{post_id}/replies: the replies to a post, with cursor pagination."""

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
from app.models import Follow, Like, Post, Repost, User
from app.services import posts as posts_service
from helpers import (
    SENSITIVE_KEYS,
    add_post,
    add_user,
    follow,
    keys_in,
    log_in,
    plant_session_cookie,
    post_columns,
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
NOT_FOUND = {"detail": "Post not found."}
INVALID_CURSOR = {"detail": "Invalid cursor."}
FOREIGN_ORIGIN = "https://evil.example"
START = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)
# Every reason a post is not shown to anyone.
HIDDEN = ["deleted", "inactive", "unverified"]


def replies(client: TestClient, post_id: object, **params: object):
    return client.get(f"/posts/{post_id}/replies", params=params)


def add_replies(
    session: Session, parent: Post, count: int, *authors: User, start: datetime = START
) -> list[Post]:
    """``count`` replies one minute apart, by the authors in turn, newest first."""
    posts = [
        add_post(
            session,
            authors[number % len(authors)],
            f"Reply {number}",
            parent=parent,
            created_at=start + (number + 1) * MINUTE,
        )
        for number in range(count)
    ]
    return posts[::-1]


def add_hidden_post(
    session: Session,
    kind: str,
    content: str = "Hidden",
    *,
    parent: Post | None = None,
    created_at: datetime | None = None,
) -> Post:
    """A post that is not shown, for the reason ``kind`` names."""
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
    client: TestClient, post_id: object, *, limit: int, cursor: str | None = None
) -> list[list[dict]]:
    """Every page from ``cursor`` on, following next_cursor until there is none."""
    pages = []
    while True:
        extra = {"cursor": cursor} if cursor else {}
        response = replies(client, post_id, limit=limit, **extra)
        assert response.status_code == 200, response.text
        body = response.json()
        pages.append(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) < 200, "pagination does not terminate"


@pytest.fixture
def parent(session: Session, alice_account: User) -> Post:
    return add_post(session, alice_account, "Parent", created_at=START)


@pytest.fixture
def carol(session: Session) -> User:
    return add_user(session, "carol")


# --- the list ------------------------------------------------------------


def test_post_without_replies_has_an_empty_list(
    client: TestClient, parent: Post
) -> None:
    response = replies(client, parent.id)

    assert response.status_code == 200
    assert response.json() == EMPTY


def test_one_reply(
    client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    reply = add_post(session, bob_account, "Hi alice", parent=parent)

    body = replies(client, parent.id).json()

    assert ids(body["items"]) == [str(reply.id)]
    assert body["next_cursor"] is None
    [item] = body["items"]
    assert item["content"] == "Hi alice"
    assert item["parent_post_id"] == str(parent.id)
    assert item["is_reply"] is True
    assert item["author"]["username"] == "bob"


def test_several_replies_come_newest_first(
    client: TestClient,
    session: Session,
    parent: Post,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    posts = add_replies(session, parent, 6, bob_account, carol, alice_account)

    items = replies(client, parent.id).json()["items"]

    assert ids(items) == ids(posts)
    assert {item["parent_post_id"] for item in items} == {str(parent.id)}
    assert [item["author"]["username"] for item in items] == [
        "alice",
        "carol",
        "bob",
    ] * 2


def test_items_are_posts_in_their_usual_shape(
    client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    reply = add_post(session, bob_account, "Reply", parent=parent)

    body = replies(client, parent.id).json()

    assert set(body) == {"items", "next_cursor"}
    [item] = body["items"]
    assert set(item) == POST_FIELDS
    assert set(item["author"]) == AUTHOR_FIELDS
    # The very post that reading it alone gives, and the feed.
    assert item == client.get(f"/posts/{reply.id}").json()
    assert item in client.get("/feed").json()["items"]


def test_the_post_itself_is_not_among_its_replies(
    client: TestClient, session: Session, parent: Post, alice_account: User
) -> None:
    add_post(session, alice_account, "Another post, not a reply")
    reply = add_post(session, alice_account, "Reply", parent=parent)

    assert ids(replies(client, parent.id).json()["items"]) == [str(reply.id)]


def test_only_direct_replies_are_listed(
    client: TestClient,
    session: Session,
    parent: Post,
    alice_account: User,
    bob_account: User,
) -> None:
    first = add_post(
        session, bob_account, "First", parent=parent, created_at=START + MINUTE
    )
    second = add_post(
        session, alice_account, "Second", parent=first, created_at=START + 2 * MINUTE
    )
    third = add_post(
        session, bob_account, "Third", parent=second, created_at=START + 3 * MINUTE
    )

    # Each level is the list of the post above it, and of that post only.
    assert ids(replies(client, parent.id).json()["items"]) == [str(first.id)]
    assert ids(replies(client, first.id).json()["items"]) == [str(second.id)]
    assert ids(replies(client, second.id).json()["items"]) == [str(third.id)]
    assert replies(client, third.id).json() == EMPTY


def test_replies_to_other_posts_are_not_listed(
    client: TestClient,
    session: Session,
    parent: Post,
    alice_account: User,
    bob_account: User,
) -> None:
    other = add_post(session, alice_account, "Other", created_at=START)
    mine = add_replies(session, parent, 2, bob_account)
    theirs = add_replies(session, other, 3, bob_account)

    assert ids(replies(client, parent.id).json()["items"]) == ids(mine)
    assert ids(replies(client, other.id).json()["items"]) == ids(theirs)


def test_reply_published_through_the_api_is_listed(
    alice_client: TestClient, bob_client: TestClient, make_client
) -> None:
    post = alice_client.post("/posts", json={"content": "Start"}).json()
    created = bob_client.post(
        "/posts", json={"content": "Answer", "parent_post_id": post["id"]}
    ).json()

    listed = make_client().get(f"/posts/{post['id']}/replies").json()["items"]

    # What the author was answered with, but for whose "by me" it is.
    assert listed == [created]


def test_authors_own_reply_to_their_own_post_is_listed(
    alice_client: TestClient, session: Session, parent: Post, alice_account: User
) -> None:
    own = add_post(session, alice_account, "And another thing", parent=parent)

    items = replies(alice_client, parent.id).json()["items"]

    assert ids(items) == [str(own.id)]
    assert items[0]["author"]["username"] == "alice"


def test_responses_are_not_to_be_cached(
    client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    add_post(session, bob_account, "Reply", parent=parent)

    assert replies(client, parent.id).headers["cache-control"] == "no-store"


# --- who may ask ---------------------------------------------------------


def test_list_needs_no_authentication_and_is_the_same_for_everyone(
    make_client,
    session: Session,
    parent: Post,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    posts = add_replies(session, parent, 4, bob_account, alice_account)
    viewers = [make_client() for _ in range(4)]
    for viewer, name in zip(viewers, ("alice", "bob", "carol")):
        log_in(viewer, name)

    answers = [replies(viewer, parent.id) for viewer in viewers]

    assert {response.status_code for response in answers} == {200}
    assert all(ids(a.json()["items"]) == ids(posts) for a in answers)
    assert len({response.text for response in answers}) == 1


def test_following_plays_no_part(
    make_client,
    session: Session,
    parent: Post,
    alice_account: User,
    bob_account: User,
    carol: User,
) -> None:
    # Alice posts, bob replies, carol follows neither of them.
    reply = add_post(session, bob_account, "Bob's reply", parent=parent)
    carol_client = make_client()
    log_in(carol_client, "carol")
    before = replies(carol_client, parent.id).json()

    follow(session, carol, alice_account)
    follow(session, carol, bob_account)

    assert ids(before["items"]) == [str(reply.id)]
    assert replies(carol_client, parent.id).json() == before


def test_stale_cookie_is_answered_as_anonymous(
    client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    reply = add_post(session, bob_account, "Reply", parent=parent)
    plant_session_cookie(client, "left-over-from-an-old-login")

    response = replies(client, parent.id)

    assert response.status_code == 200
    assert ids(response.json()["items"]) == [str(reply.id)]


def test_deactivated_viewer_is_answered_as_anonymous(
    bob_client: TestClient,
    session: Session,
    parent: Post,
    bob_account: User,
    carol: User,
) -> None:
    add_post(session, bob_account, "Written while active", parent=parent)
    carols = add_post(session, carol, "Carol's", parent=parent)
    bob_client.post(f"/posts/{carols.id}/like")
    bob_account.is_active = False
    session.flush()

    response = replies(bob_client, parent.id)

    assert response.status_code == 200
    [item] = response.json()["items"]
    assert item["id"] == str(carols.id)
    # The like is still counted. It is nobody's "mine" on this request.
    assert (item["like_count"], item["liked_by_me"]) == (1, False)


# --- the post whose replies are asked for --------------------------------


def test_post_that_does_not_exist_is_not_found(
    client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    add_post(session, bob_account, "Reply", parent=parent)

    response = replies(client, uuid.uuid4())

    assert response.status_code == 404
    assert response.json() == NOT_FOUND


@pytest.mark.parametrize("kind", HIDDEN)
def test_replies_of_a_hidden_post_cannot_be_listed(
    make_client, session: Session, bob_account: User, kind: str
) -> None:
    hidden = add_hidden_post(session, kind, created_at=START)
    add_replies(session, hidden, 3, bob_account)
    bob = make_client()
    log_in(bob, "bob")

    for viewer in (make_client(), bob):
        response = replies(viewer, hidden.id)

        # The answer of a post that does not exist, though bob's own
        # replies are among those not listed.
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
        assert "Reply" not in response.text


def test_author_cannot_list_the_replies_to_their_own_deleted_post(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    parent: Post,
    bob_account: User,
) -> None:
    add_post(session, bob_account, "Reply", parent=parent)
    assert replies(alice_client, parent.id).status_code == 200

    assert alice_client.delete(f"/posts/{parent.id}").status_code == 204

    for viewer in (alice_client, bob_client):
        assert replies(viewer, parent.id).status_code == 404


def test_reply_to_a_hidden_post_is_still_a_post_of_its_own(
    client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    reply = add_post(session, bob_account, "Reply", parent=parent)
    nested = add_post(session, bob_account, "Nested", parent=reply)
    parent.deleted_at = START
    session.flush()

    # Not listed under the post that is gone, and nothing else about it
    # changes: it is read, it is in the feed, and its own replies are listed.
    assert replies(client, parent.id).status_code == 404
    assert client.get(f"/posts/{reply.id}").status_code == 200
    assert str(reply.id) in ids(client.get("/feed").json()["items"])
    assert ids(replies(client, reply.id).json()["items"]) == [str(nested.id)]


def test_list_leaves_and_returns_with_the_parents_authors_account(
    client: TestClient,
    session: Session,
    parent: Post,
    alice_account: User,
    bob_account: User,
) -> None:
    posts = add_replies(session, parent, 2, bob_account)

    alice_account.is_active = False
    session.flush()
    assert replies(client, parent.id).status_code == 404

    alice_account.is_active = True
    session.flush()
    assert ids(replies(client, parent.id).json()["items"]) == ids(posts)


def test_hidden_post_and_missing_post_are_answered_alike(
    client: TestClient, session: Session, bob_account: User
) -> None:
    answers = [replies(client, uuid.uuid4())]
    for kind in HIDDEN:
        hidden = add_hidden_post(session, kind)
        add_post(session, bob_account, "Reply", parent=hidden)
        answers.append(replies(client, hidden.id))
        answers.append(client.get(f"/posts/{hidden.id}"))

    assert {response.status_code for response in answers} == {404}
    assert len({response.text for response in answers}) == 1
    assert len({tuple(sorted(response.headers)) for response in answers}) == 1


def test_hidden_post_takes_the_same_queries_as_one_that_does_not_exist(
    client: TestClient, session: Session, bob_account: User
) -> None:
    session.expunge_all()
    with recorded_selects() as missing:
        assert replies(client, uuid.uuid4()).status_code == 404

    counts = []
    for kind in HIDDEN:
        hidden = add_hidden_post(session, kind)
        bob = session.get(User, bob_account.id)
        add_post(session, bob, "Reply", parent=hidden)
        hidden_id = hidden.id
        session.expunge_all()
        with recorded_selects() as statements:
            assert replies(client, hidden_id).status_code == 404
        counts.append(len(statements))

    # The work done does not tell a hidden post from none at all, and the
    # replies are never read.
    assert len(missing) == 1
    assert counts == [1, 1, 1]


@pytest.mark.parametrize("post_id", ["abc", "1", "not-a-uuid", "null", "0" * 31])
def test_malformed_post_id_is_rejected(client: TestClient, post_id: str) -> None:
    response = client.get(f"/posts/{post_id}/replies")

    assert response.status_code == 422
    assert set(response.json()) == {"detail"}
    # What was sent is not sent back.
    assert "input" not in keys_in(response.json())


# --- replies that are not shown ------------------------------------------


@pytest.mark.parametrize("kind", HIDDEN)
def test_hidden_reply_is_not_listed(
    make_client,
    session: Session,
    parent: Post,
    alice_account: User,
    bob_account: User,
    kind: str,
) -> None:
    shown = add_post(session, bob_account, "Shown", parent=parent, created_at=START)
    add_hidden_post(session, kind, parent=parent, created_at=START + MINUTE)
    alice = make_client()
    log_in(alice, "alice")

    for viewer in (make_client(), alice):
        response = replies(viewer, parent.id)

        # Not even for the author of the post that was answered.
        assert ids(response.json()["items"]) == [str(shown.id)]
        assert "Hidden" not in response.text


def test_deleted_reply_is_not_listed_for_its_own_author(
    bob_client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    kept, removed = add_replies(session, parent, 2, bob_account)

    assert bob_client.delete(f"/posts/{removed.id}").status_code == 204

    assert ids(replies(bob_client, parent.id).json()["items"]) == [str(kept.id)]


def test_replies_leave_with_a_deactivation_and_return_with_a_reactivation(
    client: TestClient,
    session: Session,
    parent: Post,
    bob_account: User,
    carol: User,
) -> None:
    posts = add_replies(session, parent, 4, bob_account, carol)

    bob_account.is_active = False
    session.flush()
    carols = [post for post in posts if post.author_id == carol.id]
    assert ids(replies(client, parent.id).json()["items"]) == ids(carols)

    bob_account.is_active = True
    session.flush()
    assert ids(replies(client, parent.id).json()["items"]) == ids(posts)


@pytest.mark.parametrize("kind", HIDDEN)
def test_hidden_reply_does_not_take_up_room_on_a_page(
    client: TestClient, session: Session, parent: Post, bob_account: User, kind: str
) -> None:
    posts = add_replies(session, parent, 4, bob_account)
    for number in range(4):
        add_hidden_post(
            session,
            kind,
            parent=parent,
            created_at=START + (number + 1) * MINUTE + timedelta(seconds=30),
        )

    first = replies(client, parent.id, limit=3).json()
    second = replies(client, parent.id, limit=3, cursor=first["next_cursor"]).json()

    assert ids(first["items"]) == ids(posts[:3])
    assert ids(second["items"]) == ids(posts[3:])
    assert second["next_cursor"] is None


@pytest.mark.parametrize("kind", HIDDEN)
def test_page_is_the_last_one_when_only_hidden_replies_remain(
    client: TestClient, session: Session, parent: Post, bob_account: User, kind: str
) -> None:
    posts = add_replies(session, parent, 2, bob_account)
    add_hidden_post(session, kind, parent=parent, created_at=START)

    body = replies(client, parent.id, limit=2).json()

    assert ids(body["items"]) == ids(posts)
    assert body["next_cursor"] is None


def test_list_of_only_hidden_replies_is_empty(
    client: TestClient, session: Session, parent: Post
) -> None:
    for kind in HIDDEN:
        add_hidden_post(session, kind, parent=parent)

    assert replies(client, parent.id).json() == EMPTY


def test_hidden_reply_keeps_its_own_replies_from_being_listed_and_nothing_else(
    client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    hidden = add_hidden_post(session, "inactive", parent=parent)
    under_it = add_post(session, bob_account, "Under the hidden one", parent=hidden)

    assert replies(client, parent.id).json() == EMPTY
    assert replies(client, hidden.id).status_code == 404
    # Still a post of bob's, and not a reply to the post asked about.
    assert client.get(f"/posts/{under_it.id}").status_code == 200


# --- likes and reposts ---------------------------------------------------


def test_counts_and_the_viewers_own_state_are_on_each_reply(
    alice_client: TestClient,
    bob_client: TestClient,
    make_client,
    session: Session,
    parent: Post,
    bob_account: User,
    carol: User,
) -> None:
    liked, reposted, untouched = add_replies(session, parent, 3, carol)
    bob_client.post(f"/posts/{liked.id}/like")
    alice_client.post(f"/posts/{liked.id}/like")
    bob_client.post(f"/posts/{reposted.id}/repost")
    alice_client.post(f"/posts/{untouched.id}/repost")
    # On the parent: not counted on any reply.
    bob_client.post(f"/posts/{parent.id}/like")

    def states(client: TestClient) -> dict[str, tuple]:
        return {
            item["id"]: (
                item["like_count"],
                item["liked_by_me"],
                item["repost_count"],
                item["reposted_by_me"],
            )
            for item in replies(client, parent.id).json()["items"]
        }

    assert states(bob_client) == {
        str(liked.id): (2, True, 0, False),
        str(reposted.id): (0, False, 1, True),
        str(untouched.id): (0, False, 1, False),
    }
    # The same counts for someone who is not signed in, and nothing "mine".
    assert states(make_client()) == {
        str(liked.id): (2, False, 0, False),
        str(reposted.id): (0, False, 1, False),
        str(untouched.id): (0, False, 1, False),
    }


def test_interaction_changes_show_at_once(
    bob_client: TestClient, session: Session, parent: Post, carol: User
) -> None:
    reply = add_post(session, carol, "Reply", parent=parent)

    def state() -> tuple:
        [item] = replies(bob_client, parent.id).json()["items"]
        return (item["like_count"], item["liked_by_me"], item["repost_count"])

    assert state() == (0, False, 0)
    bob_client.post(f"/posts/{reply.id}/like")
    bob_client.post(f"/posts/{reply.id}/repost")
    assert state() == (1, True, 1)
    bob_client.delete(f"/posts/{reply.id}/like")
    assert state() == (0, False, 1)


def test_viewer_cannot_be_named_by_the_request(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    parent: Post,
    alice_account: User,
    carol: User,
) -> None:
    reply = add_post(session, carol, "Reply", parent=parent)
    alice_client.post(f"/posts/{reply.id}/like")

    response = bob_client.get(
        f"/posts/{parent.id}/replies?viewer_id={alice_account.id}"
        f"&user_id={alice_account.id}&username=alice&as=alice"
    )

    [item] = response.json()["items"]
    assert (item["like_count"], item["liked_by_me"]) == (1, False)


# --- pagination ----------------------------------------------------------


def test_first_next_and_last_page(
    client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    posts = add_replies(session, parent, 5, bob_account)

    first = replies(client, parent.id, limit=2).json()
    second = replies(client, parent.id, limit=2, cursor=first["next_cursor"]).json()
    third = replies(client, parent.id, limit=2, cursor=second["next_cursor"]).json()

    assert ids(first["items"]) == ids(posts[:2])
    assert ids(second["items"]) == ids(posts[2:4])
    assert ids(third["items"]) == ids(posts[4:])
    assert first["next_cursor"] and second["next_cursor"]
    assert third["next_cursor"] is None


def test_page_that_exactly_holds_the_rest_has_no_cursor(
    client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    posts = add_replies(session, parent, 4, bob_account)

    whole = replies(client, parent.id, limit=4).json()

    assert ids(whole["items"]) == ids(posts)
    assert whole["next_cursor"] is None


@pytest.mark.parametrize("limit", [1, 2, 3, 5, 50])
def test_walking_the_pages_returns_every_reply_exactly_once_in_order(
    client: TestClient,
    session: Session,
    parent: Post,
    alice_account: User,
    bob_account: User,
    carol: User,
    limit: int,
) -> None:
    posts = add_replies(session, parent, 12, bob_account, carol, alice_account)
    # In between, and never returned: hidden replies, replies to another
    # post, replies to a reply, and posts that are not replies.
    other = add_post(session, alice_account, "Other", created_at=START)
    for number in range(4):
        at = START + (number + 1) * MINUTE + timedelta(seconds=30)
        add_hidden_post(session, HIDDEN[number % 3], parent=parent, created_at=at)
        add_post(session, bob_account, "Elsewhere", parent=other, created_at=at)
        add_post(session, carol, "Nested", parent=posts[number], created_at=at)
        add_post(session, carol, "Top level", created_at=at)

    pages = walk(client, parent.id, limit=limit)

    walked = [item for page in pages for item in page]
    assert ids(walked) == ids(posts)
    assert all(len(page) == limit for page in pages[:-1])
    assert len(pages) == -(-12 // limit)


@pytest.mark.parametrize("limit", [1, 2, 3, 5])
def test_replies_created_at_the_same_instant_have_a_fixed_order(
    client: TestClient,
    session: Session,
    parent: Post,
    bob_account: User,
    carol: User,
    limit: int,
) -> None:
    newer = add_post(
        session, bob_account, "Newer", parent=parent, created_at=START + 2 * MINUTE
    )
    tied = [
        add_post(
            session,
            (bob_account, carol)[number % 2],
            f"Tie {number}",
            parent=parent,
            created_at=START + MINUTE,
        )
        for number in range(7)
    ]
    expected = [
        str(newer.id),
        *sorted((str(post.id) for post in tied), key=uuid.UUID, reverse=True),
    ]

    walked = [item for page in walk(client, parent.id, limit=limit) for item in page]

    # By id among themselves, on every request: none twice and none missed.
    assert ids(walked) == expected


def test_many_replies(
    client: TestClient, session: Session, parent: Post, bob_account: User, carol: User
) -> None:
    posts = add_replies(session, parent, 130, bob_account, carol)

    assert len(replies(client, parent.id).json()["items"]) == DEFAULT_PAGE_SIZE
    pages = walk(client, parent.id, limit=MAX_PAGE_SIZE)

    assert [len(page) for page in pages] == [50, 50, 30]
    assert ids([item for page in pages for item in page]) == ids(posts)


def test_new_reply_between_requests_causes_no_duplicates_or_gaps(
    bob_client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    posts = add_replies(session, parent, 5, bob_account)
    first = replies(bob_client, parent.id, limit=2).json()

    bob_client.post(
        "/posts", json={"content": "Brand new", "parent_post_id": str(parent.id)}
    )
    rest = walk(bob_client, parent.id, limit=2, cursor=first["next_cursor"])

    assert ids([*first["items"], *(i for page in rest for i in page)]) == ids(posts)


def test_reply_deleted_between_requests_causes_no_duplicates_or_gaps(
    bob_client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    posts = add_replies(session, parent, 6, bob_account)
    first = replies(bob_client, parent.id, limit=2).json()

    bob_client.delete(f"/posts/{posts[0].id}")
    bob_client.delete(f"/posts/{posts[3].id}")
    rest = walk(bob_client, parent.id, limit=2, cursor=first["next_cursor"])

    assert ids([i for page in rest for i in page]) == ids([posts[2], *posts[4:]])


def test_parent_deleted_between_requests_ends_the_list(
    alice_client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    add_replies(session, parent, 5, bob_account)
    cursor = replies(alice_client, parent.id, limit=2).json()["next_cursor"]

    alice_client.delete(f"/posts/{parent.id}")

    # A cursor that was genuine a moment ago opens nothing.
    response = replies(alice_client, parent.id, limit=2, cursor=cursor)
    assert response.status_code == 404
    assert response.json() == NOT_FOUND


def test_cursor_is_the_position_of_the_last_reply_shown(
    client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    posts = add_replies(session, parent, 3, bob_account)

    cursor = replies(client, parent.id, limit=2).json()["next_cursor"]

    assert re.fullmatch(r"[A-Za-z0-9_-]{32}", cursor)
    assert decode_cursor(cursor) == Cursor(
        created_at=posts[1].created_at, id=posts[1].id
    )


def test_cursor_is_the_one_every_list_of_posts_uses(
    client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    posts = add_replies(session, parent, 4, bob_account)

    from_feed = client.get("/feed?limit=2").json()["next_cursor"]
    from_list = replies(client, parent.id, limit=2).json()["next_cursor"]

    assert from_feed == from_list
    assert ids(replies(client, parent.id, cursor=from_feed).json()["items"]) == ids(
        posts[2:]
    )


# --- a cursor is not a key -----------------------------------------------


@pytest.mark.parametrize("kind", HIDDEN)
def test_forged_cursor_cannot_reach_a_hidden_reply(
    client: TestClient, session: Session, parent: Post, kind: str
) -> None:
    hidden = add_hidden_post(session, kind, parent=parent, created_at=START + MINUTE)
    cursors = [
        encode_cursor(Cursor(created_at=START + 2 * MINUTE, id=hidden.id)),
        # The same instant, and an id that sorts after every other.
        encode_cursor(
            Cursor(created_at=START + MINUTE, id=uuid.UUID(int=2**128 - 1))
        ),
    ]

    for cursor in cursors:
        response = replies(client, parent.id, cursor=cursor)
        assert response.status_code == 200
        assert response.json() == EMPTY


def test_forged_cursor_cannot_reach_another_posts_replies_or_any_other_post(
    client: TestClient,
    session: Session,
    parent: Post,
    alice_account: User,
    bob_account: User,
) -> None:
    mine = add_post(session, bob_account, "Mine", parent=parent, created_at=START)
    other = add_post(session, alice_account, "Other", created_at=START + MINUTE)
    theirs = add_post(
        session, bob_account, "Theirs", parent=other, created_at=START + 2 * MINUTE
    )
    cursor = encode_cursor(Cursor(created_at=START + 3 * MINUTE, id=theirs.id))

    items = replies(client, parent.id, cursor=cursor).json()["items"]

    assert ids(items) == [str(mine.id)]


@pytest.mark.parametrize("kind", HIDDEN)
def test_no_cursor_opens_the_replies_of_a_hidden_post(
    client: TestClient, session: Session, bob_account: User, kind: str
) -> None:
    hidden = add_hidden_post(session, kind, created_at=START)
    reply = add_post(
        session, bob_account, "Reply", parent=hidden, created_at=START + MINUTE
    )
    cursor = encode_cursor(Cursor(created_at=START + 2 * MINUTE, id=reply.id))

    assert replies(client, hidden.id, cursor=cursor).status_code == 404


def test_well_formed_cursor_that_was_never_issued_is_just_a_position(
    client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    posts = add_replies(session, parent, 4, bob_account)
    between = encode_cursor(
        Cursor(created_at=posts[1].created_at - timedelta(seconds=30), id=uuid.uuid4())
    )
    before_all = encode_cursor(Cursor(created_at=START, id=uuid.uuid4()))

    assert ids(replies(client, parent.id, cursor=between).json()["items"]) == ids(
        posts[2:]
    )
    assert replies(client, parent.id, cursor=before_all).json() == EMPTY


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
        "é" * 32,
        "2026-10-01T12:00:00Z",
        str(uuid.uuid4()),
        "' OR '1'='1",
        "A" * 5000,
    ],
)
def test_malformed_cursor_is_rejected(
    client: TestClient, session: Session, parent: Post, bob_account: User, cursor: str
) -> None:
    add_replies(session, parent, 3, bob_account)

    response = replies(client, parent.id, cursor=cursor)

    assert response.status_code == 400
    assert response.json() == INVALID_CURSOR


def test_malformed_cursor_gets_the_answer_every_list_gives(
    client: TestClient, parent: Post
) -> None:
    answers = [
        replies(client, parent.id, cursor="something-the-client-sent"),
        client.get("/feed?cursor=something-the-client-sent"),
        client.get("/users/alice/posts?cursor=something-the-client-sent"),
    ]

    assert {response.status_code for response in answers} == {400}
    assert len({response.text for response in answers}) == 1
    assert "something" not in answers[0].text


def test_malformed_cursor_is_answered_alike_for_a_post_that_is_not_there(
    client: TestClient, parent: Post
) -> None:
    # Decided before any post is looked for, so it says nothing about one.
    here = replies(client, parent.id, cursor="abc")
    nowhere = replies(client, uuid.uuid4(), cursor="abc")

    assert here.status_code == nowhere.status_code == 400
    assert here.text == nowhere.text


def test_limits_are_those_of_every_list(
    client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    add_replies(session, parent, MAX_PAGE_SIZE + 5, bob_account)

    assert len(replies(client, parent.id).json()["items"]) == DEFAULT_PAGE_SIZE
    assert len(replies(client, parent.id, limit=1).json()["items"]) == 1
    assert len(replies(client, parent.id, limit=MAX_PAGE_SIZE).json()["items"]) == 50


@pytest.mark.parametrize(
    "limit", [0, -1, MAX_PAGE_SIZE + 1, 1000, 10**30, "abc", "", "1.5", "5; DROP"]
)
def test_invalid_limit_is_rejected_as_the_feed_rejects_it(
    client: TestClient, session: Session, parent: Post, bob_account: User, limit: object
) -> None:
    add_replies(session, parent, 3, bob_account)

    here = replies(client, parent.id, limit=limit)
    there = client.get("/feed", params={"limit": limit})

    assert here.status_code == there.status_code == 422
    assert here.json() == there.json()
    assert "Reply" not in here.text


@pytest.mark.parametrize("param", ["offset", "page", "skip", "page_size", "per_page"])
def test_there_is_no_offset_pagination(
    client: TestClient, session: Session, parent: Post, bob_account: User, param: str
) -> None:
    posts = add_replies(session, parent, 5, bob_account)

    response = replies(client, parent.id, limit=2, **{param: 2})

    # Ignored: the answer is the first page all the same.
    assert ids(response.json()["items"]) == ids(posts[:2])


def test_list_is_paged_and_answered_like_the_other_lists_of_posts() -> None:
    paths = app.openapi()["paths"]
    here = paths["/posts/{post_id}/replies"]
    there = paths["/users/{username}/posts"]["get"]

    def page_parameters(operation: dict) -> list[dict]:
        return [param for param in operation["parameters"] if param["in"] == "query"]

    def answer(operation: dict) -> dict:
        return operation["responses"]["200"]["content"]["application/json"]["schema"]

    # Read-only, the post from the path, and the page parameters and the
    # page of every other list of posts: nothing here is this list's own.
    assert set(here) == {"get"}
    assert page_parameters(here["get"]) == page_parameters(there)
    assert {param["name"] for param in here["get"]["parameters"]} == {
        "post_id",
        "limit",
        "cursor",
    }
    assert answer(here["get"]) == answer(there)
    assert answer(here["get"]) == {"$ref": "#/components/schemas/PostPageResponse"}


# --- no parameter widens it ----------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "include_deleted=true&deleted=1",
        "include_hidden=1&all=1&visibility=all",
        "is_active=false&verified=false",
        "depth=10&nested=true&recursive=1&descendants=all",
        "parent_post_id={other}&post_id={other}&parent={other}",
        "author=inactive_author&username=inactive_author",
    ],
)
def test_no_parameter_widens_the_list(
    client: TestClient,
    session: Session,
    parent: Post,
    alice_account: User,
    bob_account: User,
    query: str,
) -> None:
    shown = add_post(session, bob_account, "Shown", parent=parent)
    add_post(session, bob_account, "Hidden: nested", parent=shown)
    other = add_post(session, alice_account, "Other")
    add_post(session, bob_account, "Hidden: elsewhere", parent=other)
    for kind in HIDDEN:
        add_hidden_post(session, kind, parent=parent)

    plain = replies(client, parent.id)
    asked = client.get(f"/posts/{parent.id}/replies?{query.format(other=other.id)}")

    assert asked.status_code == 200
    assert asked.json() == plain.json()
    assert ids(asked.json()["items"]) == [str(shown.id)]
    assert "Hidden" not in asked.text


# --- reading and nothing else --------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_list_can_only_be_read(
    alice_client: TestClient,
    session: Session,
    parent: Post,
    bob_account: User,
    method: str,
) -> None:
    add_post(session, bob_account, "Reply", parent=parent)
    count = len(session.scalars(select(Post.id)).all())

    response = alice_client.request(
        method, f"/posts/{parent.id}/replies", json={"content": "Planted"}
    )

    # A reply is written like any other post, through POST /posts.
    assert response.status_code == 405
    assert len(session.scalars(select(Post.id)).all()) == count


def test_reading_the_list_changes_nothing(
    bob_client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    posts = add_replies(session, parent, 3, bob_account)
    bob_client.post(f"/posts/{posts[0].id}/like")

    def stored() -> tuple:
        return (
            {id: post_columns(session, id) for id in session.scalars(select(Post.id))},
            set(session.execute(select(Like.user_id, Like.post_id)).all()),
            set(session.execute(select(Repost.user_id, Repost.post_id)).all()),
            set(session.execute(select(Follow.follower_id, Follow.following_id)).all()),
        )

    before = stored()

    cursor = replies(bob_client, parent.id, limit=1).json()["next_cursor"]
    replies(bob_client, parent.id)
    replies(bob_client, parent.id, limit=1, cursor=cursor)
    replies(bob_client, parent.id, cursor="abc")
    replies(bob_client, uuid.uuid4())

    assert stored() == before


def test_response_holds_the_intended_keys_and_no_account_data(
    bob_client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    add_replies(session, parent, 2, bob_account)

    body = replies(bob_client, parent.id).json()

    assert keys_in(body) == POST_FIELDS | AUTHOR_FIELDS | {"items", "next_cursor"}
    assert keys_in(body).isdisjoint(
        SENSITIVE_KEYS | {"email", "is_active", "author_id", "deleted_at", "bio"}
    )
    assert "set-cookie" not in replies(bob_client, parent.id).headers


def test_cross_site_read_gets_no_cors_permission(
    bob_client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    add_post(session, bob_account, "Reply", parent=parent)

    response = bob_client.get(
        f"/posts/{parent.id}/replies", headers={"Origin": FOREIGN_ORIGIN}
    )

    # The browser will not let the other site's page read this.
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_every_error_has_the_same_shape(client: TestClient, parent: Post) -> None:
    errors = [
        replies(client, parent.id, cursor="abc"),  # 400
        replies(client, uuid.uuid4()),  # 404
        replies(client, parent.id, limit=0),  # 422
        replies(client, "not-a-uuid"),  # 422
        client.post(f"/posts/{parent.id}/replies", json={}),  # 405
    ]

    assert [r.status_code for r in errors] == [400, 404, 422, 422, 405]
    for response in errors:
        assert set(response.json()) == {"detail"}
        assert response.headers["content-type"] == "application/json"


def test_unexpected_error_reveals_nothing_about_itself(
    make_client, monkeypatch: pytest.MonkeyPatch, parent: Post
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("connection to postgresql://hopsnop:hopsnop@db failed")

    monkeypatch.setattr(posts_service, "list_replies", fail)
    client = make_client(raise_server_exceptions=False)

    response = replies(client, parent.id)

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    assert "postgresql" not in response.text


# --- cost ----------------------------------------------------------------


def test_list_takes_the_same_queries_however_many_replies_and_authors_there_are(
    client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    parent_id = parent.id
    add_post(session, bob_account, "Reply", parent=parent)
    # Like a real request, which starts with nothing loaded.
    session.expunge_all()
    with recorded_selects() as with_one:
        assert len(replies(client, parent_id, limit=50).json()["items"]) == 1

    parent = session.get(Post, parent_id)
    authors = [add_user(session, f"author{number}") for number in range(8)]
    add_replies(session, parent, 40, *authors)
    session.expunge_all()
    with recorded_selects() as with_many:
        items = replies(client, parent_id, limit=50).json()["items"]

    assert len(items) == 41
    assert len({item["author"]["id"] for item in items}) == 9
    # One to find the post, one for the page with its authors and counts:
    # no query per reply, and none per author.
    assert len(with_one) == len(with_many) == 2


def test_list_takes_no_more_queries_per_reply_for_a_signed_in_viewer(
    alice_client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    parent_id = parent.id
    posts = add_replies(session, parent, 30, bob_account)
    for post in posts[:10]:
        alice_client.post(f"/posts/{post.id}/like")
    session.expunge_all()
    with recorded_selects() as statements:
        items = replies(alice_client, parent_id, limit=50).json()["items"]

    assert sum(item["liked_by_me"] for item in items) == 10
    # One more than for an anonymous request: the session and its user.
    assert len(statements) == 3


def test_empty_list_takes_the_same_queries(
    client: TestClient, session: Session, parent: Post
) -> None:
    parent_id = parent.id
    session.expunge_all()
    with recorded_selects() as statements:
        assert replies(client, parent_id).json() == EMPTY

    assert len(statements) == 2


def test_list_is_filtered_ordered_and_cut_by_the_database(
    client: TestClient,
    session: Session,
    parent: Post,
    alice_account: User,
    bob_account: User,
) -> None:
    parent_id = parent.id
    add_replies(session, parent, 5, bob_account)
    add_post(session, alice_account, "Not a reply")
    cursor = replies(client, parent_id, limit=2).json()["next_cursor"]
    session.expunge_all()

    with recorded_selects() as statements:
        page = replies(client, parent_id, limit=2, cursor=cursor).json()
    assert len(page["items"]) == 2

    lookup, statement = (" ".join(statement.split()) for statement in statements)
    # The post asked about is looked for under the rules every read uses.
    for condition in (
        "posts.id = ",
        "posts.deleted_at IS NULL",
        "users.is_active IS true",
        "users.email_verified_at IS NOT NULL",
    ):
        assert condition in lookup, condition
    assert "posts.content" not in lookup
    # Which replies, from where on, in which order and how many: all of it
    # is in the query, so no row is fetched only to be thrown away.
    for condition in (
        "posts.parent_post_id = ",
        "posts.deleted_at IS NULL",
        "users.is_active IS true",
        "users.email_verified_at IS NOT NULL",
        "(posts.created_at, posts.id) < (",
    ):
        assert condition in statement, condition
    assert "private" not in statement.lower()
    assert " ORDER BY posts.created_at DESC, posts.id DESC LIMIT " in statement
    assert "OFFSET" not in statement
    # Follows are not consulted: who follows whom plays no part.
    assert set(re.findall(r"(?:FROM|JOIN) (\w+)", statement)) == {
        "posts",
        "users",
        "likes",
        "reposts",
    }


def test_list_does_not_read_the_authors_private_columns(
    client: TestClient, session: Session, parent: Post, bob_account: User
) -> None:
    parent_id = parent.id
    add_replies(session, parent, 3, bob_account)
    session.expunge_all()

    with recorded_selects() as statements:
        assert replies(client, parent_id).status_code == 200

    for statement in statements:
        assert "password_hash" not in statement
        assert not re.search(r"users\.email\b(?!_)", statement)
        assert "users.bio" not in statement
    assert "users.username" in statements[-1]

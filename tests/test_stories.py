"""Stories: publishing them, who may read them and until when, viewing them
and deleting them."""

import base64
import re
import struct
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
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
from app.models import Story, StoryView, User, UserSession
from helpers import (
    add_story,
    add_user,
    expire,
    follow,
    log_in,
    plant_session_cookie,
    recorded_selects,
)

MEDIA_URL = "https://media.example.com/stories/1.jpg"
STORY_FIELDS = {
    "id",
    "author",
    "media_url",
    "media_type",
    "caption",
    "created_at",
    "expires_at",
    "viewed_by_me",
    "view_count",
}
AUTHOR_FIELDS = {"username", "display_name", "avatar_url"}
EMPTY = {"items": [], "next_cursor": None}
NOT_FOUND = {"detail": "Story not found."}
NOT_THE_AUTHOR = {"detail": "You are not the author of this story."}
NOT_AUTHENTICATED = {"detail": "Not authenticated."}
NOT_VERIFIED = {"detail": "Email address is not verified."}
INVALID_CURSOR = {"detail": "Invalid cursor."}
VIEWED = {"viewed": True}
NOT_VIEWED = {"viewed": False}
DAY = timedelta(hours=24)
MINUTE = timedelta(minutes=1)
SECOND = timedelta(seconds=1)
# Well formed in every respect, but its time lies beyond the year 9999.
IMPOSSIBLE_TIME_CURSOR = base64.urlsafe_b64encode(
    struct.pack(">q16s", 2**63 - 1, bytes(16))
).decode("ascii")
# Every reason a story is not there for a reader who is signed in.
HIDDEN = ["nonexistent", "not followed", "expired", "inactive", "unverified"]
# The three things that can be asked of one story.
ACTIONS = ["read", "view", "delete"]


def create(client: TestClient, media_url: object = MEDIA_URL, **body: object):
    return client.post("/stories", json={"media_url": media_url, **body})


def read(client: TestClient, story_id: object):
    return client.get(f"/stories/{story_id}")


def view(client: TestClient, story_id: object):
    return client.post(f"/stories/{story_id}/view")


def delete(client: TestClient, story_id: object):
    return client.delete(f"/stories/{story_id}")


def act(client: TestClient, action: str, story_id: object):
    return {"read": read, "view": view, "delete": delete}[action](client, story_id)


def feed(client: TestClient, **params: object):
    return client.get("/stories", params=params)


def ids(stories: list) -> list[str]:
    return [
        str(story["id"] if isinstance(story, dict) else story.id) for story in stories
    ]


def feed_ids(client: TestClient, **params: object) -> list[str]:
    response = feed(client, **params)
    assert response.status_code == 200, response.text
    return ids(response.json()["items"])


def story_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(Story))


def views(session: Session) -> set[tuple[uuid.UUID, uuid.UUID]]:
    """Every view in the database, as (story, viewer)."""
    found = session.execute(select(StoryView.story_id, StoryView.viewer_id)).all()
    return {(row.story_id, row.viewer_id) for row in found}


def add_view(session: Session, story: Story, viewer: User) -> None:
    session.add(StoryView(story_id=story.id, viewer_id=viewer.id))
    session.flush()


def add_stories(
    session: Session, count: int, *authors: User, start: datetime | None = None
) -> list[Story]:
    """``count`` active stories one minute apart, by the authors in turn,
    newest first. The newest was created just now."""
    newest = start or datetime.now(timezone.utc)
    stories = [
        add_story(
            session,
            authors[number % len(authors)],
            f"Story {number}",
            created_at=newest - (count - 1 - number) * MINUTE,
        )
        for number in range(count)
    ]
    return stories[::-1]


def hidden_story_id(session: Session, reason: str) -> uuid.UUID:
    """The id of a story that a signed-in reader who is not its author cannot
    see, for the reason named."""
    if reason == "nonexistent":
        return uuid.uuid4()
    author = add_user(
        session,
        f"{reason.replace(' ', '_')}_author",
        verified=reason != "unverified",
        active=reason != "inactive",
    )
    reader = session.scalar(select(User).where(User.username == "bob"))
    if reason != "not followed" and reader is not None:
        follow(session, reader, author)
    created_at = datetime.now(timezone.utc)
    if reason == "expired":
        created_at -= DAY
    return add_story(session, author, "Hidden", created_at=created_at).id


def walk(client: TestClient, *, limit: int) -> list[list[dict]]:
    """Every page of the feed, following next_cursor until there is none."""
    pages, cursor = [], None
    while True:
        extra = {"cursor": cursor} if cursor else {}
        response = feed(client, limit=limit, **extra)
        assert response.status_code == 200, response.text
        pages.append(response.json()["items"])
        cursor = response.json()["next_cursor"]
        if cursor is None:
            return pages
        assert len(pages) < 200, "pagination does not terminate"


def browser(make_client, username: str) -> TestClient:
    client = make_client()
    assert log_in(client, username).status_code == 200
    return client


@pytest.fixture
def followed(session: Session, alice_account: User, bob_account: User) -> None:
    """Bob follows alice."""
    follow(session, bob_account, alice_account)


# --- publishing a story --------------------------------------------------


def test_user_can_publish_a_story(
    alice_client: TestClient, session: Session, alice_account: User, clock
) -> None:
    alice_account.avatar_url = "https://cdn.example.com/avatars/alice.png"
    session.flush()

    response = create(alice_client, caption="Good morning")

    assert response.status_code == 201
    body = response.json()
    story = session.scalars(select(Story)).one()
    assert datetime.fromisoformat(body.pop("created_at")) == clock.now
    assert datetime.fromisoformat(body.pop("expires_at")) == clock.now + DAY
    assert body == {
        "id": str(story.id),
        "author": {
            "username": "alice",
            "display_name": "Alice",
            "avatar_url": "https://cdn.example.com/avatars/alice.png",
        },
        "media_url": MEDIA_URL,
        "media_type": "image",
        "caption": "Good morning",
        "viewed_by_me": False,
        "view_count": 0,
    }
    assert story.author_id == alice_account.id
    assert (story.created_at, story.expires_at) == (clock.now, clock.now + DAY)


def test_story_contains_exactly_the_intended_fields(alice_client: TestClient) -> None:
    body = create(alice_client).json()

    assert set(body) == STORY_FIELDS
    assert set(body["author"]) == AUTHOR_FIELDS


def test_story_can_be_read_back_at_once(alice_client: TestClient) -> None:
    created = create(alice_client, caption="Hello").json()

    assert read(alice_client, created["id"]).json() == created
    assert feed(alice_client).json() == {"items": [created], "next_cursor": None}


def test_user_can_have_several_stories_at_once(
    alice_client: TestClient, session: Session, clock
) -> None:
    created = []
    for number in range(3):
        created.append(create(alice_client, caption=f"Story {number}").json()["id"])
        clock.advance(minutes=1)

    assert story_count(session) == 3
    assert feed_ids(alice_client) == created[::-1]


def test_story_responses_are_not_to_be_cached(
    alice_client: TestClient, bob_client: TestClient, followed: None
) -> None:
    created = create(alice_client)
    story_id = created.json()["id"]
    answers = [
        created,
        read(bob_client, story_id),
        feed(bob_client),
        view(bob_client, story_id),
        delete(alice_client, story_id),
    ]

    for response in answers:
        assert response.headers["cache-control"] == "no-store"


# --- what a story is made of ---------------------------------------------


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        ("Hello", "Hello"),
        ("  Hello  ", "Hello"),
        ("Привет 👋", "Привет 👋"),
        ("<b>bold</b> & co", "<b>bold</b> & co"),
        ("x" * 150, "x" * 150),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_caption_is_optional_plain_text(
    alice_client: TestClient, session: Session, sent: object, stored: object
) -> None:
    response = create(alice_client, caption=sent)

    assert response.status_code == 201
    assert response.json()["caption"] == stored
    assert session.scalar(select(Story.caption)) == stored


def test_story_needs_no_caption(alice_client: TestClient) -> None:
    response = alice_client.post("/stories", json={"media_url": MEDIA_URL})

    assert response.status_code == 201
    assert response.json()["caption"] is None


@pytest.mark.parametrize("caption", ["x" * 151, "a\x00b", 5, ["x"], {"text": "x"}])
def test_invalid_caption_is_rejected(
    alice_client: TestClient, session: Session, caption: object
) -> None:
    response = create(alice_client, caption=caption)

    # Rejected, never cut to fit.
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "caption"]
    assert story_count(session) == 0


@pytest.mark.parametrize(
    "media_url",
    [
        "",
        "   ",
        "not a url",
        "media.example.com/1.jpg",
        "/stories/1.jpg",
        "//media.example.com/1.jpg",
        "ftp://media.example.com/1.jpg",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "https://",
        "https://" + "a" * 3000 + ".example.com/1.jpg",
        None,
        5,
        ["https://media.example.com/1.jpg"],
    ],
)
def test_media_url_must_be_an_absolute_http_url(
    alice_client: TestClient, session: Session, media_url: object
) -> None:
    response = create(alice_client, media_url)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "media_url"]
    assert story_count(session) == 0


@pytest.mark.parametrize("body", [{}, {"caption": "No image"}, None, [], "x"])
def test_story_needs_a_media_url(
    alice_client: TestClient, session: Session, body: object
) -> None:
    response = alice_client.post("/stories", json=body)

    assert response.status_code == 422
    assert story_count(session) == 0


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_media_url_is_stored_as_given_and_never_requested(
    alice_client: TestClient,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
    scheme: str,
) -> None:
    import socket

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("the server must not contact the media host")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    url = f"{scheme}://169.254.169.254/latest/meta-data/photo.jpg?size=large"

    response = create(alice_client, url)

    # An address is all it is to the server: a reference for the client.
    assert response.status_code == 201
    assert response.json()["media_url"] == url
    assert session.scalar(select(Story.media_url)) == url


def test_client_cannot_set_what_the_server_decides(
    alice_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    clock,
) -> None:
    chosen_id = str(uuid.uuid4())
    far_future = "2099-01-01T00:00:00Z"

    response = create(
        alice_client,
        id=chosen_id,
        author_id=str(bob_account.id),
        user_id=str(bob_account.id),
        author={"username": "bob"},
        username="bob",
        media_type="video",
        created_at="2000-01-01T00:00:00Z",
        expires_at=far_future,
        lifetime_hours=10_000,
        view_count=1_000_000,
        viewed_by_me=True,
        views=[{"viewer_id": str(bob_account.id)}],
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"] != chosen_id
    assert body["author"]["username"] == "alice"
    assert body["media_type"] == "image"
    assert (body["view_count"], body["viewed_by_me"]) == (0, False)
    story = session.scalars(select(Story)).one()
    assert story.author_id == alice_account.id
    assert story.media_type == "image"
    assert (story.created_at, story.expires_at) == (clock.now, clock.now + DAY)
    assert views(session) == set()


def test_author_cannot_be_named_by_the_request(
    alice_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    response = alice_client.post(
        f"/stories?author_id={bob_account.id}&username=bob&as=bob",
        json={"media_url": MEDIA_URL, "author_id": str(bob_account.id)},
    )

    assert response.status_code == 201
    assert session.scalar(select(Story.author_id)) == alice_account.id


# --- who may publish -----------------------------------------------------


def test_publishing_requires_authentication(
    client: TestClient, session: Session
) -> None:
    response = create(client)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert story_count(session) == 0


def test_unauthenticated_attempt_is_refused_before_it_is_validated(
    client: TestClient,
) -> None:
    assert create(client, "not a url").status_code == 401
    assert client.post("/stories", json={}).status_code == 401


def test_session_of_an_unverified_account_can_do_nothing_with_stories(
    alice_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    # Not reachable by logging in. Should a session ever belong to an
    # unverified account all the same, stories are still refused.
    own = add_story(session, alice_account)
    follow(session, alice_account, bob_account)
    bobs = add_story(session, bob_account)
    alice_account.email_verified_at = None
    session.flush()

    answers = [
        create(alice_client),
        feed(alice_client),
        read(alice_client, own.id),
        read(alice_client, bobs.id),
        view(alice_client, bobs.id),
        delete(alice_client, own.id),
    ]

    for response in answers:
        assert response.status_code == 403
        assert response.json() == NOT_VERIFIED
    assert story_count(session) == 2
    assert views(session) == set()


def test_deactivated_user_can_do_nothing_with_stories(
    alice_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    own = add_story(session, alice_account)
    follow(session, alice_account, bob_account)
    bobs = add_story(session, bob_account)
    alice_account.is_active = False
    session.flush()

    answers = [
        create(alice_client),
        feed(alice_client),
        read(alice_client, own.id),
        view(alice_client, bobs.id),
        delete(alice_client, own.id),
    ]

    for response in answers:
        assert response.status_code == 401
        assert response.json() == NOT_AUTHENTICATED
    assert story_count(session) == 2
    assert views(session) == set()


# --- reading a story -----------------------------------------------------


def test_author_can_read_their_own_story(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    story = add_story(session, alice_account, "Mine")

    response = read(alice_client, story.id)

    assert response.status_code == 200
    assert response.json()["caption"] == "Mine"
    assert response.json()["author"]["username"] == "alice"


def test_follower_can_read_the_story(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    story = add_story(session, alice_account, "For my followers")

    response = read(bob_client, story.id)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == STORY_FIELDS
    assert body["caption"] == "For my followers"
    assert body["author"]["username"] == "alice"
    assert datetime.fromisoformat(body["created_at"]) == story.created_at
    assert datetime.fromisoformat(body["expires_at"]) == story.created_at + DAY


def test_user_who_does_not_follow_the_author_cannot_read_the_story(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    story = add_story(session, alice_account, "Not for bob")

    response = read(bob_client, story.id)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert "Not for bob" not in response.text


def test_following_goes_one_way_for_stories_too(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    followed: None,
) -> None:
    alices = add_story(session, alice_account)
    bobs = add_story(session, bob_account)

    # Bob follows alice. That shows alice nothing of bob's.
    assert read(bob_client, alices.id).status_code == 200
    assert read(alice_client, bobs.id).status_code == 404
    assert feed_ids(alice_client) == [str(alices.id)]


def test_following_the_same_account_shows_followers_nothing_of_each_other(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    carol = add_user(session, "carol")
    follow(session, bob_account, alice_account)
    follow(session, carol, alice_account)
    bobs = add_story(session, bob_account)

    assert read(browser(make_client, "carol"), bobs.id).status_code == 404


def test_anonymous_user_cannot_read_any_story(
    client: TestClient, session: Session, alice_account: User
) -> None:
    story = add_story(session, alice_account, "Members only")

    for response in (read(client, story.id), read(client, uuid.uuid4()), feed(client)):
        assert response.status_code == 401
        assert response.json() == NOT_AUTHENTICATED
        assert "Members only" not in response.text


def test_stale_or_expired_session_is_not_a_reader(
    alice_client: TestClient, make_client, session: Session, alice_account: User
) -> None:
    story = add_story(session, alice_account)
    stale = make_client()
    plant_session_cookie(stale, "left-over-from-an-old-login")
    assert read(alice_client, story.id).status_code == 200

    expire(session, session.scalars(select(UserSession)).one())

    assert read(stale, story.id).status_code == 401
    assert read(alice_client, story.id).status_code == 401
    assert feed(alice_client).status_code == 401


def test_logging_out_ends_access(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    story = add_story(session, alice_account)
    assert read(alice_client, story.id).status_code == 200

    alice_client.post("/auth/logout")

    assert read(alice_client, story.id).status_code == 401


def test_reader_cannot_be_named_by_the_request(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    story = add_story(session, alice_account)
    as_alice = f"viewer_id={alice_account.id}&user_id={alice_account.id}&as=alice"

    # Bob does not follow alice, whoever he says he is.
    assert bob_client.get(f"/stories/{story.id}?{as_alice}").status_code == 404
    assert bob_client.get(f"/stories?{as_alice}").json() == EMPTY
    assert bob_client.post(f"/stories/{story.id}/view?{as_alice}").status_code == 404


@pytest.mark.parametrize(
    "story_id",
    ["1", "abc", "null", "me", "alice", "00000000-0000-0000-0000", "%27%20OR%201%3D1"],
)
def test_malformed_story_id_is_rejected(
    alice_client: TestClient, story_id: str
) -> None:
    for response in (read(alice_client, story_id), view(alice_client, story_id)):
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["path", "story_id"]


def test_story_cannot_be_looked_up_by_anything_but_its_id(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    add_story(session, alice_account)

    # Not by the author's id either: that names a user, not a story.
    assert read(alice_client, alice_account.id).status_code == 404


# --- following decides, at the moment of asking --------------------------


def test_following_opens_the_stories_that_are_active_now(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    old = add_story(session, alice_account, created_at=datetime.now(timezone.utc) - DAY)
    active = add_story(session, alice_account)
    assert read(bob_client, active.id).status_code == 404
    assert feed(bob_client).json() == EMPTY

    assert bob_client.post("/users/alice/follow").json() == {"following": True}

    # At once: there is nothing to wait for and nobody to approve it.
    assert read(bob_client, active.id).status_code == 200
    assert feed_ids(bob_client) == [str(active.id)]
    assert read(bob_client, old.id).status_code == 404


def test_unfollowing_ends_access_at_once(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    story = add_story(session, alice_account, "While it lasted")
    assert read(bob_client, story.id).status_code == 200

    assert bob_client.delete("/users/alice/follow").json() == {"following": False}

    for response in (read(bob_client, story.id), view(bob_client, story.id)):
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
    assert feed(bob_client).json() == EMPTY


def test_following_again_brings_the_active_stories_back(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    story = add_story(session, alice_account)
    before = read(bob_client, story.id).json()
    bob_client.delete("/users/alice/follow")
    assert read(bob_client, story.id).status_code == 404

    bob_client.post("/users/alice/follow")

    assert read(bob_client, story.id).json() == before
    assert feed_ids(bob_client) == [str(story.id)]


def test_access_is_decided_by_the_readers_own_follow_and_no_one_elses(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    carol = add_user(session, "carol")
    follow(session, carol, alice_account)
    follow(session, alice_account, bob_account)
    story = add_story(session, alice_account)

    # Carol follows alice and alice follows bob; bob himself follows nobody.
    assert read(browser(make_client, "bob"), story.id).status_code == 404
    assert read(browser(make_client, "carol"), story.id).status_code == 200


def test_user_cannot_get_at_stories_by_following_themselves(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    story = add_story(session, alice_account)

    assert bob_client.post("/users/bob/follow").status_code == 400
    assert read(bob_client, story.id).status_code == 404


# --- expiration ----------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "shown"),
    [
        (timedelta(0), True),
        (timedelta(hours=1), True),
        (timedelta(hours=23, minutes=59, seconds=59), True),
        (DAY - timedelta(microseconds=1), True),
        (DAY, False),
        (DAY + timedelta(microseconds=1), False),
        (timedelta(hours=25), False),
        (timedelta(days=30), False),
    ],
)
def test_story_is_shown_for_exactly_24_hours(
    alice_client: TestClient,
    bob_client: TestClient,
    followed: None,
    clock,
    age: timedelta,
    shown: bool,
) -> None:
    story_id = create(alice_client).json()["id"]

    clock.now += age

    for reader in (alice_client, bob_client):
        assert read(reader, story_id).status_code == (200 if shown else 404)
        assert feed_ids(reader) == ([story_id] if shown else [])


def test_expired_story_is_gone_for_its_author_too(
    alice_client: TestClient, session: Session, clock
) -> None:
    story_id = create(alice_client, caption="Yesterday's news").json()["id"]
    clock.advance(hours=24)

    for response in (
        read(alice_client, story_id),
        view(alice_client, story_id),
        delete(alice_client, story_id),
    ):
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
        assert "Yesterday" not in response.text
    assert feed(alice_client).json() == EMPTY
    # It is not shown any more. It is still stored.
    assert story_count(session) == 1


def test_expiration_is_fixed_when_the_story_is_created(
    alice_client: TestClient, bob_client: TestClient, followed: None, clock
) -> None:
    created = create(alice_client).json()
    clock.advance(hours=12)

    # Neither reading nor viewing a story gives it more time.
    read(bob_client, created["id"])
    view(bob_client, created["id"])
    later = read(alice_client, created["id"]).json()

    assert later["expires_at"] == created["expires_at"]
    clock.advance(hours=12)
    assert read(alice_client, created["id"]).status_code == 404


def test_each_story_expires_on_its_own(
    alice_client: TestClient, clock
) -> None:
    first = create(alice_client).json()["id"]
    clock.advance(hours=23)
    second = create(alice_client).json()["id"]

    clock.advance(hours=1)
    assert feed_ids(alice_client) == [second]
    assert read(alice_client, first).status_code == 404

    clock.advance(hours=23)
    assert feed(alice_client).json() == EMPTY


def test_expired_story_cannot_be_viewed(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    followed: None,
) -> None:
    story = add_story(
        session, alice_account, created_at=datetime.now(timezone.utc) - DAY
    )

    response = view(bob_client, story.id)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert views(session) == set()


# --- accounts that are not shown -----------------------------------------


@pytest.mark.parametrize("change", ["deactivated", "unverified"])
def test_stories_of_an_account_that_is_not_shown_are_not_found(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    followed: None,
    change: str,
) -> None:
    story = add_story(session, alice_account, "Gone with the account")
    assert read(bob_client, story.id).status_code == 200

    if change == "deactivated":
        alice_account.is_active = False
    else:
        alice_account.email_verified_at = None
    session.flush()

    # Following the account does not help: its profile is not shown either.
    assert bob_client.get("/users/alice").status_code == 404
    for response in (read(bob_client, story.id), view(bob_client, story.id)):
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
    assert feed(bob_client).json() == EMPTY
    assert views(session) == set()


def test_stories_return_when_the_account_is_reactivated(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    story = add_story(session, alice_account)
    alice_account.is_active = False
    session.flush()
    assert feed(bob_client).json() == EMPTY

    # Nothing was destroyed by the deactivation.
    alice_account.is_active = True
    session.flush()

    assert feed_ids(bob_client) == [str(story.id)]


# --- every story that is not there looks the same ------------------------


@pytest.mark.parametrize("action", ACTIONS)
def test_every_hidden_story_is_answered_like_one_that_does_not_exist(
    bob_client: TestClient, session: Session, bob_account: User, action: str
) -> None:
    targets = [hidden_story_id(session, reason) for reason in HIDDEN]
    stories_before, views_before = story_count(session), views(session)

    responses = [act(bob_client, action, story_id) for story_id in targets]

    # Not "forbidden", not "expired", not "follow first": nothing about the
    # answer says that there is a story behind the id.
    assert {response.status_code for response in responses} == {404}
    assert {response.text for response in responses} == {responses[0].text}
    assert responses[0].json() == NOT_FOUND
    assert len({tuple(sorted(response.headers)) for response in responses}) == 1
    assert story_count(session) == stories_before
    assert views(session) == views_before


@pytest.mark.parametrize("action", ACTIONS)
def test_hidden_story_takes_the_same_queries_as_one_that_does_not_exist(
    bob_client: TestClient, session: Session, bob_account: User, action: str
) -> None:
    targets = [hidden_story_id(session, reason) for reason in HIDDEN]

    recorded = []
    for story_id in targets:
        with recorded_selects() as statements:
            assert act(bob_client, action, story_id).status_code == 404
        recorded.append(statements)

    # The session, then one lookup that finds nothing: the work done does
    # not depend on whether, or why, the story is hidden.
    assert [len(statements) for statements in recorded] == [2] * len(HIDDEN)
    assert len({tuple(statements) for statements in recorded}) == 1


# --- the feed ------------------------------------------------------------


def test_feed_is_empty_for_a_user_with_nothing_to_see(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    add_story(session, alice_account)

    response = feed(bob_client)

    assert response.status_code == 200
    assert response.json() == EMPTY


def test_feed_holds_own_stories_and_those_of_followed_accounts(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    followed: None,
) -> None:
    carol = add_user(session, "carol")
    dave = add_user(session, "dave")
    follow(session, bob_account, carol)
    follow(session, dave, bob_account)
    stories = add_stories(session, 8, alice_account, bob_account, carol, dave)

    items = feed(bob_client).json()["items"]

    # Dave follows bob; bob does not follow dave.
    expected = [story for story in stories if story.author_id != dave.id]
    assert ids(items) == ids(expected)
    assert [item["author"]["username"] for item in items] == [
        "carol", "bob", "alice", "carol", "bob", "alice",
    ]
    assert "dave" not in feed(bob_client).text
    for item in items:
        assert set(item) == STORY_FIELDS
        assert set(item["author"]) == AUTHOR_FIELDS


def test_feed_is_ordered_newest_first(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    now = datetime.now(timezone.utc)
    middle = add_story(session, alice_account, created_at=now - 2 * MINUTE)
    oldest = add_story(session, alice_account, created_at=now - 3 * MINUTE)
    newest = add_story(session, alice_account, created_at=now - MINUTE)

    assert feed_ids(bob_client) == ids([newest, middle, oldest])


def test_stories_created_at_the_same_instant_have_a_fixed_order(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    now = datetime.now(timezone.utc)
    stories = [add_story(session, alice_account, created_at=now) for _ in range(6)]

    expected = sorted((str(story.id) for story in stories), reverse=True)
    assert feed_ids(bob_client) == expected
    assert [id for page in walk(bob_client, limit=2) for id in ids(page)] == expected


def test_feed_holds_exactly_the_stories_that_pass_every_rule(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    followed: None,
) -> None:
    shown = add_stories(session, 4, alice_account, bob_account)
    for reason in HIDDEN:
        hidden_story_id(session, reason)
    # Bob's own story of yesterday, and one of an account hidden twice over.
    add_story(session, bob_account, "Hidden", created_at=shown[0].created_at - DAY)
    several = add_user(session, "several", verified=False, active=False)
    follow(session, bob_account, several)
    add_story(session, several, "Hidden")

    response = feed(bob_client)

    assert ids(response.json()["items"]) == ids(shown)
    assert response.json()["next_cursor"] is None
    assert "Hidden" not in response.text
    assert "_author" not in response.text
    assert "several" not in response.text


def test_feed_is_each_readers_own(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    carol = add_user(session, "carol")
    follow(session, bob_account, alice_account)
    follow(session, carol, bob_account)
    alices = add_story(session, alice_account, created_at=datetime.now(timezone.utc) - MINUTE)
    bobs = add_story(session, bob_account)

    assert feed_ids(browser(make_client, "alice")) == ids([alices])
    assert feed_ids(browser(make_client, "bob")) == ids([bobs, alices])
    assert feed_ids(browser(make_client, "carol")) == ids([bobs])


def test_viewed_stories_stay_in_the_feed_where_they_were(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    stories = add_stories(session, 4, alice_account)
    before = feed(bob_client).json()

    view(bob_client, stories[1].id)
    view(bob_client, stories[3].id)
    after = feed(bob_client).json()

    # Not hidden and not moved: only marked.
    assert ids(after["items"]) == ids(before["items"]) == ids(stories)
    assert [item["viewed_by_me"] for item in after["items"]] == [
        False, True, False, True,
    ]


def test_feed_item_is_the_story_as_the_same_reader_gets_it_alone(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    followed: None,
) -> None:
    viewed = add_story(session, alice_account, "Viewed", created_at=datetime.now(timezone.utc) - MINUTE)
    unseen = add_story(session, alice_account, "Unseen")
    view(bob_client, viewed.id)

    for reader in (alice_client, bob_client):
        items = feed(reader).json()["items"]
        assert items == [read(reader, story.id).json() for story in (unseen, viewed)]


def test_for_you_feed_and_posts_are_untouched_by_stories(
    alice_client: TestClient, bob_client: TestClient, followed: None
) -> None:
    post = alice_client.post("/posts", json={"content": "A post"}).json()
    before = [bob_client.get("/feed").json(), bob_client.get("/users/alice").json()]

    story_id = create(alice_client, caption="A story").json()["id"]
    view(bob_client, story_id)

    assert [bob_client.get("/feed").json(), bob_client.get("/users/alice").json()] == before
    assert [item["id"] for item in before[0]["items"]] == [post["id"]]
    assert "A story" not in bob_client.get("/feed").text


# --- reading records nothing ---------------------------------------------


def test_reading_stories_is_not_viewing_them(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    followed: None,
) -> None:
    stories = add_stories(session, 3, alice_account)
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        for _ in range(3):
            feed(bob_client)
            feed(bob_client, limit=1)
            for story in stories:
                assert read(bob_client, story.id).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", record)

    # Only the explicit view request records a view.
    assert views(session) == set()
    assert not [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE"))]
    for item in feed(bob_client).json()["items"]:
        assert item["viewed_by_me"] is False
    for item in feed(alice_client).json()["items"]:
        assert item["view_count"] == 0


# --- viewing a story -----------------------------------------------------


def test_follower_can_view_a_story(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    followed: None,
) -> None:
    story = add_story(session, alice_account)

    response = view(bob_client, story.id)

    assert response.status_code == 200
    assert response.json() == VIEWED
    assert views(session) == {(story.id, bob_account.id)}
    assert read(bob_client, story.id).json()["viewed_by_me"] is True
    assert read(alice_client, story.id).json()["view_count"] == 1


def test_viewing_again_changes_nothing(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    followed: None,
) -> None:
    story = add_story(session, alice_account)
    first = view(bob_client, story.id)
    viewed_at = session.scalar(select(StoryView.viewed_at))

    again = [view(bob_client, story.id) for _ in range(5)]

    # Not a conflict: each request is answered with how things are.
    for response in again:
        assert response.status_code == 200
        assert response.json() == first.json() == VIEWED
    assert views(session) == {(story.id, bob_account.id)}
    assert session.scalar(select(StoryView.viewed_at)) == viewed_at
    assert read(alice_client, story.id).json()["view_count"] == 1


def test_row_that_is_already_there_is_not_an_error(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    followed: None,
) -> None:
    # What a request finds when another one, sent at the same moment, was
    # the first to write.
    story = add_story(session, alice_account)
    add_view(session, story, bob_account)

    response = view(bob_client, story.id)

    assert response.status_code == 200
    assert response.json() == VIEWED
    assert len(views(session)) == 1


def test_duplicate_view_is_left_to_the_database_to_refuse(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    story = add_story(session, alice_account)
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        view(bob_client, story.id)
        view(bob_client, story.id)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    # Not "look, then insert", which two simultaneous requests would both
    # get past. Every request inserts, and the primary key settles it.
    inserts = [statement for statement in statements if statement.startswith("INSERT")]
    assert len(inserts) == 2
    for statement in inserts:
        assert statement.startswith(
            "INSERT INTO story_views (story_id, viewer_id) VALUES"
        )
        assert statement.endswith("ON CONFLICT DO NOTHING")
    # The story is held against deletion while its view is written, so a
    # view never fails on a story that is deleted at the same moment.
    lookups = [s for s in statements if "FROM stories" in s and "FOR " in s]
    assert len(lookups) == 2
    for statement in lookups:
        assert statement.endswith("FOR KEY SHARE OF stories")


def test_author_looking_at_their_own_story_is_not_a_view(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    story = add_story(session, alice_account)

    responses = [view(alice_client, story.id) for _ in range(3)]

    for response in responses:
        assert response.status_code == 200
        assert response.json() == NOT_VIEWED
    assert views(session) == set()
    own = read(alice_client, story.id).json()
    assert (own["view_count"], own["viewed_by_me"]) == (0, False)


def test_each_viewer_counts_once(
    alice_client: TestClient,
    make_client,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    story = add_story(session, alice_account)
    for name in ("bob", "carol", "dave"):
        if name != "bob":
            add_user(session, name)
        reader = browser(make_client, name)
        reader.post("/users/alice/follow")
        for _ in range(3):
            assert view(reader, story.id).json() == VIEWED
    view(alice_client, story.id)

    assert len(views(session)) == 3
    assert read(alice_client, story.id).json()["view_count"] == 3


def test_views_belong_to_one_story(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    followed: None,
) -> None:
    stories = add_stories(session, 3, alice_account)

    view(bob_client, stories[1].id)

    assert [item["viewed_by_me"] for item in feed(bob_client).json()["items"]] == [
        False, True, False,
    ]
    assert [item["view_count"] for item in feed(alice_client).json()["items"]] == [
        0, 1, 0,
    ]


def test_story_of_an_account_that_is_not_followed_cannot_be_viewed(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    story = add_story(session, alice_account)

    response = view(bob_client, story.id)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert views(session) == set()


def test_viewing_requires_authentication(
    client: TestClient, session: Session, alice_account: User
) -> None:
    story = add_story(session, alice_account)

    for story_id in (story.id, uuid.uuid4()):
        response = view(client, story_id)
        assert response.status_code == 401
        assert response.json() == NOT_AUTHENTICATED
    assert views(session) == set()


def test_viewer_is_the_authenticated_user_and_nobody_else(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    followed: None,
) -> None:
    carol = add_user(session, "carol")
    follow(session, carol, alice_account)
    story = add_story(session, alice_account)

    response = bob_client.post(
        f"/stories/{story.id}/view?viewer_id={carol.id}&user_id={carol.id}",
        json={"viewer_id": str(carol.id), "user_id": str(carol.id), "username": "carol"},
    )

    assert response.status_code == 200
    assert views(session) == {(story.id, bob_account.id)}


def test_view_outlives_an_unfollow_but_is_shown_to_no_one_meanwhile(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    followed: None,
) -> None:
    story = add_story(session, alice_account)
    view(bob_client, story.id)

    bob_client.delete("/users/alice/follow")
    assert read(bob_client, story.id).status_code == 404
    assert view(bob_client, story.id).status_code == 404
    # It happened, so it still counts for the author.
    assert views(session) == {(story.id, bob_account.id)}
    assert read(alice_client, story.id).json()["view_count"] == 1

    bob_client.post("/users/alice/follow")
    assert read(bob_client, story.id).json()["viewed_by_me"] is True
    assert view(bob_client, story.id).json() == VIEWED
    assert len(views(session)) == 1


# --- the view count is the author's alone --------------------------------


def test_only_the_author_is_told_the_view_count(
    alice_client: TestClient,
    make_client,
    session: Session,
    alice_account: User,
    bob_account: User,
    followed: None,
) -> None:
    carol = add_user(session, "carol")
    follow(session, carol, alice_account)
    story = add_story(session, alice_account)
    bob, carols = browser(make_client, "bob"), browser(make_client, "carol")
    view(bob, story.id)
    view(carols, story.id)

    assert read(alice_client, story.id).json()["view_count"] == 2
    assert feed(alice_client).json()["items"][0]["view_count"] == 2
    # Whether they viewed it or not, a reader learns only about themselves.
    for reader in (bob, carols):
        single = read(reader, story.id).json()
        listed = feed(reader).json()["items"][0]
        assert single["view_count"] is listed["view_count"] is None
        assert single["viewed_by_me"] is listed["viewed_by_me"] is True


def test_view_count_of_someone_elses_story_never_leaves_the_database(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    followed: None,
) -> None:
    story = add_story(session, alice_account)
    for name in ("carol", "dave", "erin"):
        add_view(session, story, add_user(session, name))
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        if "FROM stories" in statement:
            seen.extend(row for row in cursor.fetchall())
            cursor.scroll(0, mode="absolute")

    event.listen(engine, "after_cursor_execute", record)
    try:
        assert read(bob_client, story.id).json()["view_count"] is None
    finally:
        event.remove(engine, "after_cursor_execute", record)

    # The strongest form of "not exposed": the number 3 is not in the row
    # the database returned for bob.
    [row] = seen
    assert 3 not in row
    assert None in row


def test_nobody_is_told_who_viewed_a_story(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    followed: None,
) -> None:
    story = add_story(session, alice_account)
    view(bob_client, story.id)

    answers = [
        read(alice_client, story.id),
        feed(alice_client),
        alice_client.get(f"/stories/{story.id}/views"),
        alice_client.get(f"/stories/{story.id}/viewers"),
        alice_client.get(f"/stories/{story.id}/view"),
    ]

    assert [response.status_code for response in answers] == [200, 200, 404, 404, 405]
    for response in answers:
        assert "bob" not in response.text.lower()
        assert str(bob_account.id) not in response.text
    assert read(alice_client, story.id).json()["view_count"] == 1


# --- deleting a story ----------------------------------------------------


def test_author_can_delete_their_story(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    followed: None,
) -> None:
    story_id = create(alice_client).json()["id"]

    response = delete(alice_client, story_id)

    assert response.status_code == 204
    assert response.content == b""
    # For good: the row is gone, not marked.
    assert story_count(session) == 0
    for reader in (alice_client, bob_client):
        assert read(reader, story_id).status_code == 404
        assert feed(reader).json() == EMPTY


def test_deleting_a_story_removes_its_views_with_it(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    followed: None,
) -> None:
    doomed = add_story(session, alice_account, created_at=datetime.now(timezone.utc) - MINUTE)
    kept = add_story(session, alice_account)
    doomed_id, kept_id, bob_id = doomed.id, kept.id, bob_account.id
    view(bob_client, doomed_id)
    view(bob_client, kept_id)
    assert len(views(session)) == 2
    session.expunge_all()

    assert delete(alice_client, doomed_id).status_code == 204

    assert views(session) == {(kept_id, bob_id)}
    assert feed_ids(bob_client) == [str(kept_id)]
    assert read(alice_client, kept_id).json()["view_count"] == 1


def test_deleted_story_cannot_be_read_viewed_or_deleted_again(
    alice_client: TestClient, bob_client: TestClient, followed: None
) -> None:
    story_id = create(alice_client).json()["id"]
    delete(alice_client, story_id)

    for client in (alice_client, bob_client):
        for action in ACTIONS:
            response = act(client, action, story_id)
            assert response.status_code == 404
            assert response.json() == NOT_FOUND


def test_follower_cannot_delete_the_story(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    story = add_story(session, alice_account)

    response = delete(bob_client, story.id)

    # He can read it, so there is nothing to hide: it is simply not his.
    assert response.status_code == 403
    assert response.json() == NOT_THE_AUTHOR
    assert story_count(session) == 1
    assert read(bob_client, story.id).status_code == 200


def test_user_who_cannot_see_a_story_cannot_delete_or_confirm_it(
    bob_client: TestClient, session: Session, alice_account: User
) -> None:
    story = add_story(session, alice_account)

    hidden = delete(bob_client, story.id)
    missing = delete(bob_client, uuid.uuid4())

    # "Forbidden" would confirm that the id belongs to a story.
    assert hidden.status_code == missing.status_code == 404
    assert hidden.text == missing.text
    assert story_count(session) == 1


def test_deleting_requires_authentication(
    client: TestClient, session: Session, alice_account: User
) -> None:
    story = add_story(session, alice_account)

    response = delete(client, story.id)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert story_count(session) == 1


def test_only_the_story_in_the_path_is_deleted(
    alice_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
) -> None:
    follow(session, alice_account, bob_account)
    own = add_stories(session, 3, alice_account)
    bobs = add_story(session, bob_account)
    kept = {own[0].id, own[2].id, bobs.id}

    response = alice_client.request(
        "DELETE",
        f"/stories/{own[1].id}?story_id={bobs.id}&id={own[0].id}",
        json={"story_id": str(bobs.id), "id": str(own[0].id), "ids": [str(bobs.id)]},
    )

    assert response.status_code == 204
    assert set(session.scalars(select(Story.id))) == kept


def test_story_whose_row_is_already_gone_is_not_found(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    # What the second of two simultaneous deletions finds once it is let in.
    story_id = add_story(session, alice_account).id
    session.expunge_all()
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(" ".join(statement.split()))

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert delete(alice_client, story_id).status_code == 204
        assert delete(alice_client, story_id).status_code == 404
    finally:
        event.remove(engine, "before_cursor_execute", record)

    # Each deletion locks the row first, so the second waits for the first.
    locks = [s for s in statements if s.endswith("FOR UPDATE OF stories")]
    assert len(locks) == 2
    assert len([s for s in statements if s.startswith("DELETE FROM stories")]) == 1


# --- cursor pagination ---------------------------------------------------


def test_first_page_returns_a_cursor_when_there_are_more_stories(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    stories = add_stories(session, 5, alice_account)

    body = feed(bob_client, limit=2).json()

    assert ids(body["items"]) == ids(stories[:2])
    assert isinstance(body["next_cursor"], str)


def test_cursor_returns_the_pages_that_follow(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    stories = add_stories(session, 5, alice_account)
    first = feed(bob_client, limit=2).json()

    second = feed(bob_client, limit=2, cursor=first["next_cursor"]).json()
    third = feed(bob_client, limit=2, cursor=second["next_cursor"]).json()

    assert ids(second["items"]) == ids(stories[2:4])
    assert ids(third["items"]) == ids(stories[4:])
    assert third["next_cursor"] is None


@pytest.mark.parametrize("limit", [1, 2, 3, 7, 50])
def test_walking_the_pages_shows_every_visible_story_once_and_no_hidden_one(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    followed: None,
    limit: int,
) -> None:
    stranger = add_user(session, "stranger")
    gone = add_user(session, "gone", active=False)
    follow(session, bob_account, gone)
    # Every second story in the order is hidden, for one reason or another.
    everything = add_stories(session, 24, alice_account, stranger, bob_account, gone)
    visible = [
        story
        for story in everything
        if story.author_id in (alice_account.id, bob_account.id)
    ]

    pages = walk(bob_client, limit=limit)

    assert [id for page in pages for id in ids(page)] == ids(visible)
    # Every page but the last is full: hidden rows use none of the room.
    assert all(len(page) == limit for page in pages[:-1])
    assert len(pages) == -(-12 // limit)


def test_page_that_exactly_holds_the_rest_has_no_cursor(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    add_stories(session, 4, alice_account)
    first = feed(bob_client, limit=2).json()

    second = feed(bob_client, limit=2, cursor=first["next_cursor"]).json()

    assert len(second["items"]) == 2
    assert second["next_cursor"] is None


def test_page_is_the_last_one_when_only_hidden_stories_remain(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    stories = add_stories(session, 2, alice_account)
    stranger = add_user(session, "stranger")
    add_stories(session, 5, stranger, start=stories[-1].created_at - MINUTE)

    body = feed(bob_client, limit=2).json()

    assert ids(body["items"]) == ids(stories)
    # No cursor: not even the existence of the other stories is given away.
    assert body["next_cursor"] is None


def test_cursor_is_the_position_of_the_last_story_shown(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    stories = add_stories(session, 3, alice_account)

    cursor = feed(bob_client, limit=2).json()["next_cursor"]

    # Usable in a query string as it is, and it carries only what the page
    # already showed: the time and id of its last story.
    assert re.fullmatch(r"[A-Za-z0-9_-]{32}", cursor)
    assert decode_cursor(cursor) == Cursor(
        created_at=stories[1].created_at, id=stories[1].id
    )


def test_new_and_deleted_stories_between_requests_cause_no_duplicates_or_gaps(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    followed: None,
) -> None:
    stories = add_stories(session, 6, alice_account)
    first = feed(bob_client, limit=2).json()

    add_story(session, alice_account, "Newer than the first page")
    assert delete(alice_client, stories[3].id).status_code == 204
    rest = [
        id
        for cursor in [first["next_cursor"]]
        for page in [feed(bob_client, limit=10, cursor=cursor).json()["items"]]
        for id in ids(page)
    ]

    assert rest == ids([stories[2], stories[4], stories[5]])


def test_story_that_expires_between_requests_is_gone_from_the_next_page(
    alice_client: TestClient, bob_client: TestClient, followed: None, clock
) -> None:
    created = []
    for _ in range(4):
        created.append(create(alice_client).json()["id"])
        clock.advance(hours=1)
    first = feed(bob_client, limit=2).json()
    assert ids(first["items"]) == created[:1:-1]

    # 24 hours after the oldest was created, to the second.
    clock.advance(hours=20)
    second = feed(bob_client, limit=2, cursor=first["next_cursor"]).json()

    assert ids(second["items"]) == [created[1]]
    assert second["next_cursor"] is None


def test_unfollowing_between_requests_ends_the_list(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    add_stories(session, 6, alice_account)
    cursor = feed(bob_client, limit=2).json()["next_cursor"]

    bob_client.delete("/users/alice/follow")

    # The cursor was issued while bob followed alice. It keeps nothing open.
    assert feed(bob_client, limit=2, cursor=cursor).json() == EMPTY


# --- a cursor is not a key -----------------------------------------------


@pytest.mark.parametrize("reason", HIDDEN[1:])
def test_forged_cursor_cannot_reach_a_hidden_story(
    bob_client: TestClient, session: Session, bob_account: User, reason: str
) -> None:
    hidden = session.get(Story, hidden_story_id(session, reason))
    just_after = encode_cursor(
        Cursor(created_at=hidden.created_at + SECOND, id=hidden.id)
    )
    # The same instant, and an id that sorts after every other.
    exactly_at = encode_cursor(
        Cursor(created_at=hidden.created_at, id=uuid.UUID(int=2**128 - 1))
    )

    for cursor in (just_after, exactly_at):
        response = feed(bob_client, cursor=cursor)
        assert response.status_code == 200
        assert response.json() == EMPTY


def test_cursor_from_another_readers_feed_shows_only_ones_own(
    make_client, session: Session, alice_account: User, bob_account: User
) -> None:
    carol = add_user(session, "carol")
    follow(session, bob_account, alice_account)
    alices = add_stories(session, 5, alice_account)
    carols = add_stories(session, 2, carol, start=alices[-1].created_at - MINUTE)
    # A genuine cursor into alice's stories, as her follower received it.
    cursor = feed(browser(make_client, "bob"), limit=2).json()["next_cursor"]

    response = feed(browser(make_client, "carol"), cursor=cursor)

    # It is only a point in time: carol's own stories older than it.
    assert ids(response.json()["items"]) == ids(carols)


def test_cursor_from_a_list_of_posts_is_only_a_point_in_time(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    for number in range(3):
        alice_client.post("/posts", json={"content": f"Post {number}"})
    story_id = create(alice_client).json()["id"]
    cursor = bob_client.get("/feed?limit=1").json()["next_cursor"]
    assert cursor is not None

    # Bob does not follow alice: her story is on no page, with any cursor.
    assert feed(bob_client, cursor=cursor).json() == EMPTY
    assert feed(alice_client, cursor=IMPOSSIBLE_TIME_CURSOR).status_code == 400
    assert story_id in feed_ids(alice_client)


# --- malformed requests --------------------------------------------------


@pytest.mark.parametrize(
    "cursor",
    [
        "abc",
        "!!!",
        "",
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "A" * 31,
        "A" * 33,
        "' OR '1'='1",
        "../../etc/passwd",
        IMPOSSIBLE_TIME_CURSOR,
    ],
)
def test_malformed_cursor_is_rejected(
    bob_client: TestClient, session: Session, alice_account: User, followed: None, cursor: str
) -> None:
    add_stories(session, 3, alice_account)

    response = feed(bob_client, cursor=cursor)

    assert response.status_code == 400
    assert response.json() == INVALID_CURSOR


def test_rejected_cursor_is_not_echoed(bob_client: TestClient) -> None:
    response = feed(bob_client, cursor="something-the-client-sent")

    assert response.status_code == 400
    assert "something" not in response.text


def test_default_and_maximum_limit(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    add_stories(session, MAX_PAGE_SIZE + 5, alice_account)

    assert len(feed(bob_client).json()["items"]) == DEFAULT_PAGE_SIZE
    assert len(feed(bob_client, limit=MAX_PAGE_SIZE).json()["items"]) == MAX_PAGE_SIZE


@pytest.mark.parametrize("limit", [0, -1, MAX_PAGE_SIZE + 1, 10_000, "abc", "", "1.5"])
def test_invalid_limit_is_rejected(bob_client: TestClient, limit: object) -> None:
    response = feed(bob_client, limit=limit)

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "limit"]


@pytest.mark.parametrize(
    "query",
    [
        "offset=2",
        "page=2&skip=2",
        "author=stranger&username=stranger",
        "include_expired=true&expired=1",
        "all=1&following=false",
        "is_private=false&visibility=all",
    ],
)
def test_no_parameter_widens_or_shifts_the_feed(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    followed: None,
    query: str,
) -> None:
    stories = add_stories(session, 3, alice_account)
    for reason in HIDDEN:
        hidden_story_id(session, reason)

    asked = bob_client.get(f"/stories?limit=2&{query}")

    assert asked.status_code == 200
    assert ids(asked.json()["items"]) == ids(stories[:2])
    assert asked.json() == feed(bob_client, limit=2).json()


def test_stories_are_paged_like_every_other_list() -> None:
    paths = app.openapi()["paths"]

    def page_parameters(documented: dict) -> list[dict]:
        return [param for param in documented["parameters"] if param["in"] == "query"]

    operation = paths["/stories"]["get"]
    assert {param["name"] for param in operation["parameters"]} == {"limit", "cursor"}
    assert page_parameters(operation) == page_parameters(paths["/feed"]["get"])


# --- cost ----------------------------------------------------------------


def test_feed_takes_the_same_queries_however_much_there_is(
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    followed: None,
) -> None:
    add_story(session, alice_account)
    session.expunge_all()
    with recorded_selects() as with_one:
        assert len(feed(bob_client, limit=50).json()["items"]) == 1

    authors = [add_user(session, f"author{number}") for number in range(8)]
    bob = session.get(User, bob_account.id)
    for author in authors:
        follow(session, bob, author)
    stories = add_stories(session, 40, *authors)
    for number, story in enumerate(stories):
        for viewer in authors[: number % 4]:
            if viewer.id != story.author_id:
                add_view(session, story, viewer)
        if number % 3 == 0:
            add_view(session, story, bob)
    session.expunge_all()
    with recorded_selects() as with_many:
        items = feed(bob_client, limit=50).json()["items"]

    assert len(items) == 41
    assert len({item["author"]["username"] for item in items}) == 9
    assert sum(item["viewed_by_me"] for item in items) == 14
    # One to find the session and its user, one for the page with its
    # authors and views: no query per story, per author or per view.
    assert len(with_one) == len(with_many) == 2


def test_single_story_is_one_query(
    alice_client: TestClient,
    bob_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    followed: None,
) -> None:
    story = add_story(session, alice_account)
    add_view(session, story, bob_account)
    story_id = story.id
    session.expunge_all()

    for reader in (alice_client, bob_client):
        with recorded_selects() as statements:
            assert read(reader, story_id).status_code == 200
        assert len(statements) == 2


def test_feed_is_filtered_ordered_and_cut_by_the_database(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    add_stories(session, 5, alice_account)
    cursor = feed(bob_client, limit=2).json()["next_cursor"]
    session.expunge_all()

    with recorded_selects() as statements:
        assert len(feed(bob_client, limit=2, cursor=cursor).json()["items"]) == 2

    sql = " ".join(statements[-1].split())
    # Which stories, from where on, in which order and how many: all of it
    # is in the query, so no row is fetched only to be thrown away.
    for condition in (
        "stories.expires_at > ",
        "users.is_active IS true",
        "users.email_verified_at IS NOT NULL",
        "follows.following_id = stories.author_id",
        "(stories.created_at, stories.id) < (",
    ):
        assert condition in sql, condition
    assert " ORDER BY stories.created_at DESC, stories.id DESC LIMIT " in sql
    assert "OFFSET" not in sql
    assert set(re.findall(r"(?:FROM|JOIN) (\w+)", sql)) == {
        "stories",
        "users",
        "follows",
        "story_views",
    }
    assert "private" not in sql.lower()


def test_reading_stories_does_not_read_the_authors_private_columns(
    bob_client: TestClient, session: Session, alice_account: User, followed: None
) -> None:
    story_id = add_story(session, alice_account).id
    session.expunge_all()

    with recorded_selects() as statements:
        assert read(bob_client, story_id).status_code == 200
        assert feed(bob_client).status_code == 200

    for statement in statements:
        if "FROM stories" not in statement:
            continue
        assert "users.username" in statement
        assert "password_hash" not in statement
        assert not re.search(r"users\.email\b(?!_)", statement)
        assert "users.bio" not in statement

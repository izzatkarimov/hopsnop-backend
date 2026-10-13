"""POST /users/me/export: the signed-in user's own data, as one document.

What the endpoint asks for and answers, what exactly the document holds, and
what it must never hold: anything that proves who someone is, and anything
that is somebody else's. What happens when an export and another request for
the same account arrive together is in ``test_data_export_concurrency``.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings, settings
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
from app.schemas import export as export_schema
from app.schemas.export import AccountExport
from app.services import auth as auth_service
from app.services import erasure, rate_limit
from app.services import export as export_service
from helpers import (
    PASSWORD,
    SENSITIVE_KEYS,
    add_post,
    add_story,
    add_user,
    keys_in,
    log_in,
    plant_session_cookie,
    session_token,
)

NOT_AUTHENTICATED = {"detail": "Not authenticated."}
WRONG_PASSWORD = {"detail": "Current password is incorrect."}
TOO_MANY = {"detail": "Too many requests. Try again later."}
FOREIGN_ORIGIN = "https://evil.example"
MEDIA = "https://media.example.com"
# The moment the documents of these tests are made, and the day the things
# in them happened.
NOW = datetime(2026, 10, 9, 12, 0, 0, 123456, tzinfo=timezone.utc)
DAY = datetime(2026, 3, 1, 8, 0, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
# The parts of the document that are lists.
COLLECTIONS = [
    "sessions",
    "posts",
    "likes",
    "reposts",
    "following",
    "followers",
    "stories",
    "story_views",
]
# How every moment in the document is written.
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z")


def export(client: TestClient, password: object = PASSWORD, **kwargs: object):
    return client.post("/users/me/export", json={"password": password}, **kwargs)


def at(minutes: int) -> datetime:
    """A moment of the day on which everything happened."""
    return DAY + timedelta(minutes=minutes)


def stamp(moment: datetime | None) -> str | None:
    """A moment as the document is to write it. Worked out here, not by the
    code under test."""
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def counter(session: Session, scope: str, user_id: object) -> int | None:
    """What one of the account's counters holds, if it exists."""
    return session.scalar(
        select(RateLimit.count).where(
            RateLimit.key_hash == rate_limit.key_hash(scope, str(user_id))
        )
    )


def wrong_passwords(session: Session, user_id: object) -> int | None:
    return counter(session, "password-confirmation:account", user_id)


def exports_counted(session: Session, user_id: object) -> int | None:
    return counter(session, "data-export:account", user_id)


def stored(session: Session) -> dict[str, list]:
    """Every row of every table but the rate limiter's, as stored."""
    return {
        name: sorted(
            (tuple(row) for row in session.execute(select(table))), key=repr
        )
        for name, table in sorted(Base.metadata.tables.items())
        if name != "rate_limits"
    }


@pytest.fixture(autouse=True)
def _made_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(export_service, "_now", lambda: NOW)


# --- an account with something of everything -----------------------------


@dataclass
class Library:
    """Alice's account with something of every kind in it, what others have
    to do with it, and the document that is to come of it."""

    alice: User
    bob: User
    carol: User
    # Neither of these is shown to anyone: one was deactivated, the other
    # never verified.
    dave: User
    erin: User
    expected: dict
    # Posts that are in the database and must not be in the document.
    tombstone_of_alice: Post
    tombstone_of_bob: Post


def a_session(user: User, created: datetime, **state: datetime) -> UserSession:
    values = {"expires_at": created + 30 * 24 * HOUR, "last_used_at": created} | state
    return UserSession(
        user_id=user.id,
        token_hash=hash_token(auth_service.generate_token()),
        created_at=created,
        **values,
    )


def a_link(model: type, user: User, created: datetime, **state: datetime) -> object:
    return model(
        user_id=user.id,
        token_hash=hash_token(auth_service.generate_token()),
        created_at=created,
        expires_at=created + HOUR,
        **state,
    )


@pytest.fixture
def library(
    session: Session, alice_client: TestClient, alice_account: User, bob_account: User
) -> Library:
    alice, bob = alice_account, bob_account
    alice.display_name = "Alice Liddell"
    alice.bio = "Curiouser and curiouser."
    alice.avatar_url = f"{MEDIA}/avatars/liddell.png"
    # A hash that nobody else has, so that it can be looked for.
    alice.password_hash = hash_password(PASSWORD)
    carol = add_user(session, "carol")
    dave = add_user(session, "deactivated_dave", active=False)
    erin = add_user(session, "unverified_erin", verified=False)
    for other in (bob, carol, dave, erin):
        # Nothing of these is Alice's to be given.
        other.display_name = f"{other.username.title()} (a private name)"
        other.bio = f"The bio of {other.username}, which is nobody else's."
        other.avatar_url = f"{MEDIA}/avatars/{other.username}-private.png"
    session.flush()

    def add(*rows: object) -> None:
        session.add_all(rows)
        session.flush()

    # Posts. Alice wrote four: one on its own, one that Bob answered, one in
    # answer to Bob and one in answer to a post that has been deleted since.
    alone = add_post(session, alice, "Alice on her own", created_at=at(1))
    answered = add_post(session, alice, "Alice, answered by Bob", created_at=at(2))
    of_bob = add_post(session, bob, "Bob's own post", created_at=at(3))
    add_post(session, bob, "Bob answering Alice", parent=answered, created_at=at(4))
    to_bob = add_post(
        session, alice, "Alice answering Bob", parent=of_bob, created_at=at(5)
    )
    tombstone_of_bob = add_post(session, bob, created_at=at(6), deleted=True)
    to_nothing = add_post(
        session,
        alice,
        "Alice answering what is gone",
        parent=tombstone_of_bob,
        created_at=at(7),
    )
    # One she deleted herself, kept as a tombstone for Carol's reply.
    tombstone_of_alice = add_post(session, alice, created_at=at(8), deleted=True)
    add_post(
        session, carol, "Carol under it", parent=tombstone_of_alice, created_at=at(9)
    )
    of_carol = add_post(session, carol, "Carol's own post", created_at=at(10))
    alone.updated_at = at(30)
    session.flush()

    # Likes and reposts, hers and other people's.
    add(
        Like(user_id=alice.id, post_id=of_carol.id, created_at=at(12)),
        Like(user_id=alice.id, post_id=of_bob.id, created_at=at(11)),
        Like(user_id=alice.id, post_id=alone.id, created_at=at(13)),
        Repost(user_id=alice.id, post_id=of_bob.id, created_at=at(14)),
        Like(user_id=bob.id, post_id=alone.id, created_at=at(15)),
        Repost(user_id=carol.id, post_id=alone.id, created_at=at(16)),
        Like(user_id=carol.id, post_id=of_bob.id, created_at=at(17)),
    )

    # Follows, in both directions, also with accounts that are not shown.
    add(
        Follow(follower_id=alice.id, following_id=carol.id, created_at=at(21)),
        Follow(follower_id=alice.id, following_id=bob.id, created_at=at(20)),
        Follow(follower_id=alice.id, following_id=dave.id, created_at=at(22)),
        Follow(follower_id=bob.id, following_id=alice.id, created_at=at(23)),
        Follow(follower_id=erin.id, following_id=alice.id, created_at=at(24)),
        Follow(follower_id=bob.id, following_id=carol.id, created_at=at(25)),
    )

    # Stories: one of hers that is shown and one that has expired, and one of
    # Bob's that she has viewed.
    expired = add_story(
        session,
        alice,
        "Alice's story of long ago",
        created_at=at(40),
        media_url=f"{MEDIA}/stories/liddell-0.jpg",
    )
    shown = add_story(
        session, alice, created_at=at(41), media_url=f"{MEDIA}/stories/liddell-1.jpg"
    )
    of_bobs = add_story(
        session,
        bob,
        "Bob's caption",
        created_at=at(42),
        media_url=f"{MEDIA}/stories/bob-private.jpg",
    )
    add(
        StoryView(story_id=expired.id, viewer_id=bob.id, viewed_at=at(43)),
        StoryView(story_id=expired.id, viewer_id=carol.id, viewed_at=at(44)),
        StoryView(story_id=shown.id, viewer_id=bob.id, viewed_at=at(45)),
        StoryView(story_id=of_bobs.id, viewer_id=alice.id, viewed_at=at(46)),
        StoryView(story_id=of_bobs.id, viewer_id=carol.id, viewed_at=at(47)),
    )

    # Sessions: the one she is logged in with, and three older ones.
    current = session.scalar(
        select(UserSession).where(UserSession.user_id == alice.id)
    )
    older = a_session(alice, at(50))
    revoked = a_session(alice, at(51), revoked_at=at(52), last_used_at=at(52))
    expired_session = a_session(alice, at(-90 * 24 * 60), last_used_at=at(-1000))
    expired_session.expires_at = at(-60 * 24 * 60)
    add(older, revoked, expired_session, a_session(bob, at(53)))

    # Emailed links.
    verification = a_link(EmailVerificationToken, alice, at(60), used_at=at(61))
    first_reset = a_link(PasswordResetToken, alice, at(62), used_at=at(63))
    second_reset = a_link(PasswordResetToken, alice, at(64))
    add(
        verification,
        first_reset,
        second_reset,
        a_link(EmailVerificationToken, bob, at(65)),
        a_link(PasswordResetToken, bob, at(66)),
    )

    # Counters of the rate limiter, under her names.
    add(
        *(
            RateLimit(
                key_hash=rate_limit.key_hash(scope, subject),
                window_start=datetime.now(timezone.utc),
                count=1,
            )
            for scope, subject in (
                ("login:identifier", "alice@example.com"),
                ("login:identifier+address", "alice@example.com\x00testclient"),
            )
        )
    )
    session.refresh(alice)
    session.refresh(current)

    def session_entry(row: UserSession, is_current: bool = False) -> dict:
        return {
            "created_at": stamp(row.created_at),
            "last_used_at": stamp(row.last_used_at),
            "expires_at": stamp(row.expires_at),
            "revoked_at": stamp(row.revoked_at),
            "is_current": is_current,
        }

    def link_entry(row: object) -> dict:
        return {
            "requested_at": stamp(row.created_at),
            "expires_at": stamp(row.expires_at),
            "used_at": stamp(row.used_at),
        }

    def post_entry(row: Post, parent: Post | None = None) -> dict:
        return {
            "id": str(row.id),
            "content": row.content,
            "parent_post_id": str(parent.id) if parent is not None else None,
            "created_at": stamp(row.created_at),
            "updated_at": stamp(row.updated_at),
        }

    def story_entry(row: Story, view_count: int) -> dict:
        return {
            "id": str(row.id),
            "media_url": row.media_url,
            "media_type": "image",
            "caption": row.caption,
            "created_at": stamp(row.created_at),
            "expires_at": stamp(row.expires_at),
            "view_count": view_count,
        }

    expected = {
        "export_version": 1,
        "generated_at": stamp(NOW),
        "account": {
            "id": str(alice.id),
            "username": "alice",
            "email": "alice@example.com",
            "display_name": "Alice Liddell",
            "bio": "Curiouser and curiouser.",
            "avatar_url": f"{MEDIA}/avatars/liddell.png",
            "is_active": True,
            "email_verified_at": stamp(alice.email_verified_at),
            "created_at": stamp(alice.created_at),
            "updated_at": stamp(alice.updated_at),
        },
        # Oldest first, the ended ones among them.
        "sessions": [
            session_entry(expired_session),
            session_entry(older),
            session_entry(revoked),
            session_entry(current, is_current=True),
        ],
        "account_requests": {
            "email_verifications": [link_entry(verification)],
            "password_resets": [link_entry(first_reset), link_entry(second_reset)],
        },
        "posts": [
            post_entry(alone),
            post_entry(answered),
            post_entry(to_bob, of_bob),
            post_entry(to_nothing, tombstone_of_bob),
        ],
        "likes": [
            {"post_id": str(of_bob.id), "created_at": stamp(at(11))},
            {"post_id": str(of_carol.id), "created_at": stamp(at(12))},
            {"post_id": str(alone.id), "created_at": stamp(at(13))},
        ],
        "reposts": [{"post_id": str(of_bob.id), "created_at": stamp(at(14))}],
        "following": [
            {"username": "bob", "created_at": stamp(at(20))},
            {"username": "carol", "created_at": stamp(at(21))},
        ],
        "followers": [{"username": "bob", "created_at": stamp(at(23))}],
        "stories": [story_entry(expired, 2), story_entry(shown, 1)],
        "story_views": [{"story_id": str(of_bobs.id), "viewed_at": stamp(at(46))}],
    }
    return Library(
        alice, bob, carol, dave, erin, expected, tombstone_of_alice, tombstone_of_bob
    )


# --- the document --------------------------------------------------------


def test_export_of_an_account_with_everything_is_exactly_this_document(
    alice_client: TestClient, library: Library
) -> None:
    response = export(alice_client)

    assert response.status_code == 200
    # Every part, every entry, every field and every value, and nothing else.
    assert response.json() == library.expected


def test_export_of_an_account_with_nothing_has_every_part_and_no_entries(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    own = session.scalar(select(UserSession))

    response = export(alice_client)

    assert response.status_code == 200
    assert response.json() == {
        "export_version": 1,
        "generated_at": stamp(NOW),
        "account": {
            "id": str(alice_account.id),
            "username": "alice",
            "email": "alice@example.com",
            "display_name": "Alice",
            "bio": None,
            "avatar_url": None,
            "is_active": True,
            "email_verified_at": stamp(alice_account.email_verified_at),
            "created_at": stamp(alice_account.created_at),
            "updated_at": stamp(alice_account.updated_at),
        },
        # Asking for an export takes a session, so there is always the one.
        "sessions": [
            {
                "created_at": stamp(own.created_at),
                "last_used_at": stamp(own.last_used_at),
                "expires_at": stamp(own.expires_at),
                "revoked_at": None,
                "is_current": True,
            }
        ],
        # Empty lists, not missing ones.
        "account_requests": {"email_verifications": [], "password_resets": []},
        "posts": [],
        "likes": [],
        "reposts": [],
        "following": [],
        "followers": [],
        "stories": [],
        "story_views": [],
    }


def test_parts_and_fields_come_in_the_documented_order(
    alice_client: TestClient, library: Library
) -> None:
    document = export(alice_client).json()

    assert list(document) == [
        "export_version",
        "generated_at",
        "account",
        "sessions",
        "account_requests",
        "posts",
        "likes",
        "reposts",
        "following",
        "followers",
        "stories",
        "story_views",
    ]
    assert list(document["account"]) == list(library.expected["account"])
    assert list(document["account_requests"]) == [
        "email_verifications",
        "password_resets",
    ]
    for part in COLLECTIONS:
        assert [list(entry) for entry in document[part]] == [
            list(entry) for entry in library.expected[part]
        ]


def test_two_exports_of_the_same_data_are_the_same_document(
    alice_client: TestClient, library: Library
) -> None:
    first, second = export(alice_client), export(alice_client)

    # To the byte: same order, same spelling of every value.
    assert first.status_code == second.status_code == 200
    assert first.content == second.content


def moments_in(payload: object, key: str = "") -> list[object]:
    """Every value in a decoded document whose key says it is a moment."""
    if isinstance(payload, dict):
        return [m for k, value in payload.items() for m in moments_in(value, k)]
    if isinstance(payload, list):
        return [m for item in payload for m in moments_in(item, key)]
    return [payload] if key.endswith("_at") else []


def test_every_moment_is_written_the_same_way(
    alice_client: TestClient, library: Library
) -> None:
    moments = moments_in(export(alice_client).json())

    # ISO 8601 in UTC, to the microsecond, ending in "Z": 27 characters each,
    # whether or not there are any microseconds to speak of.
    assert len(moments) > 40
    for moment in moments:
        assert moment is None or TIMESTAMP.fullmatch(moment), moment
    assert stamp(at(11)) == "2026-03-01T08:11:00.000000Z"
    assert stamp(at(11)) in moments


@pytest.mark.parametrize(
    ("moment", "written"),
    [
        (
            datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc),
            "2026-03-01T08:00:00.000000Z",
        ),
        (
            datetime(2026, 3, 1, 8, 0, 0, 5, tzinfo=timezone.utc),
            "2026-03-01T08:00:00.000005Z",
        ),
        # The same moment, told in another part of the world.
        (
            datetime(2026, 3, 1, 10, 0, tzinfo=timezone(timedelta(hours=2))),
            "2026-03-01T08:00:00.000000Z",
        ),
        (
            datetime(2026, 2, 28, 22, 30, tzinfo=timezone(timedelta(hours=-9.5))),
            "2026-03-01T08:00:00.000000Z",
        ),
    ],
    ids=["whole second", "microseconds", "east of UTC", "west of UTC"],
)
def test_moment_is_written_in_utc_whatever_zone_it_was_given_in(
    moment: datetime, written: str
) -> None:
    entry = export_schema.ExportedStoryView(story_id=uuid.uuid4(), viewed_at=moment)

    assert f'"viewed_at":"{written}"' in entry.model_dump_json()
    assert entry.model_dump(mode="json")["viewed_at"] == written


# --- its parts, one by one -----------------------------------------------


def test_account_is_the_profile_and_what_only_its_owner_is_told(
    alice_client: TestClient, library: Library
) -> None:
    account = export(alice_client).json()["account"]

    # What /users/me says, and three things besides. Nothing of the password.
    profile = alice_client.get("/users/me").json()
    for field in ("id", "username", "email", "display_name", "bio", "avatar_url"):
        assert account[field] == profile[field]
    assert set(account) - set(profile) == {"is_active", "updated_at"}
    assert not [field for field in account if "password" in field]


def test_sessions_are_told_by_their_times_and_never_by_an_id(
    alice_client: TestClient, session: Session, library: Library
) -> None:
    sessions = export(alice_client).json()["sessions"]

    ids = session.scalars(
        select(UserSession.id).where(UserSession.user_id == library.alice.id)
    ).all()
    assert len(sessions) == len(ids) == 4
    for entry in sessions:
        assert set(entry) == {
            "created_at",
            "last_used_at",
            "expires_at",
            "revoked_at",
            "is_current",
        }
    # The one the export was asked for with is marked, and only that one.
    assert [entry["is_current"] for entry in sessions] == [False, False, False, True]
    # Ended sessions are told as well: the one that was revoked, and the one
    # that ran out.
    assert sessions[2]["revoked_at"] is not None
    assert sessions[0]["expires_at"] < stamp(NOW)
    # Somebody else's session is not among them.
    assert session.scalar(select(func.count()).select_from(UserSession)) == 5


def test_current_session_is_the_one_that_asks(
    make_client, alice_client: TestClient, alice_account: User
) -> None:
    elsewhere = make_client()
    assert log_in(elsewhere).status_code == 200

    here, there = export(alice_client).json(), export(elsewhere).json()

    # Two sessions, the older one first, and each request marks its own.
    assert [entry["is_current"] for entry in here["sessions"]] == [True, False]
    assert [entry["is_current"] for entry in there["sessions"]] == [False, True]
    assert here["posts"] == there["posts"] == []


def test_posts_are_the_accounts_own_posts_and_replies(
    alice_client: TestClient, session: Session, library: Library
) -> None:
    posts = export(alice_client).json()["posts"]

    assert [post["content"] for post in posts] == [
        "Alice on her own",
        "Alice, answered by Bob",
        "Alice answering Bob",
        "Alice answering what is gone",
    ]
    # A reply is told by the post it answers. The first two answer none.
    assert [post["parent_post_id"] is not None for post in posts] == [
        False,
        False,
        True,
        True,
    ]
    # An edit shows in when the post was last changed.
    assert posts[0]["updated_at"] == stamp(at(30))
    assert posts[0]["created_at"] == stamp(at(1))
    # Everything that was written by somebody else is somebody else's.
    hers = session.scalar(
        select(func.count()).select_from(Post).where(Post.author_id == library.alice.id)
    )
    assert hers == len(posts) == 4
    assert session.scalar(select(func.count()).select_from(Post)) == 10


def test_post_the_account_deleted_is_not_in_its_export(
    alice_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    kept = alice_client.post("/posts", json={"content": "Staying"}).json()
    leaf = alice_client.post("/posts", json={"content": "Deleted, unanswered"}).json()
    answered = alice_client.post(
        "/posts", json={"content": "Deleted, but answered"}
    ).json()
    parent = session.get(Post, uuid.UUID(answered["id"]))
    add_post(session, bob_account, "Bob's answer", parent=parent)
    for post in (leaf, answered):
        assert alice_client.delete(f"/posts/{post['id']}").status_code == 204

    response = export(alice_client)

    # One is gone altogether. The other left a row for Bob's answer, with no
    # author and no text: it is nobody's, and nothing is made of it here.
    assert session.scalar(select(func.count()).select_from(Post)) == 3
    assert [post["id"] for post in response.json()["posts"]] == [kept["id"]]
    assert "Deleted" not in response.text
    assert answered["id"] not in response.text
    assert leaf["id"] not in response.text


def test_reply_to_a_deleted_post_says_which_post_and_nothing_about_it(
    alice_client: TestClient, library: Library
) -> None:
    response = export(alice_client)

    reply = response.json()["posts"][3]
    assert reply["content"] == "Alice answering what is gone"
    assert reply["parent_post_id"] == str(library.tombstone_of_bob.id)
    # That id is all there is of the deleted post: once, where the reply
    # names what it answers.
    assert response.text.count(str(library.tombstone_of_bob.id)) == 1
    assert "deleted_at" not in response.text


def test_likes_and_reposts_are_the_ones_the_account_made(
    alice_client: TestClient, session: Session, library: Library
) -> None:
    document = export(alice_client).json()

    # Of others' posts and of her own, by the post and the moment. Not the
    # likes and reposts that others gave her posts: those are theirs.
    assert len(document["likes"]) == 3 and len(document["reposts"]) == 1
    assert session.scalar(select(func.count()).select_from(Like)) == 5
    assert session.scalar(select(func.count()).select_from(Repost)) == 2
    for entry in document["likes"] + document["reposts"]:
        assert set(entry) == {"post_id", "created_at"}


def test_follows_are_told_by_username_in_both_directions(
    alice_client: TestClient, library: Library
) -> None:
    document = export(alice_client).json()

    assert [entry["username"] for entry in document["following"]] == ["bob", "carol"]
    assert [entry["username"] for entry in document["followers"]] == ["bob"]
    for entry in document["following"] + document["followers"]:
        assert set(entry) == {"username", "created_at"}


def test_accounts_that_are_shown_to_nobody_are_not_named_in_an_export(
    alice_client: TestClient, session: Session, library: Library
) -> None:
    response = export(alice_client)

    # She follows Dave, whose account was deactivated, and is followed by
    # Erin, who never verified hers. The lists of followers name neither,
    # and neither does this.
    assert session.scalar(select(func.count()).select_from(Follow)) == 6
    for hidden in (library.dave, library.erin):
        assert hidden.username not in response.text
        assert str(hidden.id) not in response.text
    listed = alice_client.get("/users/alice/following").json()["items"]
    assert sorted(user["username"] for user in listed) == ["bob", "carol"]


def test_stories_are_all_that_are_still_stored_with_how_often_each_was_viewed(
    alice_client: TestClient, library: Library
) -> None:
    stories = export(alice_client).json()["stories"]

    # The one that has expired is not shown to anyone any more, its author
    # included. Its row is still there, so it is still her data.
    captions = [story["caption"] for story in stories]
    assert captions == ["Alice's story of long ago", None]
    assert [story["view_count"] for story in stories] == [2, 1]
    assert stories[0]["expires_at"] < stamp(NOW)
    assert alice_client.get(f"/stories/{stories[0]['id']}").status_code == 404


def test_who_viewed_a_story_is_told_to_nobody(
    alice_client: TestClient, library: Library
) -> None:
    response = export(alice_client)

    # How many, not who. A viewer is only ever named as a follower, if they
    # are one, and their id is in no part of the document.
    for story in response.json()["stories"]:
        assert "viewers" not in story and "views" not in story
    for viewer in (library.bob, library.carol):
        assert str(viewer.id) not in response.text


def test_story_views_are_the_ones_the_account_made(
    alice_client: TestClient, session: Session, library: Library
) -> None:
    views = export(alice_client).json()["story_views"]

    assert len(views) == 1
    assert set(views[0]) == {"story_id", "viewed_at"}
    assert session.scalar(select(func.count()).select_from(StoryView)) == 5


def test_emailed_links_are_told_by_their_times_alone(
    alice_client: TestClient, library: Library
) -> None:
    requests = export(alice_client).json()["account_requests"]

    assert len(requests["email_verifications"]) == 1
    assert len(requests["password_resets"]) == 2
    for entry in requests["email_verifications"] + requests["password_resets"]:
        assert set(entry) == {"requested_at", "expires_at", "used_at"}
    # Which one was used and which is still outstanding can be seen.
    assert [e["used_at"] is None for e in requests["password_resets"]] == [False, True]


# --- order ---------------------------------------------------------------


def test_entries_of_the_same_moment_are_ordered_by_their_keys(
    alice_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    alice, bob = alice_account, bob_account
    others = [add_user(session, f"user{number}") for number in range(6)]
    posts = [add_post(session, bob, f"Bob {n}", created_at=at(0)) for n in range(6)]
    own = [add_post(session, alice, f"Alice {n}", created_at=at(1)) for n in range(6)]
    stories = [add_story(session, bob, created_at=at(2)) for _ in range(6)]
    mine = [add_story(session, alice, created_at=at(2)) for _ in range(6)]
    for post, story, other in zip(posts, stories, others):
        session.add_all(
            [
                Like(user_id=alice.id, post_id=post.id, created_at=at(3)),
                Repost(user_id=alice.id, post_id=post.id, created_at=at(3)),
                StoryView(story_id=story.id, viewer_id=alice.id, viewed_at=at(3)),
                Follow(follower_id=alice.id, following_id=other.id, created_at=at(3)),
                Follow(follower_id=other.id, following_id=alice.id, created_at=at(3)),
                a_session(alice, at(4)),
                a_link(PasswordResetToken, alice, at(5)),
            ]
        )
    session.flush()

    document = export(alice_client).json()

    # Six of each at one and the same moment. A key of the row then decides,
    # so the order does not depend on how the database happened to find them.
    def in_order(values: list) -> list[str]:
        return [str(value) for value in sorted(values)]

    usernames_by_id = [user.username for user in sorted(others, key=lambda u: u.id)]
    assert [p["id"] for p in document["posts"]] == in_order(p.id for p in own)
    assert [s["id"] for s in document["stories"]] == in_order(s.id for s in mine)
    for part in ("likes", "reposts"):
        assert [e["post_id"] for e in document[part]] == in_order(p.id for p in posts)
    assert [v["story_id"] for v in document["story_views"]] == in_order(
        s.id for s in stories
    )
    for part in ("following", "followers"):
        assert [e["username"] for e in document[part]] == usernames_by_id
    assert len(document["sessions"]) == 7
    assert len(document["account_requests"]["password_resets"]) == 6


def test_each_part_is_ordered_by_when_its_entries_came_about(
    alice_client: TestClient, session: Session, alice_account: User, bob_account: User
) -> None:
    # Written to the database newest first, to make sure it is not the order
    # of writing that is read back.
    for number in reversed(range(8)):
        post = add_post(session, bob_account, f"Bob {number}", created_at=at(number))
        add_post(session, alice_account, f"Alice {number}", created_at=at(number))
        session.add(
            Like(user_id=alice_account.id, post_id=post.id, created_at=at(100 - number))
        )
    session.flush()

    document = export(alice_client).json()

    assert [post["content"] for post in document["posts"]] == [
        f"Alice {number}" for number in range(8)
    ]
    moments = [like["created_at"] for like in document["likes"]]
    assert moments == sorted(moments) and len(set(moments)) == 8


# --- what must never be in it --------------------------------------------


def test_no_credential_and_no_secret_is_anywhere_in_the_document(
    alice_client: TestClient, session: Session, library: Library
) -> None:
    raw_token = session_token(alice_client)
    secrets = {
        "the password": PASSWORD,
        "the session token": raw_token,
        "the hash of the session token": hash_token(raw_token),
        "the secret of the rate limiter": settings.rate_limit_secret.get_secret_value(),
    }
    # From everybody's rows, not only hers: nothing of the kind is to be
    # there, whoever's it is.
    columns = {
        "a password hash": User.password_hash,
        "a session id": UserSession.id,
        "a session token hash": UserSession.token_hash,
        "a verification link id": EmailVerificationToken.id,
        "a verification link hash": EmailVerificationToken.token_hash,
        "a reset link id": PasswordResetToken.id,
        "a reset link hash": PasswordResetToken.token_hash,
        "a rate-limit key": RateLimit.key_hash,
    }
    for name, column in columns.items():
        values = session.scalars(select(column)).all()
        assert values, name
        for number, value in enumerate(values):
            secrets[f"{name} ({number})"] = str(value)

    response = export(alice_client)

    assert response.status_code == 200
    assert len(secrets) > 25
    for name, value in secrets.items():
        assert value not in response.text, name
        assert value not in repr(sorted(response.headers.items())), name


def test_no_field_of_the_document_is_named_like_a_secret(
    alice_client: TestClient, library: Library
) -> None:
    document = export(alice_client).json()

    names = keys_in(document)
    assert not names & SENSITIVE_KEYS
    # The one name with "password" in it is of the list that says when
    # reset links were asked for.
    for name in names - {"password_resets"}:
        for word in ("password", "hash", "token", "secret", "key", "cookie"):
            assert word not in name, name
    # An id is that of the account, a post or a story. A session has none
    # here, and neither has an emailed link.
    for part in ("sessions", "likes", "reposts", "following", "followers"):
        assert not [entry for entry in document[part] if "id" in entry]
    for entries in document["account_requests"].values():
        assert not [entry for entry in entries if "id" in entry]


def test_nothing_of_anybody_else_is_in_the_document_but_what_the_user_could_see(
    alice_client: TestClient, session: Session, library: Library
) -> None:
    response = export(alice_client)

    for other in (library.bob, library.carol, library.dave, library.erin):
        wrote = Post.author_id == other.id
        published = Story.author_id == other.id
        theirs = [
            str(other.id),
            other.email,
            other.display_name,
            other.bio,
            other.avatar_url,
            *session.scalars(select(Post.content).where(wrote)),
            *session.scalars(select(Story.media_url).where(published)),
            *session.scalars(select(Story.caption).where(published)),
        ]
        for value in filter(None, theirs):
            assert value not in response.text, (other.username, value)
    # What is there of others: the usernames of the accounts she follows or
    # is followed by, and the ids of what she answered, liked or viewed.
    document = response.json()
    assert {e["username"] for e in document["following"] + document["followers"]} == {
        "bob",
        "carol",
    }


def test_tombstones_are_in_nobodys_export(
    make_client, alice_client: TestClient, session: Session, library: Library
) -> None:
    bobs = make_client()
    assert log_in(bobs, "bob").status_code == 200

    of_alice, of_bob = export(alice_client), export(bobs)

    # Each of the two has deleted a post that others had answered. What is
    # left of those is in the database and in neither one's document.
    assert session.scalar(
        select(func.count()).select_from(Post).where(Post.author_id.is_(None))
    ) == 2
    assert str(library.tombstone_of_alice.id) not in of_alice.text
    assert str(library.tombstone_of_alice.id) not in of_bob.text
    assert str(library.tombstone_of_bob.id) not in of_bob.text
    assert [post["content"] for post in of_bob.json()["posts"]] == [
        "Bob's own post",
        "Bob answering Alice",
    ]


def test_export_is_only_ever_of_the_account_that_asks(
    make_client, alice_client: TestClient, library: Library
) -> None:
    bobs = make_client()
    assert log_in(bobs, "bob").status_code == 200

    of_bob = export(bobs).json()

    assert of_bob["account"]["username"] == "bob"
    assert of_bob["account"]["email"] == "bob@example.com"
    assert [entry["username"] for entry in of_bob["following"]] == ["alice", "carol"]
    assert [entry["username"] for entry in of_bob["followers"]] == ["alice"]
    assert "Alice on her own" not in repr(of_bob)
    assert "alice@example.com" not in repr(of_bob)


# --- every column, accounted for -----------------------------------------
#
# Each column of the database is either in the export, at the place named
# here, or left out, for the reason given here. A column that is added
# later is in neither list, and this fails until somebody has decided.


@dataclass(frozen=True)
class Exported:
    # Where in the document; "[]" stands for the entries of a list.
    where: str


@dataclass(frozen=True)
class Excluded:
    kind: str
    why: str


CREDENTIAL = "credential"
INTERNAL = "internal identifier"
ITSELF = "the account itself"
OTHERS = "somebody else's data"
ERASURE = "erasure"
NOT_USER_DATA = "not user data"

_whose = Excluded(ITSELF, "Says whose row it is, which is the account being exported.")
_hash = Excluded(CREDENTIAL, "The stored form of a secret that proves who someone is.")
_link_id = Excluded(INTERNAL, "Names a row of a link; says nothing about the user.")
_limiter = Excluded(
    NOT_USER_DATA,
    "A counter of the rate limiter. Kept under a keyed hash, attributable to no "
    "account without the server's secret, and a control, not a record of the user.",
)

MATRIX: dict[str, Exported | Excluded] = {
    # --- the account
    "users.id": Exported("account.id"),
    "users.username": Exported("account.username"),
    "users.email": Exported("account.email"),
    "users.password_hash": _hash,
    "users.display_name": Exported("account.display_name"),
    "users.bio": Exported("account.bio"),
    "users.avatar_url": Exported("account.avatar_url"),
    "users.is_active": Exported("account.is_active"),
    "users.email_verified_at": Exported("account.email_verified_at"),
    "users.created_at": Exported("account.created_at"),
    "users.updated_at": Exported("account.updated_at"),
    # --- its sessions
    "sessions.id": Excluded(
        INTERNAL, "Addresses a session on the server; says nothing about the user."
    ),
    "sessions.user_id": _whose,
    "sessions.token_hash": _hash,
    "sessions.created_at": Exported("sessions[].created_at"),
    "sessions.expires_at": Exported("sessions[].expires_at"),
    "sessions.last_used_at": Exported("sessions[].last_used_at"),
    "sessions.revoked_at": Exported("sessions[].revoked_at"),
    # --- the links emailed for it
    "email_verification_tokens.id": _link_id,
    "email_verification_tokens.user_id": _whose,
    "email_verification_tokens.token_hash": _hash,
    "email_verification_tokens.created_at": Exported(
        "account_requests.email_verifications[].requested_at"
    ),
    "email_verification_tokens.expires_at": Exported(
        "account_requests.email_verifications[].expires_at"
    ),
    "email_verification_tokens.used_at": Exported(
        "account_requests.email_verifications[].used_at"
    ),
    "password_reset_tokens.id": _link_id,
    "password_reset_tokens.user_id": _whose,
    "password_reset_tokens.token_hash": _hash,
    "password_reset_tokens.created_at": Exported(
        "account_requests.password_resets[].requested_at"
    ),
    "password_reset_tokens.expires_at": Exported(
        "account_requests.password_resets[].expires_at"
    ),
    "password_reset_tokens.used_at": Exported(
        "account_requests.password_resets[].used_at"
    ),
    # --- what it wrote
    "posts.id": Exported("posts[].id"),
    "posts.author_id": _whose,
    "posts.parent_post_id": Exported("posts[].parent_post_id"),
    "posts.content": Exported("posts[].content"),
    "posts.created_at": Exported("posts[].created_at"),
    "posts.updated_at": Exported("posts[].updated_at"),
    "posts.deleted_at": Excluded(
        ERASURE,
        "Set only on what is left of an erased post. That row has no author, so "
        "it is nobody's, and it is in nobody's export.",
    ),
    "stories.id": Exported("stories[].id"),
    "stories.author_id": _whose,
    "stories.media_url": Exported("stories[].media_url"),
    "stories.media_type": Exported("stories[].media_type"),
    "stories.caption": Exported("stories[].caption"),
    "stories.created_at": Exported("stories[].created_at"),
    "stories.expires_at": Exported("stories[].expires_at"),
    # --- what it did
    "likes.user_id": _whose,
    "likes.post_id": Exported("likes[].post_id"),
    "likes.created_at": Exported("likes[].created_at"),
    "reposts.user_id": _whose,
    "reposts.post_id": Exported("reposts[].post_id"),
    "reposts.created_at": Exported("reposts[].created_at"),
    # A follow has the account on one side. The other side is told, by its
    # username, in the list for that direction.
    "follows.follower_id": Exported("followers[].username"),
    "follows.following_id": Exported("following[].username"),
    "follows.created_at": Exported("following[].created_at"),
    "story_views.story_id": Exported("story_views[].story_id"),
    "story_views.viewer_id": Excluded(
        OTHERS,
        "On a view the account made, it is the account. On a view of one of its "
        "stories, it is who looked: that is counted (stories[].view_count) and "
        "told to nobody.",
    ),
    "story_views.viewed_at": Exported("story_views[].viewed_at"),
    # --- the rate limiter
    "rate_limits.key_hash": _limiter,
    "rate_limits.window_start": _limiter,
    "rate_limits.count": _limiter,
}

# What the document holds that is not a column: what it is, when it was
# made, and two things worked out from rows without telling the rows.
DERIVED = {
    "export_version",
    "generated_at",
    "sessions[].is_current",
    "stories[].view_count",
    # The other direction of follows.created_at.
    "followers[].created_at",
}


def leaves(payload: object, path: str = "") -> set[str]:
    """The place of every value in a decoded document."""
    if isinstance(payload, dict):
        return {
            leaf
            for key, value in payload.items()
            for leaf in leaves(value, f"{path}.{key}" if path else key)
        }
    if isinstance(payload, list):
        return {leaf for item in payload for leaf in leaves(item, f"{path}[]")}
    return {path}


def test_every_column_of_the_database_is_either_exported_or_excluded_for_a_reason() -> (
    None
):
    columns = {
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
    }

    assert set(MATRIX) == columns
    for column, decision in MATRIX.items():
        if isinstance(decision, Excluded):
            assert decision.kind and len(decision.why) > 30, column


def test_every_table_that_holds_something_of_an_account_has_a_part_in_the_export() -> (
    None
):
    exported = {
        column.split(".")[0]
        for column, decision in MATRIX.items()
        if isinstance(decision, Exported)
    }

    # Every table that refers to an account, and the accounts themselves.
    referring = {
        table.name
        for table in Base.metadata.tables.values()
        for column in table.columns
        if any(key.column.table.name == "users" for key in column.foreign_keys)
    }
    assert exported == referring | {"users"}
    assert set(Base.metadata.tables) - exported == {"rate_limits"}


def test_document_holds_what_the_matrix_says_and_nothing_besides(
    alice_client: TestClient, library: Library
) -> None:
    document = export(alice_client).json()

    claimed = {d.where for d in MATRIX.values() if isinstance(d, Exported)}
    # With an account that has something of everything, every place that is
    # claimed is really there, and there is no place that nothing claims.
    assert leaves(document) == claimed | DERIVED
    assert not claimed & DERIVED


def test_schema_has_a_field_for_every_exported_column_and_for_nothing_else() -> None:
    def places(model: type, path: str = "") -> set[str]:
        found = set()
        for name, field in model.model_fields.items():
            here = f"{path}.{name}" if path else name
            inner = [
                argument
                for argument in getattr(field.annotation, "__args__", ())
                if isinstance(argument, type) and hasattr(argument, "model_fields")
            ]
            if hasattr(field.annotation, "model_fields"):
                found |= places(field.annotation, here)
            elif inner:
                found |= places(inner[0], f"{here}[]")
            else:
                found.add(here)
        return found

    claimed = {d.where for d in MATRIX.values() if isinstance(d, Exported)}
    assert places(AccountExport) == claimed | DERIVED


@pytest.mark.parametrize(
    "column",
    [c for c, d in MATRIX.items() if isinstance(d, Excluded) and d.kind == ITSELF],
)
def test_column_left_out_as_the_account_itself_holds_nothing_but_its_id(
    session: Session, library: Library, column: str
) -> None:
    table_name, column_name = column.split(".")
    table = Base.metadata.tables[table_name]
    mine = {
        "posts": Post.author_id.is_not(None),
        "stories": Story.author_id.is_not(None),
    }.get(table_name, table.c[column_name].is_not(None))

    owners = set(session.scalars(select(table.c[column_name]).where(mine)))

    # It only ever says whose the row is. For her rows that is her id, which
    # the document gives once, as account.id.
    assert library.alice.id in owners
    assert owners <= {u.id for u in session.scalars(select(User))}


@pytest.mark.parametrize(
    "column",
    [
        c
        for c, d in MATRIX.items()
        if isinstance(d, Excluded) and d.kind in (CREDENTIAL, INTERNAL)
    ],
)
def test_column_left_out_as_a_credential_or_an_internal_id_is_in_no_export(
    alice_client: TestClient, session: Session, library: Library, column: str
) -> None:
    table_name, column_name = column.split(".")
    values = session.scalars(
        select(Base.metadata.tables[table_name].c[column_name])
    ).all()
    assert values

    text = export(alice_client).text

    for value in values:
        assert str(value) not in text


def test_schemas_of_the_document_take_nothing_over_from_a_row_as_a_whole() -> None:
    models = [
        model
        for model in vars(export_schema).values()
        if isinstance(model, type)
        and issubclass(model, export_schema.BaseModel)
        and model is not export_schema.BaseModel
    ]

    assert len(models) == 11
    for model in models:
        # Built field by field. None of them can be filled from an object's
        # attributes, and none lets through a field it does not name.
        assert not model.model_config.get("from_attributes"), model.__name__
        assert model.model_config.get("extra") in (None, "ignore"), model.__name__


def test_documented_schema_is_exactly_this() -> None:
    schemas = app.openapi()["components"]["schemas"]

    def fields(name: str) -> list[str]:
        assert schemas[name]["required"] == list(schemas[name]["properties"])
        return list(schemas[name]["properties"])

    assert fields("AccountExport") == [
        "export_version",
        "generated_at",
        "account",
        "sessions",
        "account_requests",
        "posts",
        "likes",
        "reposts",
        "following",
        "followers",
        "stories",
        "story_views",
    ]
    assert fields("ExportedAccount") == [
        "id",
        "username",
        "email",
        "display_name",
        "bio",
        "avatar_url",
        "is_active",
        "email_verified_at",
        "created_at",
        "updated_at",
    ]
    assert fields("ExportedSession") == [
        "created_at",
        "last_used_at",
        "expires_at",
        "revoked_at",
        "is_current",
    ]
    assert fields("ExportedAccountRequests") == [
        "email_verifications",
        "password_resets",
    ]
    assert fields("ExportedLinkRequest") == ["requested_at", "expires_at", "used_at"]
    assert fields("ExportedPost") == [
        "id",
        "content",
        "parent_post_id",
        "created_at",
        "updated_at",
    ]
    assert fields("ExportedInteraction") == ["post_id", "created_at"]
    assert fields("ExportedFollow") == ["username", "created_at"]
    assert fields("ExportedStory") == [
        "id",
        "media_url",
        "media_type",
        "caption",
        "created_at",
        "expires_at",
        "view_count",
    ]
    assert fields("ExportedStoryView") == ["story_id", "viewed_at"]
    assert schemas["AccountExport"]["properties"]["export_version"]["const"] == 1
    moment = schemas["ExportedPost"]["properties"]["created_at"]
    assert moment == {"type": "string", "format": "date-time", "title": "Created At"}


# --- who may ask ---------------------------------------------------------


def test_unauthenticated_request_is_refused(
    client: TestClient, alice_account: User
) -> None:
    response = export(client)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


def test_unauthenticated_request_is_refused_before_it_is_validated(
    client: TestClient,
) -> None:
    assert client.post("/users/me/export", json={}).status_code == 401
    assert client.post("/users/me/export").status_code == 401


def test_unknown_session_cookie_is_refused(
    client: TestClient, alice_account: User
) -> None:
    plant_session_cookie(client, auth_service.generate_token())

    assert export(client).json() == NOT_AUTHENTICATED


@pytest.mark.parametrize("ended", ["revoked", "expired"])
def test_session_that_has_ended_gets_no_export(
    alice_client: TestClient, session: Session, alice_account: User, ended: str
) -> None:
    now = datetime.now(timezone.utc)
    own = session.scalar(select(UserSession))
    if ended == "revoked":
        own.revoked_at = now
    else:
        own.created_at, own.expires_at = now - 2 * HOUR, now - HOUR
    session.flush()

    response = export(alice_client)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


def test_deactivated_account_is_refused_like_no_session_at_all(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    alice_account.is_active = False
    session.flush()

    response = export(alice_client)

    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED


def test_account_that_is_not_verified_gets_its_data_all_the_same(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    # Not reachable by logging in. Should a session belong to an unverified
    # account all the same, its own data is not among the things kept from it.
    alice_account.email_verified_at = None
    session.flush()

    response = export(alice_client)

    assert response.status_code == 200
    assert response.json()["account"]["email_verified_at"] is None


def test_cross_site_request_is_refused_and_counted_as_nothing(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    response = export(alice_client, headers={"Origin": FOREIGN_ORIGIN})

    assert response.status_code == 403
    assert response.json() == {"detail": "Cross-origin request rejected."}
    assert exports_counted(session, alice_account.id) is None
    assert wrong_passwords(session, alice_account.id) is None


@pytest.mark.parametrize("method", ["GET", "PUT", "PATCH", "DELETE"])
def test_export_is_only_given_to_a_post(alice_client: TestClient, method: str) -> None:
    # Not to a GET: the password belongs in a body, not in a URL, and a
    # link must not be enough to fetch all of an account's data.
    response = alice_client.request(
        method, "/users/me/export", json={"password": PASSWORD}
    )

    assert response.status_code == 405
    assert "account" not in response.text


def test_whose_data_it_is_cannot_be_said_by_the_request(
    alice_client: TestClient, bob_account: User
) -> None:
    # Whatever a body or a query says about who is meant, it is not read.
    response = alice_client.post(
        f"/users/me/export?user_id={bob_account.id}&username=bob",
        json={
            "password": PASSWORD,
            "user_id": str(bob_account.id),
            "username": "bob",
            "email": "bob@example.com",
        },
    )

    assert response.status_code == 200
    assert response.json()["account"]["username"] == "alice"
    assert "bob" not in response.text


@pytest.mark.parametrize("path", ["/users/bob/export", "/users/alice/export"])
def test_there_is_no_export_by_username(
    alice_client: TestClient, bob_account: User, path: str
) -> None:
    response = alice_client.post(path, json={"password": PASSWORD})

    assert response.status_code == 404
    assert "email" not in response.text


def test_endpoint_takes_a_password_and_nothing_else() -> None:
    operation = app.openapi()["paths"]["/users/me/export"]
    body = app.openapi()["components"]["schemas"]["ExportRequest"]

    assert list(operation) == ["post"]
    assert "parameters" not in operation["post"]
    assert list(body["properties"]) == ["password"]
    assert body["required"] == ["password"]
    assert body["properties"]["password"]["writeOnly"] is True
    answer = operation["post"]["responses"]["200"]["content"]["application/json"]
    assert answer["schema"] == {"$ref": "#/components/schemas/AccountExport"}
    # What kind of picture it is, is part of what the endpoint says of itself.
    assert "not a snapshot of one moment" in operation["post"]["description"]


# --- the password --------------------------------------------------------


def test_wrong_password_gets_no_data(
    alice_client: TestClient, library: Library
) -> None:
    response = export(alice_client, "not my password at all")

    assert response.status_code == 400
    assert response.json() == WRONG_PASSWORD
    assert "content-disposition" not in response.headers


def test_session_alone_is_not_enough(
    alice_client: TestClient, session: Session, library: Library
) -> None:
    # Everything else this session may read does not add up to the export:
    # the sessions with their times, the links, the likes with their times.
    for body in ({}, {"password": ""}):
        response = alice_client.post("/users/me/export", json=body)
        assert response.status_code == 422
        assert "Alice on her own" not in response.text


def test_wrong_password_keeps_the_session_and_changes_nothing(
    alice_client: TestClient, session: Session, library: Library
) -> None:
    before = stored(session)
    token = session_token(alice_client)

    assert export(alice_client, "not my password at all").status_code == 400

    assert stored(session) == before
    assert session_token(alice_client) == token
    assert alice_client.get("/auth/me").status_code == 200


def test_password_of_another_account_is_a_wrong_password(
    alice_client: TestClient, session: Session, bob_account: User
) -> None:
    bob_account.password_hash = hash_password("what bob logs in with")
    session.flush()

    assert export(alice_client, "what bob logs in with").status_code == 400


@pytest.mark.parametrize(
    "almost", [PASSWORD.upper(), PASSWORD + " ", " " + PASSWORD, PASSWORD[:-1]]
)
def test_password_is_taken_exactly_as_typed(
    alice_client: TestClient, alice_account: User, almost: str
) -> None:
    assert export(alice_client, almost).status_code == 400


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"password": ""},
        {"password": None},
        {"password": 12345678},
        {"password": "x" * (PASSWORD_MAX_LENGTH + 1)},
        {"current_password": PASSWORD},
        [],
    ],
    ids=["empty", "blank", "null", "number", "too long", "wrong name", "not an object"],
)
def test_request_without_a_usable_password_is_invalid_and_counted_as_nothing(
    alice_client: TestClient, session: Session, alice_account: User, body: object
) -> None:
    response = alice_client.post("/users/me/export", json=body)

    assert response.status_code == 422
    assert wrong_passwords(session, alice_account.id) is None
    assert exports_counted(session, alice_account.id) is None


def test_rejected_password_is_not_echoed(alice_client: TestClient) -> None:
    response = export(alice_client, "x" * (PASSWORD_MAX_LENGTH + 1))

    assert response.status_code == 422
    assert "xxxx" not in response.text
    assert "input" not in keys_in(response.json())


# --- the answer ----------------------------------------------------------


def test_export_is_answered_as_a_file_to_download(
    alice_client: TestClient, alice_account: User
) -> None:
    response = export(alice_client)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    # Named after the account and the day, so two of them can be told apart.
    assert response.headers["content-disposition"] == (
        'attachment; filename="hopsnop-export-alice-2026-10-09.json"'
    )


def test_export_is_kept_by_no_cache(alice_client: TestClient) -> None:
    for password in (PASSWORD, "not my password at all"):
        response = export(alice_client, password)
        assert response.headers["cache-control"] == "no-store"


def test_export_leaves_the_session_as_it_is(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    token = session_token(alice_client)

    response = export(alice_client)

    assert "set-cookie" not in response.headers
    assert session_token(alice_client) == token
    assert alice_client.get("/auth/me").status_code == 200


def test_export_changes_nothing_in_the_database(
    alice_client: TestClient, session: Session, library: Library
) -> None:
    before = stored(session)

    assert export(alice_client).status_code == 200

    # Reading it out is all it does: no row of any table is touched, not
    # even the time the session was last used.
    assert stored(session) == before


@pytest.mark.parametrize("password", [PASSWORD, "not my password at all"])
def test_password_and_export_do_not_appear_in_logs(
    alice_client: TestClient,
    library: Library,
    caplog: pytest.LogCaptureFixture,
    password: str,
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    token = session_token(alice_client)

    export(alice_client, password)

    for private in (password, PASSWORD, token, "alice@example.com", "Alice on her own"):
        assert private not in caplog.text


# --- guessing the password -----------------------------------------------


def test_guessing_the_password_is_limited(
    alice_client: TestClient, library: Library
) -> None:
    allowed = settings.password_confirmation_failures

    guesses = [export(alice_client, f"guess number {n}") for n in range(allowed)]
    refused = export(alice_client)

    assert [response.status_code for response in guesses] == [400] * allowed
    # Also with the right password, like a login.
    assert refused.status_code == 429
    assert refused.json() == TOO_MANY
    assert refused.headers["retry-after"].isdigit()
    assert "content-disposition" not in refused.headers


def test_refusal_for_the_limit_is_decided_before_the_password_is_looked_at(
    alice_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(settings.password_confirmation_failures):
        export(alice_client, f"guess number {number}")
    checked = []
    monkeypatch.setattr(
        auth_service, "verify_password", lambda *args: checked.append(args) or True
    )

    assert export(alice_client).status_code == 429
    assert checked == []


@pytest.mark.parametrize("other", ["change-password", "delete-account"])
@pytest.mark.parametrize("first", ["export", "other"])
def test_limit_is_one_for_everything_that_asks_for_the_password(
    alice_client: TestClient,
    session: Session,
    alice_account: User,
    other: str,
    first: str,
) -> None:
    def elsewhere(password: str):
        body = {"password": password}
        if other == "change-password":
            body = {
                "current_password": password,
                "new_password": "an entirely new passphrase",
            }
        return alice_client.post(f"/auth/{other}", json=body)

    guess = {"export": lambda p: export(alice_client, p), "other": elsewhere}
    second = "other" if first == "export" else "export"
    allowed = settings.password_confirmation_failures

    # A session that guesses has that many guesses, not that many for each
    # thing that can be asked for.
    for number in range(allowed - 2):
        assert guess[first](f"guess number {number}").status_code == 400
    for number in range(2):
        assert guess[second](f"another guess {number}").status_code == 400

    assert wrong_passwords(session, alice_account.id) == allowed
    assert guess["export"](PASSWORD).status_code == 429
    assert guess["other"](PASSWORD).status_code == 429
    assert session.scalar(select(func.count()).select_from(User)) == 1


def test_limit_follows_the_account_to_every_session(
    make_client, alice_client: TestClient, alice_account: User
) -> None:
    elsewhere = make_client(client=("203.0.113.9", 50000))
    assert log_in(elsewhere).status_code == 200
    for number in range(settings.password_confirmation_failures):
        assert export(alice_client, f"guess number {number}").status_code == 400

    assert export(elsewhere).status_code == 429


def test_limit_is_each_accounts_own(
    alice_client: TestClient, bob_client: TestClient
) -> None:
    for number in range(settings.password_confirmation_failures):
        assert export(alice_client, f"guess number {number}").status_code == 400

    assert export(bob_client).status_code == 200


def test_right_password_is_not_counted_as_a_guess(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    assert export(alice_client, "not my password at all").status_code == 400
    assert wrong_passwords(session, alice_account.id) == 1

    assert export(alice_client).status_code == 200

    assert wrong_passwords(session, alice_account.id) == 1
    assert exports_counted(session, alice_account.id) == 1


def test_wrong_password_is_not_counted_as_an_export(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    for number in range(settings.password_confirmation_failures - 1):
        assert export(alice_client, f"guess number {number}").status_code == 400

    # Four wrong passwords have used up none of the account's exports.
    assert exports_counted(session, alice_account.id) == 0
    for _ in range(settings.data_exports_per_account):
        assert export(alice_client).status_code == 200


# --- how often -----------------------------------------------------------


def test_exports_of_one_account_are_limited(
    alice_client: TestClient, session: Session, library: Library
) -> None:
    allowed = settings.data_exports_per_account

    given = [export(alice_client) for _ in range(allowed)]
    refused = export(alice_client)

    assert [response.status_code for response in given] == [200] * allowed
    assert refused.status_code == 429
    assert refused.json() == TOO_MANY
    assert refused.headers["retry-after"].isdigit()
    assert "content-disposition" not in refused.headers
    assert exports_counted(session, library.alice.id) == allowed


def test_export_that_is_refused_for_the_limit_is_not_a_wrong_password(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    for _ in range(settings.data_exports_per_account):
        assert export(alice_client).status_code == 200

    # Asking again and again, with the right password or a wrong one, is
    # refused and counts against nothing: the password is not looked at.
    for password in (PASSWORD, "a guess", "another guess", PASSWORD):
        assert export(alice_client, password).status_code == 429
    assert wrong_passwords(session, alice_account.id) == 0
    assert exports_counted(session, alice_account.id) == 3
    # So the limit on exports cannot be used to lock the account's owner out
    # of changing the password.
    changed = alice_client.post(
        "/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "an entirely new one"},
    )
    assert changed.status_code == 200


def test_limit_on_exports_follows_the_account_and_is_each_accounts_own(
    make_client, alice_client: TestClient, bob_client: TestClient
) -> None:
    elsewhere = make_client(client=("203.0.113.9", 50000))
    assert log_in(elsewhere).status_code == 200
    for _ in range(settings.data_exports_per_account):
        assert export(alice_client).status_code == 200

    assert export(elsewhere).status_code == 429
    assert export(bob_client).status_code == 200


def test_export_is_possible_again_when_the_window_is_over(
    alice_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(settings.data_exports_per_account):
        assert export(alice_client).status_code == 200
    assert export(alice_client).status_code == 429

    later = datetime.now(timezone.utc) + settings.rate_limit_window
    monkeypatch.setattr(rate_limit, "_now", lambda: later)

    assert export(alice_client).status_code == 200


def test_number_of_exports_is_a_setting(
    alice_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_EXPORTS_PER_ACCOUNT", "7")
    assert Settings().data_exports_per_account == 7
    assert Settings.model_fields["data_exports_per_account"].default == 3

    # Read on every request, like every other limit.
    monkeypatch.setattr(settings, "data_exports_per_account", 1)
    assert export(alice_client).status_code == 200
    assert export(alice_client).status_code == 429


def test_deleting_the_account_leaves_no_count_of_its_exports(
    alice_client: TestClient, session: Session, alice_account: User
) -> None:
    alice_id = alice_account.id
    assert export(alice_client).status_code == 200
    assert exports_counted(session, alice_id) == 1

    deleted = alice_client.post("/auth/delete-account", json={"password": PASSWORD})

    assert deleted.status_code == 204
    assert exports_counted(session, alice_id) is None
    assert wrong_passwords(session, alice_id) is None


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


# What each part of the document is read from, in the order they are read.
PARTS = [
    "FROM sessions",
    "FROM email_verification_tokens",
    "FROM password_reset_tokens",
    "FROM posts",
    "FROM likes",
    "FROM reposts",
    "FROM follows JOIN users ON users.id = follows.following_id",
    "FROM follows JOIN users ON users.id = follows.follower_id",
    "FROM stories",
    "FROM story_views",
]


def test_each_part_is_read_by_one_statement_with_the_account_held_still(
    alice_client: TestClient, library: Library, statements: Statements
) -> None:
    statements.clear()

    assert export(alice_client).status_code == 200

    [lock] = statements.where("FROM users", "FOR NO KEY UPDATE")
    reading = statements[lock + 1 :]
    selects = [s for s in reading if s.startswith("SELECT")]
    # The account is locked, in the way a password change locks it, and then
    # ten statements read the ten parts. That is all the reading there is.
    assert len(selects) == len(PARTS)
    for statement, source in zip(selects, PARTS):
        assert source in statement, statement
        assert "ORDER BY" in statement
        assert " FOR " not in statement
    # Nothing is written but the two counters of the rate limiter.
    writes = [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE"))]
    assert writes and all("rate_limits" in statement for statement in writes)
    assert statements.where("pg_advisory") == []


def test_no_statement_of_the_export_reads_a_credential_or_a_row_as_a_whole(
    alice_client: TestClient, library: Library, statements: Statements
) -> None:
    statements.clear()

    assert export(alice_client).status_code == 200

    [lock] = statements.where("FROM users", "FOR NO KEY UPDATE")
    for statement in statements[lock + 1 :]:
        if not statement.startswith("SELECT"):
            continue
        columns = statement.split(" FROM ")[0]
        assert "token_hash" not in statement
        assert "password_hash" not in statement
        assert "key_hash" not in statement
        # A session's id is compared in the database, to tell which session
        # is the current one. It is not among what is read.
        assert "sessions.id," not in columns and not columns.endswith("sessions.id")
        assert "tokens.id" not in columns
        assert "viewer_id" not in columns
        assert "follower_id" not in columns and "following_id" not in columns
        assert "*" not in columns.replace("count(*)", "")


def test_export_takes_the_same_statements_however_much_there_is_to_export(
    make_client, session: Session, statements: Statements
) -> None:
    def export_of(username: str, size: int) -> list[str]:
        user = add_user(session, username)
        for number in range(size):
            other = add_user(session, f"{username}_reader_{number}")
            post = add_post(session, user, f"{username} wrote {number}")
            answer = add_post(session, other, "An answer", parent=post)
            add_post(session, user, "And an answer to that", parent=answer)
            story = add_story(session, user)
            theirs = add_story(session, other)
            session.add_all(
                [
                    Like(user_id=user.id, post_id=answer.id),
                    Repost(user_id=user.id, post_id=answer.id),
                    Follow(follower_id=user.id, following_id=other.id),
                    Follow(follower_id=other.id, following_id=user.id),
                    StoryView(story_id=story.id, viewer_id=other.id),
                    StoryView(story_id=theirs.id, viewer_id=user.id),
                    a_session(user, at(number)),
                    a_link(PasswordResetToken, user, at(number)),
                ]
            )
        session.flush()
        client = make_client()
        assert log_in(client, username).status_code == 200
        statements.clear()
        response = export(client)
        assert response.status_code == 200
        assert len(response.json()["posts"]) == 2 * size
        assert len(response.json()["stories"]) == size
        return list(statements)

    small, large = export_of("small", 2), export_of("large", 40)

    # No statement per post, per story or per follower: twenty times as much
    # to export is the same statements, returning more rows.
    assert small == large
    assert len(small) < 20


def test_nothing_is_locked_or_read_until_the_password_is_known_to_be_right(
    alice_client: TestClient, library: Library, statements: Statements
) -> None:
    statements.clear()

    assert export(alice_client, "not my password at all").status_code == 400

    assert statements.where(" FOR ") == []
    for source in set(PARTS) - {"FROM sessions"}:
        assert statements.where(source) == [], source


def test_parts_are_read_one_after_the_other_and_not_as_one_picture(
    alice_client: TestClient,
    session: Session,
    alice_account: User,
    bob_account: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    of_bob = add_post(session, bob_account, "Bob's post")
    read_likes = export_service._interactions

    def something_happens_first(db: Session, model: type, user_id: uuid.UUID):
        # Between the reading of the posts and the reading of the likes, the
        # user writes a post and likes one.
        if model is Like:
            add_post(db, alice_account, "Written while the export was being made")
            db.add(Like(user_id=user_id, post_id=of_bob.id))
            db.flush()
        return read_likes(db, model, user_id)

    monkeypatch.setattr(export_service, "_interactions", something_happens_first)

    document = export(alice_client).json()

    # The like is in the part that was read after it was made. The post is
    # not in the part that had been read before. Each part is whole; they
    # are not all of one moment, and the export does not claim that they are.
    assert [like["post_id"] for like in document["likes"]] == [str(of_bob.id)]
    assert document["posts"] == []


# --- an account that goes while it is being asked for --------------------


def test_account_that_was_removed_under_the_request_gets_no_data(
    alice_client: TestClient,
    session: Session,
    library: Library,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authenticate = auth_service.authenticate_session

    def authenticate_then_remove(db: Session, raw_token: str) -> UserSession | None:
        found = authenticate(db, raw_token)
        if found is not None:
            # As by another request, between this one being let in and its
            # getting to work. What it has read of the account stays as it
            # was read; the rows are gone.
            erasure.erase_account(db, found.user_id, datetime.now(timezone.utc))
            expire, db.expire_on_commit = db.expire_on_commit, False
            db.commit()
            db.expire_on_commit = expire
        return found

    monkeypatch.setattr(auth_service, "authenticate_session", authenticate_then_remove)

    response = export(alice_client)

    # Not a document with an account and empty lists, and not somebody
    # else's: no document.
    assert response.status_code == 401
    assert response.json() == NOT_AUTHENTICATED
    assert "content-disposition" not in response.headers
    assert session.scalar(select(func.count()).select_from(User)) == 4

"""The cursor pagination utility on its own, apart from any endpoint."""

import base64
import struct
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    Cursor,
    InvalidCursorError,
    decode_cursor,
    encode_cursor,
    paginate,
)
from app.models import Follow, Post, User
from helpers import add_post, add_user

MOMENT = datetime(2026, 10, 5, 12, 30, 45, 123456, tzinfo=timezone.utc)


def packed(microseconds: int, id_bytes: bytes = b"\x00" * 16) -> str:
    """A well-formed cursor holding exactly these values."""
    raw = struct.pack(">q16s", microseconds, id_bytes)
    return base64.urlsafe_b64encode(raw).decode("ascii")


# --- the cursor ----------------------------------------------------------


@pytest.mark.parametrize(
    "created_at",
    [
        MOMENT,
        MOMENT.replace(microsecond=0),
        MOMENT.replace(microsecond=1),
        MOMENT.replace(microsecond=999999),
        datetime(1970, 1, 1, tzinfo=timezone.utc),
        datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc),
        datetime(1, 1, 1, tzinfo=timezone.utc),
        datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc),
    ],
)
def test_cursor_survives_encoding_exactly(created_at: datetime) -> None:
    cursor = Cursor(created_at=created_at, id=uuid.uuid4())

    assert decode_cursor(encode_cursor(cursor)) == cursor


def test_cursor_keeps_every_microsecond() -> None:
    # Two rows one microsecond apart must not end up at the same position.
    id = uuid.uuid4()
    earlier = encode_cursor(Cursor(created_at=MOMENT, id=id))
    later = encode_cursor(Cursor(MOMENT + timedelta(microseconds=1), id=id))

    assert earlier != later
    assert decode_cursor(later).created_at - decode_cursor(earlier).created_at == (
        timedelta(microseconds=1)
    )


def test_cursor_stands_for_an_instant_whatever_the_timezone() -> None:
    id = uuid.uuid4()
    plus_five = timezone(timedelta(hours=5))

    in_utc = encode_cursor(Cursor(created_at=MOMENT, id=id))
    elsewhere = encode_cursor(Cursor(created_at=MOMENT.astimezone(plus_five), id=id))

    assert in_utc == elsewhere
    assert decode_cursor(elsewhere).created_at.tzinfo is not None


def test_cursor_is_a_short_url_safe_string() -> None:
    for _ in range(50):
        value = encode_cursor(Cursor(created_at=MOMENT, id=uuid.uuid4()))

        assert len(value) == 32
        # Nothing that would need escaping in a query string.
        assert value.isascii()
        assert value.replace("-", "").replace("_", "").isalnum()


def test_cursor_holds_only_the_position() -> None:
    id = uuid.uuid4()

    raw = base64.urlsafe_b64decode(encode_cursor(Cursor(created_at=MOMENT, id=id)))

    assert len(raw) == 8 + 16
    assert raw[8:] == id.bytes


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "abc",
        "null",
        "0",
        str(uuid.uuid4()),
        MOMENT.isoformat(),
        "A" * 31,
        "A" * 33,
        "A" * 64,
        "A" * 100_000,
        "!" * 32,
        " " * 32,
        "A" * 30 + "==",
        "A" * 31 + "=",
        "A" * 31 + "\n",
        "A" * 31 + "+",  # standard base64, not the URL-safe alphabet
        "A" * 31 + "/",
        "é" * 32,
        "é" * 16,
        "😀" * 8,
        "A" * 31 + "\x00",
        "' OR '1'='1' -- " * 2,
    ],
)
def test_malformed_cursor_is_rejected(value: str) -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(value)


@pytest.mark.parametrize(
    "microseconds",
    [
        2**63 - 1,
        -(2**63),
        # One microsecond outside what a datetime can hold, either side.
        253_402_300_800_000_000,
        -62_135_596_800_000_001,
    ],
)
def test_cursor_with_an_impossible_time_is_rejected(microseconds: int) -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(packed(microseconds))


def test_well_formed_cursor_that_was_never_issued_is_just_a_position() -> None:
    # A client can make one up; all it gets is a place to start reading.
    cursor = decode_cursor(packed(0))

    assert cursor == Cursor(
        created_at=datetime(1970, 1, 1, tzinfo=timezone.utc),
        id=uuid.UUID(int=0),
    )


def test_invalid_cursor_error_does_not_echo_the_value() -> None:
    with pytest.raises(InvalidCursorError) as raised:
        decode_cursor("something the client sent")

    assert "something" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_page_size_limits_are_sensible() -> None:
    assert 1 <= DEFAULT_PAGE_SIZE <= MAX_PAGE_SIZE <= 100


# --- paginate ------------------------------------------------------------


def posts_at_distinct_times(session: Session, author: User, count: int) -> list[Post]:
    """``count`` posts, one minute apart, returned newest first."""
    posts = [
        add_post(
            session,
            author,
            f"Post {number}",
            created_at=MOMENT + timedelta(minutes=number),
        )
        for number in range(count)
    ]
    return posts[::-1]


def test_first_page_holds_the_newest_rows(session: Session, alice: User) -> None:
    posts = posts_at_distinct_times(session, alice, 5)

    page = paginate(session, select(Post), Post, limit=3)

    assert list(page.items) == posts[:3]
    assert page.next_cursor is not None


def test_next_page_starts_right_after_the_cursor(session: Session, alice: User) -> None:
    posts = posts_at_distinct_times(session, alice, 5)
    first = paginate(session, select(Post), Post, limit=3)

    second = paginate(
        session, select(Post), Post, limit=3, after=decode_cursor(first.next_cursor)
    )

    assert list(second.items) == posts[3:]
    assert second.next_cursor is None


def test_cursor_points_at_the_last_row_of_the_page(
    session: Session, alice: User
) -> None:
    posts = posts_at_distinct_times(session, alice, 5)

    page = paginate(session, select(Post), Post, limit=2)

    assert decode_cursor(page.next_cursor) == Cursor(
        created_at=posts[1].created_at, id=posts[1].id
    )


@pytest.mark.parametrize(("count", "limit"), [(0, 5), (1, 5), (4, 5), (5, 5)])
def test_page_that_holds_everything_has_no_next_cursor(
    session: Session, alice: User, count: int, limit: int
) -> None:
    posts_at_distinct_times(session, alice, count)

    page = paginate(session, select(Post), Post, limit=limit)

    assert len(page.items) == count
    assert page.next_cursor is None


def test_one_row_more_than_the_page_gives_a_next_cursor(
    session: Session, alice: User
) -> None:
    posts_at_distinct_times(session, alice, 6)

    page = paginate(session, select(Post), Post, limit=5)

    assert len(page.items) == 5
    assert page.next_cursor is not None


def test_rows_created_at_the_same_instant_are_ordered_by_id(
    session: Session, alice: User
) -> None:
    posts = [add_post(session, alice, created_at=MOMENT) for _ in range(9)]
    expected = sorted((post.id for post in posts), reverse=True)

    seen: list[uuid.UUID] = []
    after = None
    while True:
        page = paginate(session, select(Post), Post, limit=2, after=after)
        seen += [post.id for post in page.items]
        if page.next_cursor is None:
            break
        after = decode_cursor(page.next_cursor)

    # Every row exactly once, although no timestamp tells them apart.
    assert seen == expected


def test_paginate_keeps_the_statements_own_conditions_on_every_page(
    session: Session, alice: User, bob: User
) -> None:
    posts_at_distinct_times(session, bob, 4)
    alices = posts_at_distinct_times(session, alice, 4)
    statement = select(Post).where(Post.author_id == alice.id)

    first = paginate(session, statement, Post, limit=3)
    second = paginate(
        session, statement, Post, limit=3, after=decode_cursor(first.next_cursor)
    )

    assert [*first.items, *second.items] == alices


def test_cursor_beyond_the_last_row_gives_an_empty_page(
    session: Session, alice: User
) -> None:
    posts_at_distinct_times(session, alice, 3)
    long_ago = Cursor(created_at=MOMENT - timedelta(days=1), id=uuid.uuid4())

    page = paginate(session, select(Post), Post, limit=5, after=long_ago)

    assert list(page.items) == []
    assert page.next_cursor is None


# --- rows without an id of their own -------------------------------------


def followers_at(session: Session, followed: User, times: list[datetime]) -> list:
    """One new follower of ``followed`` per time, as (time, id of the follower)."""
    followers = []
    for number, time in enumerate(times):
        follower = add_user(session, f"follower{number:02d}")
        session.add(
            Follow(follower_id=follower.id, following_id=followed.id, created_at=time)
        )
        followers.append((time, follower.id))
    session.flush()
    return followers


def walk_follows(session: Session, followed: User, limit: int) -> list:
    statement = select(Follow).where(Follow.following_id == followed.id)
    seen, after = [], None
    while True:
        page = paginate(
            session,
            statement,
            Follow,
            limit=limit,
            after=after,
            tiebreaker="follower_id",
        )
        seen += [(follow.created_at, follow.follower_id) for follow in page.items]
        if page.next_cursor is None:
            return seen
        after = decode_cursor(page.next_cursor)


def test_named_column_takes_the_place_of_the_id(session: Session, alice: User) -> None:
    # A follow has no id. Among the followers of one user, the follower does.
    times = [MOMENT + timedelta(minutes=number) for number in range(5)]
    followers = followers_at(session, alice, times)
    statement = select(Follow).where(Follow.following_id == alice.id)

    first = paginate(session, statement, Follow, limit=2, tiebreaker="follower_id")
    second = paginate(
        session,
        statement,
        Follow,
        limit=2,
        after=decode_cursor(first.next_cursor),
        tiebreaker="follower_id",
    )

    newest_first = followers[::-1]
    assert [follow.follower_id for follow in first.items] == [
        id for _, id in newest_first[:2]
    ]
    assert [follow.follower_id for follow in second.items] == [
        id for _, id in newest_first[2:4]
    ]
    # The cursor is the same kind of cursor: a time, and that column as its id.
    time, follower_id = newest_first[1]
    assert decode_cursor(first.next_cursor) == Cursor(created_at=time, id=follower_id)


def test_named_column_orders_rows_created_at_the_same_instant(
    session: Session, alice: User
) -> None:
    followers = followers_at(session, alice, [MOMENT] * 9)

    for limit in (1, 2, 4, 9, 10):
        # Every row exactly once, although no timestamp tells them apart.
        assert walk_follows(session, alice, limit) == sorted(followers, reverse=True)


def test_naming_the_id_changes_nothing(session: Session, alice: User) -> None:
    posts_at_distinct_times(session, alice, 5)

    default = paginate(session, select(Post), Post, limit=3)
    named = paginate(session, select(Post), Post, limit=3, tiebreaker="id")

    assert list(named.items) == list(default.items)
    assert named.next_cursor == default.next_cursor

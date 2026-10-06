"""Cursor pagination for lists that are shown newest first.

A page is not addressed by its number or by an offset but by a position: the
last row of the page before it. The next page is "the rows that sort after
that one", which the database answers from an index however deep the page is.
Unlike an offset, a position does not move when rows are added in front of it
or removed, so a reader paging through a list is never shown a row twice and
never skips one.

The order is ``created_at`` descending, then ``id`` descending. The id only
breaks ties between rows created at the same instant, which makes the order
total; without it, rows with equal timestamps could change places between two
requests.

The cursor handed to clients encodes exactly those two values of the last row
on the page. It is opaque by contract, but it is not secret and it is not
signed, and it does not need to be: both values are already in the page the
client was given, and a cursor only selects a position. What a query may
return is decided by the query's own conditions, which apply to every page
whatever cursor comes with the request.
"""

import base64
import struct
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Generic, Protocol, TypeVar

from sqlalchemy import Select, tuple_
from sqlalchemy.orm import Mapped, Session

DEFAULT_PAGE_SIZE = 20
# Upper bound on what one request can make the server load and serialize.
MAX_PAGE_SIZE = 50

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MICROSECOND = timedelta(microseconds=1)
# created_at as whole microseconds since the epoch (the precision PostgreSQL
# stores), followed by the 16 bytes of the id.
_LAYOUT = struct.Struct(">q16s")
# 24 bytes are exactly 32 base64 characters, with no padding.
_ENCODED_LENGTH = 32


class InvalidCursorError(ValueError):
    """The value is not a cursor produced by ``encode_cursor``."""


@dataclass(frozen=True)
class Cursor:
    """The position of one row in the order."""

    created_at: datetime
    id: uuid.UUID


def encode_cursor(cursor: Cursor) -> str:
    microseconds = (cursor.created_at - _EPOCH) // _MICROSECOND
    packed = _LAYOUT.pack(microseconds, cursor.id.bytes)
    return base64.urlsafe_b64encode(packed).decode("ascii")


def decode_cursor(value: str) -> Cursor:
    """The position a cursor stands for.

    Anything that ``encode_cursor`` could not have produced is rejected.
    """
    # A shortcut, so that nothing is decoded for a value of the wrong size.
    # The checks below would reject it as well.
    if len(value) != _ENCODED_LENGTH:
        raise InvalidCursorError
    try:
        packed = base64.b64decode(value, altchars=b"-_", validate=True)
        microseconds, id_bytes = _LAYOUT.unpack(packed)
        created_at = _EPOCH + microseconds * _MICROSECOND
    except (ValueError, struct.error, OverflowError):
        # Not base64, not the right size, or a time no datetime can hold.
        raise InvalidCursorError from None
    cursor = Cursor(created_at=created_at, id=uuid.UUID(bytes=id_bytes))
    # The decoder also takes the characters of standard base64. Encoding the
    # result again is what makes each position have exactly one cursor.
    if encode_cursor(cursor) != value:
        raise InvalidCursorError
    return cursor


class _Ordered(Protocol):
    """A model that can be paginated: it has the two ordering columns."""

    id: Mapped[uuid.UUID]
    created_at: Mapped[datetime]


_Row = TypeVar("_Row", bound=_Ordered)


@dataclass(frozen=True)
class Page(Generic[_Row]):
    items: Sequence[_Row]
    # Where the next page starts, or None if this is the last one.
    next_cursor: str | None


def paginate(
    db: Session,
    statement: Select[tuple[_Row]],
    model: type[_Row],
    *,
    limit: int,
    after: Cursor | None = None,
) -> Page[_Row]:
    """One page of ``statement``'s rows of ``model``, newest first.

    ``statement`` selects and filters; the order, the starting position and
    the size are applied here. ``after`` is the cursor of the previous page,
    or None for the first page.
    """
    if after is not None:
        # A row comparison: strictly after the cursor's row in the order.
        statement = statement.where(
            tuple_(model.created_at, model.id) < (after.created_at, after.id)
        )
    rows = db.scalars(
        statement.order_by(model.created_at.desc(), model.id.desc())
        # One row more than asked for, only to learn whether a next page
        # exists. It is not returned.
        .limit(limit + 1)
    ).all()

    items = rows[:limit]
    has_more = len(rows) > limit
    next_cursor = (
        encode_cursor(Cursor(created_at=items[-1].created_at, id=items[-1].id))
        if has_more
        else None
    )
    return Page(items=items, next_cursor=next_cursor)

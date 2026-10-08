"""Rate limiting: counting how often something is done, and refusing it once
that is too often.

The counters are rows in PostgreSQL, so every worker process sees the same
ones and they outlast a restart. Nothing is kept in memory.

A counter is addressed by a keyed hash of what it counts. Neither an address
nor an identifier is ever written to the table, and a row cannot be tested
against a guess of one without the secret.

Counting is one statement. It takes a place in the counter if one is free
and reports whether it did, as a single step that the database serializes
per row: of any number of requests arriving at once, no more get a place
than there are. A request that finds the counter full takes nothing, so a
counter never holds more than its maximum and never holds anything for a
request that was refused.

A limit on failures is counted the same way, before it is known how the
attempt ends: the place is taken first and given back if the attempt
succeeds. While attempts are under way the counter therefore holds them as
well as the failures. That is what keeps simultaneous attempts from
outnumbering the limit, and it means that more simultaneous attempts than
there are places are refused for the moment, whatever they would have
turned out to be.

The window is fixed: it begins with the first request counted and ends a set
time later, however many requests follow. Being refused does not extend it.

Transactions. ``count`` and ``uncount_all`` commit the session they are
given, so that a count stands whatever becomes of the request and no row
stays locked while the request does its work. This is not a general-purpose
utility that can be called from anywhere: committing the session commits
everything pending in it. Call these only at a point where the session holds
no other changes, as the authentication endpoints do: before their own work
begins, or after it has been committed.
"""

import hashlib
import hmac
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import case, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import RateLimit


class RateLimitedError(Exception):
    """Reported to the client as 429, the same whichever limit was reached."""

    status_code = 429
    detail = "Too many requests. Try again later."

    def __init__(self, retry_after: int) -> None:
        super().__init__(self.detail)
        # Whole seconds until the window that was full is over.
        self.retry_after = retry_after


@dataclass(frozen=True)
class Limit:
    """One counter and how high it may go within a window."""

    # Which limit this is, e.g. "login:ip". Part of the key, so that the same
    # address is counted separately for each thing it is limited in.
    scope: str
    # Whom it is counted for: an address, an identifier, an account id.
    subject: str
    maximum: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


def key_hash(scope: str, subject: str) -> str:
    """The form in which a counter is addressed: an HMAC-SHA256 hex digest."""
    secret = settings.rate_limit_secret.get_secret_value().encode("utf-8")
    # The separator cannot occur in a scope, so no two pairs share a message.
    message = f"{scope}\x00{subject}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Reservation:
    """A place taken in a counter, with the window it was taken in."""

    limit: Limit
    window_start: datetime


def count(db: Session, limit: Limit) -> Reservation:
    """Take a place in ``limit``'s counter, or refuse if none is free.

    Commits the session (see the module's note on transactions). A request
    that is refused has taken nothing.
    """
    now = _now()
    window = settings.rate_limit_window
    key = key_hash(limit.scope, limit.subject)
    window_is_over = RateLimit.window_start <= now - window
    # Nothing is returned when the row exists and the condition fails: the
    # window is still running and every place in it is taken.
    window_start = db.scalar(
        insert(RateLimit)
        .values(key_hash=key, window_start=now, count=1)
        .on_conflict_do_update(
            index_elements=[RateLimit.key_hash],
            set_={
                "count": case((window_is_over, 1), else_=RateLimit.count + 1),
                "window_start": case(
                    (window_is_over, now), else_=RateLimit.window_start
                ),
            },
            where=or_(window_is_over, RateLimit.count < limit.maximum),
        )
        .returning(RateLimit.window_start)
    )
    if window_start is not None:
        db.commit()
        return Reservation(limit, window_start)

    # Only to say when to come back. The row is still locked by the
    # statement above, so this is the window that was found full.
    full_since = db.scalar(
        select(RateLimit.window_start).where(RateLimit.key_hash == key)
    )
    db.commit()
    remaining = (
        (full_since + window - now).total_seconds()
        if full_since is not None
        else window.total_seconds()
    )
    raise RateLimitedError(retry_after=max(1, math.ceil(remaining)))


def count_all(db: Session, limits: Sequence[Limit]) -> list[Reservation]:
    """Take a place in each limit's counter, in turn, or in none of them.

    If one of them is full, the places already taken in those before it are
    given back and the request is refused: it counts against nothing. The
    order still matters, for which limit is asked first: a request that the
    narrowest limit refuses never reaches the wider ones.
    """
    taken: list[Reservation] = []
    try:
        for limit in limits:
            taken.append(count(db, limit))
    except RateLimitedError:
        uncount_all(db, taken)
        raise
    return taken


def uncount_all(db: Session, reservations: Sequence[Reservation]) -> None:
    """Give back the places that were taken.

    For limits on failures, when the attempt turns out not to be one, and
    for a request that was refused by a later limit.

    A place is only given back in the window it was taken in. If that
    window has ended meanwhile and another has begun in the same row, the
    counts in it belong to other requests and are left alone.

    Commits the session (see the module's note on transactions).
    """
    for reservation in reservations:
        limit = reservation.limit
        db.execute(
            update(RateLimit)
            .where(
                RateLimit.key_hash == key_hash(limit.scope, limit.subject),
                RateLimit.window_start == reservation.window_start,
                RateLimit.count > 0,
            )
            .values(count=RateLimit.count - 1)
        )
    db.commit()

"""Retention: removing rows that are of no use any more.

Much of what the application stores stops being used without being removed:
a story expires, a session ends, a link is opened or runs out, a rate-limit
window is over, somebody registers and never opens the link. Nothing reads
those rows again, except an export of the account they belong to. ``purge``
removes them once they have been of no use for a set time. It is run from
outside the application, by hand or by cron (``python -m app.purge``).

What is removed
---------------

Each period is a setting (``app.core.config``) and begins when the row stops
being of use. A row is removed when that moment is the period ago or
longer, counted from the ``now`` that ``purge`` is given.

- Stories that expired. Who viewed them goes with them.
- Sessions that expired or were revoked, whichever came first.
- Verification and reset tokens that were used or expired, whichever came
  first.
- Rate-limit counters whose window is over. Such a counter counts nothing:
  the next request would start it again from one, which is what it finds
  when the row is gone.
- Accounts that were never verified (``_abandoned``), if that is switched
  on (``PURGE_UNVERIFIED_ACCOUNTS``). It is off unless asked for.

Nothing else is. A post is never removed for its age, and neither is an
account that was ever verified, whatever became of it since.

Accounts that were never verified
---------------------------------

An account whose email address was not verified cannot be logged in to, so
nothing was ever written in its name. It holds a username and an address,
perhaps somebody else's, and keeps both from being registered.

Removing such accounts presumes that the link reaches whoever registered.
Where no email is sent, nobody can verify, and every registration would be
removed for it. So this part of the purge does nothing unless it is switched
on, by whoever knows that verification works where it runs. Switched on, an
account is removed when all of this is true:

- its address was never verified, and it was registered the period ago or
  longer;
- it is active. One that was deactivated was deactivated by somebody, and
  that is not undone here by setting its names free;
- no verification link of it could still be opened. Asking for a new link
  thus keeps an account for as long as that link lasts;
- it has no post, no story and no session. It cannot have any; if one does,
  it is not what this rule is about and it is left alone.

It is removed the way a user removes their own account, by
``erasure.erase_account``, one account in a transaction.

Transactions
------------

Rows are removed in batches of at most ``PURGE_BATCH_SIZE``, each in a
transaction of its own that is committed before the next begins. No lock is
kept for longer than one batch takes, and what a run has removed stays
removed if it fails later on: it then stops, the batch it was in is rolled
back, and ``PurgeError`` says what was done until then. The next run goes on
where this one stopped, since what is left to do is read from the rows.

Running it again removes nothing that the run before it should not have,
and running it when there is nothing to remove does nothing.

Locks
-----

The order is that of ``app.services.erasure``, and the purge fits into it
without anything new.

A batch locks its rows FOR UPDATE and skips those that somebody else holds
(SKIP LOCKED). It never waits for a row, so it is never one of two requests
that each hold what the other is waiting for; a row that was skipped is
found by the next run. For sessions, tokens and counters that is all, since
removing one of those removes nothing else.

Removing a story removes its views, and the views of several stories are
also what the removal of an account takes with it, in whichever order the
database comes across them. So a batch of stories takes the erasure lock
first, like everything else that removes a story of more than one account:
an account's removal and a batch of stories then come one after the other.
It is taken before any story is locked, which is its place in the order.

An account is locked FOR UPDATE, which is what its removal by its user
takes, and skipped if it is held: somebody is opening its link or asking
for one. Whether it is abandoned is asked again once it is held, by a
statement that begins after that, so that a link issued or opened a moment
before is seen.

Two runs at once are safe, if pointless: each skips what the other holds.
"""

from collections.abc import Iterator
from dataclasses import dataclass, fields
from datetime import datetime

from sqlalchemy import ColumnElement, delete, exists, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.core.config import settings
from app.models import (
    EmailVerificationToken,
    PasswordResetToken,
    Post,
    RateLimit,
    Story,
    User,
    UserSession,
)
from app.services import erasure

_Conditions = tuple[ColumnElement[bool], ...]


@dataclass
class Report:
    """How many rows a run removed, by what they were."""

    unverified_accounts: int = 0
    expired_stories: int = 0
    ended_sessions: int = 0
    email_verification_tokens: int = 0
    password_reset_tokens: int = 0
    rate_limit_counters: int = 0

    @property
    def total(self) -> int:
        return sum(self.as_dict().values())

    def as_dict(self) -> dict[str, int]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


class PurgeError(Exception):
    """A run that failed part of the way. The cause is chained to it.

    ``report`` is what was removed and committed before that, and
    ``category`` the field of the report it failed in.
    """

    def __init__(self, category: str, report: Report) -> None:
        super().__init__(f"The purge failed while removing {category}.")
        self.category = category
        self.report = report


# --- what is of no use any more -------------------------------------------
#
# Which rows are removed is decided in this section and nowhere else.


def _expired_stories(now: datetime) -> _Conditions:
    return (Story.expires_at <= now - settings.expired_story_retention,)


def _ended_sessions(now: datetime) -> _Conditions:
    cutoff = now - settings.ended_session_retention
    return (or_(UserSession.expires_at <= cutoff, UserSession.revoked_at <= cutoff),)


def _spent(
    model: type[EmailVerificationToken] | type[PasswordResetToken], now: datetime
) -> _Conditions:
    cutoff = now - settings.spent_token_retention
    return (or_(model.expires_at <= cutoff, model.used_at <= cutoff),)


def _finished_counters(now: datetime) -> _Conditions:
    # A window is over once it began a window ago (``rate_limit.count``).
    window_over_since = now - settings.rate_limit_window
    return (
        RateLimit.window_start <= window_over_since - settings.rate_limit_retention,
    )


def _abandoned(now: datetime) -> _Conditions:
    """Conditions on a ``users`` row: was this account never taken up?

    See the module's note on such accounts.
    """
    link = EmailVerificationToken
    return (
        User.email_verified_at.is_(None),
        User.is_active.is_(True),
        User.created_at <= now - settings.unverified_account_retention,
        ~exists().where(
            link.user_id == User.id, link.used_at.is_(None), link.expires_at > now
        ),
        ~exists().where(Post.author_id == User.id),
        ~exists().where(Story.author_id == User.id),
        ~exists().where(UserSession.user_id == User.id),
    )


# --- removing ---------------------------------------------------------------


def _remove_rows(
    db: Session,
    key: InstrumentedAttribute,
    of_no_use: _Conditions,
    *,
    among_erasures: bool = False,
) -> Iterator[int]:
    """Remove the rows of ``key``'s table that match, a batch at a time.

    Yields how many each batch removed, once it is committed.

    The rows are locked before they are removed, and whether they match is
    decided with the lock: a counter that a request has just started again
    is looked at as it is now, and left. Rows that somebody else holds are
    passed over.

    ``among_erasures`` takes the erasure lock for each batch, for rows whose
    removal removes others (see the module's note on locks).
    """
    while True:
        if among_erasures:
            erasure.lock_erasure(db)
        keys = db.scalars(
            select(key)
            .where(*of_no_use)
            .limit(settings.purge_batch_size)
            .with_for_update(skip_locked=True)
        ).all()
        if not keys:
            # Ends the transaction, and gives the erasure lock back.
            db.commit()
            return
        db.execute(
            delete(key.class_).where(key.in_(keys)),
            execution_options={"synchronize_session": False},
        )
        db.commit()
        yield len(keys)


def _remove_accounts(db: Session, now: datetime) -> Iterator[int]:
    """Remove the accounts that were never taken up, one in a transaction.

    Yields 1 for each that was removed, once that is committed. They are
    looked for in the order of their ids, a batch at a time and without a
    lock, and each is then decided on by itself. An account that is passed
    over is not come back to in this run.
    """
    after = None
    while True:
        found = select(User.id).where(*_abandoned(now))
        if after is not None:
            found = found.where(User.id > after)
        user_ids = db.scalars(
            found.order_by(User.id).limit(settings.purge_batch_size)
        ).all()
        db.commit()
        if not user_ids:
            return
        after = user_ids[-1]
        for user_id in user_ids:
            if _remove_account(db, user_id, now):
                yield 1


def _remove_account(db: Session, user_id: object, now: datetime) -> bool:
    held = db.scalar(
        select(User.id).where(User.id == user_id).with_for_update(skip_locked=True)
    )
    # A statement of its own, begun after the lock is held: it sees the link
    # that was issued, or opened, while the account was being looked for.
    if held is None or not db.scalar(
        select(exists().where(User.id == user_id, *_abandoned(now)))
    ):
        db.commit()
        return False
    erasure.erase_account(db, user_id, now)
    db.commit()
    return True


# The field of the report that counts the accounts.
_ACCOUNTS = "unverified_accounts"


def _rows_of_no_use(
    now: datetime,
) -> dict[str, tuple[InstrumentedAttribute, _Conditions, bool]]:
    """What is removed row by row: for each field of the report, the key of
    the table, the rows of it that are of no use, and whether removing them
    is done among erasures (see ``_remove_rows``)."""
    return {
        "expired_stories": (Story.id, _expired_stories(now), True),
        "ended_sessions": (UserSession.id, _ended_sessions(now), False),
        "email_verification_tokens": (
            EmailVerificationToken.id,
            _spent(EmailVerificationToken, now),
            False,
        ),
        "password_reset_tokens": (
            PasswordResetToken.id,
            _spent(PasswordResetToken, now),
            False,
        ),
        "rate_limit_counters": (RateLimit.key_hash, _finished_counters(now), False),
    }


def purge(db: Session, now: datetime) -> Report:
    """Remove everything that has been of no use for its period at ``now``.

    Commits the session, once for every batch: it is to be given a session
    that holds no changes of its own. Fails with ``PurgeError``, which
    carries what was removed until then.
    """
    report = Report()

    def count(category: str, batches: Iterator[int]) -> None:
        try:
            for removed in batches:
                setattr(report, category, getattr(report, category) + removed)
        except Exception as error:
            db.rollback()
            raise PurgeError(category, report) from error

    # Accounts first: their tokens go with them and need not be found again.
    if settings.purge_unverified_accounts:
        count(_ACCOUNTS, _remove_accounts(db, now))
    for category, (key, of_no_use, among_erasures) in _rows_of_no_use(now).items():
        count(
            category,
            _remove_rows(db, key, of_no_use, among_erasures=among_erasures),
        )
    return report


def pending(db: Session, now: datetime) -> Report:
    """What ``purge`` would find to remove at ``now``, counted and not touched.

    Nothing is locked and nothing is written; the session is committed only
    to end its transaction. The rules are the ones the
    purge goes by, so the counts are of the same rows; a run can remove
    fewer, by what is held by somebody else at that moment, or what has
    been used again since.
    """
    report = Report()
    found = {
        category: (key.class_, of_no_use)
        for category, (key, of_no_use, _) in _rows_of_no_use(now).items()
    }
    if settings.purge_unverified_accounts:
        found[_ACCOUNTS] = (User, _abandoned(now))
    for category, (model, of_no_use) in found.items():
        matching = select(func.count()).select_from(model).where(*of_no_use)
        setattr(report, category, db.scalar(matching))
    # Ends the transaction the counting was done in. There is nothing in it.
    db.commit()
    return report

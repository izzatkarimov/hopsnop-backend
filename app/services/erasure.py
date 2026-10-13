"""Erasure: removing what a user wrote, and the user, for good.

This module is where posts and accounts are erased, whoever asks for it: a
user deleting a post or their account, and the retention purge. It knows the
models and nothing of the other services, so each of them can use it.

Erasing a post
--------------

A post that is erased leaves nothing of its author or its text. What becomes
of its row depends on whether anyone has replied to it:

- With no replies, the row is removed, and its likes and reposts with it.
- With replies, the row is kept as a tombstone: no author, no text, no likes
  and no reposts, only what its replies need to stay where they are in the
  thread (its id, the post it answered, its timestamps).

A tombstone is there for its replies and for nothing else. When the last of
them is gone it is removed as well, and then so is the tombstone above it if
that was its last reply, and so on up the thread.

Both cases are one operation here (``erase_posts``): every post becomes a
tombstone, and every tombstone that has no reply is removed. A post without
replies is thus a tombstone only within the transaction that erases it.

Erasing an account
------------------

An account that is erased leaves no row of its own and nothing that says
whose anything was (``erase_account``), in this order:

1. Its posts are erased as above, each by the same rule. Where others have
   replied, a tombstone stays; it names no author and holds no text.
2. Its stories are removed, and the record of who viewed them with them.
3. The account's row is removed. With it go, by the foreign keys, its
   sessions, its verification and reset tokens, whom it follows and who
   follows it, its likes and reposts, and the views it made of others'
   stories.

The row goes last because the database insists on it: a post or a story
keeps its author's row from being removed (ON DELETE RESTRICT), so that an
account can never go while something it wrote is still attributed to it.

It is one transaction: all of it or none of it. Nothing is deactivated or
marked first, and nothing is kept from which the account could be restored:
its username and its email address are free the moment it is committed.

Locks, and the order they are taken in
--------------------------------------

Every lock that erasing involves is taken in this order, and nothing in the
application takes two of them in the opposite one. So no two requests can
each hold what the other is waiting for.

1. The row in ``users`` of the account that is acting.
   FOR KEY SHARE by whatever writes a row in the account's name: a post, a
   like, a follow, a story, a view (``app.services.auth.hold_account``).
   FOR NO KEY UPDATE by whatever needs the account to stand still: a login,
   a password change or reset, a link being issued or redeemed, an export
   of the account's data (``app.services.auth._lock_account``).
   FOR UPDATE by what removes the account, which must keep out all of the
   above and wait for what is under way.
2. The erasure lock (``lock_erasure`` below).
3. What the request is about. For an erasure, rows of ``posts``: the posts
   being erased, FOR UPDATE, in the order of their ids; then the posts they
   answered, upwards; then the account's stories. For a write, the one row
   it refers to, FOR KEY SHARE: the post that is replied to or liked, the
   story that is viewed, the account that is followed.
4. Rows of tokens and sessions.

The first of these is what makes removing an account safe to do beside
everything else. A request never comes to need its own account's row while
it holds something else, because it took that row before anything else. So
a writer and the removal of its account meet at the account's row and
nowhere else: one of them waits there, holding nothing the other needs.

A request holds at most one row that is not its own, and it holds that one
FOR KEY SHARE, which any number of requests can do at once. It can be made
to wait by an erasure, and an erasure by it, but it cannot be part of a
cycle.

Why erasures are serialized
---------------------------

Erasing a post can take the post it answered with it, if that was only kept
for its replies, and so on upwards. Two erasures that reach into each
other's threads would lock such posts one after the other, each from its own
end, and could meet in the middle with each holding what the other needs.
The order of ids does not help there: which posts an erasure will need is
only known as it goes.

Rather than a scheme that orders those locks, there is one lock that every
erasure takes before it touches a post. Erasures then happen one at a time
and cannot deadlock with each other at all. They are rare and short, and
nothing else waits for this lock: reading, writing a post, liking and
following are not held up by it.

The retention purge takes the same lock for every batch of expired stories
it removes, for the like reason: the views that go with those stories are
also what the removal of an account takes with it
(``app.services.retention``).
"""

import uuid
from collections.abc import Collection
from datetime import datetime

from sqlalchemy import ColumnElement, Uuid, any_, delete, exists, func, literal
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import InstrumentedAttribute, Session, aliased

from app.models import Like, Post, Repost, Story, User

# The key of the erasure lock among PostgreSQL's advisory locks, which are
# addressed by a number that means nothing to the database. It is "hopsnop"
# in ASCII, read as one integer; any other constant would do as well, as long
# as nothing else in this database uses the same one.
ERASURE_LOCK_KEY = 0x686F70736E6F70


def lock_erasure(db: Session) -> None:
    """Take the erasure lock, waiting for whoever holds it.

    Held until the transaction ends, by commit or by rollback; there is
    nothing to release and no way to forget to. Taken a second time in the
    same transaction it is simply already held.

    To be called before any post is locked, and after the account's row if
    that is locked as well (see the order above).
    """
    db.execute(select(func.pg_advisory_xact_lock(ERASURE_LOCK_KEY)))


def _one_of(
    column: InstrumentedAttribute[uuid.UUID] | InstrumentedAttribute[uuid.UUID | None],
    ids: Collection[uuid.UUID],
) -> ColumnElement[bool]:
    """Condition: the column holds one of these ids.

    The ids are handed to the database as a single array, however many there
    are. Listed one by one they would each be a parameter of the statement,
    and a statement takes 65,535 of those and no more: everything a prolific
    account ever wrote could not be erased in one go.
    """
    return column == any_(literal(list(ids), ARRAY(Uuid)))


# Erasing works on rows, in bulk, and not on the objects a session may hold
# of them. Those are not brought up to date here: the caller commits, and a
# commit expires them.
_IN_BULK = {"synchronize_session": False}


def erase_posts(db: Session, post_ids: Collection[uuid.UUID], now: datetime) -> None:
    """Erase these posts: no author, no text, no likes or reposts are left.

    Each is removed if nothing answers it and kept as a tombstone if
    something does; tombstones that this leaves without a reply are removed
    as well (``prune_tombstones``).

    The caller holds the erasure lock and has locked the posts themselves
    FOR UPDATE. The second matters as much as the first. A reply or a like
    that is being written at this moment holds its post FOR KEY SHARE, so
    locking the posts waits for it, and what follows here sees it: the like
    is removed with the others, and the reply keeps its post as a tombstone.
    Nothing is committed here.
    """
    post_ids = list(post_ids)
    if not post_ids:
        return
    # Explicitly, and not left to the row being removed: a post that stays
    # as a tombstone keeps its row, and must not keep these.
    for kind in (Like, Repost):
        db.execute(
            delete(kind).where(_one_of(kind.post_id, post_ids)),
            execution_options=_IN_BULK,
        )
    db.execute(
        update(Post)
        .where(_one_of(Post.id, post_ids), Post.deleted_at.is_(None))
        .values(author_id=None, content=None, deleted_at=now),
        execution_options=_IN_BULK,
    )
    prune_tombstones(db, post_ids)


def prune_tombstones(db: Session, candidate_ids: Collection[uuid.UUID]) -> int:
    """Remove those of the candidates that are tombstones without a reply.

    And then the tombstones above them that this has left without a reply,
    and so on upwards, for as long as there is something to remove. Returns
    how many rows were removed. Posts that are not tombstones are never
    touched, whatever is passed in.

    Each round locks its candidates before it looks at their replies. A
    reply that was being written to one of them is waited for, so a
    tombstone is not removed from under a reply that had just arrived. No
    new reply can arrive at all: a tombstone is not shown, and what is not
    shown cannot be answered.

    The caller holds the erasure lock.
    """
    reply = aliased(Post)
    candidates = set(candidate_ids)
    removed = 0
    while candidates:
        tombstones = db.scalars(
            select(Post.id)
            .where(_one_of(Post.id, candidates), Post.deleted_at.is_not(None))
            .order_by(Post.id)
            .with_for_update()
        ).all()
        if not tombstones:
            break
        # A statement of its own, begun after the locks are held: it sees
        # every reply that was committed while they were waited for.
        answered_posts = db.scalars(
            delete(Post)
            .where(
                _one_of(Post.id, tombstones),
                ~exists().where(reply.parent_post_id == Post.id),
            )
            .returning(Post.parent_post_id),
            execution_options=_IN_BULK,
        ).all()
        removed += len(answered_posts)
        # What the removed rows answered may now be without a reply itself.
        candidates = {post_id for post_id in answered_posts if post_id is not None}
    return removed


def erase_account(db: Session, user_id: uuid.UUID, now: datetime) -> None:
    """Erase the account and everything that is its own. Nothing is committed.

    The caller holds the account's row FOR UPDATE
    (``app.services.auth._lock_account`` with ``exclusive``). That lock is
    what makes this complete. Whatever was being written for the account
    when it was taken (a post, a like, a session by a login) has been waited
    for and is seen here; whatever comes after it cannot be written until
    this is committed, and then has no account to be written for.

    The order follows the foreign keys. Posts and stories refuse to lose
    their author, so they go first; everything else that names the account
    goes with its row.
    """
    lock_erasure(db)
    # Locked before they are erased: a reply or a like that is being written
    # to one of them at this moment is waited for (see ``erase_posts``).
    post_ids = db.scalars(
        select(Post.id)
        .where(Post.author_id == user_id)
        .order_by(Post.id)
        .with_for_update()
    ).all()
    erase_posts(db, post_ids, now)
    # The views of them go with the rows (ON DELETE CASCADE), and a view
    # that is being recorded right now is waited for in the same way.
    db.execute(
        delete(Story).where(Story.author_id == user_id), execution_options=_IN_BULK
    )
    db.execute(delete(User).where(User.id == user_id), execution_options=_IN_BULK)

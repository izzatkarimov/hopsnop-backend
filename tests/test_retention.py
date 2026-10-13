"""The retention purge (Phase 7B-4): what it removes, when, and what it leaves.

Every test here gives the purge its own ``NOW`` and writes rows that ended a
known time before it, so nothing depends on how long a test takes. What
happens when the purge runs beside other requests is in
``test_retention_concurrency.py``.
"""

import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app import purge as purge_command
from app.core.config import Settings, settings
from app.db.session import engine
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
from app.services import erasure, retention
from app.services.rate_limit import key_hash
from database_guard import tables_with_rows
from helpers import (
    PASSWORD,
    PASSWORD_HASH,
    add_post,
    add_user,
    erase,
    follow,
    log_in,
    registration,
    what_is_left_of,
)

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
A_MOMENT = timedelta(seconds=1)
LONG_AGO = NOW - timedelta(days=400)
NOTHING = {
    "unverified_accounts": 0,
    "expired_stories": 0,
    "ended_sessions": 0,
    "email_verification_tokens": 0,
    "password_reset_tokens": 0,
    "rate_limit_counters": 0,
}


def removed(**counts: int) -> dict[str, int]:
    return {**NOTHING, **counts}


@pytest.fixture(autouse=True)
def _accounts_are_purged(monkeypatch: pytest.MonkeyPatch) -> None:
    # Off unless it is switched on. It is switched on for the tests of the
    # rule; those of the switch itself turn it off again.
    monkeypatch.setattr(settings, "purge_unverified_accounts", True)


def purge(session: Session, now: datetime = NOW) -> dict[str, int]:
    return retention.purge(session, now).as_dict()


def exists(session: Session, model: type, key: object) -> bool:
    session.expire_all()
    return session.get(model, key) is not None


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model))


# --- rows that ended at a known moment ------------------------------------
#
# Each of these writes one row that stopped being of use at ``ended``, and
# returns its model and its key.


def a_story(session: Session, ended: datetime) -> tuple[type, object]:
    story = Story(
        author_id=add_user(session, f"s{uuid.uuid4().hex[:10]}").id,
        media_url="https://media.example.com/stories/1.jpg",
        media_type="image",
        created_at=ended - timedelta(hours=24),
        expires_at=ended,
    )
    session.add(story)
    session.flush()
    return Story, story.id


def add_session(
    session: Session,
    user: User,
    *,
    expires_at: datetime,
    revoked_at: datetime | None = None,
) -> UserSession:
    row = UserSession(
        user_id=user.id,
        token_hash=uuid.uuid4().hex,
        created_at=LONG_AGO,
        expires_at=expires_at,
        last_used_at=LONG_AGO,
        revoked_at=revoked_at,
    )
    session.add(row)
    session.flush()
    return row


def an_expired_session(session: Session, ended: datetime) -> tuple[type, object]:
    user = add_user(session, f"x{uuid.uuid4().hex[:10]}")
    return UserSession, add_session(session, user, expires_at=ended).id


def a_revoked_session(session: Session, ended: datetime) -> tuple[type, object]:
    # Revoked well before it would have expired.
    user = add_user(session, f"r{uuid.uuid4().hex[:10]}")
    row = add_session(
        session, user, expires_at=NOW + timedelta(days=20), revoked_at=ended
    )
    return UserSession, row.id


def add_token(
    session: Session,
    model: type,
    user: User,
    *,
    expires_at: datetime,
    used_at: datetime | None = None,
) -> EmailVerificationToken | PasswordResetToken:
    row = model(
        user_id=user.id,
        token_hash=uuid.uuid4().hex,
        created_at=LONG_AGO,
        expires_at=expires_at,
        used_at=used_at,
    )
    session.add(row)
    session.flush()
    return row


def an_expired_token(model: type) -> Callable[[Session, datetime], tuple[type, object]]:
    def make(session: Session, ended: datetime) -> tuple[type, object]:
        user = add_user(session, f"t{uuid.uuid4().hex[:10]}")
        return model, add_token(session, model, user, expires_at=ended).id

    return make


def a_used_token(model: type) -> Callable[[Session, datetime], tuple[type, object]]:
    def make(session: Session, ended: datetime) -> tuple[type, object]:
        # Used well before it would have expired.
        user = add_user(session, f"u{uuid.uuid4().hex[:10]}")
        row = add_token(
            session,
            model,
            user,
            expires_at=NOW + timedelta(days=20),
            used_at=ended,
        )
        return model, row.id

    return make


def add_counter(session: Session, window_start: datetime, count: int = 3) -> str:
    key = key_hash("test", uuid.uuid4().hex)
    session.add(RateLimit(key_hash=key, window_start=window_start, count=count))
    session.flush()
    return key


def a_counter(session: Session, ended: datetime) -> tuple[type, object]:
    # Its window was over at ``ended``: it began one window before.
    return RateLimit, add_counter(session, ended - settings.rate_limit_window)


def add_unverified(
    session: Session, username: str, registered: datetime, *, active: bool = True
) -> User:
    user = User(
        username=username,
        email=f"{username}@example.com",
        password_hash=PASSWORD_HASH,
        display_name=username.title(),
        is_active=active,
        created_at=registered,
        updated_at=registered,
    )
    session.add(user)
    session.flush()
    return user


def an_unverified_account(session: Session, ended: datetime) -> tuple[type, object]:
    # The period of an account begins when it is registered.
    return User, add_unverified(session, f"a{uuid.uuid4().hex[:10]}", ended).id


# What is removed, the setting that says how long after, and where the
# purge counts it.
RULES = {
    "story": (a_story, "expired_story_retention_hours", "hours", "expired_stories"),
    "expired session": (
        an_expired_session,
        "ended_session_retention_days",
        "days",
        "ended_sessions",
    ),
    "revoked session": (
        a_revoked_session,
        "ended_session_retention_days",
        "days",
        "ended_sessions",
    ),
    "expired verification token": (
        an_expired_token(EmailVerificationToken),
        "spent_token_retention_days",
        "days",
        "email_verification_tokens",
    ),
    "used verification token": (
        a_used_token(EmailVerificationToken),
        "spent_token_retention_days",
        "days",
        "email_verification_tokens",
    ),
    "expired reset token": (
        an_expired_token(PasswordResetToken),
        "spent_token_retention_days",
        "days",
        "password_reset_tokens",
    ),
    "used reset token": (
        a_used_token(PasswordResetToken),
        "spent_token_retention_days",
        "days",
        "password_reset_tokens",
    ),
    "rate-limit counter": (
        a_counter,
        "rate_limit_retention_hours",
        "hours",
        "rate_limit_counters",
    ),
    "unverified account": (
        an_unverified_account,
        "unverified_account_retention_days",
        "days",
        "unverified_accounts",
    ),
}
DEFAULTS = {
    "expired_story_retention_hours": 24,
    "ended_session_retention_days": 30,
    "spent_token_retention_days": 7,
    "rate_limit_retention_hours": 1,
    "unverified_account_retention_days": 7,
    "purge_batch_size": 1000,
}

each_rule = pytest.mark.parametrize("rule", RULES)


def period(rule: str) -> timedelta:
    _, setting, unit, _ = RULES[rule]
    return timedelta(**{unit: getattr(settings, setting)})


# --- configuration --------------------------------------------------------


def configured(**values: object) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://nobody@localhost/nothing",
        environment="development",
        **values,
    )


def test_the_documented_defaults() -> None:
    fresh = configured()

    assert {name: getattr(fresh, name) for name in DEFAULTS} == DEFAULTS
    assert fresh.purge_unverified_accounts is False
    assert fresh.expired_story_retention == timedelta(hours=24)
    assert fresh.ended_session_retention == timedelta(days=30)
    assert fresh.spent_token_retention == timedelta(days=7)
    assert fresh.rate_limit_retention == timedelta(hours=1)
    assert fresh.unverified_account_retention == timedelta(days=7)


@pytest.mark.parametrize("name", DEFAULTS)
def test_each_period_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name.upper(), "3")

    assert getattr(configured(), name) == 3


@pytest.mark.parametrize(
    ("written", "meant"),
    [("true", True), ("1", True), ("false", False), ("0", False)],
)
def test_removing_accounts_is_switched_on_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, written: str, meant: bool
) -> None:
    monkeypatch.setenv("PURGE_UNVERIFIED_ACCOUNTS", written)

    assert configured().purge_unverified_accounts is meant


def test_a_switch_that_is_neither_on_nor_off_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PURGE_UNVERIFIED_ACCOUNTS", "perhaps")

    with pytest.raises(ValueError, match="purge_unverified_accounts"):
        configured()


def test_a_period_for_accounts_does_not_switch_their_removal_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNVERIFIED_ACCOUNT_RETENTION_DAYS", "3")

    assert configured().purge_unverified_accounts is False


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("expired_story_retention_hours", -1),
        ("ended_session_retention_days", -1),
        ("spent_token_retention_days", -1),
        ("rate_limit_retention_hours", -1),
        # An account must be given time to open its link.
        ("unverified_account_retention_days", 0),
        ("unverified_account_retention_days", -1),
        ("purge_batch_size", 0),
        ("purge_batch_size", 10001),
    ],
)
def test_a_period_that_makes_no_sense_is_refused(name: str, value: int) -> None:
    with pytest.raises(ValueError, match=name):
        configured(**{name: value})


@pytest.mark.parametrize(
    "name",
    [
        "expired_story_retention_hours",
        "ended_session_retention_days",
        "spent_token_retention_days",
        "rate_limit_retention_hours",
    ],
)
def test_no_time_at_all_is_a_period(name: str) -> None:
    assert getattr(configured(**{name: 0}), name) == 0


# --- the boundary of each rule --------------------------------------------


@each_rule
def test_removed_once_the_period_has_passed(session: Session, rule: str) -> None:
    make, _, _, category = RULES[rule]
    model, key = make(session, NOW - period(rule))

    assert purge(session) == removed(**{category: 1})
    assert not exists(session, model, key)


@each_rule
def test_kept_a_moment_before_the_period_has_passed(
    session: Session, rule: str
) -> None:
    make, _, _, _ = RULES[rule]
    model, key = make(session, NOW - period(rule) + A_MOMENT)

    assert purge(session) == NOTHING
    assert exists(session, model, key)
    # And gone with the next run, a moment later.
    assert sum(purge(session, NOW + A_MOMENT).values()) == 1
    assert not exists(session, model, key)


@each_rule
def test_removed_long_after_the_period(session: Session, rule: str) -> None:
    make, _, _, category = RULES[rule]
    model, key = make(session, NOW - period(rule) - timedelta(days=300))

    assert purge(session) == removed(**{category: 1})
    assert not exists(session, model, key)


@each_rule
def test_the_period_is_the_configured_one(
    session: Session, monkeypatch: pytest.MonkeyPatch, rule: str
) -> None:
    make, setting, unit, category = RULES[rule]
    monkeypatch.setattr(settings, setting, 3)
    changed = timedelta(**{unit: 3})
    assert period(rule) == changed
    outside = make(session, NOW - changed)
    inside = make(session, NOW - changed + A_MOMENT)

    assert purge(session) == removed(**{category: 1})
    assert not exists(session, *outside)
    assert exists(session, *inside)


@pytest.mark.parametrize(
    "rule", [rule for rule in RULES if rule != "unverified account"]
)
def test_with_no_period_a_row_is_removed_the_moment_it_ends(
    session: Session, monkeypatch: pytest.MonkeyPatch, rule: str
) -> None:
    make, setting, _, category = RULES[rule]
    monkeypatch.setattr(settings, setting, 0)
    ended = make(session, NOW)
    not_yet = make(session, NOW + A_MOMENT)

    assert purge(session) == removed(**{category: 1})
    assert not exists(session, *ended)
    assert exists(session, *not_yet)


def test_a_counter_is_kept_for_its_window_and_then_for_the_period(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rate_limit_window_minutes", 45)
    monkeypatch.setattr(settings, "rate_limit_retention_hours", 2)
    age = timedelta(minutes=45) + timedelta(hours=2)
    outside = add_counter(session, NOW - age)
    inside = add_counter(session, NOW - age + A_MOMENT)

    assert purge(session) == removed(rate_limit_counters=1)
    assert not exists(session, RateLimit, outside)
    assert exists(session, RateLimit, inside)


# --- what is in use is not touched ----------------------------------------


def test_sessions_that_can_be_used_are_kept(session: Session) -> None:
    alice = add_user(session, "alice")
    # Signed in a year ago, on a session that lasts: old is not ended.
    old = add_session(session, alice, expires_at=NOW + A_MOMENT)
    fresh = add_session(session, alice, expires_at=NOW + timedelta(days=30))
    just_revoked = add_session(
        session, alice, expires_at=NOW + timedelta(days=30), revoked_at=NOW
    )
    just_expired = add_session(session, alice, expires_at=NOW - timedelta(days=29))

    assert purge(session) == NOTHING
    assert count(session, UserSession) == 4
    for row in (old, fresh, just_revoked, just_expired):
        assert exists(session, UserSession, row.id)


def test_a_session_ends_when_it_is_revoked_or_expires_whichever_is_first(
    session: Session,
) -> None:
    alice = add_user(session, "alice")
    period = settings.ended_session_retention
    # Expired long ago, revoked only now (as a password reset does to every
    # session there is): it ended when it expired.
    expired_first = add_session(
        session, alice, expires_at=NOW - period, revoked_at=NOW
    )
    # Revoked long ago, and would only have expired just now.
    revoked_first = add_session(
        session, alice, expires_at=NOW - A_MOMENT, revoked_at=NOW - period
    )

    ended = [expired_first.id, revoked_first.id]

    assert purge(session) == removed(ended_sessions=2)
    assert not any(exists(session, UserSession, key) for key in ended)
    assert exists(session, User, alice.id)


@pytest.mark.parametrize("model", [EmailVerificationToken, PasswordResetToken])
def test_tokens_that_can_be_redeemed_are_kept(session: Session, model: type) -> None:
    alice = add_user(session, "alice")
    usable = add_token(session, model, alice, expires_at=NOW + A_MOMENT)
    just_used = add_token(
        session, model, alice, expires_at=NOW + timedelta(hours=1), used_at=NOW
    )
    just_expired = add_token(session, model, alice, expires_at=NOW - A_MOMENT)

    assert purge(session) == NOTHING
    for row in (usable, just_used, just_expired):
        assert exists(session, model, row.id)


def test_counters_that_still_count_are_kept(session: Session) -> None:
    window = settings.rate_limit_window
    running = add_counter(session, NOW - window + A_MOMENT, count=5)
    just_begun = add_counter(session, NOW, count=1)
    just_over = add_counter(session, NOW - window, count=5)

    assert purge(session) == NOTHING
    assert session.get(RateLimit, running).count == 5
    assert session.get(RateLimit, just_begun).count == 1
    assert session.get(RateLimit, just_over).count == 5


def test_stories_that_are_shown_are_kept(session: Session) -> None:
    _, shown = a_story(session, NOW + A_MOMENT)
    _, new = a_story(session, NOW + timedelta(hours=24))
    _, just_expired = a_story(session, NOW)

    assert purge(session) == NOTHING
    for story_id in (shown, new, just_expired):
        assert exists(session, Story, story_id)


def test_views_go_with_the_story_and_nothing_else_does(session: Session) -> None:
    alice, bob = add_user(session, "alice"), add_user(session, "bob")
    follow(session, bob, alice)
    _, old = a_story(session, NOW - timedelta(days=3))
    _, shown = a_story(session, NOW + timedelta(hours=5))
    session.add_all(
        [
            StoryView(story_id=old, viewer_id=alice.id),
            StoryView(story_id=old, viewer_id=bob.id),
            StoryView(story_id=shown, viewer_id=bob.id),
        ]
    )
    session.flush()
    author_of_old = session.get(Story, old).author_id

    # Views are not counted: they are part of the story.
    assert purge(session) == removed(expired_stories=1)
    assert not exists(session, Story, old)
    assert session.execute(select(StoryView.story_id, StoryView.viewer_id)).all() == [
        (shown, bob.id)
    ]
    assert exists(session, User, author_of_old)
    assert count(session, User) == 4
    assert count(session, Follow) == 1


# --- accounts that were never verified ------------------------------------


def old_enough() -> datetime:
    return NOW - settings.unverified_account_retention


def test_an_account_is_removed_with_everything_of_it(session: Session) -> None:
    ghost = add_unverified(session, "ghost", old_enough())
    add_token(session, EmailVerificationToken, ghost, expires_at=NOW)
    # A reset link does not make an account usable, so it does not keep it.
    add_token(session, PasswordResetToken, ghost, expires_at=NOW + timedelta(hours=1))
    ghost_id = ghost.id

    # Its tokens went with it. They are not counted as tokens.
    assert purge(session) == removed(unverified_accounts=1)
    assert (
        what_is_left_of(
            session,
            user_id=ghost_id,
            texts=["ghost", "ghost@example.com", "Ghost", PASSWORD_HASH],
        )
        == []
    )
    assert tables_with_rows(session.connection()) == {}


def test_its_username_and_address_can_be_registered_again(
    session: Session, client: TestClient, outbox
) -> None:
    old = add_unverified(session, "alice", old_enough()).id
    assert client.post("/auth/register", json=registration()).status_code == 409
    session.execute(RateLimit.__table__.delete())

    assert purge(session) == removed(unverified_accounts=1)

    assert client.post("/auth/register", json=registration()).status_code == 201
    new = session.scalar(select(User).where(User.username == "alice"))
    assert new.id != old
    assert new.email_verified_at is None
    assert [to for to, _ in outbox.verification] == ["alice@example.com"]


def test_an_account_that_was_verified_is_never_removed(session: Session) -> None:
    alice = add_user(session, "alice")
    dormant = add_user(session, "dormant", active=False)
    for user in (alice, dormant):
        user.created_at = user.email_verified_at = LONG_AGO
    session.flush()

    assert purge(session) == NOTHING
    assert count(session, User) == 2


def test_a_deactivated_account_is_left_to_whoever_deactivated_it(
    session: Session,
) -> None:
    banned = add_unverified(session, "banned", LONG_AGO, active=False)

    assert purge(session) == NOTHING
    assert exists(session, User, banned.id)


def test_a_link_that_can_still_be_opened_keeps_the_account(
    session: Session, client: TestClient
) -> None:
    waiting = add_unverified(session, "waiting", LONG_AGO)
    link = add_token(
        session, EmailVerificationToken, waiting, expires_at=NOW + A_MOMENT
    )

    assert purge(session) == NOTHING
    assert exists(session, User, waiting.id)
    assert exists(session, EmailVerificationToken, link.id)
    # Once the link has run out, nothing keeps it.
    assert purge(session, NOW + A_MOMENT) == removed(unverified_accounts=1)


def test_a_link_that_was_asked_for_again_keeps_the_account_until_it_expires(
    session: Session, client: TestClient, outbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_unverified(session, "alice", datetime.now(timezone.utc) - timedelta(days=90))
    asked = client.post(
        "/auth/resend-verification", json={"email": "alice@example.com"}
    )
    assert asked.status_code == 202 and len(outbox.verification) == 1
    session.execute(RateLimit.__table__.delete())
    now = datetime.now(timezone.utc)

    assert purge(session, now) == NOTHING
    lifetime = settings.email_verification_token_lifetime
    assert purge(session, now + lifetime - timedelta(minutes=1)) == NOTHING
    assert purge(session, now + lifetime + timedelta(minutes=1)) == removed(
        unverified_accounts=1
    )


def test_an_account_that_holds_anything_is_not_what_the_rule_is_about(
    session: Session,
) -> None:
    # None of these can come about through the API: an account that was
    # never verified cannot sign in. If one exists all the same, somebody
    # made it so, and it is not removed for being unverified.
    author = add_unverified(session, "author", LONG_AGO)
    add_post(session, author, "Written by somebody unverified")
    storyteller = add_unverified(session, "storyteller", LONG_AGO)
    session.add(
        Story(
            author_id=storyteller.id,
            media_url="https://media.example.com/stories/1.jpg",
            media_type="image",
            created_at=NOW,
            expires_at=NOW + timedelta(hours=24),
        )
    )
    signed_in = add_unverified(session, "signedin", LONG_AGO)
    add_session(session, signed_in, expires_at=NOW + timedelta(days=1))
    session.flush()

    assert purge(session) == NOTHING
    assert count(session, User) == 3
    assert count(session, Post) == 1
    assert count(session, Story) == 1


def test_an_unverified_account_whose_story_is_purged_goes_with_the_next_run(
    session: Session,
) -> None:
    # What holds it is removed by its own rule first; the account is looked
    # at before that, so it stays for this run and goes with the next.
    odd = add_unverified(session, "odd", LONG_AGO)
    session.add(
        Story(
            author_id=odd.id,
            media_url="https://media.example.com/stories/1.jpg",
            media_type="image",
            created_at=LONG_AGO,
            expires_at=LONG_AGO + timedelta(hours=24),
        )
    )
    session.flush()

    assert purge(session) == removed(expired_stories=1)
    assert exists(session, User, odd.id)
    assert purge(session) == removed(unverified_accounts=1)


def test_a_purged_account_cannot_redeem_what_was_sent_to_it(
    session: Session, client: TestClient, issued: list[str], outbox
) -> None:
    registered = client.post("/auth/register", json=registration())
    assert registered.status_code == 201
    asked = client.post("/auth/forgot-password", json={"email": "alice@example.com"})
    assert asked.status_code == 202
    verification, reset = issued
    session.execute(RateLimit.__table__.delete())
    later = datetime.now(timezone.utc) + timedelta(days=8)

    assert purge(session, later) == removed(unverified_accounts=1)

    assert (
        client.post("/auth/verify-email", json={"token": verification}).status_code
        == 400
    )
    assert (
        client.post(
            "/auth/reset-password",
            json={"token": reset, "new_password": "an entirely new passphrase"},
        ).status_code
        == 400
    )
    assert log_in(client).status_code == 401
    session.execute(RateLimit.__table__.delete())
    assert tables_with_rows(session.connection()) == {}


def test_an_account_that_verifies_in_time_is_an_account_like_any_other(
    session: Session, client: TestClient, issued: list[str], outbox
) -> None:
    assert client.post("/auth/register", json=registration()).status_code == 201
    assert (
        client.post("/auth/verify-email", json={"token": issued[0]}).status_code == 200
    )
    session.execute(RateLimit.__table__.delete())
    years_later = datetime.now(timezone.utc) + timedelta(days=3650)

    # The used link is of no use any more. The account is.
    assert purge(session, years_later) == removed(email_verification_tokens=1)
    assert log_in(client).status_code == 200


# --- removing accounts has to be asked for ----------------------------------


@pytest.fixture
def switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The settings as they are when nothing is said about accounts."""
    monkeypatch.setattr(
        settings,
        "purge_unverified_accounts",
        configured().purge_unverified_accounts,
    )


def test_by_default_no_account_is_removed_however_old(
    session: Session, switched_off: None
) -> None:
    ghost = add_unverified(session, "ghost", LONG_AGO)
    link = add_token(
        session, EmailVerificationToken, ghost, expires_at=NOW + timedelta(days=30)
    ).id

    assert purge(session) == NOTHING
    assert purge(session, NOW + timedelta(days=3650)) == removed(
        # The link is of no use by then. The account it was for stays.
        email_verification_tokens=1
    )
    assert exists(session, User, ghost.id)
    assert not exists(session, EmailVerificationToken, link)
    session.refresh(ghost)
    assert ghost.email_verified_at is None


def test_by_default_everything_else_is_removed_all_the_same(
    session: Session, switched_off: None
) -> None:
    kept = a_world(session)

    assert purge(session) == {**EVERYTHING_OF_NO_USE, "unverified_accounts": 0}

    # What was in use is as it was, and the account nobody verified is
    # there beside it, and nothing else.
    left = snapshot(session)
    ghosts = [row for row in left["users"] if row not in kept["users"]]
    assert [row[1] for row in ghosts] == ["ghost"]
    left["users"] = [row for row in left["users"] if row not in ghosts]
    assert left == kept
    assert_invariants(session)


def test_by_default_no_account_is_looked_for_or_locked(
    session: Session, switched_off: None
) -> None:
    add_unverified(session, "ghost", LONG_AGO)

    with statements("SELECT") as selects, statements("DELETE") as deletes:
        purge(session)

    assert not any("FROM users" in statement for statement in selects + deletes)


def test_switched_on_the_rule_is_the_configured_one(
    session: Session, switched_off: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = add_unverified(session, "outside", NOW - timedelta(days=3)).id
    inside = add_unverified(session, "inside", NOW - timedelta(days=3) + A_MOMENT).id
    monkeypatch.setattr(settings, "unverified_account_retention_days", 3)
    assert purge(session) == NOTHING

    monkeypatch.setattr(settings, "purge_unverified_accounts", True)

    assert purge(session) == removed(unverified_accounts=1)
    assert not exists(session, User, outside)
    assert exists(session, User, inside)


# --- everything else stays as it is ---------------------------------------


def a_world(session: Session) -> dict[str, object]:
    """Accounts in every state with things of every kind, all of it in use,
    and one row of every kind that is not."""
    alice, bob = add_user(session, "alice"), add_user(session, "bob")
    for user in (alice, bob):
        user.created_at = user.email_verified_at = LONG_AGO
    newcomer = add_unverified(session, "newcomer", NOW - timedelta(days=1))
    add_token(
        session, EmailVerificationToken, newcomer, expires_at=NOW + timedelta(hours=1)
    )
    follow(session, alice, bob)
    follow(session, bob, alice)

    # A thread as old as the hills, with a tombstone in it.
    root = add_post(session, alice, "An old post", created_at=LONG_AGO)
    erased = add_post(session, bob, "To be erased", parent=root, created_at=LONG_AGO)
    answer = add_post(session, alice, "An answer", parent=erased, created_at=LONG_AGO)
    erase(session, erased, at=LONG_AGO)
    session.add_all(
        [
            Like(user_id=bob.id, post_id=root.id, created_at=LONG_AGO),
            Repost(user_id=bob.id, post_id=answer.id, created_at=LONG_AGO),
        ]
    )

    story = Story(
        author_id=alice.id,
        media_url="https://media.example.com/stories/now.jpg",
        media_type="image",
        created_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=23),
    )
    session.add(story)
    session.flush()
    session.add(StoryView(story_id=story.id, viewer_id=bob.id))
    add_session(session, alice, expires_at=NOW + timedelta(days=3))
    add_token(session, PasswordResetToken, bob, expires_at=NOW + timedelta(minutes=5))
    add_counter(session, NOW - timedelta(minutes=1))
    session.flush()
    kept = snapshot(session)

    # And what is of no use, beside it.
    old_story = Story(
        author_id=alice.id,
        media_url="https://media.example.com/stories/old.jpg",
        media_type="image",
        created_at=LONG_AGO,
        expires_at=LONG_AGO + timedelta(hours=24),
    )
    session.add(old_story)
    session.flush()
    session.add(StoryView(story_id=old_story.id, viewer_id=bob.id))
    add_session(session, alice, expires_at=LONG_AGO + timedelta(days=30))
    add_session(session, bob, expires_at=NOW + timedelta(days=1), revoked_at=LONG_AGO)
    add_token(session, EmailVerificationToken, alice, expires_at=LONG_AGO + A_MOMENT)
    add_token(
        session,
        PasswordResetToken,
        bob,
        expires_at=LONG_AGO + timedelta(days=1),
        used_at=LONG_AGO + A_MOMENT,
    )
    add_counter(session, LONG_AGO)
    add_unverified(session, "ghost", LONG_AGO)
    session.flush()
    return kept


def snapshot(session: Session) -> dict[str, list[tuple]]:
    """Every row of every table, as stored."""
    session.expire_all()
    return {
        name: sorted(
            (tuple(row) for row in session.execute(select(table))), key=repr
        )
        for name, table in sorted(User.metadata.tables.items())
    }


EVERYTHING_OF_NO_USE = {
    "unverified_accounts": 1,
    "expired_stories": 1,
    "ended_sessions": 2,
    "email_verification_tokens": 1,
    "password_reset_tokens": 1,
    "rate_limit_counters": 1,
}


def test_only_what_is_of_no_use_is_removed(session: Session) -> None:
    kept = a_world(session)

    assert purge(session) == EVERYTHING_OF_NO_USE
    # Row for row and column for column what was in use before.
    assert snapshot(session) == kept


def test_posts_and_tombstones_are_never_removed_for_their_age(
    session: Session,
) -> None:
    a_world(session)
    posts = snapshot(session)["posts"]

    purge(session, NOW + timedelta(days=3650))

    assert snapshot(session)["posts"] == posts
    assert len(posts) == 3
    assert_invariants(session)


def assert_invariants(session: Session) -> None:
    """What holds of the database whatever was removed from it."""
    reply = Post.__table__.alias("reply")
    # A tombstone is answered by something.
    assert (
        session.scalar(
            select(func.count())
            .select_from(Post)
            .where(
                Post.deleted_at.is_not(None),
                ~select(reply.c.id).where(reply.c.parent_post_id == Post.id).exists(),
            )
        )
        == 0
    )
    # Every reference is to a row that is there.
    for table in User.metadata.tables.values():
        for key in table.foreign_keys:
            referred = key.column.table.alias("referred")
            dangling = session.scalar(
                select(func.count())
                .select_from(table)
                .where(
                    key.parent.is_not(None),
                    ~select(referred.c[key.column.name])
                    .where(referred.c[key.column.name] == key.parent)
                    .exists(),
                )
            )
            assert dangling == 0, f"{table.name}.{key.parent.name}"


def test_the_database_is_left_consistent(session: Session) -> None:
    a_world(session)

    purge(session)

    assert_invariants(session)


# --- data that was erased already -----------------------------------------


def test_what_an_erased_account_left_behind_is_removed_in_time(
    session: Session, alice_client: TestClient, bob_account: User
) -> None:
    post = alice_client.post("/posts", json={"content": "Soon without an author"})
    answered = session.get(Post, uuid.UUID(post.json()["id"]))
    add_post(session, bob_account, "A reply", parent=answered)
    deleted = alice_client.post("/auth/delete-account", json={"password": PASSWORD})
    assert deleted.status_code == 204
    # The counter of the address alone is not the account's and stays.
    assert count(session, RateLimit) == 1
    posts = snapshot(session)["posts"]
    now = datetime.now(timezone.utc)

    assert purge(session, now) == NOTHING
    after_the_window = now + settings.rate_limit_window + timedelta(hours=1, minutes=1)
    assert purge(session, after_the_window) == removed(rate_limit_counters=1)

    # The tombstone is where it was, for the reply that needs it.
    assert snapshot(session)["posts"] == posts
    assert [row for row in posts if row[1] is None] != []
    assert count(session, User) == 1
    assert_invariants(session)


def test_an_account_erased_before_the_purge_is_not_looked_for(
    session: Session,
) -> None:
    ghost = add_unverified(session, "ghost", LONG_AGO)
    erasure.erase_account(session, ghost.id, NOW)
    session.commit()

    assert purge(session) == NOTHING
    assert tables_with_rows(session.connection()) == {}


# --- running it -----------------------------------------------------------


def test_nothing_to_remove(session: Session) -> None:
    report = retention.purge(session, NOW)

    assert report.as_dict() == NOTHING
    assert report.total == 0
    assert tables_with_rows(session.connection()) == {}


def test_a_second_run_removes_nothing_more(session: Session) -> None:
    kept = a_world(session)

    first = retention.purge(session, NOW)
    assert first.total == 7
    assert first.as_dict() == EVERYTHING_OF_NO_USE

    for _ in range(3):
        assert purge(session) == NOTHING
        assert snapshot(session) == kept


@contextmanager
def statements(verb: str) -> Iterator[list[str]]:
    """Collects the statements of one kind sent to the database in the block."""
    found: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:
        if statement.lstrip().upper().startswith(verb):
            found.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield found
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest.mark.parametrize("batch_size", [1, 2, 3, 5, 6, 1000])
def test_rows_are_removed_in_batches_of_the_configured_size(
    session: Session, monkeypatch: pytest.MonkeyPatch, batch_size: int
) -> None:
    monkeypatch.setattr(settings, "purge_batch_size", batch_size)
    alice = add_user(session, "alice")
    for _ in range(5):
        add_session(session, alice, expires_at=LONG_AGO + timedelta(days=1))
        add_counter(session, LONG_AGO)
        add_unverified(session, f"g{uuid.uuid4().hex[:10]}", LONG_AGO)
    kept = add_session(session, alice, expires_at=NOW + timedelta(days=1))

    with statements("DELETE") as deletes:
        report = purge(session)

    assert report == removed(
        ended_sessions=5, rate_limit_counters=5, unverified_accounts=5
    )
    assert exists(session, UserSession, kept.id)
    assert count(session, User) == 1
    # However many there are, no statement removes more than a batch.
    batches = -(-5 // batch_size)
    assert sum("FROM sessions" in delete for delete in deletes) == batches
    assert sum("FROM rate_limits" in delete for delete in deletes) == batches
    # Accounts are removed one at a time, whatever the size of a batch.
    assert sum("FROM users" in delete for delete in deletes) == 5


def test_each_batch_is_a_transaction_of_its_own(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "purge_batch_size", 2)
    alice = add_user(session, "alice")
    for _ in range(5):
        add_session(session, alice, expires_at=LONG_AGO + timedelta(days=1))
    commits: list[int] = []
    event.listen(session, "after_commit", lambda _: commits.append(1))

    purge(session)

    # Three batches of sessions, and the look that finds no more of each kind.
    assert len(commits) >= 3 + 6


# --- failing part of the way ----------------------------------------------


class Failing:
    """Makes the database connection fail at a DELETE from one table."""

    def __init__(self) -> None:
        self._listener: Callable | None = None

    def at(self, table: str, *, after: int = 0) -> None:
        """Fail from now on, once ``after`` such statements have gone through."""
        seen = []

        def fail(conn, cursor, statement, parameters, context, executemany) -> None:
            if statement.startswith(f"DELETE FROM {table} "):
                seen.append(statement)
                if len(seen) > after:
                    raise ConnectionError("the connection was lost")

        event.listen(engine, "before_cursor_execute", fail)
        self._listener = fail

    def stop(self) -> None:
        if self._listener is not None:
            event.remove(engine, "before_cursor_execute", self._listener)
            self._listener = None


@pytest.fixture
def failing() -> Iterator[Failing]:
    failing = Failing()
    yield failing
    failing.stop()


def test_a_failure_keeps_what_was_done_and_says_how_far_it_got(
    session: Session, failing: Failing
) -> None:
    kept = a_world(session)
    failing.at("email_verification_tokens")

    with pytest.raises(retention.PurgeError) as failure:
        retention.purge(session, NOW)

    error = failure.value
    assert error.category == "email_verification_tokens"
    assert isinstance(error.__cause__, ConnectionError)
    # What came before it was removed, and is reported.
    assert error.report.as_dict() == removed(
        unverified_accounts=1, expired_stories=1, ended_sessions=2
    )
    assert count(session, UserSession) == 1
    # What it failed at, and what comes after it, is all there.
    assert count(session, EmailVerificationToken) == 2
    assert count(session, PasswordResetToken) == 2
    assert count(session, RateLimit) == 2

    # The next run does the rest, and only the rest.
    failing.stop()
    assert purge(session) == removed(
        email_verification_tokens=1, password_reset_tokens=1, rate_limit_counters=1
    )
    assert snapshot(session) == kept


def test_a_batch_that_fails_is_rolled_back_and_those_before_it_are_not(
    session: Session, monkeypatch: pytest.MonkeyPatch, failing: Failing
) -> None:
    monkeypatch.setattr(settings, "purge_batch_size", 2)
    alice = add_user(session, "alice")
    for _ in range(5):
        add_session(session, alice, expires_at=LONG_AGO + timedelta(days=1))
    failing.at("sessions", after=1)

    with pytest.raises(retention.PurgeError) as failure:
        retention.purge(session, NOW)

    assert failure.value.category == "ended_sessions"
    assert failure.value.report.as_dict() == removed(ended_sessions=2)
    assert count(session, UserSession) == 3

    failing.stop()
    assert purge(session) == removed(ended_sessions=3)


def test_an_account_whose_removal_fails_is_all_there(
    session: Session, failing: Failing
) -> None:
    for name in ("first", "second"):
        ghost = add_unverified(session, name, LONG_AGO)
        add_token(
            session, EmailVerificationToken, ghost, expires_at=LONG_AGO + A_MOMENT
        )
    before = snapshot(session)
    failing.at("users", after=1)

    with pytest.raises(retention.PurgeError) as failure:
        retention.purge(session, NOW)

    assert failure.value.category == "unverified_accounts"
    assert failure.value.report.as_dict() == removed(unverified_accounts=1)
    # One of the two is gone with everything of it. The other is as it was,
    # its token included: no account is left half removed.
    left = snapshot(session)
    assert len(left["users"]) == len(left["email_verification_tokens"]) == 1
    assert left["users"][0] in before["users"]
    assert left["email_verification_tokens"][0] in before["email_verification_tokens"]
    assert left["email_verification_tokens"][0][1] == left["users"][0][0]
    assert_invariants(session)

    failing.stop()
    assert purge(session) == removed(unverified_accounts=1)
    assert tables_with_rows(session.connection()) == {}


# --- counting without removing ----------------------------------------------


def test_what_would_be_removed_is_what_is_then_removed(session: Session) -> None:
    a_world(session)
    before = snapshot(session)

    with statements("") as all_statements:
        would = retention.pending(session, NOW)

    assert would.as_dict() == EVERYTHING_OF_NO_USE
    # Counted and not touched: nothing was written and nothing was locked.
    assert snapshot(session) == before
    assert all_statements != []
    for statement in all_statements:
        assert statement.startswith(("SELECT count(", "SAVEPOINT", "RELEASE"))
        assert "FOR UPDATE" not in statement
    assert purge(session) == would.as_dict()
    assert retention.pending(session, NOW).total == 0


def test_accounts_are_not_counted_unless_they_would_be_removed(
    session: Session, switched_off: None
) -> None:
    a_world(session)

    would = retention.pending(session, NOW)

    assert would.as_dict() == {**EVERYTHING_OF_NO_USE, "unverified_accounts": 0}


def test_nothing_is_counted_in_an_empty_database(session: Session) -> None:
    assert retention.pending(session, NOW).as_dict() == NOTHING


# --- the command ----------------------------------------------------------------

DATABASE = engine.url.database
SERVER = f"{engine.url.host}:{engine.url.port}"
EXECUTE = ["--execute", "--database", DATABASE, "--host", SERVER]


@pytest.fixture
def command(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> Callable[[list[str]], int]:
    """``python -m app.purge``, run on the test's transaction."""

    @contextmanager
    def the_test_session() -> Iterator[Session]:
        yield session

    monkeypatch.setattr(purge_command, "SessionLocal", the_test_session)
    return purge_command.main


def counts_in(output: str, heading: str) -> dict[str, int]:
    """The counts the command printed under a heading."""
    lines = output[output.index(heading) :].splitlines()[1:8]
    return {
        " ".join(words[:-1]): int(words[-1]) for words in map(str.split, lines)
    }


@pytest.fixture
def of_no_use(session: Session) -> None:
    alice = add_user(session, "alice")
    for _ in range(3):
        add_session(session, alice, expires_at=LONG_AGO + timedelta(days=1))
    add_counter(session, LONG_AGO)
    add_unverified(session, "ghost", LONG_AGO)


def test_command_removes_nothing_unless_told_to(
    session: Session, command, of_no_use: None, capsys: pytest.CaptureFixture[str]
) -> None:
    before = snapshot(session)

    assert command([]) == 0

    out = capsys.readouterr()
    assert snapshot(session) == before
    assert "(dry run: nothing is removed)" in out.out.splitlines()[0]
    assert counts_in(out.out, "Would remove:") == {
        "unverified accounts": 1,
        "expired stories": 0,
        "ended sessions": 3,
        "email verification tokens": 0,
        "password reset tokens": 0,
        "rate limit counters": 1,
        "total": 5,
    }
    assert "Removed:" not in out.out
    assert f"run with --execute --database {DATABASE} --host {SERVER}" in out.out
    assert out.err == ""


def test_command_told_to_removes_and_reports(
    session: Session, command, of_no_use: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert command(EXECUTE) == 0

    out = capsys.readouterr()
    assert "dry run" not in out.out
    assert counts_in(out.out, "Removed:")["total"] == 5
    assert counts_in(out.out, "Removed:")["ended sessions"] == 3
    assert out.err == ""
    assert count(session, UserSession) == count(session, RateLimit) == 0
    assert count(session, User) == 1


# The database of the same name on another server, on another port of this
# one, and another database on this server.
ELSEWHERE = {
    "another host": ("--database", DATABASE, "--host", "db.production.example:5432"),
    "another port": (
        "--database",
        DATABASE,
        "--host",
        f"{engine.url.host}:{engine.url.port + 1}",
    ),
    "another name": ("--database", "hopsnop_production", "--host", SERVER),
}


@pytest.mark.parametrize("elsewhere", ELSEWHERE)
def test_command_refuses_any_database_but_the_one_it_is_told(
    session: Session,
    command,
    of_no_use: None,
    capsys: pytest.CaptureFixture[str],
    elsewhere: str,
) -> None:
    before = snapshot(session)
    *_, name, _, server = ("--execute", *ELSEWHERE[elsewhere])

    assert command(["--execute", *ELSEWHERE[elsewhere]]) == 2

    out = capsys.readouterr()
    assert snapshot(session) == before
    assert (
        f"Refused: this is the database {DATABASE!r} on {SERVER}, and "
        f"{name!r} on {server} was asked for. Nothing was changed." in out.err
    )
    assert "Removed:" not in out.out and "Would remove:" not in out.out


def test_command_takes_the_host_in_any_case_of_letters(
    session: Session, command, of_no_use: None
) -> None:
    told = ["--execute", "--database", DATABASE, "--host", SERVER.upper()]

    assert command(told) == 0
    assert count(session, UserSession) == 0


@pytest.mark.parametrize("elsewhere", ELSEWHERE)
def test_command_refuses_the_wrong_database_for_a_dry_run_too(
    command, capsys: pytest.CaptureFixture[str], elsewhere: str
) -> None:
    assert command(list(ELSEWHERE[elsewhere])) == 2
    assert "Refused" in capsys.readouterr().err


@pytest.mark.parametrize(
    "told",
    [
        [],
        ["--database", DATABASE],
        ["--host", SERVER],
    ],
    ids=["neither", "no host", "no database"],
)
def test_command_does_not_remove_without_being_told_database_and_host(
    session: Session,
    command,
    of_no_use: None,
    capsys: pytest.CaptureFixture[str],
    told: list[str],
) -> None:
    before = snapshot(session)

    with pytest.raises(SystemExit) as refused:
        command(["--execute", *told])

    assert refused.value.code == 2
    assert (
        "--execute needs --database NAME and --host HOST:PORT"
        in capsys.readouterr().err
    )
    assert snapshot(session) == before


def test_command_tells_no_more_of_the_connection_than_where_it_goes(
    command, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = engine.url.set(username="a-user-name", password="a-secret-password")
    monkeypatch.setattr(purge_command, "engine", type("Engine", (), {"url": secret}))

    assert command([]) == 0
    assert command(["--execute", "--database", DATABASE, "--host", "other:1"]) == 2

    out = capsys.readouterr()
    assert f"{DATABASE} on {SERVER}" in out.out
    for hidden in ("a-user-name", "a-secret-password", "://", "@"):
        assert hidden not in out.out + out.err


def test_command_names_the_default_port_and_the_local_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def server(url: str) -> str:
        monkeypatch.setattr(
            purge_command, "engine", type("Engine", (), {"url": make_url(url)})
        )
        return purge_command._server()

    assert server("postgresql+psycopg://u:p@DB.Internal/hopsnop") == "db.internal:5432"
    assert server("postgresql+psycopg://u:p@db.internal:6432/hopsnop") == (
        "db.internal:6432"
    )
    assert server("postgresql+psycopg:///hopsnop") == "local:5432"


def test_command_says_what_it_runs_with(
    command, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(settings, "rate_limit_window_minutes", 45)
    monkeypatch.setattr(settings, "ended_session_retention_days", 11)
    monkeypatch.setattr(settings, "unverified_account_retention_days", 9)

    assert command([]) == 0
    on = capsys.readouterr().out
    monkeypatch.setattr(settings, "purge_unverified_accounts", False)
    assert command([]) == 0
    off = capsys.readouterr().out

    for line in (
        f"database                      {DATABASE} on {SERVER}",
        "environment                   production",
        "rate-limit window             45 minutes",
        "expired stories kept          24 hours",
        "ended sessions kept           11 days",
        "spent tokens kept             7 days",
        "finished counters kept        1 hours",
        "batch size                    1000",
    ):
        assert line in on and line in off
    assert "unverified accounts           removed after 9 days" in on
    assert (
        "unverified accounts           not removed "
        "(PURGE_UNVERIFIED_ACCOUNTS is off)" in off
    )
    # Where it is connected to, and never with what: no URL is printed.
    assert "://" not in on + off and "@" not in on + off
    assert settings.rate_limit_secret.get_secret_value() not in on + off


def test_command_succeeds_with_nothing_to_remove(
    command, capsys: pytest.CaptureFixture[str]
) -> None:
    assert command(EXECUTE) == 0
    assert command(EXECUTE) == 0
    assert command([]) == 0

    out = capsys.readouterr()
    assert out.out.count("  total                                0") == 3
    assert out.err == ""


def test_command_that_fails_ends_with_1_and_says_what_it_did(
    session: Session, command, failing: Failing, capsys: pytest.CaptureFixture[str]
) -> None:
    alice = add_user(session, "alice")
    add_session(session, alice, expires_at=LONG_AGO + timedelta(days=1))
    add_counter(session, LONG_AGO)
    failing.at("rate_limits")

    assert command(EXECUTE) == 1

    out = capsys.readouterr()
    removed_before = counts_in(out.out, "Removed:")
    assert removed_before["ended sessions"] == 1
    assert removed_before["rate limit counters"] == 0
    assert "Failed while removing rate limit counters (ConnectionError)" in out.err
    # Nothing of the error but its kind: its text can hold what must not be
    # printed.
    assert "the connection was lost" not in out.out + out.err
    assert count(session, UserSession) == 0
    assert count(session, RateLimit) == 1


def test_command_that_fails_at_once_says_so_and_no_more(
    command, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def lost(*_: object) -> None:
        raise ConnectionError("password=a-secret-password")

    monkeypatch.setattr(retention, "pending", lost)

    assert command([]) == 1

    out = capsys.readouterr()
    assert "Failed before anything was removed (ConnectionError)." in out.err
    assert "a-secret-password" not in out.out + out.err

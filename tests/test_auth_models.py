import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import EmailVerificationToken, PasswordResetToken, User, UserSession

TOKEN_MODELS = [EmailVerificationToken, PasswordResetToken]


def now() -> datetime:
    return datetime.now(timezone.utc)


def make_session(
    user: User, token_hash: str = "a" * 64, **overrides: object
) -> UserSession:
    values = {
        "user_id": user.id,
        "token_hash": token_hash,
        "expires_at": now() + timedelta(days=30),
        "last_used_at": now(),
        **overrides,
    }
    return UserSession(**values)


def make_token(model: type, user_id: uuid.UUID, **overrides: object):
    values = {
        "user_id": user_id,
        "token_hash": "a" * 64,
        "expires_at": now() + timedelta(hours=1),
        **overrides,
    }
    return model(**values)


# --- users ---------------------------------------------------------------


def test_user_is_unverified_by_default(session: Session, alice: User) -> None:
    session.refresh(alice)

    assert alice.email_verified_at is None


def test_email_verified_at_is_timezone_aware(session: Session, alice: User) -> None:
    alice.email_verified_at = now()
    session.flush()
    session.refresh(alice)

    assert alice.email_verified_at.tzinfo is not None


# --- sessions ------------------------------------------------------------


def test_session_can_be_created(session: Session, alice: User) -> None:
    user_session = make_session(alice)
    session.add(user_session)
    session.flush()
    session.refresh(user_session)

    assert isinstance(user_session.id, uuid.UUID)
    assert user_session.user is alice
    assert alice.sessions == [user_session]
    assert user_session.revoked_at is None
    assert user_session.created_at.tzinfo is not None
    assert user_session.expires_at.tzinfo is not None
    assert user_session.last_used_at.tzinfo is not None


def test_user_can_have_several_sessions(session: Session, alice: User) -> None:
    session.add_all([make_session(alice, "a" * 64), make_session(alice, "b" * 64)])
    session.flush()

    assert session.scalar(select(func.count()).select_from(UserSession)) == 2


def test_session_token_hash_must_be_unique(
    session: Session, alice: User, bob: User
) -> None:
    session.add(make_session(alice, "a" * 64))
    session.flush()

    session.add(make_session(bob, "a" * 64))
    with pytest.raises(IntegrityError, match="uq_sessions_token_hash"):
        session.flush()


def test_session_requires_an_existing_user(session: Session) -> None:
    session.add(
        UserSession(
            user_id=uuid.uuid4(),
            token_hash="a" * 64,
            expires_at=now() + timedelta(days=30),
            last_used_at=now(),
        )
    )
    with pytest.raises(IntegrityError, match="fk_sessions_user_id_users"):
        session.flush()


@pytest.mark.parametrize("column", ["token_hash", "last_used_at"])
def test_session_columns_are_required(
    session: Session, alice: User, column: str
) -> None:
    session.add(make_session(alice, **{column: None}))
    with pytest.raises(IntegrityError, match=column):
        session.flush()


def test_session_has_no_database_default_for_expiry(
    session: Session, alice: User
) -> None:
    # The lifetime is decided by the application, never by the database.
    session.add(UserSession(user_id=alice.id, token_hash="a" * 64, last_used_at=now()))
    with pytest.raises(IntegrityError, match="expires_at"):
        session.flush()


def test_session_cannot_expire_before_it_is_created(
    session: Session, alice: User
) -> None:
    session.add(make_session(alice, expires_at=now() - timedelta(hours=1)))
    with pytest.raises(IntegrityError, match="ck_sessions_expires_after_created"):
        session.flush()


# --- email verification and password reset tokens ------------------------


@pytest.mark.parametrize("model", TOKEN_MODELS)
def test_token_can_be_created(session: Session, alice: User, model: type) -> None:
    token = make_token(model, alice.id)
    session.add(token)
    session.flush()
    session.refresh(token)

    assert isinstance(token.id, uuid.UUID)
    assert token.user is alice
    assert token.used_at is None
    assert token.created_at.tzinfo is not None
    assert token.expires_at.tzinfo is not None


@pytest.mark.parametrize("model", TOKEN_MODELS)
def test_token_hash_must_be_unique(
    session: Session, alice: User, bob: User, model: type
) -> None:
    session.add(make_token(model, alice.id))
    session.flush()

    session.add(make_token(model, bob.id))
    with pytest.raises(IntegrityError, match=f"uq_{model.__tablename__}_token_hash"):
        session.flush()


@pytest.mark.parametrize("model", TOKEN_MODELS)
def test_token_requires_an_existing_user(session: Session, model: type) -> None:
    session.add(make_token(model, uuid.uuid4()))
    with pytest.raises(IntegrityError, match=f"fk_{model.__tablename__}_user_id_users"):
        session.flush()


@pytest.mark.parametrize("model", TOKEN_MODELS)
def test_token_requires_an_expiration_timestamp(
    session: Session, alice: User, model: type
) -> None:
    session.add(model(user_id=alice.id, token_hash="a" * 64))
    with pytest.raises(IntegrityError, match="expires_at"):
        session.flush()


@pytest.mark.parametrize("model", TOKEN_MODELS)
def test_token_cannot_expire_before_it_is_created(
    session: Session, alice: User, model: type
) -> None:
    session.add(make_token(model, alice.id, expires_at=now() - timedelta(hours=1)))
    with pytest.raises(
        IntegrityError, match=f"ck_{model.__tablename__}_expires_after_created"
    ):
        session.flush()


# --- deletion behaviour --------------------------------------------------


def test_deleting_a_user_removes_their_sessions_and_tokens(
    session: Session, alice: User, bob: User
) -> None:
    for user, token_hash in ((alice, "a" * 64), (bob, "b" * 64)):
        session.add_all(
            [
                make_session(user, token_hash),
                make_token(EmailVerificationToken, user.id, token_hash=token_hash),
                make_token(PasswordResetToken, user.id, token_hash=token_hash),
            ]
        )
    session.flush()
    session.expire_all()

    session.delete(bob)
    session.flush()

    for model in (UserSession, EmailVerificationToken, PasswordResetToken):
        assert session.scalars(select(model.user_id)).all() == [alice.id]


def test_deactivating_a_user_keeps_their_sessions_and_tokens(
    session: Session, alice: User
) -> None:
    session.add(make_session(alice))
    session.flush()

    alice.is_active = False
    session.flush()

    assert session.scalar(select(func.count()).select_from(UserSession)) == 1

"""Authentication use cases: accounts, sessions and single-use tokens.

Every public function that writes is one unit of work. It commits once, at
the end, so its changes are applied together or not at all; a failure part
way through leaves nothing behind once the request's database session is
closed.

Raw tokens are only ever returned to the caller, which hands them to the
browser (session cookie) or to the email sender (links). They are not stored
and not logged.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, contains_eager

from app.core.config import settings
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    generate_token,
    hash_password,
    hash_token,
    password_needs_rehash,
    verify_password,
)
from app.models import EmailVerificationToken, PasswordResetToken, User, UserSession


class AuthError(Exception):
    """A failure that is reported to the client as ``detail`` with ``status_code``."""

    status_code = 400
    detail = "The request could not be completed."


class UsernameTakenError(AuthError):
    status_code = 409
    detail = "Username already in use."


class EmailTakenError(AuthError):
    status_code = 409
    detail = "Email already in use."


class InvalidCredentialsError(AuthError):
    status_code = 401
    detail = "Invalid username/email or password."


class EmailNotVerifiedError(AuthError):
    status_code = 403
    detail = "Email address is not verified."


class InvalidVerificationTokenError(AuthError):
    detail = "Invalid or expired verification token."


class InvalidPasswordResetTokenError(AuthError):
    detail = "Invalid or expired password reset token."


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- registration --------------------------------------------------------


def register_user(
    db: Session,
    *,
    username: str,
    email: str,
    password: str,
    display_name: str,
) -> tuple[User, str]:
    """Create an unverified account together with its first verification token.

    Returns the user and the raw token for the verification link. No session
    is created: the account has to be verified and then logged in to.
    """
    conflict = _registration_conflict(db, username, email)
    if conflict is not None:
        raise conflict

    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        display_name=display_name,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        # A concurrent registration took the username or email between the
        # check above and this insert. The unique constraints are what
        # actually guarantee uniqueness; the check only gives a clean answer
        # in the common case.
        db.rollback()
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint == "uq_users_username":
            raise UsernameTakenError from None
        if constraint == "uq_users_email":
            raise EmailTakenError from None
        raise

    raw_token = _issue_token(
        db,
        EmailVerificationToken,
        user,
        settings.email_verification_token_lifetime,
    )
    db.commit()
    return user, raw_token


def _registration_conflict(db: Session, username: str, email: str) -> AuthError | None:
    if db.scalar(select(User.id).where(User.username == username)) is not None:
        return UsernameTakenError()
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        return EmailTakenError()
    return None


# --- login and sessions --------------------------------------------------


def log_in(
    db: Session,
    *,
    identifier: str,
    password: str,
    replaced_token: str | None = None,
) -> tuple[UserSession, str]:
    """Check credentials and start a session.

    Returns the session and its raw token, which goes into the cookie.
    ``replaced_token`` is the session token the browser presented with the
    login request, if any; that session is ended, since its cookie is about
    to be overwritten.
    """
    column = User.email if "@" in identifier else User.username
    user = db.scalar(select(User).where(column == identifier))

    # An unknown account, a wrong password and a deactivated account are
    # indistinguishable to the client. The password is verified even when
    # there is no such account, against a dummy hash, so that the response
    # time does not reveal whether the account exists either.
    password_ok = verify_password(
        password,
        user.password_hash if user is not None else DUMMY_PASSWORD_HASH,
    )
    if user is None or not password_ok or not user.is_active:
        raise InvalidCredentialsError

    # Reported only once the password has been proven, so it tells nothing to
    # someone who does not already hold the credentials.
    if user.email_verified_at is None:
        raise EmailNotVerifiedError

    now = _now()
    if password_needs_rehash(user.password_hash):
        # The Argon2 parameters have been raised since this hash was made.
        # This is the one moment the plaintext is available to upgrade it.
        user.password_hash = hash_password(password)
    if replaced_token:
        _revoke_session_by_token(db, replaced_token, now)

    # The token is always freshly generated here; a value supplied by the
    # client is never adopted as a session token.
    raw_token = generate_token()
    session = UserSession(
        user=user,
        token_hash=hash_token(raw_token),
        created_at=now,
        expires_at=now + settings.session_lifetime,
        last_used_at=now,
    )
    db.add(session)
    db.commit()
    return session, raw_token


def authenticate_session(db: Session, raw_token: str) -> UserSession | None:
    """Return the session a presented token belongs to, if it may be used.

    This is the single place that decides whether a session is valid: it must
    exist, not be revoked, not be expired, and belong to an active user.
    """
    now = _now()
    session = db.scalar(
        select(UserSession)
        .join(UserSession.user)
        .options(contains_eager(UserSession.user))
        .where(
            UserSession.token_hash == hash_token(raw_token),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
            User.is_active.is_(True),
        )
    )
    if session is None:
        return None

    # last_used_at is refreshed at most once per configured interval, so it
    # can lag behind the real last use by up to that interval. In exchange,
    # most authenticated requests do not write to the database.
    if now - session.last_used_at >= settings.session_last_used_interval:
        session.last_used_at = now
        db.commit()
    return session


def log_out(db: Session, raw_token: str) -> None:
    """End the session a token belongs to. Unknown tokens are ignored."""
    _revoke_session_by_token(db, raw_token, _now())
    db.commit()


def list_sessions(db: Session, user: User) -> list[UserSession]:
    """The user's usable sessions, most recently used first."""
    return list(
        db.scalars(
            select(UserSession)
            .where(*_active_sessions_of(user.id, _now()))
            .order_by(UserSession.last_used_at.desc(), UserSession.created_at.desc())
        )
    )


def revoke_session(db: Session, user: User, session_id: uuid.UUID) -> bool:
    """End one of the user's own sessions. False if they have no such session."""
    now = _now()
    result = db.execute(
        update(UserSession)
        .where(UserSession.id == session_id, *_active_sessions_of(user.id, now))
        .values(revoked_at=now)
    )
    db.commit()
    return result.rowcount == 1


def revoke_other_sessions(db: Session, current: UserSession) -> int:
    """End every session of the current user except the one in use."""
    now = _now()
    result = db.execute(
        update(UserSession)
        .where(
            UserSession.id != current.id,
            *_active_sessions_of(current.user_id, now),
        )
        .values(revoked_at=now)
    )
    db.commit()
    return result.rowcount


def _active_sessions_of(user_id: uuid.UUID, now: datetime) -> tuple:
    # Scoping by user_id is the authorization check for session management:
    # no statement built on this can reach another user's sessions.
    return (
        UserSession.user_id == user_id,
        UserSession.revoked_at.is_(None),
        UserSession.expires_at > now,
    )


def _revoke_session_by_token(db: Session, raw_token: str, now: datetime) -> None:
    db.execute(
        update(UserSession)
        .where(
            UserSession.token_hash == hash_token(raw_token),
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )


# --- email verification --------------------------------------------------


def verify_email(db: Session, raw_token: str) -> None:
    """Redeem a verification token and mark the account's email as verified."""
    now = _now()
    token = _usable_token(db, EmailVerificationToken, raw_token, now)
    if token is None:
        raise InvalidVerificationTokenError
    token.used_at = now
    token.user.email_verified_at = now
    db.commit()


def request_email_verification(db: Session, email: str) -> str | None:
    """Issue a new verification token for an account that still needs one.

    Returns the raw token, or None if there is nothing to send. The caller
    must respond identically in both cases.
    """
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active or user.email_verified_at is not None:
        return None
    raw_token = _issue_token(
        db,
        EmailVerificationToken,
        user,
        settings.email_verification_token_lifetime,
    )
    db.commit()
    return raw_token


# --- password reset ------------------------------------------------------


def request_password_reset(db: Session, email: str) -> str | None:
    """Issue a password reset token for an active account.

    Returns the raw token, or None if there is nothing to send. The caller
    must respond identically in both cases.
    """
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        return None
    raw_token = _issue_token(
        db,
        PasswordResetToken,
        user,
        settings.password_reset_token_lifetime,
    )
    db.commit()
    return raw_token


def reset_password(db: Session, raw_token: str, new_password: str) -> None:
    """Redeem a reset token: set the new password and end every session."""
    now = _now()
    token = _usable_token(db, PasswordResetToken, raw_token, now)
    if token is None:
        raise InvalidPasswordResetTokenError
    token.used_at = now
    token.user.password_hash = hash_password(new_password)
    # Whoever was logged in with the old password, on any device, is logged
    # out. After a reset the only way in is the new password.
    db.execute(
        update(UserSession)
        .where(
            UserSession.user_id == token.user_id,
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    db.commit()


# --- single-use tokens ---------------------------------------------------

_TokenModel = type[EmailVerificationToken] | type[PasswordResetToken]


def _issue_token(
    db: Session,
    model: _TokenModel,
    user: User,
    lifetime: timedelta,
) -> str:
    """Store a new token for the user and return its raw value.

    Any token of the same kind that the user has not used yet is deleted
    first, so a user has at most one usable token of each kind and only the
    most recent link works.
    """
    now = _now()
    db.execute(delete(model).where(model.user_id == user.id, model.used_at.is_(None)))
    raw_token = generate_token()
    db.add(
        model(
            user_id=user.id,
            token_hash=hash_token(raw_token),
            created_at=now,
            expires_at=now + lifetime,
        )
    )
    db.flush()
    return raw_token


def _usable_token(
    db: Session,
    model: _TokenModel,
    raw_token: str,
    now: datetime,
) -> EmailVerificationToken | PasswordResetToken | None:
    """Find a token that may be redeemed: unused, unexpired, of an active user.

    The row is locked until the transaction ends. If two requests present the
    same token at once, the second waits for the first, then finds the token
    already used and gets nothing, so a token can never be redeemed twice.
    """
    return db.scalar(
        select(model)
        .join(model.user)
        .options(contains_eager(model.user))
        .where(
            model.token_hash == hash_token(raw_token),
            model.used_at.is_(None),
            model.expires_at > now,
            User.is_active.is_(True),
        )
        .with_for_update(of=model)
    )

"""Authentication endpoints.

The handlers are thin: they translate between HTTP (bodies, the session
cookie, status codes) and ``app.services.auth``, which holds the rules.
"""

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.deps import (
    ClientAddress,
    CurrentSession,
    CurrentUser,
    DbSession,
    VerifiedUser,
    limit_per_address,
)
from app.core.config import settings
from app.schemas.auth import (
    AccountResponse,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    RevokedSessionsResponse,
    SessionResponse,
    VerifyEmailRequest,
)
from app.services import auth as auth_service
from app.services import rate_limit
from app.services.email import (
    EmailSender,
    email_verification_url,
    get_email_sender,
    password_reset_url,
)


def _no_store(response: Response) -> None:
    # Responses here carry account data or the session cookie; neither a
    # browser nor an intermediary should keep a copy.
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(_no_store)])

EmailSenderDep = Annotated[EmailSender, Depends(get_email_sender)]


def _set_session_cookie(
    response: Response, raw_token: str, *, max_age: int | None = None
) -> None:
    if max_age is None:
        max_age = int(settings.session_lifetime.total_seconds())
    response.set_cookie(
        key=settings.session_cookie,
        value=raw_token,
        # The browser drops the cookie when the session expires server-side.
        max_age=max_age,
        path="/",
        # No Domain attribute: the cookie is only sent back to this host.
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.session_cookie,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
    )


# --- registration and login ----------------------------------------------


# --- rate limits ----------------------------------------------------------
#
# How often each of these may be asked for is set in ``app.core.config``.
# Every refusal is the same 429, whichever limit was reached.


def _login_limits(identifier: str, address: str) -> list[rate_limit.Limit]:
    """The three counters of failed logins, narrowest first.

    They are keyed by what was typed, not by the account it may belong to,
    so they behave the same for an identifier that names no account. A
    request that one of them refuses counts against none of them, so an
    address that has used up its own guesses at an account adds nothing
    more to the account's limit as a whole.
    """
    return [
        rate_limit.Limit(
            "login:identifier+address",
            f"{identifier}\x00{address}",
            settings.login_failures_per_identifier_and_ip,
        ),
        rate_limit.Limit("login:address", address, settings.login_failures_per_ip),
        rate_limit.Limit(
            "login:identifier", identifier, settings.login_failures_per_identifier
        ),
    ]


@router.post(
    "/register",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[limit_per_address("register", "registrations_per_ip")],
)
def register(
    data: RegisterRequest,
    db: DbSession,
    email_sender: EmailSenderDep,
) -> AccountResponse:
    user, raw_token = auth_service.register_user(
        db,
        username=data.username,
        email=data.email,
        password=data.password.get_secret_value(),
        display_name=data.display_name,
    )
    email_sender.send_email_verification(
        to=user.email,
        url=email_verification_url(raw_token),
    )
    return AccountResponse.model_validate(user)


@router.post("/login", response_model=AccountResponse)
def login(
    data: LoginRequest,
    request: Request,
    response: Response,
    address: ClientAddress,
    db: DbSession,
) -> AccountResponse:
    # The attempt is counted first, before the password is looked at. Too
    # many failures are refused here whatever this password is, without the
    # cost of checking it; and of several attempts made at once, each is
    # counted before any of them is checked.
    counted = rate_limit.count_all(db, _login_limits(data.identifier, address))
    try:
        session, raw_token = auth_service.log_in(
            db,
            identifier=data.identifier,
            password=data.password.get_secret_value(),
            replaced_token=request.cookies.get(settings.session_cookie),
        )
    except auth_service.EmailNotVerifiedError:
        # The password was right, so this was not a guess.
        rate_limit.uncount_all(db, counted)
        raise
    # Only failures are limited: a login that succeeds is not counted.
    rate_limit.uncount_all(db, counted)
    # The token leaves the server only in this cookie, never in the body.
    _set_session_cookie(response, raw_token)
    return AccountResponse.model_validate(session.user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: DbSession) -> None:
    """Ends the session server-side and clears the cookie.

    Succeeds whether or not the request carried a usable session.
    """
    raw_token = request.cookies.get(settings.session_cookie)
    if raw_token:
        auth_service.log_out(db, raw_token)
    _clear_session_cookie(response)


@router.get("/me", response_model=AccountResponse)
def me(user: CurrentUser) -> AccountResponse:
    return AccountResponse.model_validate(user)


# --- email verification --------------------------------------------------


@router.post(
    "/verify-email",
    response_model=MessageResponse,
    dependencies=[limit_per_address("verify-email", "token_redemptions_per_ip")],
)
def verify_email(data: VerifyEmailRequest, db: DbSession) -> MessageResponse:
    auth_service.verify_email(db, data.token.get_secret_value())
    return MessageResponse(message="Email address verified.")


@router.post(
    "/resend-verification",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[limit_per_address("resend-verification", "email_requests_per_ip")],
)
def resend_verification(
    data: ResendVerificationRequest,
    db: DbSession,
    email_sender: EmailSenderDep,
) -> MessageResponse:
    raw_token = auth_service.request_email_verification(db, data.email)
    if raw_token is not None:
        email_sender.send_email_verification(
            to=data.email,
            url=email_verification_url(raw_token),
        )
    # The same answer whether or not the address belongs to an account, and
    # whether or not a link was issued too recently for another to be sent.
    return MessageResponse(
        message=(
            "If that email address belongs to an account that still needs "
            "verification, a verification email has been sent."
        )
    )


# --- password reset ------------------------------------------------------


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[limit_per_address("forgot-password", "email_requests_per_ip")],
)
def forgot_password(
    data: ForgotPasswordRequest,
    db: DbSession,
    email_sender: EmailSenderDep,
) -> MessageResponse:
    raw_token = auth_service.request_password_reset(db, data.email)
    if raw_token is not None:
        email_sender.send_password_reset(
            to=data.email,
            url=password_reset_url(raw_token),
        )
    # The same answer whether or not the address belongs to an account, and
    # whether or not a link was issued too recently for another to be sent.
    return MessageResponse(
        message=(
            "If that email address belongs to an account, a password reset "
            "email has been sent."
        )
    )


@router.post(
    "/reset-password",
    response_model=MessageResponse,
    dependencies=[limit_per_address("reset-password", "token_redemptions_per_ip")],
)
def reset_password(data: ResetPasswordRequest, db: DbSession) -> MessageResponse:
    auth_service.reset_password(
        db,
        data.token.get_secret_value(),
        data.new_password.get_secret_value(),
    )
    return MessageResponse(message="Password has been reset.")


# --- password change -----------------------------------------------------


@router.post("/change-password", response_model=MessageResponse)
def change_password(
    data: ChangePasswordRequest,
    current: CurrentSession,
    # Named for what it requires. The user is the session's.
    _verified: VerifiedUser,
    response: Response,
    db: DbSession,
) -> MessageResponse:
    """Sets a new password for the signed-in user, who must know the old one.

    Every other session of the user is ended. This one continues, under a
    new cookie.
    """
    # A session alone must not be enough to find out the password by trying:
    # wrong current passwords are limited per account, like failed logins.
    limits = [
        rate_limit.Limit(
            "change-password:account",
            str(current.user_id),
            settings.password_change_failures,
        )
    ]
    counted = rate_limit.count_all(db, limits)
    # Read before the change commits, which expires what the session holds.
    remaining = current.expires_at - datetime.now(timezone.utc)
    raw_token = auth_service.change_password(
        db,
        current,
        current_password=data.current_password.get_secret_value(),
        new_password=data.new_password.get_secret_value(),
    )
    rate_limit.uncount_all(db, counted)
    # The session ends when it would have: the cookie is given the time that
    # is left, not a new lifetime.
    _set_session_cookie(
        response, raw_token, max_age=max(1, int(remaining.total_seconds()))
    )
    return MessageResponse(message="Password has been changed.")


# --- session management --------------------------------------------------


@router.get("/sessions", response_model=list[SessionResponse])
def list_sessions(
    current: CurrentSession,
    db: DbSession,
) -> list[SessionResponse]:
    return [
        SessionResponse(
            id=session.id,
            created_at=session.created_at,
            last_used_at=session.last_used_at,
            expires_at=session.expires_at,
            is_current=session.id == current.id,
        )
        for session in auth_service.list_sessions(db, current.user)
    ]


@router.post("/sessions/revoke-others", response_model=RevokedSessionsResponse)
def revoke_other_sessions(
    current: CurrentSession,
    db: DbSession,
) -> RevokedSessionsResponse:
    revoked_count = auth_service.revoke_other_sessions(db, current)
    return RevokedSessionsResponse(revoked_count=revoked_count)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_session(
    session_id: uuid.UUID,
    current: CurrentSession,
    response: Response,
    db: DbSession,
) -> None:
    is_current = session_id == current.id
    if not auth_service.revoke_session(db, current.user, session_id):
        # Also the answer for a session that belongs to someone else, so the
        # endpoint does not reveal which session ids exist.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found.",
        )
    if is_current:
        _clear_session_cookie(response)

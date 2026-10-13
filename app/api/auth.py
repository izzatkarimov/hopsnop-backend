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
from app.models import UserSession
from app.schemas.auth import (
    AccountResponse,
    ChangePasswordRequest,
    DeleteAccountRequest,
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


def password_confirmation_limits(current: UserSession) -> list[rate_limit.Limit]:
    """The counter of wrong passwords given to confirm an operation.

    A session alone must not be enough to find out the password by trying.
    Wrong passwords are limited per account, like failed logins, and there
    is one counter for every operation that asks for the password again: it
    is the account's, not the operation's.
    """
    return [
        rate_limit.Limit(
            "password-confirmation:account",
            str(current.user_id),
            settings.password_confirmation_failures,
        )
    ]


def data_export_limit(current: UserSession) -> rate_limit.Limit:
    """The counter of exports of an account's data.

    An export reads everything the account has. It is limited per account,
    wherever its sessions are, and it is exports that are counted: a request
    that was refused for its password is not one.
    """
    return rate_limit.Limit(
        "data-export:account",
        str(current.user_id),
        settings.data_exports_per_account,
    )


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
    # The account is read again here, after the session was committed. Should
    # it have been removed in that moment, the login has come to nothing, and
    # is answered as that and not with a cookie for a session that is gone.
    account = auth_service.account_of(session)
    # The token leaves the server only in this cookie, never in the body.
    _set_session_cookie(response, raw_token)
    return AccountResponse.model_validate(account)


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
    # Read first. Counting commits, and a commit expires what the session
    # holds; the session may be gone by the time it would be read again.
    remaining = current.expires_at - datetime.now(timezone.utc)
    limits = password_confirmation_limits(current)
    counted = rate_limit.count_all(db, limits)
    try:
        raw_token = auth_service.change_password(
            db,
            current,
            current_password=data.current_password.get_secret_value(),
            new_password=data.new_password.get_secret_value(),
        )
    except auth_service.AccountNoLongerExistsError:
        # The account was removed while this was under way, and its counter
        # with it. What was counted here since is not left behind for an
        # account that is not there.
        rate_limit.forget(db, limits)
        raise
    rate_limit.uncount_all(db, counted)
    # The session ends when it would have: the cookie is given the time that
    # is left, not a new lifetime.
    _set_session_cookie(
        response, raw_token, max_age=max(1, int(remaining.total_seconds()))
    )
    return MessageResponse(message="Password has been changed.")


# --- account deletion ----------------------------------------------------


def _counters_named_after(
    current: UserSession, address: str
) -> list[rate_limit.Limit]:
    """The rate-limit counters that are kept under the account's own names.

    Those of failed logins by its username and by its email address, as far
    as they can be addressed (from every address taken together, and from
    the address of this request), and the account's own counters: of
    password confirmations and of exports. Counters of its logins from other
    addresses cannot be told from here; they hold no name and are gone when
    their window is.

    The counter of the address alone is not among them. It is the
    address's, not the account's: removing an account must not give
    whoever sits at that address their guesses at other accounts back.
    """
    account = auth_service.account_of(current)
    limits = [*password_confirmation_limits(current), data_export_limit(current)]
    for identifier in (account.username, account.email):
        limits += [
            limit
            for limit in _login_limits(identifier, address)
            if limit.subject != address
        ]
    return limits


@router.post("/delete-account", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    data: DeleteAccountRequest,
    current: CurrentSession,
    address: ClientAddress,
    response: Response,
    db: DbSession,
) -> None:
    """Removes the signed-in user's account, at once and for good.

    The password has to be given again: a session alone does not remove an
    account. Whose account it is, is always the signed-in user's; there is
    no way to name another.

    With the account go its posts, stories, likes, reposts and follows, and
    every session of it. A post that others have replied to leaves an empty
    place in its thread, with no author and no text. The username and the
    email address are free again from that moment.
    """
    # Worked out first, while the account can still be read.
    to_forget = _counters_named_after(current, address)
    # Wrong passwords count against the same limit as for a password change:
    # the account's, whatever asks for the password.
    rate_limit.count_all(db, password_confirmation_limits(current))
    auth_service.delete_account(
        db, current, password=data.password.get_secret_value()
    )
    # What was counted under the account's names does not stay behind for
    # whoever takes them next.
    rate_limit.forget(db, to_forget)
    _clear_session_cookie(response)


# --- session management --------------------------------------------------


@router.get("/sessions", response_model=list[SessionResponse])
def list_sessions(
    current: CurrentSession,
    db: DbSession,
) -> list[SessionResponse]:
    account = auth_service.account_of(current)
    return [
        SessionResponse(
            id=session.id,
            created_at=session.created_at,
            last_used_at=session.last_used_at,
            expires_at=session.expires_at,
            is_current=session.id == current.id,
        )
        for session in auth_service.list_sessions(db, account)
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
    account = auth_service.account_of(current)
    if not auth_service.revoke_session(db, account, session_id):
        # Also the answer for a session that belongs to someone else, so the
        # endpoint does not reveal which session ids exist.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found.",
        )
    if is_current:
        _clear_session_cookie(response)

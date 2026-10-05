"""Authentication endpoints.

The handlers are thin: they translate between HTTP (bodies, the session
cookie, status codes) and ``app.services.auth``, which holds the rules.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.deps import CurrentSession, CurrentUser, DbSession
from app.core.config import settings
from app.schemas.auth import (
    AccountResponse,
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


def _set_session_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=raw_token,
        # The browser drops the cookie when the session expires server-side.
        max_age=int(settings.session_lifetime.total_seconds()),
        path="/",
        # No Domain attribute: the cookie is only sent back to this host.
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite=settings.session_cookie_samesite,
    )


# --- registration and login ----------------------------------------------


@router.post(
    "/register",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
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
    db: DbSession,
) -> AccountResponse:
    session, raw_token = auth_service.log_in(
        db,
        identifier=data.identifier,
        password=data.password.get_secret_value(),
        replaced_token=request.cookies.get(settings.session_cookie_name),
    )
    # The token leaves the server only in this cookie, never in the body.
    _set_session_cookie(response, raw_token)
    return AccountResponse.model_validate(session.user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, db: DbSession) -> None:
    """Ends the session server-side and clears the cookie.

    Succeeds whether or not the request carried a usable session.
    """
    raw_token = request.cookies.get(settings.session_cookie_name)
    if raw_token:
        auth_service.log_out(db, raw_token)
    _clear_session_cookie(response)


@router.get("/me", response_model=AccountResponse)
def me(user: CurrentUser) -> AccountResponse:
    return AccountResponse.model_validate(user)


# --- email verification --------------------------------------------------


@router.post("/verify-email", response_model=MessageResponse)
def verify_email(data: VerifyEmailRequest, db: DbSession) -> MessageResponse:
    auth_service.verify_email(db, data.token.get_secret_value())
    return MessageResponse(message="Email address verified.")


@router.post(
    "/resend-verification",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
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
    # The same answer whether or not the address belongs to an account.
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
    # The same answer whether or not the address belongs to an account.
    return MessageResponse(
        message=(
            "If that email address belongs to an account, a password reset "
            "email has been sent."
        )
    )


@router.post("/reset-password", response_model=MessageResponse)
def reset_password(data: ResetPasswordRequest, db: DbSession) -> MessageResponse:
    auth_service.reset_password(
        db,
        data.token.get_secret_value(),
        data.new_password.get_secret_value(),
    )
    return MessageResponse(message="Password has been reset.")


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

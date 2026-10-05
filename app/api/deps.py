"""Dependencies shared by the API routers."""

from collections.abc import Iterator
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import User, UserSession
from app.services import auth as auth_service

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def get_db() -> Iterator[Session]:
    # Closing the session rolls back anything that was not committed.
    with SessionLocal() as db:
        yield db


DbSession = Annotated[Session, Depends(get_db)]


def verify_request_origin(request: Request) -> None:
    """CSRF defence: reject state-changing requests sent by another website.

    The session cookie is attached by the browser automatically, so a request
    is not proven to be intended by the user just because it carries one.
    Browsers state where a request comes from in the Origin header (older
    ones in Referer), and a page cannot forge either. A request that changes
    state is accepted only if that origin is the configured frontend or this
    API itself.

    A request with neither header does not come from a browser acting for
    another site, which is the only way CSRF can happen, so it is let
    through. Safe methods never change state and are not checked.

    Applied to the whole application in ``app.main``, so it also covers every
    route added later.
    """
    if request.method in _SAFE_METHODS:
        return

    source = request.headers.get("origin")
    if source is None:
        referer = request.headers.get("referer")
        if referer is None:
            return
        parts = urlsplit(referer)
        source = f"{parts.scheme}://{parts.netloc}"

    own_origin = f"{request.url.scheme}://{request.url.netloc}"
    # Anything else is refused, including the opaque origin "null" that
    # browsers send from sandboxed documents.
    if source.lower() not in (settings.frontend_origin, own_origin.lower()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-origin request rejected.",
        )


def get_current_session(request: Request, db: DbSession) -> UserSession:
    """The session the request's cookie belongs to.

    Every way of not having a usable session gives the same 401: no cookie,
    unknown token, revoked, expired, or deactivated user.
    """
    raw_token = request.cookies.get(settings.session_cookie_name)
    session = auth_service.authenticate_session(db, raw_token) if raw_token else None
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
        )
    return session


CurrentSession = Annotated[UserSession, Depends(get_current_session)]


def get_current_user(session: CurrentSession) -> User:
    """The authenticated user. Endpoints that need one depend on this."""
    return session.user


CurrentUser = Annotated[User, Depends(get_current_user)]

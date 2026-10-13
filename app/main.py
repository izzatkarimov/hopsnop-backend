from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg.errors import ForeignKeyViolation
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError

from app.api.auth import router as auth_router
from app.api.deps import verify_request_origin
from app.api.feed import router as feed_router
from app.api.posts import router as posts_router
from app.api.stories import router as stories_router
from app.api.users import router as users_router
from app.core.config import settings
from app.core.middleware import (
    RequestBodyLimitMiddleware,
    SecurityHeadersMiddleware,
    add_security_headers,
)
from app.services.auth import AuthError
from app.services.posts import PostError
from app.services.rate_limit import RateLimitedError
from app.services.stories import StoryError
from app.services.users import UserError

# Every error has the same shape: {"detail": ...}.


async def handle_service_error(
    request: Request, exc: AuthError | PostError | StoryError | UserError
) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


async def handle_rate_limited(request: Request, exc: RateLimitedError) -> JSONResponse:
    # One answer for every limit. Which one was reached is not said.
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers={"Retry-After": str(exc.retry_after)},
    )


async def handle_lost_reference(request: Request, exc: IntegrityError) -> JSONResponse:
    """A write that refers to something which was removed just before it.

    The services hold what they refer to in place while they write: the
    post that is replied to or liked, the account that is followed, and
    first of all the account they write for. So this is not how a deletion
    that happens at the same moment is met, and nothing in the application
    is known to end up here. It is what is left should a write ever refer
    to something without holding it. That would not be a fault of the
    request, and it is answered as a conflict, not as a server error.

    Any other integrity error is one, and goes on to be handled as such.
    """
    if not isinstance(exc.orig, ForeignKeyViolation):
        raise exc
    return JSONResponse(
        status_code=409,
        content={"detail": "The request could not be completed."},
    )


async def handle_removed_account(
    request: Request, exc: ObjectDeletedError | StaleDataError
) -> JSONResponse:
    """A request whose own account was removed while it was under way.

    A request is authenticated at its start and then works with the rows it
    was given then: the session and the account. If the account is removed
    by another request in the meantime, those rows are gone, and reading or
    changing them fails in one of these two ways. Every row the application
    handles like this is the requester's own, so this is what it means, and
    the answer is the one a request without a session gets, which is what
    the request has become.

    Nothing was written: the change found no row to apply to.
    """
    return JSONResponse(status_code=401, content={"detail": "Not authenticated."})


async def handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # By default each error also carries the rejected input, which would echo
    # submitted passwords and tokens back in the response. Only the location
    # and the reason are returned.
    errors = [
        {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # The exception itself still reaches the server log; the client learns
    # nothing about what went wrong internally.
    response = JSONResponse(
        status_code=500, content={"detail": "Internal server error."}
    )
    # This answer is sent from outside every middleware, so it is given the
    # headers that the others get from there.
    add_security_headers(response.headers)
    return response


async def health_check():
    return {"status": "ok"}


def create_app() -> FastAPI:
    """The application, built from the settings as they are when called.

    Called once, below, when the module is imported. It is a function so
    that what depends on the environment (whether the documentation is
    served, how large a body may be) can be built again for another one.
    """
    docs_enabled = settings.api_docs_enabled
    app = FastAPI(
        title="Hopsnop API",
        version="0.1.0",
        # CSRF defence for every route, present and future.
        dependencies=[Depends(verify_request_origin)],
        # The interactive documentation and the schema behind it describe
        # every route. They are served in development and nowhere else.
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    # Middleware is listed from the inside out: the last one added is the
    # first to see a request and the last to see its response.

    # Innermost, so that a body refused for its size is still answered with
    # the headers that the two below add.
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=settings.max_request_body_bytes,
    )

    # The frontend is a separate origin, so the browser needs explicit
    # permission to let it call this API with the session cookie. Exactly one
    # origin gets it; a wildcard is never used together with credentials.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
        # Of the response headers a browser keeps from scripts by default,
        # the one the frontend needs: when to try again after a 429.
        expose_headers=["Retry-After"],
    )

    # Outermost: every response gets these, whatever produced it.
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(posts_router)
    app.include_router(feed_router)
    app.include_router(stories_router)

    for service_error in (AuthError, PostError, StoryError, UserError):
        app.add_exception_handler(service_error, handle_service_error)
    app.add_exception_handler(RateLimitedError, handle_rate_limited)
    app.add_exception_handler(IntegrityError, handle_lost_reference)
    for removed in (ObjectDeletedError, StaleDataError):
        app.add_exception_handler(removed, handle_removed_account)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)

    app.add_api_route("/health", health_check, methods=["GET"])
    return app


app = create_app()

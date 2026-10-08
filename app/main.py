from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
    app.add_exception_handler(RequestValidationError, handle_validation_error)
    app.add_exception_handler(Exception, handle_unexpected_error)

    app.add_api_route("/health", health_check, methods=["GET"])
    return app


app = create_app()

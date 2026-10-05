from fastapi import Depends, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.auth import router as auth_router
from app.api.deps import verify_request_origin
from app.api.users import router as users_router
from app.core.config import settings
from app.services.auth import AuthError

app = FastAPI(
    title="Hopsnop API",
    version="0.1.0",
    # CSRF defence for every route, present and future.
    dependencies=[Depends(verify_request_origin)],
)

# The frontend is a separate origin, so the browser needs explicit permission
# to let it call this API with the session cookie. Exactly one origin gets it;
# a wildcard is never used together with credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type"],
)

app.include_router(auth_router)
app.include_router(users_router)


# Every error has the same shape: {"detail": ...}.


@app.exception_handler(AuthError)
async def handle_auth_error(request: Request, exc: AuthError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
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


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    # The exception itself still reaches the server log; the client learns
    # nothing about what went wrong internally.
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


@app.get("/health")
async def health_check():
    return {"status": "ok"}

"""Middleware that applies to every request and response.

Both are plain ASGI middleware: they wrap the application's ``receive`` and
``send`` and add no per-endpoint code.
"""

import json

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Where the interactive API documentation lives while it is enabled. Those
# pages load a script and a stylesheet, which the policy below would block.
_DOCUMENTATION_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})

# This API answers with JSON and has no pages. A response of it has no reason
# to load anything, to be rendered inside a frame, or to be read as any type
# but its own.
_CONTENT_SECURITY_POLICY = "default-src 'none'; frame-ancestors 'none'"


def add_security_headers(
    headers: MutableHeaders, *, documentation: bool = False
) -> None:
    """Put the baseline security headers on a response's headers."""
    headers["X-Content-Type-Options"] = "nosniff"
    headers["Referrer-Policy"] = "no-referrer"
    if not documentation:
        headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
    # Nothing this API says is meant to be kept. The routes that carry
    # account data say so themselves, but an error raised before they answer
    # does not, so it is said here for whatever did not.
    if "Cache-Control" not in headers:
        headers["Cache-Control"] = "no-store"


class SecurityHeadersMiddleware:
    """Adds the baseline security headers to every response.

    Strict-Transport-Security is not among them on purpose: it belongs to
    whatever terminates TLS, which is not this application. Neither is the
    policy for the frontend's pages, which is the frontend's to send.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_documentation = scope["path"] in _DOCUMENTATION_PATHS

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                add_security_headers(
                    MutableHeaders(scope=message), documentation=is_documentation
                )
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestBodyLimitMiddleware:
    """Refuses a request whose body is larger than ``max_bytes``, with 413.

    A request that declares its size is refused, or let through, on that
    declaration; the server holds the body to it. One that declares none (a
    chunked upload) is read here first, up to the limit and no further, and
    handed on only if it ended within it. Either way a body that is too
    large never reaches validation, a password hash or the database.

    A declared size counts only if the request has no Transfer-Encoding.
    With one, the body is framed by its chunks and the declared size binds
    nothing: a server that accepts both headers reads the chunks. Such a
    request is measured like one that declares no size at all.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = self._declared_length(scope)
        if declared is not None:
            if declared > self.max_bytes:
                await self._refuse(send)
            else:
                await self.app(scope, receive, send)
            return

        # No usable Content-Length. The limit is small, so the whole body
        # can be held while it is measured.
        chunks: list[bytes] = []
        size = 0
        # Whatever ended the reading early (the client going away), to be
        # passed on as it came.
        interruption: Message | None = None
        while True:
            message = await receive()
            if message["type"] != "http.request":
                interruption = message
                break
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                await self._refuse(send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                if interruption is not None:
                    return interruption
                return {
                    "type": "http.request",
                    "body": b"".join(chunks),
                    "more_body": False,
                }
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    def _declared_length(scope: Scope) -> int | None:
        """The size the request declares, if that is what its body is held to."""
        declared: bytes | None = None
        for name, value in scope["headers"]:
            if name == b"transfer-encoding":
                # Whatever Content-Length says beside it, it is not what
                # ends this body.
                return None
            if name == b"content-length" and declared is None:
                declared = value
        if declared is None:
            return None
        try:
            return int(declared)
        except ValueError:
            # Not a number: treated as not declared, and measured.
            return None

    @staticmethod
    async def _refuse(send: Send) -> None:
        # The same shape as every other error of this API.
        body = json.dumps({"detail": "Request body too large."}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

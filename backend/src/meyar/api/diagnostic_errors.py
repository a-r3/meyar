"""Consume unexpected HTTP failures before ASGI server traceback reporting."""

from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class DiagnosticErrorMiddleware:
    def __init__(
        self, app: ASGIApp, handler: Callable[[Request, Exception], Awaitable[Response]],
    ) -> None:
        self.app, self.handler = app, handler

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, guarded_send)
        except Exception as exc:
            # ServerErrorMiddleware normally re-raises even after sending a safe
            # response. Consume here so Uvicorn cannot format sensitive causes.
            try:
                response = await self.handler(Request(scope), exc)
            except Exception:
                response = JSONResponse({"detail": "Internal server error."}, status_code=500)
            if not started:
                await response(scope, receive, send)

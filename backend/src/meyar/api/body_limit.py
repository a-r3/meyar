"""Request-envelope bound for candidate-document uploads (issue #46 PR-1).

FastAPI/Starlette parses a multipart body (spooling it to memory/disk)
before the route handler runs, so bounding only the handler's read cannot
bound what a request makes the process consume. This pure ASGI middleware
(deliberately not ``BaseHTTPMiddleware``, which would buffer the body)
bounds the request itself, before multipart parsing, for the candidate
document upload route only. ``Content-Length`` is an early, advisory
rejection; the count of body bytes actually received is the authority.
"""

import json
import re
from collections.abc import Callable, MutableMapping
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from meyar.config import Settings, get_settings
from meyar.ingestion.validation import too_large_message

# Maximum accepted multipart envelope overhead on top of ``max_upload_bytes``.
# One document upload carries a single ``file`` part: two boundary lines
# (<= 74 bytes each), a Content-Disposition header with the filename and a
# Content-Type header (a few hundred bytes for real clients), and CRLFs.
# 64 KiB leaves orders of magnitude of headroom for that envelope plus a
# few small extra form fields, while still capping the non-document bytes a
# client can add to a request at a constant. A request whose envelope alone
# exceeds this (e.g. a ~60 KiB filename) is intentionally unsupported.
MAX_MULTIPART_OVERHEAD_BYTES = 64 * 1024

_UPLOAD_PATH = re.compile(r"^/api/v1/candidates/[^/]+/documents/?$")

SettingsProvider = Callable[[], Settings]


def _resolve_settings(scope: Scope) -> Settings:
    app = scope.get("app")
    overrides = getattr(app, "dependency_overrides", None) or {}
    provider: SettingsProvider = overrides.get(get_settings, get_settings)
    return provider()


def _declared_content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", []):
        if name == b"content-length":
            try:
                declared = int(value.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                return None
            return declared if declared >= 0 else None
    return None


async def _send_too_large(send: Send, max_bytes: int) -> None:
    body = json.dumps({"detail": too_large_message(max_bytes)}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class DocumentUploadBodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not _UPLOAD_PATH.match(scope["path"])
        ):
            await self.app(scope, receive, send)
            return

        max_bytes = _resolve_settings(scope).max_upload_bytes
        request_bound = max_bytes + MAX_MULTIPART_OVERHEAD_BYTES

        declared = _declared_content_length(scope)
        if declared is not None and declared > request_bound:
            await _send_too_large(send, max_bytes)
            return

        state: MutableMapping[str, Any] = {"received": 0, "exceeded": False}

        async def bounded_receive() -> Message:
            if state["exceeded"]:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                state["received"] += len(message.get("body", b""))
                if state["received"] > request_bound:
                    state["exceeded"] = True
                    # The downstream parser sees a dropped connection and
                    # stops; the real body is never read any further.
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            if not state["exceeded"]:
                await send(message)

        try:
            await self.app(scope, bounded_receive, guarded_send)
        except Exception:
            if not state["exceeded"]:
                raise
        if state["exceeded"]:
            await _send_too_large(send, max_bytes)


__all__ = ["DocumentUploadBodyLimitMiddleware", "MAX_MULTIPART_OVERHEAD_BYTES"]

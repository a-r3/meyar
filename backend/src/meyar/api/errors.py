"""JSON error mapping for the machine and human API surfaces (issue #45).

``ApplicationError`` → the stable envelope of ``meyar.application.errors``.
The human JSON prefix additionally maps framework errors (validation, HTTP,
inactive tenant, unexpected) into the same envelope so a JSON client never
receives an HTML page or a redirect. Nothing here echoes request input,
exception text, SQL, paths or storage keys.

The machine (``/api/v1``) surface returns the SAME stable envelope for every
failure. Where a historical machine endpoint already had a top-level
``detail`` string, that string is preserved (as ``legacy_detail``) so
existing FastAPI-style clients keep working, and the stable
``error { code, reason, message, retryable }`` object is added. The human
(JSON) surface never serializes ``legacy_detail`` as its top-level
``detail``: there ``detail == error.message``."""

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from meyar.application.errors import (
    ApplicationError,
    ErrorCode,
    code_for_status,
    error_envelope,
)
from meyar.config import get_settings
from meyar.ui.auth import UI_SESSION_COOKIE

HUMAN_JSON_PREFIX = "/ui/api/"

# Machine surface path prefix, distinct from the human JSON and HTML UI paths.
MACHINE_API_PREFIX = "/api/v1"


def is_human_json_path(path: str) -> bool:
    return path.startswith(HUMAN_JSON_PREFIX)


def is_machine_api_path(path: str) -> bool:
    return path.startswith(MACHINE_API_PREFIX)


def clear_session_cookie(response: JSONResponse) -> None:
    response.delete_cookie(
        UI_SESSION_COOKIE,
        path="/ui",
        secure=get_settings().ui_cookie_secure,
        httponly=True,
        samesite="lax",
    )


async def application_error_handler(request: Request, exc: ApplicationError) -> JSONResponse:
    if is_human_json_path(request.url.path):
        # Human JSON: ``detail`` is the fixed, privacy-safe human message and
        # must never be a machine/internal ``legacy_detail`` sub-code.
        body = error_envelope(
            exc.code,
            reason=exc.reason,
            message=exc.message,
            retryable=exc.retryable,
        )
    else:
        # Machine API: preserve the historical top-level ``detail`` (when one
        # was set) for backward compatibility, while always returning the full
        # stable envelope.
        body = exc.body()
    response = JSONResponse(body, status_code=exc.status_code, headers=exc.headers)
    if exc.clear_session_cookie:
        clear_session_cookie(response)
    return response


def validation_error_response() -> JSONResponse:
    # Pydantic's default errors include raw input and exception context.
    return JSONResponse(
        error_envelope(ErrorCode.VALIDATION_FAILED, reason="REQUEST_INVALID"), status_code=422
    )


def http_error_response(exc: StarletteHTTPException) -> JSONResponse:
    code = code_for_status(exc.status_code)
    return JSONResponse(
        error_envelope(code, retryable=exc.status_code in {429, 503}),
        status_code=exc.status_code,
    )


def internal_error_response() -> JSONResponse:
    return JSONResponse(error_envelope(ErrorCode.INTERNAL_ERROR), status_code=500)


def unauthenticated_response() -> JSONResponse:
    response = JSONResponse(
        error_envelope(ErrorCode.AUTHENTICATION_REQUIRED), status_code=401
    )
    clear_session_cookie(response)
    return response

# --- Machine (/api/v1) framework-error responses ----------------------------

# These mirror the historical FastAPI-style detail strings so existing machine
# clients keep a compatible top-level ``detail``, while adding the stable
# envelope contract. Nothing here echoes request input, secrets, paths, or
# exception text.


def machine_unauthenticated_response() -> JSONResponse:
    """401 for a missing/invalid API key on the machine surface.

    Keeps ``WWW-Authenticate: Bearer`` so Bearer-auth challenges still work.
    """
    response = JSONResponse(
        error_envelope(
            ErrorCode.AUTHENTICATION_REQUIRED,
            reason=None,
            legacy_detail="Invalid or missing API key.",
        ),
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )
    return response


def machine_http_error_response(exc: StarletteHTTPException) -> JSONResponse:
    """Framework HTTP errors (401/403/404/409/500/503/...) on /api/v1.

    The historical ``detail`` is preserved as ``legacy_detail``; the stable
    envelope is added. Any headers the exception carried (for example
    ``WWW-Authenticate: Bearer``) are forwarded.
    """
    code = code_for_status(exc.status_code)
    headers = dict(exc.headers) if exc.headers else {}
    return JSONResponse(
        error_envelope(
            code,
            reason=None,
            retryable=exc.status_code in {429, 503},
            legacy_detail=exc.detail,
        ),
        status_code=exc.status_code,
        headers=headers,
    )


def machine_validation_error_response() -> JSONResponse:
    """422 request validation on /api/v1.

    Preserves the historical ``"Invalid request."`` detail without echoing
    Pydantic's raw input or exception context.
    """
    return JSONResponse(
        error_envelope(
            ErrorCode.VALIDATION_FAILED,
            reason="REQUEST_INVALID",
            legacy_detail="Invalid request.",
        ),
        status_code=422,
    )


def machine_internal_error_response() -> JSONResponse:
    """500 unexpected server failure on /api/v1.

    No stack trace or exception text is returned.
    """
    return JSONResponse(
        error_envelope(
            ErrorCode.INTERNAL_ERROR,
            reason=None,
            legacy_detail="Internal server error.",
        ),
        status_code=500,
    )

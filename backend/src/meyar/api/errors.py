"""JSON error mapping for the machine and human API surfaces (issue #45).

``ApplicationError`` → the stable envelope of ``meyar.application.errors``.
The human JSON prefix additionally maps framework errors (validation, HTTP,
inactive tenant, unexpected) into the same envelope so a JSON client never
receives an HTML page or a redirect. Nothing here echoes request input,
exception text, SQL, paths or storage keys."""

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


def is_human_json_path(path: str) -> bool:
    return path.startswith(HUMAN_JSON_PREFIX)


def clear_session_cookie(response: JSONResponse) -> None:
    response.delete_cookie(
        UI_SESSION_COOKIE,
        path="/ui",
        secure=get_settings().ui_cookie_secure,
        httponly=True,
        samesite="lax",
    )


async def application_error_handler(_request: Request, exc: ApplicationError) -> JSONResponse:
    response = JSONResponse(exc.body(), status_code=exc.status_code, headers=exc.headers)
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

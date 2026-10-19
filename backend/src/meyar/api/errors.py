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
    _DEFAULT_MESSAGES,
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

# Historical top-level ``detail`` strings that are server-owned and privacy
# safe. These may be preserved as ``legacy_detail`` for backward compatibility.
# Anything not in this set is treated as untrusted and replaced with a fixed
# safe message (never promoted to ``error.reason``).
_KNOWN_SAFE_DETAILS: frozenset[str] = frozenset(
    {
        "Invalid or missing API key.",
        "Invalid request.",
        "Internal server error.",
        "Job not found.",
        "Candidate not found.",
        "Document not found.",
        "Criteria version not found.",
        "The search request is invalid.",
        "Search infrastructure is currently unavailable.",
    }
)

# Closed machine sub-reason codes. Only these bounded values may appear in
# ``error.reason`` for machine /api/v1 errors. Arbitrary exception text is
# never promoted to a reason.
_KNOWN_REASON_CODES: frozenset[str] = frozenset(
    {
        # Authorization
        "SCOPE_MISSING",
        # Search / embedding (bounded provider and service codes)
        "INFERENCE_BUSY",
        "EMBEDDING_UNAVAILABLE",
        "EMBEDDING_TIMEOUT",
        "EMBEDDING_INVALID_OUTPUT",
        "EMBEDDING_PROVIDER_ERROR",
        "EMBEDDING_PROVIDER_REQUIRED",
        "EMBEDDING_PROVIDER_CONFIG_MISMATCH",
        "EMBEDDING_RESULT_PROVENANCE_MISMATCH",
        "QUERY_VECTOR_INVALID",
        "QUERY_VECTOR_DIMENSION_MISMATCH",
        "QUERY_VECTOR_ZERO_NORM",
    }
)


def classify_machine_http_error(
    exc: StarletteHTTPException,
) -> tuple[ErrorCode, str | None, str, bool]:
    """Classify a machine /api/v1 ``HTTPException`` into
    ``(code, reason, detail, retryable)``.

    The classification is fail-closed:

    * A known safe historical ``detail`` is preserved as ``legacy_detail``
      (top-level ``detail``) with ``reason=None``.
    * A known closed reason code is moved to ``error.reason`` and the
      top-level ``detail`` is replaced with the fixed safe status message.
    * Anything unknown or unclassified gets the fixed safe status message
      with ``reason=None`` — arbitrary ``exc.detail`` is never trusted and
      never becomes ``error.reason``.
    """
    code = code_for_status(exc.status_code)
    retryable = exc.status_code in {429, 503}
    detail = exc.detail

    if detail in _KNOWN_SAFE_DETAILS:
        # Known safe historical detail: preserve it as legacy_detail.
        return code, None, detail, retryable

    if detail in _KNOWN_REASON_CODES:
        # Known closed reason code: move to error.reason, fixed safe detail.
        return code, detail, _DEFAULT_MESSAGES[code], retryable

    # Unknown/unclassified: never trust arbitrary detail.
    return code, None, _DEFAULT_MESSAGES[code], retryable


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

    Classifies ``exc.detail`` fail-closed: known safe historical details are
    preserved as ``legacy_detail``; known closed reason codes are moved to
    ``error.reason`` with a fixed safe detail; unknown details are replaced
    with a fixed safe message and never become ``error.reason``. Any headers
    the exception carried (for example ``WWW-Authenticate: Bearer``) are
    forwarded.
    """
    code, reason, detail, retryable = classify_machine_http_error(exc)
    headers = dict(exc.headers) if exc.headers else {}
    return JSONResponse(
        error_envelope(
            code,
            reason=reason,
            retryable=retryable,
            legacy_detail=detail,
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

"""Stable, privacy-safe machine error contract (issue #45, D-116).

Three layers are kept deliberately separate:

1. ``code`` — a closed, stable machine enum (``ErrorCode``) clients may branch on.
2. ``reason`` — an optional closed sub-reason (for example ``JOB_ARCHIVED``).
3. ``message`` — fixed, server-owned, privacy-safe human text.

Internal diagnostic detail (exception text, SQL, paths, storage keys, planner
debug reason strings, model output) is never part of an error. Planner
non-executable *outcomes* are successful answers, not errors."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(StrEnum):
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    CSRF_REJECTED = "CSRF_REJECTED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    RETRYABLE_BUSY = "RETRYABLE_BUSY"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_DEFAULT_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.AUTHENTICATION_REQUIRED: "Authentication is required.",
    ErrorCode.AUTHORIZATION_DENIED: "This action is not permitted.",
    ErrorCode.CSRF_REJECTED: "The request could not be verified.",
    ErrorCode.VALIDATION_FAILED: "The request is invalid.",
    ErrorCode.NOT_FOUND: "The requested resource was not found.",
    ErrorCode.CONFLICT: "The request conflicts with the current state.",
    ErrorCode.RETRYABLE_BUSY: "The service is busy. Retry shortly.",
    ErrorCode.SERVICE_UNAVAILABLE: "The service is temporarily unavailable.",
    ErrorCode.PAYLOAD_TOO_LARGE: "The request is too large.",
    ErrorCode.INTERNAL_ERROR: "An unexpected error occurred.",
}

_STATUS_CODES: dict[int, ErrorCode] = {
    400: ErrorCode.VALIDATION_FAILED,
    401: ErrorCode.AUTHENTICATION_REQUIRED,
    403: ErrorCode.AUTHORIZATION_DENIED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.VALIDATION_FAILED,
    409: ErrorCode.CONFLICT,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    422: ErrorCode.VALIDATION_FAILED,
    429: ErrorCode.RETRYABLE_BUSY,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}


def code_for_status(status_code: int) -> ErrorCode:
    return _STATUS_CODES.get(
        status_code,
        ErrorCode.INTERNAL_ERROR if status_code >= 500 else ErrorCode.VALIDATION_FAILED,
    )


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    reason: str | None = Field(
        default=None, max_length=64, description="Closed machine sub-reason, when one exists."
    )
    message: str = Field(max_length=300, description="Fixed privacy-safe text.")
    retryable: bool = False


class ErrorEnvelope(BaseModel):
    """Every error response. ``detail`` repeats the message so clients written
    against FastAPI's default shape keep working; ``error`` is the contract."""

    model_config = ConfigDict(extra="forbid")

    detail: str
    error: ErrorDetail


def error_envelope(
    code: ErrorCode,
    *,
    reason: str | None = None,
    message: str | None = None,
    retryable: bool = False,
    legacy_detail: str | None = None,
) -> dict[str, object]:
    text = message or _DEFAULT_MESSAGES[code]
    return {
        "detail": legacy_detail if legacy_detail is not None else text,
        "error": {"code": code.value, "reason": reason, "message": text, "retryable": retryable},
    }


class ApplicationError(Exception):
    """Raised by shared operations and human-JSON dependencies; adapters map it
    to the envelope. Carries no exception chain text into the response."""

    def __init__(
        self,
        code: ErrorCode,
        *,
        status_code: int,
        reason: str | None = None,
        message: str | None = None,
        retryable: bool = False,
        clear_session_cookie: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.reason = reason
        self.message = message or _DEFAULT_MESSAGES[code]
        self.retryable = retryable
        self.clear_session_cookie = clear_session_cookie
        self.headers = headers or {}
        super().__init__(code.value)

    def body(self) -> dict[str, object]:
        return error_envelope(
            self.code, reason=self.reason, message=self.message, retryable=self.retryable
        )


def not_found(message: str | None = None) -> ApplicationError:
    return ApplicationError(ErrorCode.NOT_FOUND, status_code=404, message=message)


def conflict(
    reason: str, message: str | None = None, *, retryable: bool = False
) -> ApplicationError:
    return ApplicationError(
        ErrorCode.CONFLICT, status_code=409, reason=reason, message=message, retryable=retryable
    )


def validation_failed(reason: str, message: str | None = None) -> ApplicationError:
    return ApplicationError(
        ErrorCode.VALIDATION_FAILED, status_code=422, reason=reason, message=message
    )


def unavailable(reason: str, *, retryable: bool = True) -> ApplicationError:
    return ApplicationError(
        ErrorCode.SERVICE_UNAVAILABLE, status_code=503, reason=reason, retryable=retryable
    )

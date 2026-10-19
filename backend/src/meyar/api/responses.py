"""OpenAPI error-response declarations shared by the machine and human APIs."""

from typing import Any

from meyar.application.errors import ErrorEnvelope

_DESCRIPTIONS: dict[int, str] = {
    401: "Authentication required (AUTHENTICATION_REQUIRED).",
    403: "Not permitted (AUTHORIZATION_DENIED, or CSRF_REJECTED on the human surface).",
    404: "Not found inside the caller's own scope (NOT_FOUND). Foreign and missing "
    "resources are indistinguishable.",
    409: "Conflicts with current state (CONFLICT); see error.reason.",
    413: "Request too large (PAYLOAD_TOO_LARGE).",
    422: "Invalid request (VALIDATION_FAILED).",
    500: "An unexpected error occurred.",
    503: "Temporarily unavailable or busy; retry when error.retryable is true.",
}


def error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {
        code: {"model": ErrorEnvelope, "description": _DESCRIPTIONS[code]} for code in codes
    }

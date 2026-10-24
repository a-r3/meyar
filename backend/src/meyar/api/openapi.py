"""Truthful OpenAPI security and error contracts for the two authentication
models (issue #45).

FastAPI emits one security requirement per ``Security`` dependency, and the
OpenAPI specification reads a list of requirements as *alternatives* (OR). The
human surface needs the BrowserSession cookie AND, for mutations, the CSRF
header, so those are merged into a single requirement object (AND). The machine
surface keeps the single ``ApiKeyBearer`` scheme; the two are never mixed.

On the machine surface (``/api/v1``) every failure returns the stable
``ErrorEnvelope`` contract. FastAPI auto-generates ``HTTPValidationError`` for
422 on routes with Pydantic body models; this normaliser replaces those
references with ``ErrorEnvelope`` so the generated schema is truthful about the
actual runtime contract.
"""

from typing import Any

from fastapi import FastAPI

HUMAN_PATH_PREFIX = "/ui/api/v1"
MACHINE_PATH_PREFIX = "/api/v1"

# Error codes that must be documented with ErrorEnvelope on every protected
# machine route.
COMMON_MACHINE_ERROR_CODES = ("401", "403", "422", "500")


def _merge_human_security(schema: dict[str, Any]) -> None:
    for path, operations in schema.get("paths", {}).items():
        if not path.startswith(HUMAN_PATH_PREFIX):
            continue
        for operation in operations.values():
            requirements = operation.get("security")
            if not requirements:
                continue
            merged: dict[str, list[str]] = {}
            for requirement in requirements:
                merged.update(requirement)
            operation["security"] = [merged]


def _normalise_machine_error_responses(schema: dict[str, Any]) -> None:
    """Ensure /api/v1 error responses reference ErrorEnvelope, not
    FastAPI's default HTTPValidationError, and that common framework
    errors are documented on every protected machine route."""
    components = schema.get("components", {})
    schemas = components.get("schemas", {})
    has_validation_error = "HTTPValidationError" in schemas

    for path, operations in schema.get("paths", {}).items():
        if not path.startswith(MACHINE_PATH_PREFIX):
            continue
        # Health routes are intentionally unauthenticated — leave them alone.
        if path.startswith("/api/v1/health"):
            continue
        for operation in operations.values():
            responses = operation.get("responses", {})
            if not responses:
                continue

            # Replace any HTTPValidationError 422 reference with ErrorEnvelope.
            if "422" in responses:
                resp = responses["422"]
                content = resp.get("content", {})
                json_media = content.get("application/json", {})
                ref = json_media.get("schema", {}).get("$ref", "")
                if ref.endswith("/HTTPValidationError"):
                    json_media["schema"] = {"$ref": "#/components/schemas/ErrorEnvelope"}
                    resp["description"] = "Invalid request (VALIDATION_FAILED)."

            # Ensure common framework errors are declared with ErrorEnvelope.
            for code in COMMON_MACHINE_ERROR_CODES:
                if code not in responses:
                    responses[code] = {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
                            }
                        },
                        "description": {
                            "401": "Authentication required (AUTHENTICATION_REQUIRED).",
                            "403": "Not permitted (AUTHORIZATION_DENIED).",
                            "422": "Invalid request (VALIDATION_FAILED).",
                            "500": "An unexpected error occurred.",
                        }[code],
                    }

    # Drop the now-unreferenced HTTPValidationError schema if nothing else
    # uses it (keeps the generated schema clean and truthful).
    if has_validation_error and "HTTPValidationError" in schemas:
        still_referenced = False
        for operations in schema.get("paths", {}).values():
            for operation in operations.values():
                for resp in operation.get("responses", {}).values():
                    content = resp.get("content", {})
                    for media in content.values():
                        if media.get("schema", {}).get("$ref", "").endswith(
                            "/HTTPValidationError"
                        ):
                            still_referenced = True
                            break
        if not still_referenced:
            del schemas["HTTPValidationError"]


def install_openapi_security(app: FastAPI) -> None:
    default_openapi = app.openapi
    patched = False

    def openapi() -> dict[str, Any]:
        nonlocal patched
        schema = default_openapi()
        if not patched:
            _merge_human_security(schema)
            _normalise_machine_error_responses(schema)
            patched = True
        return schema

    app.openapi = openapi  # type: ignore[method-assign]

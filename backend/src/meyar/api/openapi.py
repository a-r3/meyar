"""Truthful OpenAPI security for the two authentication models (issue #45).

FastAPI emits one security requirement per ``Security`` dependency, and the
OpenAPI specification reads a list of requirements as *alternatives* (OR). The
human surface needs the BrowserSession cookie AND, for mutations, the CSRF
header, so those are merged into a single requirement object (AND). The machine
surface keeps the single ``ApiKeyBearer`` scheme; the two are never mixed."""

from typing import Any

from fastapi import FastAPI

HUMAN_PATH_PREFIX = "/ui/api/v1"


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


def install_openapi_security(app: FastAPI) -> None:
    default_openapi = app.openapi
    patched = False

    def openapi() -> dict[str, Any]:
        nonlocal patched
        schema = default_openapi()
        if not patched:
            _merge_human_security(schema)
            patched = True
        return schema

    app.openapi = openapi  # type: ignore[method-assign]

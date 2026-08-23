"""Slice 12 — OpenAPI/Swagger completeness contract. See
docs/DECISIONS.md (offline Swagger UI) and .claude/rules/architecture.md
(OpenAPI is the canonical spec)."""

import re

from httpx import AsyncClient

_EXPECTED_PATHS = {
    "/api/v1/health",
    "/api/v1/usage",
    "/api/v1/jobs",
    "/api/v1/jobs/{job_id}",
    "/api/v1/jobs/{job_id}/criteria",
    "/api/v1/jobs/{job_id}/criteria/{version_number}",
    "/api/v1/candidates",
    "/api/v1/candidates/{candidate_id}",
    "/api/v1/candidates/{candidate_id}/detail",
    "/api/v1/candidates/{candidate_id}/documents",
    "/api/v1/candidates/{candidate_id}/documents/{document_id}",
    "/api/v1/search",
    "/api/v1/search/natural-language",
    "/api/v1/jobs/{job_id}/criteria/{version_number}/score",
    "/api/v1/jobs/{job_id}/criteria/{version_number}/rank",
}


async def test_all_expected_v1_routes_present(client: AsyncClient) -> None:
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    data = resp.json()
    assert _EXPECTED_PATHS.issubset(set(data["paths"].keys()))


async def test_ui_paths_absent_from_openapi(client: AsyncClient) -> None:
    resp = await client.get("/openapi.json")
    data = resp.json()
    ui_paths = [p for p in data["paths"] if p.startswith("/ui")]
    assert ui_paths == []


async def test_docs_route_and_asset_mount_absent_from_openapi(client: AsyncClient) -> None:
    resp = await client.get("/openapi.json")
    data = resp.json()
    assert "/docs" not in data["paths"]
    assert not any(p.startswith("/docs-assets") for p in data["paths"])


async def test_bearer_security_scheme_present(client: AsyncClient) -> None:
    resp = await client.get("/openapi.json")
    data = resp.json()
    schemes = data["components"]["securitySchemes"]
    assert any(scheme.get("scheme") == "bearer" for scheme in schemes.values())


async def test_protected_operations_declare_security_health_does_not(
    client: AsyncClient,
) -> None:
    resp = await client.get("/openapi.json")
    data = resp.json()
    for path, methods in data["paths"].items():
        for method, operation in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            if path == "/api/v1/health":
                continue
            assert "security" in operation, f"{method.upper()} {path} missing security"


async def test_operation_ids_are_unique(client: AsyncClient) -> None:
    resp = await client.get("/openapi.json")
    data = resp.json()
    op_ids = []
    for methods in data["paths"].values():
        for method, operation in methods.items():
            if method in {"get", "post", "put", "patch", "delete"}:
                op_ids.append(operation.get("operationId"))
    assert all(op_ids)
    assert len(op_ids) == len(set(op_ids))


async def test_docs_route_is_fully_offline(client: AsyncClient) -> None:
    resp = await client.get("/docs")
    assert resp.status_code == 200
    text = resp.text
    for forbidden in ("cdn.jsdelivr", "unpkg", "cdnjs"):
        assert forbidden not in text
    urls = re.findall(r'https?://[^"\'\s]+', text)
    assert urls == []


async def test_docs_assets_resolve_locally(client: AsyncClient) -> None:
    for asset in ("swagger-ui-bundle.js", "swagger-ui.css"):
        resp = await client.get(f"/docs-assets/{asset}")
        assert resp.status_code == 200

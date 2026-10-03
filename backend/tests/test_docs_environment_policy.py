"""The real app factory controls docs routing; all content is synthetic."""

import re

import pytest
from httpx import ASGITransport, AsyncClient

from meyar.config import Settings
from meyar.main import create_app


def _settings(environment):
    return Settings(
        _env_file=None, env=environment, pending_login_secret="synthetic-s3-only-secret",
        database_url="postgresql+asyncpg://synthetic:synthetic@127.0.0.1:5432/synthetic",
        ollama_model="synthetic-s3:v1", ollama_embedding_model="synthetic-s3-embed:v1",
    )


@pytest.mark.parametrize("environment", ["development", "test"])
async def test_nonproduction_offline_swagger_and_schema_are_local(environment):
    application = create_app(_settings(environment))
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as ac:
        docs = await ac.get("/docs")
        assert docs.status_code == 200
        assert re.findall(r'https?://[^"\'\s]+', docs.text) == []
        for asset in ("swagger-ui-bundle.js", "swagger-ui.css", "favicon-32x32.png"):
            assert f"/docs-assets/{asset}" in docs.text
            assert (await ac.get(f"/docs-assets/{asset}")).status_code == 200
        schema = await ac.get("/openapi.json")
        assert schema.status_code == 200
        assert "/api/v1/candidates/{candidate_id}/detail" in schema.json()["paths"]
        assert (await ac.get("/redoc")).status_code == 404


@pytest.mark.parametrize("path", [
    "/docs", "/docs/", "/openapi.json", "/redoc",
    "/docs-assets/swagger-ui-bundle.js", "/docs-assets/swagger-ui.css",
    "/docs-assets/favicon-32x32.png",
])
async def test_production_docs_schema_and_assets_are_ordinary_not_found(path):
    application = create_app(_settings("production"))
    assert application.openapi_url is None
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as ac:
        response = await ac.get(path)
        assert response.status_code == 404
        assert response.json() == {"detail": "Not Found"}
        assert response.content == (await ac.get("/ordinary-absent-path")).content
        health = await ac.get("/api/v1/health")
        assert health.status_code == 200
        assert health.headers["cache-control"] == "no-store"
        protected = await ac.get("/api/v1/usage")
        assert protected.status_code == 401
        assert protected.headers["x-content-type-options"] == "nosniff"

"""S3 response policy over real routes and ASGI streaming/file transports."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from test_demo_seed import _seed

from meyar.config import Settings, get_settings
from meyar.db import get_db
from meyar.main import app, create_app
from meyar.models.candidate import Candidate
from meyar.models.candidate_document import CandidateDocument
from meyar.models.job import Job
from meyar.services.api_key_repo import create_api_key
from meyar.services.tenant_authority import set_tenant_active


def _private(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_real_rest_candidate_search_evaluation_and_204_are_private(
    db_session, tmp_path, client
):
    summary = await _seed(db_session, tmp_path)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {summary.api_key_plaintext}"}
    candidate = (await db_session.scalars(select(Candidate))).first()
    job = (await db_session.scalars(select(Job))).first()
    detail = await client.get(f"/api/v1/candidates/{candidate.id}/detail", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["candidate_id"] == str(candidate.id)
    _private(detail)
    search = await client.post(
        "/api/v1/search", json={"mode": "STRUCTURED_ONLY"}, headers=headers
    )
    assert search.status_code == 200 and search.json()["result_count"] > 0
    _private(search)
    score = await client.post(
        f"/api/v1/jobs/{job.id}/criteria/1/score",
        json={"candidate_id": str(candidate.id), "evaluation_as_of_date": "2026-01-01"},
        headers=headers,
    )
    assert score.status_code == 200 and "numeric_score" in score.json()
    _private(score)
    document = (await db_session.scalars(select(CandidateDocument).where(
        CandidateDocument.candidate_id == candidate.id
    ))).first()
    metadata = await client.get(
        f"/api/v1/candidates/{candidate.id}/documents/{document.id}", headers=headers
    )
    assert metadata.status_code == 200
    _private(metadata)
    # Real original delivery is UI-only on current main, protected by human auth.
    app.dependency_overrides[get_settings] = lambda: Settings(ui_cookie_secure=False)
    login = await client.post("/ui/login", data={
        "username": summary.human_username, "password": summary.human_temp_password,
    })
    assert login.status_code == 303
    original = await client.get(f"/ui/candidates/{candidate.id}/documents/{document.id}/original")
    assert original.status_code == 200 and original.content[:2] == b"PK"
    _private(original)
    deleted = await client.delete(f"/api/v1/candidates/{candidate.id}", headers=headers)
    assert deleted.status_code == 204 and deleted.content == b""
    _private(deleted)


@pytest.mark.parametrize("status", [401, 403, 404, 422])
async def test_real_rest_safe_errors_are_private(client, db_session, tenant_and_key, status):
    tenant, _, key = tenant_and_key
    if status == 401:
        response = await client.get("/api/v1/usage")
    elif status == 403:
        _, limited = await create_api_key(
            db_session, tenant_id=tenant.id, env="test", scopes=["jobs:read"]
        )
        await db_session.commit()
        response = await client.post(
            "/api/v1/search", json={"mode": "STRUCTURED_ONLY"},
            headers={"Authorization": f"Bearer {limited}"},
        )
    else:
        target = str(uuid.uuid4()) if status == 404 else "invalid-uuid"
        response = await client.get(
            f"/api/v1/candidates/{target}/detail", headers={"Authorization": f"Bearer {key}"}
        )
    assert response.status_code == status
    _private(response)


async def test_inactive_tenant_generic_401_is_private(client, db_session, tenant_and_key):
    tenant, _, key = tenant_and_key
    await set_tenant_active(db_session, tenant_id=tenant.id, is_active=False)
    await db_session.commit()
    response = await client.get("/api/v1/usage", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API key."}
    _private(response)


async def test_unexpected_safe_500_is_private(monkeypatch):
    async def broken_db():
        raise RuntimeError("SYNTHETIC_PRIVATE_S3")
        yield  # noqa: B018

    monkeypatch.setitem(app.dependency_overrides, get_db, broken_db)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/usage", headers={"Authorization": "Bearer synthetic"})
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    _private(response)


@pytest.mark.parametrize("transport", ["stream", "file"])
async def test_api_stream_and_file_bytes_receive_policy(tmp_path, transport):
    application = create_app(Settings(_env_file=None, env="test"))
    original = b"%PDF-1.4\nSYNTHETIC ORIGINAL CV S3\n%%EOF"
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(original)

    async def download():
        if transport == "file":
            return FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})

        async def chunks():
            yield original[:9]
            yield original[9:]

        return StreamingResponse(chunks(), media_type="application/pdf")

    application.add_api_route("/api/v1/synthetic-original", download, methods=["GET"])
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as ac:
        response = await ac.get("/api/v1/synthetic-original")
    assert response.status_code == 200 and response.content == original
    _private(response)


async def test_early_upload_413_receives_policy(client, monkeypatch):
    monkeypatch.setitem(
        app.dependency_overrides, get_settings, lambda: Settings(max_upload_bytes=1)
    )
    response = await client.post(
        f"/api/v1/candidates/{uuid.uuid4()}/documents",
        content=b"synthetic", headers={"content-length": "999999"},
    )
    assert response.status_code == 413
    _private(response)


async def test_unexpected_upload_limiter_failure_is_safe_and_private(monkeypatch):
    def broken_settings():
        raise ValueError("SYNTHETIC_PRIVATE_LIMITER_S3")

    monkeypatch.setitem(app.dependency_overrides, get_settings, broken_settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.post(f"/api/v1/candidates/{uuid.uuid4()}/documents", content=b"x")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    _private(response)


async def test_ui_headers_and_static_cache_behavior_remain_unchanged(client):
    for path in ("/ui/login", "/ui/static/styles.css"):
        response = await client.get(path)
        assert response.status_code == 200
        _private(response)
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "default-src 'self'" in response.headers["content-security-policy"]
    swagger = await client.get("/docs-assets/swagger-ui.css")
    assert swagger.status_code == 200
    assert "no-store" not in swagger.headers.get("cache-control", "")
    assert swagger.headers.get("etag")


async def test_api_policy_does_not_match_neighboring_path_prefixes():
    application = create_app(Settings(_env_file=None, env="test"))

    async def public():
        return JSONResponse({}, headers={"Cache-Control": "public, max-age=60"})

    application.add_api_route("/api/v10/public", public, methods=["GET"])
    async with AsyncClient(transport=ASGITransport(app=application), base_url="http://test") as ac:
        response = await ac.get("/api/v10/public")
        root = await ac.get("/api/v1")
    assert response.headers["cache-control"] == "public, max-age=60"
    assert "x-content-type-options" not in response.headers
    assert root.status_code == 404
    _private(root)

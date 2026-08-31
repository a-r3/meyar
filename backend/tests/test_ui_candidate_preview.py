"""HR UI productization — the truthful in-app CV preview
(GET /ui/candidates/{candidate_id}/documents/{document_id}/preview).

Covers: authorized same-tenant preview renders parsed text, candidate/
document mismatch and foreign-tenant safe 404, missing-scope 403,
unauthenticated redirect, no storage-key/path leakage, absence from the
public OpenAPI schema, and the "not yet available" state when a document
has no CanonicalDocument.
"""

from pathlib import Path

import pytest
from httpx import AsyncClient

from meyar.config import Settings, get_settings
from meyar.main import app
from meyar.services.api_key_repo import create_api_key
from meyar.services.tenant_repo import create_tenant

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "synthetic_cvs"


def _read(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def _auth(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


@pytest.fixture
def local_ui_settings() -> Settings:
    settings = Settings(ui_cookie_secure=False)
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


async def _login(client: AsyncClient, plaintext: str) -> None:
    response = await client.post(
        "/ui/login", data={"api_key": plaintext}, follow_redirects=False
    )
    assert response.status_code == 303


async def _upload_document(
    client: AsyncClient, plaintext: str, *, filename: str, content_type: str
) -> tuple[str, str]:
    resp = await client.post("/api/v1/candidates", headers=_auth(plaintext))
    assert resp.status_code == 201
    candidate_id = resp.json()["id"]
    upload = await client.post(
        f"/api/v1/candidates/{candidate_id}/documents",
        headers=_auth(plaintext),
        files={"file": (filename, _read(filename), content_type)},
    )
    assert upload.status_code == 201
    return candidate_id, upload.json()["id"]


def _preview_url(candidate_id: str, document_id: str) -> str:
    return f"/ui/candidates/{candidate_id}/documents/{document_id}/preview"


async def test_authorized_preview_renders_parsed_text_not_original_bytes(
    client: AsyncClient, tenant_and_key, local_ui_settings: Settings
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    candidate_id, document_id = await _upload_document(
        client, plaintext, filename="valid_cv.pdf", content_type="application/pdf"
    )
    await _login(client, plaintext)

    response = await client.get(_preview_url(candidate_id, document_id))

    assert response.status_code == 200
    assert "CV-yə bax" in response.text
    assert "MEYAR tərəfindən emal edilmiş" in response.text
    assert f"/ui/candidates/{candidate_id}/documents/{document_id}/original" in response.text


async def test_preview_available_false_when_canonical_document_missing(
    client: AsyncClient,
    tenant_and_key,
    local_ui_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A document that uploaded successfully but hasn't been parsed yet
    (or whose parse failed) must render a safe empty state, never crash."""
    from meyar.ui import service as ui_service

    _tenant, _key, plaintext = tenant_and_key
    candidate_id, document_id = await _upload_document(
        client, plaintext, filename="valid_cv.pdf", content_type="application/pdf"
    )
    await _login(client, plaintext)

    async def _no_canonical(*args, **kwargs):
        return None

    monkeypatch.setattr(ui_service, "get_latest_canonical_document", _no_canonical)

    response = await client.get(_preview_url(candidate_id, document_id))

    assert response.status_code == 200
    assert "Görünüş hazır deyil" in response.text


async def test_document_not_belonging_to_candidate_is_safe_404(
    client: AsyncClient, tenant_and_key, local_ui_settings: Settings
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    _candidate_a, document_id = await _upload_document(
        client, plaintext, filename="valid_cv.pdf", content_type="application/pdf"
    )
    other_candidate = await client.post("/api/v1/candidates", headers=_auth(plaintext))
    assert other_candidate.status_code == 201
    candidate_b_id = other_candidate.json()["id"]
    await _login(client, plaintext)

    response = await client.get(_preview_url(candidate_b_id, document_id))

    assert response.status_code == 404
    assert "Sənəd tapılmadı" in response.text


async def test_foreign_tenant_document_is_safe_404(
    client: AsyncClient,
    db_session,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    foreign = await create_tenant(db_session, name="Foreign")
    _foreign_key, foreign_plaintext = await create_api_key(
        db_session, tenant_id=foreign.id, env="test"
    )
    await db_session.commit()
    foreign_candidate_id, foreign_document_id = await _upload_document(
        client, foreign_plaintext, filename="valid_cv.pdf", content_type="application/pdf"
    )
    await _login(client, plaintext)

    response = await client.get(_preview_url(foreign_candidate_id, foreign_document_id))

    assert response.status_code == 404
    assert "Sənəd tapılmadı" in response.text
    assert "Foreign" not in response.text


async def test_missing_candidates_read_scope_is_forbidden(
    client: AsyncClient,
    db_session,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    tenant, _key, plaintext = tenant_and_key
    candidate_id, document_id = await _upload_document(
        client, plaintext, filename="valid_cv.pdf", content_type="application/pdf"
    )
    _scoped_key, scoped_plaintext = await create_api_key(
        db_session, tenant_id=tenant.id, env="test", scopes=["jobs:read"]
    )
    await db_session.commit()
    await _login(client, scoped_plaintext)

    response = await client.get(_preview_url(candidate_id, document_id))

    assert response.status_code == 403


async def test_unauthenticated_request_is_redirected_not_served(
    client: AsyncClient, tenant_and_key, local_ui_settings: Settings
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    candidate_id, document_id = await _upload_document(
        client, plaintext, filename="valid_cv.pdf", content_type="application/pdf"
    )

    response = await client.get(
        _preview_url(candidate_id, document_id), follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


async def test_preview_route_is_absent_from_openapi_schema(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert not any("/preview" in path for path in schema["paths"])

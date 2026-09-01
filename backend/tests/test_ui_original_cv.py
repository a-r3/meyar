"""Slice 13 — authorized original-CV retrieval
(GET /ui/candidates/{candidate_id}/documents/{document_id}/original).

Covers: same-tenant PDF/DOCX success, always-attachment disposition (a
true download regardless of MIME type — the in-app text view is the
separate /preview route), stored MIME type used, candidate/document
mismatch and foreign-tenant safe 404,
missing-scope 403, unauthenticated redirect, session revocation, no
storage key/path leakage, synthetic filename only, and absence from the
public OpenAPI schema.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.config import Settings, get_settings
from meyar.main import app
from meyar.models.api_key import ApiKey
from meyar.models.user import User
from meyar.services.api_key_repo import create_api_key
from meyar.services.tenant_membership_repo import create_membership
from meyar.services.tenant_repo import create_tenant
from meyar.services.user_repo import create_user

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "synthetic_cvs"

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _read(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def _auth(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


@pytest.fixture
def local_ui_settings() -> Settings:
    settings = Settings(ui_cookie_secure=False)
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


async def _login(client: AsyncClient, username: str, password: str) -> None:
    response = await client.post(
        "/ui/login",
        data={"username": username, "password": password},
        follow_redirects=False,
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


def _original_url(candidate_id: str, document_id: str) -> str:
    return f"/ui/candidates/{candidate_id}/documents/{document_id}/original"


async def test_authorized_pdf_download_succeeds_as_attachment_with_stored_mime(
    client: AsyncClient, tenant_key_and_user, local_ui_settings: Settings
) -> None:
    """'Originalı yüklə' must truthfully download, not silently open the PDF
    inline in the browser tab — the safe in-app view is the separate
    /preview route. Regression for owner visual-inspection Blocker 7."""
    _tenant, _api_key, plaintext, user, password, _membership = tenant_key_and_user
    candidate_id, document_id = await _upload_document(
        client, plaintext, filename="valid_cv.pdf", content_type="application/pdf"
    )
    await _login(client, user.username, password)

    response = await client.get(_original_url(candidate_id, document_id))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content == _read("valid_cv.pdf")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f"cv-{document_id.replace('-', '')}.pdf" in disposition


async def test_authorized_docx_open_succeeds_as_attachment(
    client: AsyncClient, tenant_key_and_user, local_ui_settings: Settings
) -> None:
    _tenant, _api_key, plaintext, user, password, _membership = tenant_key_and_user
    candidate_id, document_id = await _upload_document(
        client, plaintext, filename="valid_cv.docx", content_type=DOCX_MIME
    )
    await _login(client, user.username, password)

    response = await client.get(_original_url(candidate_id, document_id))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(DOCX_MIME)
    assert response.content == _read("valid_cv.docx")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert f"cv-{document_id.replace('-', '')}.docx" in disposition


async def test_response_never_leaks_storage_key_or_filesystem_path(
    client: AsyncClient, tenant_key_and_user, local_ui_settings: Settings
) -> None:
    _tenant, _api_key, plaintext, user, password, _membership = tenant_key_and_user
    candidate_id, document_id = await _upload_document(
        client, plaintext, filename="valid_cv.pdf", content_type="application/pdf"
    )
    await _login(client, user.username, password)

    response = await client.get(_original_url(candidate_id, document_id))

    assert response.status_code == 200
    for header_value in response.headers.values():
        assert "/storage/" not in header_value
        assert str(candidate_id) not in header_value
    # only the opaque document UUID may appear (in the synthetic filename) —
    # never a storage_key segment or an absolute/relative filesystem path.
    disposition = response.headers["content-disposition"]
    assert "/" not in disposition.split("filename=")[1]


async def test_document_not_belonging_to_candidate_is_safe_404(
    client: AsyncClient, tenant_key_and_user, local_ui_settings: Settings
) -> None:
    _tenant, _api_key, plaintext, user, password, _membership = tenant_key_and_user
    _candidate_a, document_id = await _upload_document(
        client, plaintext, filename="valid_cv.pdf", content_type="application/pdf"
    )
    other_candidate = await client.post("/api/v1/candidates", headers=_auth(plaintext))
    assert other_candidate.status_code == 201
    candidate_b_id = other_candidate.json()["id"]
    await _login(client, user.username, password)

    response = await client.get(_original_url(candidate_b_id, document_id))

    assert response.status_code == 404
    assert "Sənəd tapılmadı" in response.text


async def test_foreign_tenant_document_is_safe_404(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_key_and_user,
    local_ui_settings: Settings,
) -> None:
    _tenant, _api_key, plaintext, user, password, _membership = tenant_key_and_user
    foreign = await create_tenant(db_session, name="Foreign")
    _foreign_key, foreign_plaintext = await create_api_key(
        db_session, tenant_id=foreign.id, env="test"
    )
    await db_session.commit()
    foreign_candidate_id, foreign_document_id = await _upload_document(
        client, foreign_plaintext, filename="valid_cv.pdf", content_type="application/pdf"
    )
    await _login(client, user.username, password)

    response = await client.get(_original_url(foreign_candidate_id, foreign_document_id))

    assert response.status_code == 404
    assert "Sənəd tapılmadı" in response.text
    assert "Foreign" not in response.text


async def test_missing_candidates_read_scope_is_forbidden(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_key_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, _api_key, plaintext, _user, _password, _membership = tenant_key_and_user
    candidate_id, document_id = await _upload_document(
        client, plaintext, filename="valid_cv.pdf", content_type="application/pdf"
    )
    # meyar.core.roles.permissions_for_role fails closed on any role it
    # doesn't recognize, so an unrecognized role deterministically has
    # zero UI permissions — exercises the same require_ui_scopes
    # enforcement path a genuinely reduced-permission role would.
    restricted_user = await create_user(
        db_session, username="restricted-preview-user", plaintext_password="restricted-password-1"
    )
    await create_membership(
        db_session, user_id=restricted_user.id, tenant_id=tenant.id, role="NO_PERMISSIONS"
    )
    await db_session.commit()
    await _login(client, restricted_user.username, "restricted-password-1")

    response = await client.get(_original_url(candidate_id, document_id))

    assert response.status_code == 403


async def test_unauthenticated_request_is_redirected_not_served(
    client: AsyncClient, tenant_key_and_user, local_ui_settings: Settings
) -> None:
    _tenant, _api_key, plaintext, user, password, _membership = tenant_key_and_user
    candidate_id, document_id = await _upload_document(
        client, plaintext, filename="valid_cv.pdf", content_type="application/pdf"
    )

    response = await client.get(
        _original_url(candidate_id, document_id), follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


async def test_revoking_the_seeding_api_key_never_affects_the_independent_ui_session(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_key_and_user,
    local_ui_settings: Settings,
) -> None:
    """Dual access must stay real: a human's BrowserSession is no longer
    bridged through any ApiKey row, so revoking the machine credential
    used only to seed this document must have zero effect on the already
    -established, fully independent human UI session."""
    _tenant, api_key, plaintext, user, password, _membership = tenant_key_and_user
    candidate_id, document_id = await _upload_document(
        client, plaintext, filename="valid_cv.pdf", content_type="application/pdf"
    )
    await _login(client, user.username, password)
    await db_session.execute(
        ApiKey.__table__.update()
        .where(ApiKey.id == api_key.id)
        .values(revoked_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    await db_session.commit()

    response = await client.get(_original_url(candidate_id, document_id))

    assert response.status_code == 200


async def test_disabled_user_blocks_a_previously_valid_ui_session(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_key_and_user,
    local_ui_settings: Settings,
) -> None:
    _tenant, _api_key, plaintext, user, password, _membership = tenant_key_and_user
    candidate_id, document_id = await _upload_document(
        client, plaintext, filename="valid_cv.pdf", content_type="application/pdf"
    )
    await _login(client, user.username, password)
    await db_session.execute(
        User.__table__.update().where(User.id == user.id).values(is_active=False)
    )
    await db_session.commit()

    response = await client.get(
        _original_url(candidate_id, document_id), follow_redirects=False
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


async def test_original_cv_route_is_absent_from_openapi_schema(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert not any("/original" in path for path in schema["paths"])
    assert not any(path.startswith("/ui/") for path in schema["paths"])

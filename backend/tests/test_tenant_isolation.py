"""Cross-tenant isolation tests. See docs/SECURITY_PRIVACY.md testing
priority #1: tenant A must never be able to access tenant B's resources."""

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.services.api_key_repo import create_api_key
from meyar.services.tenant_repo import create_tenant


async def test_usage_never_leaks_another_tenants_data(
    client: AsyncClient, db_session: AsyncSession, tenant_and_key
) -> None:
    tenant_a, _key_a, plaintext_a = tenant_and_key
    tenant_b = await create_tenant(db_session, name="Tenant B")
    _key_b, plaintext_b = await create_api_key(db_session, tenant_id=tenant_b.id, env="test")
    await db_session.commit()

    resp_a = await client.get("/api/v1/usage", headers={"Authorization": f"Bearer {plaintext_a}"})
    resp_b = await client.get("/api/v1/usage", headers={"Authorization": f"Bearer {plaintext_b}"})

    assert resp_a.json()["tenant_id"] == str(tenant_a.id)
    assert resp_b.json()["tenant_id"] == str(tenant_b.id)
    assert resp_a.json()["tenant_id"] != resp_b.json()["tenant_id"]


async def test_tenant_cannot_use_another_tenants_key_identity(
    db_session: AsyncSession, tenant_and_key
) -> None:
    """The tenant identity returned by auth is derived solely from the
    matched API key's own tenant_id column — there is no client-suppliable
    tenant parameter anywhere in the auth path to spoof."""
    from meyar.services.api_key_repo import get_api_key_by_plaintext

    tenant_a, _key_a, plaintext_a = tenant_and_key
    resolved = await get_api_key_by_plaintext(db_session, plaintext_a)
    assert resolved is not None
    assert resolved.tenant_id == tenant_a.id


async def test_tenant_b_cannot_read_tenant_as_job(
    client: AsyncClient, db_session: AsyncSession, tenant_and_key
) -> None:
    _tenant_a, _key_a, plaintext_a = tenant_and_key
    tenant_b = await create_tenant(db_session, name="Tenant B")
    _key_b, plaintext_b = await create_api_key(db_session, tenant_id=tenant_b.id, env="test")
    await db_session.commit()

    create_resp = await client.post(
        "/api/v1/jobs",
        headers={"Authorization": f"Bearer {plaintext_a}"},
        json={
            "title": "Tenant A's confidential role",
            "criteria": [
                {
                    "id": "python_exp",
                    "kind": "SKILL",
                    "type": "MUST_HAVE",
                    "label": "Python",
                    "value": "Python",
                }
            ],
        },
    )
    assert create_resp.status_code == 201
    job_id = create_resp.json()["id"]

    # tenant B must not be able to read tenant A's job — 404, not 403,
    # so existence is never leaked across tenants.
    leak_resp = await client.get(
        f"/api/v1/jobs/{job_id}", headers={"Authorization": f"Bearer {plaintext_b}"}
    )
    assert leak_resp.status_code == 404

    # nor add a new criteria version to it
    leak_write_resp = await client.post(
        f"/api/v1/jobs/{job_id}/criteria",
        headers={"Authorization": f"Bearer {plaintext_b}"},
        json={
            "criteria": [
                {
                    "id": "sql_exp",
                    "kind": "SKILL",
                    "type": "MUST_HAVE",
                    "label": "SQL",
                    "value": "SQL",
                }
            ]
        },
    )
    assert leak_write_resp.status_code == 404

    # nor list/read its criteria history
    leak_list_resp = await client.get(
        f"/api/v1/jobs/{job_id}/criteria", headers={"Authorization": f"Bearer {plaintext_b}"}
    )
    assert leak_list_resp.status_code == 404


async def test_tenant_b_cannot_access_tenant_as_candidate(
    client: AsyncClient, db_session: AsyncSession, tenant_and_key
) -> None:
    from pathlib import Path

    fixtures_dir = Path(__file__).resolve().parent.parent.parent / "fixtures" / "synthetic_cvs"
    pdf_bytes = (fixtures_dir / "valid_cv.pdf").read_bytes()

    _tenant_a, _key_a, plaintext_a = tenant_and_key
    tenant_b = await create_tenant(db_session, name="Tenant B")
    _key_b, plaintext_b = await create_api_key(db_session, tenant_id=tenant_b.id, env="test")
    await db_session.commit()

    auth_a = {"Authorization": f"Bearer {plaintext_a}"}
    auth_b = {"Authorization": f"Bearer {plaintext_b}"}

    create_resp = await client.post("/api/v1/candidates", headers=auth_a)
    assert create_resp.status_code == 201
    candidate_id = create_resp.json()["id"]

    upload_resp = await client.post(
        f"/api/v1/candidates/{candidate_id}/documents",
        headers=auth_a,
        files={"file": ("valid_cv.pdf", pdf_bytes, "application/pdf")},
    )
    assert upload_resp.status_code == 201
    document_id = upload_resp.json()["id"]

    # tenant B must not be able to read tenant A's candidate
    read_leak = await client.get(f"/api/v1/candidates/{candidate_id}", headers=auth_b)
    assert read_leak.status_code == 404

    # nor upload a document to it
    upload_leak = await client.post(
        f"/api/v1/candidates/{candidate_id}/documents",
        headers=auth_b,
        files={"file": ("valid_cv.pdf", pdf_bytes, "application/pdf")},
    )
    assert upload_leak.status_code == 404

    # nor list its documents
    list_leak = await client.get(f"/api/v1/candidates/{candidate_id}/documents", headers=auth_b)
    assert list_leak.status_code == 404

    # nor read a specific document's metadata
    doc_leak = await client.get(
        f"/api/v1/candidates/{candidate_id}/documents/{document_id}", headers=auth_b
    )
    assert doc_leak.status_code == 404

    # nor delete tenant A's candidate
    delete_leak = await client.delete(f"/api/v1/candidates/{candidate_id}", headers=auth_b)
    assert delete_leak.status_code == 404

    # tenant A's candidate must still exist, untouched by tenant B's attempt
    still_there = await client.get(f"/api/v1/candidates/{candidate_id}", headers=auth_a)
    assert still_there.status_code == 200

"""Slice 12 — REST tests for GET /api/v1/candidates/{id}/detail.
Synthetic fixtures only. See .claude/rules/testing.md."""

from httpx import AsyncClient
from search_helpers import seed_candidate_with_profile
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.services.tenant_repo import create_tenant

EVIDENCE = [{"page": 1, "block_index": 0, "quote": "Synthetic evidence"}]
PROFILE = {
    "skills": [{"name": "Python", "category": "Backend", "evidence": EVIDENCE}],
    "employment_history": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "projects": [],
}


def _auth(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


async def test_candidate_detail_requires_auth(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/candidates/00000000-0000-0000-0000-000000000000/detail"
    )
    assert resp.status_code == 401


async def test_candidate_detail_not_found_is_404(client: AsyncClient, tenant_and_key) -> None:
    _tenant, _key, plaintext = tenant_and_key
    resp = await client.get(
        "/api/v1/candidates/00000000-0000-0000-0000-000000000000/detail",
        headers=_auth(plaintext),
    )
    assert resp.status_code == 404


async def test_candidate_detail_returns_privacy_safe_shape(
    db_session: AsyncSession, client: AsyncClient, tenant_and_key
) -> None:
    tenant, _key, plaintext = tenant_and_key
    candidate, _profile = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=PROFILE
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/candidates/{candidate.id}/detail", headers=_auth(plaintext)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["candidate_id"] == str(candidate.id)
    assert body["skills"][0]["title"] == "Python"
    # No raw CV bytes, storage paths, prompts, or embedding vectors ever
    # leave this endpoint.
    forbidden_keys = {"storage_key", "storage_path", "prompt", "embedding", "vector"}
    assert forbidden_keys.isdisjoint(body.keys())
    assert "storage" not in resp.text.lower()


async def test_candidate_detail_cross_tenant_is_safe_404(
    db_session: AsyncSession, client: AsyncClient, tenant_and_key
) -> None:
    tenant, _key, plaintext = tenant_and_key
    foreign_tenant = await create_tenant(db_session, name="Foreign")
    foreign_candidate, _fp = await seed_candidate_with_profile(
        db_session, tenant_id=foreign_tenant.id, profile_content=PROFILE
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/candidates/{foreign_candidate.id}/detail", headers=_auth(plaintext)
    )
    assert resp.status_code == 404
    assert str(foreign_candidate.id) not in resp.text

"""Slice 12 — REST tests for POST /api/v1/search (structured/hybrid).
Synthetic fixtures only. See .claude/rules/testing.md."""

import uuid

import pytest
from fakes import FakeEmbeddingProvider
from httpx import AsyncClient
from search_helpers import seed_candidate_with_profile, synthetic_evidence
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.embedding.dependency import get_embedding_provider
from meyar.embedding.provider import EmbeddingUnavailableError
from meyar.main import app
from meyar.services.api_key_repo import create_api_key
from meyar.services.tenant_repo import create_tenant

EVIDENCE = [{"page": 1, "block_index": 0, "quote": "Synthetic evidence"}]
EMPTY_PROFILE = {
    "skills": [],
    "employment_history": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "projects": [],
}


def _auth(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


def _profile(*skills: str) -> dict:
    return {
        **EMPTY_PROFILE,
        "skills": [
            {"name": skill, "category": "Backend", "evidence": synthetic_evidence(skill, "Backend")}
            for skill in skills
        ],
    }


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


async def test_search_requires_auth(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/search", json={"mode": "STRUCTURED_ONLY"})
    assert resp.status_code == 401


async def test_search_requires_candidates_read_scope(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    tenant = await create_tenant(db_session, name="Scope-tenant")
    _api_key, plaintext = await create_api_key(
        db_session, tenant_id=tenant.id, env="test", scopes=["jobs:read"]
    )
    await db_session.commit()
    resp = await client.post(
        "/api/v1/search", json={"mode": "STRUCTURED_ONLY"}, headers=_auth(plaintext)
    )
    assert resp.status_code == 403


async def test_structured_only_never_touches_embedding_provider(
    db_session: AsyncSession, client: AsyncClient, tenant_and_key
) -> None:
    tenant, _key, plaintext = tenant_and_key
    candidate, _profile_row = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    failing_provider = FakeEmbeddingProvider(
        error=EmbeddingUnavailableError("must not be called")
    )
    app.dependency_overrides[get_embedding_provider] = lambda: failing_provider

    resp = await client.post(
        "/api/v1/search",
        json={"mode": "STRUCTURED_ONLY", "required_filters": {"skills": ["Python"]}},
        headers=_auth(plaintext),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result_count"] == 1
    assert body["results"][0]["candidate_id"] == str(candidate.id)
    assert failing_provider.call_count == 0


async def test_structured_search_result_shape(
    db_session: AsyncSession, client: AsyncClient, tenant_and_key
) -> None:
    tenant, _key, plaintext = tenant_and_key
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    resp = await client.post(
        "/api/v1/search",
        json={"mode": "STRUCTURED_ONLY", "required_filters": {"skills": ["Python"]}},
        headers=_auth(plaintext),
    )
    assert resp.status_code == 200
    body = resp.json()
    result = body["results"][0]
    assert set(
        [
            "candidate_id",
            "full_name",
            "rank",
            "mode",
            "relevance_score",
            "structured_score",
            "semantic_score",
            "required_filters_matched",
            "preferred_filters_matched",
            "candidate_profile_version_id",
            "search_policy_version",
        ]
    ).issubset(result.keys())


async def test_tenant_isolation_structured_search(
    db_session: AsyncSession, client: AsyncClient, tenant_and_key
) -> None:
    tenant, _key, plaintext = tenant_and_key
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    foreign_tenant = await create_tenant(db_session, name="Foreign")
    foreign_candidate, _fp = await seed_candidate_with_profile(
        db_session, tenant_id=foreign_tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    resp = await client.post(
        "/api/v1/search",
        json={"mode": "STRUCTURED_ONLY", "required_filters": {"skills": ["Python"]}},
        headers=_auth(plaintext),
    )
    assert resp.status_code == 200
    ids = {item["candidate_id"] for item in resp.json()["results"]}
    assert str(foreign_candidate.id) not in ids


@pytest.mark.parametrize(
    "smuggled_field",
    ["embedding_config", "structured_weight", "semantic_weight"],
)
async def test_client_cannot_smuggle_trusted_only_fields(
    client: AsyncClient, tenant_and_key, smuggled_field: str
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    payload = {"mode": "STRUCTURED_ONLY", smuggled_field: 0.9}
    resp = await client.post("/api/v1/search", json=payload, headers=_auth(plaintext))
    assert resp.status_code == 422


async def test_no_results_returns_empty_list(client: AsyncClient, tenant_and_key) -> None:
    _tenant, _key, plaintext = tenant_and_key
    resp = await client.post(
        "/api/v1/search",
        json={"mode": "STRUCTURED_ONLY", "required_filters": {"skills": [str(uuid.uuid4())]}},
        headers=_auth(plaintext),
    )
    assert resp.status_code == 200
    assert resp.json()["result_count"] == 0

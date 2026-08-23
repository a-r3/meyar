"""Slice 12 — REST tests proving /api/v1/usage reports truthful,
tenant-scoped counts (no more hardcoded zero placeholder)."""

from datetime import date

from httpx import AsyncClient
from search_helpers import seed_candidate_with_profile
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.evaluation.service import evaluate_and_score_candidate
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
from meyar.services.job_criteria_repo import create_criteria_version
from meyar.services.job_repo import create_job
from meyar.services.tenant_repo import create_tenant

EVIDENCE = [{"page": 1, "block_index": 0, "quote": "Synthetic"}]
PROFILE = {
    "skills": [{"name": "Python", "evidence": EVIDENCE}],
    "employment_history": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "projects": [],
}


def _auth(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


async def test_fresh_tenant_usage_is_genuinely_zero(client: AsyncClient, tenant_and_key) -> None:
    _tenant, _key, plaintext = tenant_and_key
    resp = await client.get("/api/v1/usage", headers=_auth(plaintext))
    assert resp.status_code == 200
    body = resp.json()
    assert body["candidates_count"] == 0
    assert body["evaluations_count"] == 0
    assert body["period"] == "all_time"


async def test_usage_reflects_created_candidate_and_evaluation(
    db_session: AsyncSession, client: AsyncClient, tenant_and_key
) -> None:
    tenant, _key, plaintext = tenant_and_key
    candidate, profile = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=PROFILE
    )
    job = await create_job(db_session, tenant_id=tenant.id, title="Usage Job")
    criteria = await create_criteria_version(
        db_session,
        tenant_id=tenant.id,
        job_id=job.id,
        criteria=[
            CriterionIn(
                id="python",
                kind=CriterionKind.SKILL,
                type=CriterionType.MUST_HAVE,
                label="Python",
                value="Python",
                weight=1.0,
            ).model_dump(mode="json")
        ],
        created_by_api_key_id=None,
    )
    await db_session.commit()

    await evaluate_and_score_candidate(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_profile_version_id=profile.id,
        job_id=job.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=date(2026, 1, 1),
    )
    await db_session.commit()

    resp = await client.get("/api/v1/usage", headers=_auth(plaintext))
    assert resp.status_code == 200
    body = resp.json()
    assert body["candidates_count"] == 1
    assert body["evaluations_count"] == 1


async def test_usage_never_counts_other_tenant_activity(
    db_session: AsyncSession, client: AsyncClient, tenant_and_key
) -> None:
    tenant, _key, plaintext = tenant_and_key
    foreign_tenant = await create_tenant(db_session, name="Foreign")
    await seed_candidate_with_profile(
        db_session, tenant_id=foreign_tenant.id, profile_content=PROFILE
    )
    await db_session.commit()

    resp = await client.get("/api/v1/usage", headers=_auth(plaintext))
    assert resp.status_code == 200
    body = resp.json()
    assert body["candidates_count"] == 0
    assert body["evaluations_count"] == 0

"""Slice 12 — REST tests for score/rank endpoints. Synthetic fixtures
only. See .claude/rules/testing.md."""

from datetime import date
from decimal import Decimal

from httpx import AsyncClient
from search_helpers import seed_candidate_with_profile
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
from meyar.services.api_key_repo import create_api_key
from meyar.services.job_criteria_repo import create_criteria_version
from meyar.services.job_repo import create_job
from meyar.services.tenant_repo import create_tenant

AS_OF = "2026-01-01"
AS_OF_DATE = date(2026, 1, 1)
EVIDENCE = [{"page": 1, "block_index": 0, "quote": "Synthetic"}]
EMPTY = {
    "skills": [],
    "employment_history": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "projects": [],
}


def _auth(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


def _skill(criterion_id: str, value: str, *, weight: float, criterion_type: CriterionType):
    return CriterionIn(
        id=criterion_id,
        kind=CriterionKind.SKILL,
        type=criterion_type,
        label=value,
        value=value,
        weight=weight,
    )


def _profile(*skills: str) -> dict:
    return {**EMPTY, "skills": [{"name": skill, "evidence": EVIDENCE} for skill in skills]}


async def _job_with_criteria(db: AsyncSession, tenant_id, criteria: list[CriterionIn]):
    job = await create_job(db, tenant_id=tenant_id, title="API Score Job")
    version = await create_criteria_version(
        db,
        tenant_id=tenant_id,
        job_id=job.id,
        criteria=[item.model_dump(mode="json") for item in criteria],
        created_by_api_key_id=None,
    )
    return job, version


async def test_score_missing_as_of_date_rejected(client: AsyncClient, tenant_and_key) -> None:
    _tenant, _key, plaintext = tenant_and_key
    resp = await client.post(
        "/api/v1/jobs/00000000-0000-0000-0000-000000000000/criteria/1/score",
        json={"candidate_id": "00000000-0000-0000-0000-000000000000"},
        headers=_auth(plaintext),
    )
    assert resp.status_code == 422


async def test_score_idempotency_and_decimal_contract(
    db_session: AsyncSession, client: AsyncClient, tenant_and_key
) -> None:
    tenant, _key, plaintext = tenant_and_key
    candidate, _profile_row = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    job, criteria = await _job_with_criteria(
        db_session,
        tenant.id,
        [_skill("python", "Python", weight=2.0, criterion_type=CriterionType.MUST_HAVE)],
    )
    await db_session.commit()

    payload = {"candidate_id": str(candidate.id), "evaluation_as_of_date": AS_OF}
    resp1 = await client.post(
        f"/api/v1/jobs/{job.id}/criteria/{criteria.version_number}/score",
        json=payload,
        headers=_auth(plaintext),
    )
    assert resp1.status_code == 200
    body1 = resp1.json()
    assert body1["reused"] is False
    assert body1["numeric_score"] == "100.00"
    assert isinstance(body1["numeric_score"], str)
    assert body1["explanation"]["numeric_score"] == "100.00"

    resp2 = await client.post(
        f"/api/v1/jobs/{job.id}/criteria/{criteria.version_number}/score",
        json=payload,
        headers=_auth(plaintext),
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["reused"] is True
    assert body2["evaluation_id"] == body1["evaluation_id"]
    assert body2["numeric_score"] == "100.00"


async def test_score_requires_evaluations_read_scope(
    db_session: AsyncSession, client: AsyncClient, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    candidate, _profile_row = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    job, criteria = await _job_with_criteria(
        db_session,
        tenant.id,
        [_skill("python", "Python", weight=2.0, criterion_type=CriterionType.MUST_HAVE)],
    )
    _key2, plaintext_no_scope = await create_api_key(
        db_session, tenant_id=tenant.id, env="test", scopes=["jobs:read"]
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/jobs/{job.id}/criteria/{criteria.version_number}/score",
        json={"candidate_id": str(candidate.id), "evaluation_as_of_date": AS_OF},
        headers=_auth(plaintext_no_scope),
    )
    assert resp.status_code == 403


async def test_score_cross_tenant_candidate_is_safe_404(
    db_session: AsyncSession, client: AsyncClient, tenant_and_key
) -> None:
    tenant, _key, plaintext = tenant_and_key
    job, criteria = await _job_with_criteria(
        db_session,
        tenant.id,
        [_skill("python", "Python", weight=2.0, criterion_type=CriterionType.MUST_HAVE)],
    )
    foreign_tenant = await create_tenant(db_session, name="Foreign")
    foreign_candidate, _fp = await seed_candidate_with_profile(
        db_session, tenant_id=foreign_tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/jobs/{job.id}/criteria/{criteria.version_number}/score",
        json={"candidate_id": str(foreign_candidate.id), "evaluation_as_of_date": AS_OF},
        headers=_auth(plaintext),
    )
    assert resp.status_code == 404
    assert str(foreign_candidate.id) not in resp.text


async def test_score_cross_tenant_job_is_safe_404(
    db_session: AsyncSession, client: AsyncClient, tenant_and_key
) -> None:
    tenant, _key, plaintext = tenant_and_key
    candidate, _profile_row = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    foreign_tenant = await create_tenant(db_session, name="Foreign")
    foreign_job, foreign_criteria = await _job_with_criteria(
        db_session,
        foreign_tenant.id,
        [_skill("python", "Python", weight=2.0, criterion_type=CriterionType.MUST_HAVE)],
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/jobs/{foreign_job.id}/criteria/{foreign_criteria.version_number}/score",
        json={"candidate_id": str(candidate.id), "evaluation_as_of_date": AS_OF},
        headers=_auth(plaintext),
    )
    assert resp.status_code == 404


async def test_rank_requires_evaluations_write_scope(
    db_session: AsyncSession, client: AsyncClient, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    job, criteria = await _job_with_criteria(
        db_session,
        tenant.id,
        [_skill("python", "Python", weight=2.0, criterion_type=CriterionType.MUST_HAVE)],
    )
    _key2, plaintext_read_only = await create_api_key(
        db_session, tenant_id=tenant.id, env="test", scopes=["evaluations:read"]
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/jobs/{job.id}/criteria/{criteria.version_number}/rank",
        json={"evaluation_as_of_date": AS_OF},
        headers=_auth(plaintext_read_only),
    )
    assert resp.status_code == 403


async def test_rank_cross_tenant_criteria_is_safe_404(
    db_session: AsyncSession, client: AsyncClient, tenant_and_key
) -> None:
    tenant, _key, plaintext = tenant_and_key
    foreign_tenant = await create_tenant(db_session, name="Foreign")
    foreign_job, foreign_criteria = await _job_with_criteria(
        db_session,
        foreign_tenant.id,
        [_skill("python", "Python", weight=2.0, criterion_type=CriterionType.MUST_HAVE)],
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/jobs/{foreign_job.id}/criteria/{foreign_criteria.version_number}/rank",
        json={"evaluation_as_of_date": AS_OF},
        headers=_auth(plaintext),
    )
    assert resp.status_code == 404


async def test_rank_order_matches_exact_backend_order(
    db_session: AsyncSession, client: AsyncClient, tenant_and_key
) -> None:
    tenant, _key, plaintext = tenant_and_key
    both, _p1 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python", "AWS")
    )
    python_only, _p2 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    aws_only, _p3 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("AWS")
    )
    job, criteria = await _job_with_criteria(
        db_session,
        tenant.id,
        [
            _skill("python", "Python", weight=1, criterion_type=CriterionType.MUST_HAVE),
            _skill("aws", "AWS", weight=9, criterion_type=CriterionType.PREFERRED),
        ],
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/jobs/{job.id}/criteria/{criteria.version_number}/rank",
        json={"evaluation_as_of_date": AS_OF},
        headers=_auth(plaintext),
    )
    assert resp.status_code == 200
    body = resp.json()
    ordered_ids = [item["candidate_id"] for item in body["results"]]
    assert ordered_ids == [str(both.id), str(python_only.id), str(aws_only.id)]
    assert [item["rank"] for item in body["results"]] == [1, 2, 3]
    for item in body["results"]:
        assert isinstance(item["numeric_score"], str)
        assert Decimal(item["numeric_score"]) >= 0


async def test_rank_response_never_leaks_other_tenant_candidates(
    db_session: AsyncSession, client: AsyncClient, tenant_and_key
) -> None:
    tenant, _key, plaintext = tenant_and_key
    candidate, _p = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    foreign_tenant = await create_tenant(db_session, name="Foreign")
    foreign_candidate, _fp = await seed_candidate_with_profile(
        db_session, tenant_id=foreign_tenant.id, profile_content=_profile("Python")
    )
    job, criteria = await _job_with_criteria(
        db_session,
        tenant.id,
        [_skill("python", "Python", weight=1, criterion_type=CriterionType.MUST_HAVE)],
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/jobs/{job.id}/criteria/{criteria.version_number}/rank",
        json={"evaluation_as_of_date": AS_OF},
        headers=_auth(plaintext),
    )
    assert resp.status_code == 200
    ids = {item["candidate_id"] for item in resp.json()["results"]}
    assert str(candidate.id) in ids
    assert str(foreign_candidate.id) not in ids

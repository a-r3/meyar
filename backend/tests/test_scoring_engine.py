import asyncio
from datetime import date
from decimal import Decimal

import pytest
from conftest import TEST_DATABASE_URL
from search_helpers import seed_candidate_with_profile, seed_next_profile_version
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from meyar.evaluation.service import evaluate_and_score_candidate
from meyar.models.audit_event import AuditEvent
from meyar.models.evaluation import Evaluation
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
from meyar.scoring.policy import ScoringPolicyError
from meyar.services.candidate_identity_repo import create_identity_version
from meyar.services.job_criteria_repo import create_criteria_version
from meyar.services.job_repo import create_job

EVIDENCE = [{"page": 1, "block_index": 0, "quote": "Synthetic evidence"}]
EMPTY_PROFILE = {
    "skills": [],
    "employment_history": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "projects": [],
}


def _skill(
    criterion_id: str,
    value: str,
    *,
    weight: float = 1,
    criterion_type: CriterionType = CriterionType.MUST_HAVE,
) -> CriterionIn:
    return CriterionIn(
        id=criterion_id,
        kind=CriterionKind.SKILL,
        type=criterion_type,
        label=value,
        value=value,
        weight=weight,
    )


async def _job_with_criteria(db: AsyncSession, tenant_id, criteria: list[CriterionIn]):
    job = await create_job(db, tenant_id=tenant_id, title="Synthetic Job")
    version = await create_criteria_version(
        db,
        tenant_id=tenant_id,
        job_id=job.id,
        criteria=[item.model_dump(mode="json") for item in criteria],
        created_by_api_key_id=None,
    )
    return job, version


async def _score(db, tenant_id, candidate, profile, job, criteria, as_of):
    return await evaluate_and_score_candidate(
        db,
        tenant_id=tenant_id,
        candidate_id=candidate.id,
        candidate_profile_version_id=profile.id,
        job_id=job.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=as_of,
    )


async def test_scored_evaluation_persists_complete_provenance_and_safe_explanation(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    content = {**EMPTY_PROFILE, "skills": [{"name": "Python", "evidence": EVIDENCE}]}
    candidate, profile = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=content
    )
    job, criteria = await _job_with_criteria(
        db_session, tenant.id, [_skill("python", "Python", weight=2.5)]
    )
    result = await _score(
        db_session, tenant.id, candidate, profile, job, criteria, date(2026, 6, 15)
    )
    await db_session.commit()

    evaluation = result.evaluation
    assert result.reused is False
    assert evaluation.evaluation_as_of_date == date(2026, 6, 15)
    assert evaluation.numeric_score == Decimal("100.00")
    assert evaluation.policy_engine_version == "meyar-policy-v1"
    assert evaluation.scoring_policy_version == "meyar-score-v1"
    assert evaluation.score_explanation["evaluation_id"] == str(evaluation.id)
    assert evaluation.score_explanation["numeric_score"] == "100.00"
    assert "Synthetic evidence" not in str(evaluation.score_explanation)


async def test_exact_provenance_is_idempotent_but_date_profile_and_criteria_change_create_rows(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    content = {**EMPTY_PROFILE, "skills": [{"name": "Python", "evidence": EVIDENCE}]}
    candidate, profile_v1 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=content
    )
    job, criteria_v1 = await _job_with_criteria(
        db_session, tenant.id, [_skill("python", "Python")]
    )

    first = await _score(
        db_session, tenant.id, candidate, profile_v1, job, criteria_v1, date(2026, 1, 1)
    )
    repeated = await _score(
        db_session, tenant.id, candidate, profile_v1, job, criteria_v1, date(2026, 1, 1)
    )
    changed_date = await _score(
        db_session, tenant.id, candidate, profile_v1, job, criteria_v1, date(2027, 1, 1)
    )
    profile_v2 = await seed_next_profile_version(
        db_session, tenant_id=tenant.id, candidate=candidate, profile_content=content
    )
    changed_profile = await _score(
        db_session, tenant.id, candidate, profile_v2, job, criteria_v1, date(2026, 1, 1)
    )
    criteria_v2 = await create_criteria_version(
        db_session,
        tenant_id=tenant.id,
        job_id=job.id,
        criteria=[_skill("python_v2", "Python").model_dump(mode="json")],
        created_by_api_key_id=None,
    )
    changed_criteria = await _score(
        db_session, tenant.id, candidate, profile_v2, job, criteria_v2, date(2026, 1, 1)
    )
    await db_session.commit()

    assert repeated.reused is True
    assert repeated.evaluation.id == first.evaluation.id
    assert repeated.evaluation.numeric_score == first.evaluation.numeric_score
    assert repeated.evaluation.score_explanation == first.evaluation.score_explanation
    assert changed_date.evaluation.id != first.evaluation.id
    assert changed_profile.evaluation.id not in {first.evaluation.id, changed_date.evaluation.id}
    assert changed_criteria.evaluation.id != changed_profile.evaluation.id
    count = await db_session.scalar(select(func.count(Evaluation.id)))
    assert count == 4


async def test_present_experience_is_reproducible_for_explicit_historical_date(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    content = {
        **EMPTY_PROFILE,
        "employment_history": [
            {
                "title": "Engineer",
                "start_date": "2023",
                "end_date": "Present",
                "is_current": True,
                "evidence": EVIDENCE,
            }
        ],
    }
    candidate, profile = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=content
    )
    experience = CriterionIn(
        id="experience",
        kind=CriterionKind.EXPERIENCE,
        type=CriterionType.MUST_HAVE,
        label="Four years",
        min_years=4,
    )
    job, criteria = await _job_with_criteria(db_session, tenant.id, [experience])

    first_2026 = await _score(
        db_session, tenant.id, candidate, profile, job, criteria, date(2026, 1, 1)
    )
    in_2027 = await _score(
        db_session, tenant.id, candidate, profile, job, criteria, date(2027, 1, 1)
    )
    repeated_2026 = await _score(
        db_session, tenant.id, candidate, profile, job, criteria, date(2026, 1, 1)
    )

    assert first_2026.evaluation.criterion_results[0]["status"] == "NOT_MATCHED"
    assert first_2026.evaluation.numeric_score == Decimal("0.00")
    assert in_2027.evaluation.criterion_results[0]["status"] == "MATCH"
    assert in_2027.evaluation.numeric_score == Decimal("100.00")
    assert repeated_2026.reused is True
    assert repeated_2026.evaluation.id == first_2026.evaluation.id
    assert repeated_2026.evaluation.criterion_results == first_2026.evaluation.criterion_results


async def test_identity_versions_do_not_change_exact_score(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    content = {**EMPTY_PROFILE, "skills": [{"name": "Python", "evidence": EVIDENCE}]}
    candidate, profile = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=content
    )
    job, criteria = await _job_with_criteria(
        db_session, tenant.id, [_skill("python", "Python")]
    )
    before = await _score(
        db_session, tenant.id, candidate, profile, job, criteria, date(2026, 1, 1)
    )

    for name, email, phone in [
        ("Synthetic Alpha", "alpha@example.invalid", "+00000000001"),
        ("Synthetic Zulu", "zulu@example.invalid", "+00000000099"),
    ]:
        await create_identity_version(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            candidate_document_id=profile.candidate_document_id,
            canonical_document_id=profile.canonical_document_id,
            source_sha256=profile.source_sha256,
            schema_version="candidate-identity-v1",
            prompt_version="test",
            model_provider="fake",
            model_name="fake",
            status="COMPLETED",
            identity_content={"full_name": name, "email": email, "phone": phone},
        )
    after = await _score(
        db_session, tenant.id, candidate, profile, job, criteria, date(2026, 1, 1)
    )
    assert after.reused is True
    assert after.evaluation.id == before.evaluation.id
    assert after.evaluation.numeric_score == before.evaluation.numeric_score
    assert after.evaluation.score_explanation == before.evaluation.score_explanation


async def test_legacy_all_zero_criteria_version_fails_safely(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    candidate, profile = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=EMPTY_PROFILE
    )
    job = await create_job(db_session, tenant_id=tenant.id, title="Legacy all-zero")
    criteria = await create_criteria_version(
        db_session,
        tenant_id=tenant.id,
        job_id=job.id,
        criteria=[_skill("legacy", "Python", weight=0).model_dump(mode="json")],
        created_by_api_key_id=None,
    )
    with pytest.raises(ScoringPolicyError) as exc_info:
        await _score(
            db_session, tenant.id, candidate, profile, job, criteria, date(2026, 1, 1)
        )
    assert exc_info.value.code == "ZERO_TOTAL_CRITERION_WEIGHT"
    count = await db_session.scalar(select(func.count(Evaluation.id)))
    assert count == 0


async def test_score_audit_metadata_is_pii_safe(db_session: AsyncSession, tenant_and_key) -> None:
    tenant, _key, _plaintext = tenant_and_key
    content = {**EMPTY_PROFILE, "skills": [{"name": "Python", "evidence": EVIDENCE}]}
    candidate, profile = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=content
    )
    job, criteria = await _job_with_criteria(
        db_session, tenant.id, [_skill("python", "Python")]
    )
    await _score(db_session, tenant.id, candidate, profile, job, criteria, date(2026, 1, 1))
    events = list(
        (
            await db_session.execute(
                select(AuditEvent).where(AuditEvent.event_type == "CANDIDATE_SCORE_COMPUTED")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    metadata_text = str(events[0].event_metadata).lower()
    for forbidden in ("name", "email", "phone", "quote", "vector", "semantic", "llm"):
        assert forbidden not in metadata_text


async def test_concurrent_exact_requests_converge_on_one_evaluation(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    content = {**EMPTY_PROFILE, "skills": [{"name": "Python", "evidence": EVIDENCE}]}
    candidate, profile = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=content
    )
    job, criteria = await _job_with_criteria(
        db_session, tenant.id, [_skill("python", "Python")]
    )
    await db_session.commit()

    engine = create_async_engine(TEST_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def score_once():
        async with factory() as session:
            result = await _score(
                session,
                tenant.id,
                candidate,
                profile,
                job,
                criteria,
                date(2026, 1, 1),
            )
            await session.commit()
            return result

    first, second = await asyncio.gather(score_once(), score_once())
    await engine.dispose()

    assert first.evaluation.id == second.evaluation.id
    count = await db_session.scalar(
        select(func.count(Evaluation.id)).where(
            Evaluation.candidate_profile_version_id == profile.id,
            Evaluation.job_criteria_version_id == criteria.id,
            Evaluation.evaluation_as_of_date == date(2026, 1, 1),
            Evaluation.scoring_policy_version == "meyar-score-v1",
        )
    )
    assert count == 1

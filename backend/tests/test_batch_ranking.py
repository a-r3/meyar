from datetime import date
from decimal import Decimal

import pytest
from search_helpers import (
    seed_candidate_with_profile,
    seed_next_profile_version,
    synthetic_evidence,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import meyar.scoring.batch as batch_module
from meyar.models.audit_event import AuditEvent
from meyar.models.evaluation import Evaluation
from meyar.schemas.criteria import CriterionIn, CriterionKind, CriterionType
from meyar.scoring.batch import rank_candidates_for_job
from meyar.scoring.policy import ScoringPolicyError
from meyar.search.schemas import CandidateSearchRequest, RequiredFilters, SearchMode
from meyar.search.service import search_candidates
from meyar.services.candidate_identity_repo import create_identity_version
from meyar.services.candidate_profile_repo import list_current_profile_versions_for_tenant
from meyar.services.candidate_repo import create_candidate, list_candidates_for_tenant
from meyar.services.job_criteria_repo import create_criteria_version
from meyar.services.job_repo import create_job
from meyar.services.tenant_repo import create_tenant

AS_OF = date(2026, 1, 1)
EVIDENCE = [{"page": 1, "block_index": 0, "quote": "Synthetic"}]
EMPTY = {
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
    weight: float,
    criterion_type: CriterionType,
    manual_review_required: bool = False,
) -> CriterionIn:
    return CriterionIn(
        id=criterion_id,
        kind=CriterionKind.SKILL,
        type=criterion_type,
        label=value,
        value=value,
        weight=weight,
        manual_review_required=manual_review_required,
    )


def _profile(*skills: str) -> dict:
    return {
        **EMPTY,
        "skills": [{"name": skill, "evidence": synthetic_evidence(skill)} for skill in skills],
    }


async def test_acams_ranking_uses_same_bounded_identity_as_structured_search(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    canonical = "ACAMS Certified Anti-Money Laundering Specialist"
    seeded = {}
    for name in (canonical, "ACAMS Advanced CAMS-Risk Management", "ACAMS"):
        content = {
            **EMPTY,
            "certifications": [{"name": name, "evidence": synthetic_evidence(name)}],
        }
        candidate, _version = await seed_candidate_with_profile(
            db_session, tenant_id=tenant.id, profile_content=content
        )
        seeded[name] = candidate.id
    criteria = await _criteria(
        db_session,
        tenant.id,
        [
            CriterionIn(
                id="cams",
                kind=CriterionKind.CERTIFICATION,
                type=CriterionType.MUST_HAVE,
                label="ACAMS",
                value="ACAMS",
                weight=1,
            )
        ],
        eligible_only=True,
    )
    ranking = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=AS_OF,
    )
    search = await search_candidates(
        db_session,
        tenant_id=tenant.id,
        request=CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY,
            required_filters=RequiredFilters(certifications=["ACAMS"]),
        ),
    )
    expected = {seeded[canonical]}
    assert {item.candidate_id for item in search.results} == expected
    assert {item.candidate_id for item in ranking.results} == expected
    assert ranking.results[0].numeric_score == Decimal("100.00")
    evaluation = await db_session.get(Evaluation, ranking.results[0].evaluation_id)
    assert evaluation is not None
    assert evaluation.criterion_results[0]["status"] == "MATCH"
    assert evaluation.criterion_results[0]["evidence"] == synthetic_evidence(canonical)


async def _criteria(
    db: AsyncSession,
    tenant_id,
    criteria: list[CriterionIn],
    *,
    result_limit: int = 20,
    eligible_only: bool = False,
):
    job = await create_job(db, tenant_id=tenant_id, title="Batch Job")
    version = await create_criteria_version(
        db,
        tenant_id=tenant_id,
        job_id=job.id,
        criteria=[item.model_dump(mode="json") for item in criteria],
        created_by_api_key_id=None,
        result_limit=result_limit,
        eligible_only=eligible_only,
    )
    return version


async def test_duplicate_language_fact_order_has_identical_v3_batch_ranking(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    candidates = []
    for levels in (("B1", "C1"), ("C1", "B1")):
        candidate, _profile = await seed_candidate_with_profile(
            db_session,
            tenant_id=tenant.id,
            profile_content={
                **EMPTY,
                "languages": [
                    {
                        "language": "English",
                        "proficiency": level,
                        "evidence": synthetic_evidence("English", level),
                    }
                    for level in levels
                ],
            },
        )
        candidates.append(candidate.id)
    criteria = await _criteria(
        db_session,
        tenant.id,
        [
            CriterionIn(
                id="english_b2",
                kind=CriterionKind.LANGUAGE,
                type=CriterionType.MUST_HAVE,
                label="English B2",
                value="English",
                required_level="B2",
                weight=1,
            )
        ],
        eligible_only=True,
    )
    ranking = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=AS_OF,
    )
    assert ranking.evaluated_count == ranking.eligible_count == 2
    assert ranking.reused_count == 0
    assert [item.candidate_id for item in ranking.results] == sorted(
        candidates, key=lambda candidate_id: candidate_id.int
    )
    assert {item.evaluation_policy_version for item in ranking.results} == {"meyar-policy-v3"}
    assert {item.scoring_policy_version for item in ranking.results} == {"meyar-score-v1"}
    assert {item.numeric_score for item in ranking.results} == {Decimal("100.00")}
    assert {item.fit_band for item in ranking.results} == {"STRONG_MATCH"}
    for item in ranking.results:
        evaluation = await db_session.get(Evaluation, item.evaluation_id)
        assert evaluation is not None
        assert evaluation.criterion_results[0]["status"] == "MATCH"
        assert evaluation.criterion_results[0]["evidence"] == synthetic_evidence("English", "C1")


async def test_agent_workflow_top_k_returns_only_ranked_eligible_candidates(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    matching_ids = []
    for _ in range(12):
        candidate, _profile_version = await seed_candidate_with_profile(
            db_session, tenant_id=tenant.id, profile_content=_profile("Python")
        )
        matching_ids.append(candidate.id)
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("AWS")
    )
    criteria = await _criteria(
        db_session,
        tenant.id,
        [_skill("python", "Python", weight=1, criterion_type=CriterionType.MUST_HAVE)],
        result_limit=10,
        eligible_only=True,
    )
    result = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=AS_OF,
    )
    assert len(result.results) == 10
    assert result.result_limit == 10
    assert result.eligible_count == 12
    assert all(item.candidate_id in matching_ids for item in result.results)
    assert all(item.fit_band in ("STRONG_MATCH", "POTENTIAL_MATCH") for item in result.results)


async def test_agent_workflow_top_k_returns_fewer_and_truthful_zero(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    for _ in range(4):
        await seed_candidate_with_profile(
            db_session, tenant_id=tenant.id, profile_content=_profile("Python")
        )
    criteria = await _criteria(
        db_session,
        tenant.id,
        [_skill("python", "Python", weight=1, criterion_type=CriterionType.MUST_HAVE)],
        result_limit=10,
        eligible_only=True,
    )
    four = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=AS_OF,
    )
    assert len(four.results) == 4

    empty_criteria = await _criteria(
        db_session,
        tenant.id,
        [_skill("go", "Go", weight=1, criterion_type=CriterionType.MUST_HAVE)],
        result_limit=10,
        eligible_only=True,
    )
    zero = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=empty_criteria.id,
        evaluation_as_of_date=AS_OF,
    )
    assert zero.eligible_count == 0
    assert zero.results == []


async def test_fit_tier_dominates_high_preferred_score(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    high_score_failed_gate, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("AWS")
    )
    low_score_passed_gate, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    criteria = await _criteria(
        db_session,
        tenant.id,
        [
            _skill("python", "Python", weight=1, criterion_type=CriterionType.MUST_HAVE),
            _skill("aws", "AWS", weight=9, criterion_type=CriterionType.PREFERRED),
        ],
    )
    result = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=AS_OF,
    )
    assert [item.candidate_id for item in result.results] == [
        low_score_passed_gate.id,
        high_score_failed_gate.id,
    ]
    assert result.results[0].numeric_score == Decimal("10.00")
    assert result.results[1].numeric_score == Decimal("90.00")
    assert [item.fit_tier for item in result.results] == [1, 3]


async def test_zero_weight_must_have_still_gates_batch_order(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    failed_gate, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("AWS")
    )
    passed_gate, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    criteria = await _criteria(
        db_session,
        tenant.id,
        [
            _skill("python", "Python", weight=0, criterion_type=CriterionType.MUST_HAVE),
            _skill("aws", "AWS", weight=10, criterion_type=CriterionType.PREFERRED),
        ],
    )
    result = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=AS_OF,
    )
    assert [item.candidate_id for item in result.results] == [passed_gate.id, failed_gate.id]
    assert result.results[0].numeric_score == Decimal("0.00")
    assert result.results[1].numeric_score == Decimal("100.00")
    assert result.results[0].fit_tier < result.results[1].fit_tier


async def test_manual_review_tier_remains_ahead_of_insufficient_evidence(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    manual_profile = {
        **EMPTY,
        "employment_history": [
            {
                "title": "Engineer",
                "start_date": "ambiguous",
                "end_date": "Present",
                "is_current": True,
                "evidence": synthetic_evidence("Engineer", "ambiguous", "Present", "present"),
            }
        ],
    }
    manual, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=manual_profile
    )
    insufficient, _ = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=EMPTY
    )
    criteria = await _criteria(
        db_session,
        tenant.id,
        [
            CriterionIn(
                id="experience",
                kind=CriterionKind.EXPERIENCE,
                type=CriterionType.MUST_HAVE,
                label="Experience",
                min_years=1,
                weight=1,
            )
        ],
    )
    result = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=AS_OF,
    )
    assert [item.candidate_id for item in result.results] == [manual.id, insufficient.id]
    assert [item.fit_tier for item in result.results] == [2, 3]


async def test_batch_uses_only_current_profile_version(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    candidate, profile_v1 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    profile_v2 = await seed_next_profile_version(
        db_session, tenant_id=tenant.id, candidate=candidate, profile_content=EMPTY
    )
    criteria = await _criteria(
        db_session,
        tenant.id,
        [_skill("python", "Python", weight=1, criterion_type=CriterionType.MUST_HAVE)],
    )
    result = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=AS_OF,
    )
    assert len(result.results) == 1
    assert result.results[0].candidate_profile_version_id == profile_v2.id
    assert result.results[0].candidate_profile_version_id != profile_v1.id
    assert result.results[0].numeric_score == Decimal("0.00")


async def test_stable_uuid_tie_is_insertion_order_and_identity_invariant(
    db_session: AsyncSession, tenant_and_key, monkeypatch
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    first, first_profile = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    second, second_profile = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    for candidate, profile, identity in [
        (first, first_profile, {"full_name": "Zulu", "email": "z@example.invalid"}),
        (second, second_profile, {"full_name": "Alpha", "email": "a@example.invalid"}),
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
            identity_content=identity,
        )
    criteria = await _criteria(
        db_session,
        tenant.id,
        [_skill("python", "Python", weight=1, criterion_type=CriterionType.MUST_HAVE)],
    )
    expected = sorted([first.id, second.id], key=lambda candidate_id: candidate_id.int)
    normal = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=AS_OF,
    )

    async def reversed_candidates(db, *, tenant_id):
        return list(reversed(await list_candidates_for_tenant(db, tenant_id=tenant_id)))

    async def reversed_profiles(db, *, tenant_id):
        return list(
            reversed(await list_current_profile_versions_for_tenant(db, tenant_id=tenant_id))
        )

    monkeypatch.setattr(batch_module, "list_candidates_for_tenant", reversed_candidates)
    monkeypatch.setattr(
        batch_module, "list_current_profile_versions_for_tenant", reversed_profiles
    )
    reversed_result = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=AS_OF,
    )
    assert [item.candidate_id for item in normal.results] == expected
    assert [item.candidate_id for item in reversed_result.results] == expected
    assert reversed_result.reused_count == 2


async def test_batch_tenant_isolation_zero_set_and_skip_counts(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant_a, _key, _plaintext = tenant_and_key
    tenant_b = await create_tenant(db_session, name="Tenant B")
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant_b.id, profile_content=_profile("Python")
    )
    await create_candidate(db_session, tenant_id=tenant_a.id)
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant_a.id, profile_content=None, status="FAILED"
    )
    criteria = await _criteria(
        db_session,
        tenant_a.id,
        [_skill("python", "Python", weight=1, criterion_type=CriterionType.MUST_HAVE)],
    )
    result = await rank_candidates_for_job(
        db_session,
        tenant_id=tenant_a.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=AS_OF,
    )
    assert result.evaluated_count == 0
    assert result.skipped_count == 2
    assert result.skip_reason_counts == {
        "CURRENT_PROFILE_NOT_COMPLETED": 1,
        "NO_CURRENT_PROFILE": 1,
    }
    assert result.results == []


async def test_legacy_zero_weight_batch_fails_before_candidate_iteration(
    db_session: AsyncSession, tenant_and_key, monkeypatch
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    job = await create_job(db_session, tenant_id=tenant.id, title="Legacy")
    criteria = await create_criteria_version(
        db_session,
        tenant_id=tenant.id,
        job_id=job.id,
        criteria=[
            _skill(
                "zero", "Python", weight=0, criterion_type=CriterionType.MUST_HAVE
            ).model_dump(mode="json")
        ],
        created_by_api_key_id=None,
    )

    async def must_not_iterate(*args, **kwargs):
        raise AssertionError("candidate iteration must not begin")

    monkeypatch.setattr(batch_module, "list_candidates_for_tenant", must_not_iterate)
    with pytest.raises(ScoringPolicyError) as exc_info:
        await rank_candidates_for_job(
            db_session,
            tenant_id=tenant.id,
            job_criteria_version_id=criteria.id,
            evaluation_as_of_date=AS_OF,
        )
    assert exc_info.value.code == "ZERO_TOTAL_CRITERION_WEIGHT"


async def test_batch_audit_is_safe(db_session: AsyncSession, tenant_and_key) -> None:
    tenant, _key, _plaintext = tenant_and_key
    criteria = await _criteria(
        db_session,
        tenant.id,
        [_skill("python", "Python", weight=1, criterion_type=CriterionType.MUST_HAVE)],
    )
    await rank_candidates_for_job(
        db_session,
        tenant_id=tenant.id,
        job_criteria_version_id=criteria.id,
        evaluation_as_of_date=AS_OF,
    )
    event = (
        await db_session.execute(
            select(AuditEvent).where(AuditEvent.event_type == "JOB_BATCH_RANKED")
        )
    ).scalar_one()
    metadata = str(event.event_metadata).lower()
    for forbidden in ("name", "email", "phone", "quote", "vector", "semantic", "llm"):
        assert forbidden not in metadata

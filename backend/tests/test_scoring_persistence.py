from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from search_helpers import seed_candidate_with_profile, seed_next_profile_version
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.services.evaluation_repo import create_evaluation
from meyar.services.job_criteria_repo import create_criteria_version
from meyar.services.job_repo import create_job

EMPTY = {
    "skills": [],
    "employment_history": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "projects": [],
}


async def _inputs(db_session: AsyncSession, tenant_id):
    candidate, profile = await seed_candidate_with_profile(
        db_session, tenant_id=tenant_id, profile_content=EMPTY
    )
    job = await create_job(db_session, tenant_id=tenant_id, title="Persistence")
    criteria = await create_criteria_version(
        db_session,
        tenant_id=tenant_id,
        job_id=job.id,
        criteria=[
            {
                "id": "python",
                "kind": "SKILL",
                "type": "MUST_HAVE",
                "label": "Python",
                "value": "Python",
                "weight": 1,
            }
        ],
        created_by_api_key_id=None,
    )
    return candidate, profile, job, criteria


async def test_legacy_null_scoring_rows_survive_and_may_share_old_provenance(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    candidate, profile, job, criteria = await _inputs(db_session, tenant.id)
    rows = []
    for _index in range(2):
        rows.append(
            await create_evaluation(
                db_session,
                tenant_id=tenant.id,
                candidate_id=candidate.id,
                candidate_profile_version_id=profile.id,
                job_id=job.id,
                job_criteria_version_id=criteria.id,
                status="COMPLETED",
                policy_engine_version="meyar-policy-v1",
                overall_result="INSUFFICIENT_EVIDENCE",
                criterion_results=[],
            )
        )
    await db_session.commit()
    assert rows[0].id != rows[1].id
    for row in rows:
        assert row.evaluation_as_of_date is None
        assert row.numeric_score is None
        assert row.scoring_policy_version is None
        assert row.score_explanation is None


async def test_partial_unique_index_rejects_exact_scored_provenance_only(
    db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, _key, _plaintext = tenant_and_key
    candidate, profile, job, criteria = await _inputs(db_session, tenant.id)

    async def insert(*, as_of: date, scoring_version: str, profile_id=profile.id):
        return await create_evaluation(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            candidate_profile_version_id=profile_id,
            job_id=job.id,
            job_criteria_version_id=criteria.id,
            status="COMPLETED",
            policy_engine_version="meyar-policy-v1",
            evaluation_as_of_date=as_of,
            numeric_score=Decimal("0.00"),
            scoring_policy_version=scoring_version,
            score_explanation={"safe": True},
            overall_result="INSUFFICIENT_EVIDENCE",
            criterion_results=[],
        )

    await insert(as_of=date(2026, 1, 1), scoring_version="meyar-score-v1")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await insert(as_of=date(2026, 1, 1), scoring_version="meyar-score-v1")

    await insert(as_of=date(2027, 1, 1), scoring_version="meyar-score-v1")
    await insert(as_of=date(2026, 1, 1), scoring_version="meyar-score-v2")
    profile_v2 = await seed_next_profile_version(
        db_session, tenant_id=tenant.id, candidate=candidate, profile_content=EMPTY
    )
    await insert(
        as_of=date(2026, 1, 1), scoring_version="meyar-score-v1", profile_id=profile_v2.id
    )
    await db_session.commit()


async def test_database_schema_uses_nullable_score_columns_and_partial_unique_index(
    db_session: AsyncSession,
) -> None:
    columns = {
        row.column_name: (row.data_type, row.is_nullable)
        for row in (
            await db_session.execute(
                text(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'evaluations' AND column_name IN "
                    "('evaluation_as_of_date','numeric_score','scoring_policy_version',"
                    "'score_explanation')"
                )
            )
        )
    }
    assert columns == {
        "evaluation_as_of_date": ("date", "YES"),
        "numeric_score": ("numeric", "YES"),
        "score_explanation": ("json", "YES"),
        "scoring_policy_version": ("character varying", "YES"),
    }
    definition = await db_session.scalar(
        text(
            "SELECT indexdef FROM pg_indexes WHERE tablename='evaluations' "
            "AND indexname='uq_evaluations_scored_provenance'"
        )
    )
    assert definition is not None
    assert "UNIQUE INDEX" in definition
    assert "evaluation_as_of_date IS NOT NULL" in definition
    assert "scoring_policy_version IS NOT NULL" in definition


def test_scoring_and_ranking_modules_have_no_ai_search_or_identity_dependency() -> None:
    root = Path(__file__).resolve().parent.parent / "src" / "meyar"
    paths = [
        root / "scoring" / "policy.py",
        root / "scoring" / "batch.py",
        root / "evaluation" / "service.py",
    ]
    forbidden = (
        "LLMProvider",
        "Ollama",
        "EmbeddingProvider",
        "pgvector",
        "search_candidates",
        "search planner",
        "CandidateIdentity",
        "candidate_identity",
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, f"{path.name} contains forbidden dependency {token}"

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.evaluation import EVALUATION_STATUS_COMPLETED, Evaluation


async def create_evaluation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    candidate_profile_version_id: uuid.UUID,
    job_id: uuid.UUID,
    job_criteria_version_id: uuid.UUID,
    status: str,
    policy_engine_version: str,
    evaluation_id: uuid.UUID | None = None,
    evaluation_as_of_date: date | None = None,
    numeric_score: Decimal | None = None,
    scoring_policy_version: str | None = None,
    score_explanation: dict | None = None,
    overall_result: str | None = None,
    criterion_results: list[dict] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> Evaluation:
    """Always inserts a new row — Evaluation is immutable once created and
    is never looked up-and-updated. See docs/MASTER_SPEC.md (Slice 5
    spec) §15."""
    evaluation = Evaluation(
        id=evaluation_id or uuid.uuid4(),
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_profile_version_id=candidate_profile_version_id,
        job_id=job_id,
        job_criteria_version_id=job_criteria_version_id,
        status=status,
        overall_result=overall_result,
        policy_engine_version=policy_engine_version,
        evaluation_as_of_date=evaluation_as_of_date,
        numeric_score=numeric_score,
        scoring_policy_version=scoring_policy_version,
        score_explanation=score_explanation,
        criterion_results=criterion_results,
        error_code=error_code,
        error_message=error_message[:500] if error_message else None,
        # Audit timestamp only; deterministic policy/scoring/ranking never reads it.
        completed_at=datetime.now(UTC),
    )
    db.add(evaluation)
    await db.flush()
    return evaluation


async def get_scored_evaluation_by_provenance(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_profile_version_id: uuid.UUID,
    job_criteria_version_id: uuid.UUID,
    evaluation_as_of_date: date,
    policy_engine_version: str,
    scoring_policy_version: str,
) -> Evaluation | None:
    result = await db.execute(
        select(Evaluation).where(
            Evaluation.tenant_id == tenant_id,
            Evaluation.candidate_profile_version_id == candidate_profile_version_id,
            Evaluation.job_criteria_version_id == job_criteria_version_id,
            Evaluation.evaluation_as_of_date == evaluation_as_of_date,
            Evaluation.policy_engine_version == policy_engine_version,
            Evaluation.scoring_policy_version == scoring_policy_version,
            Evaluation.status == EVALUATION_STATUS_COMPLETED,
            Evaluation.numeric_score.is_not(None),
            Evaluation.score_explanation.is_not(None),
        )
    )
    return result.scalar_one_or_none()


async def get_evaluation(
    db: AsyncSession, *, tenant_id: uuid.UUID, evaluation_id: uuid.UUID
) -> Evaluation | None:
    result = await db.execute(
        select(Evaluation).where(Evaluation.id == evaluation_id, Evaluation.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()


async def list_evaluations_for_candidate(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> list[Evaluation]:
    result = await db.execute(
        select(Evaluation)
        .where(Evaluation.candidate_id == candidate_id, Evaluation.tenant_id == tenant_id)
        .order_by(Evaluation.created_at.asc())
    )
    return list(result.scalars().all())

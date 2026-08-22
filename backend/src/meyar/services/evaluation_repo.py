import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.evaluation import Evaluation


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
    overall_result: str | None = None,
    criterion_results: list[dict] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> Evaluation:
    """Always inserts a new row — Evaluation is immutable once created and
    is never looked up-and-updated. See docs/MASTER_SPEC.md (Slice 5
    spec) §15."""
    evaluation = Evaluation(
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_profile_version_id=candidate_profile_version_id,
        job_id=job_id,
        job_criteria_version_id=job_criteria_version_id,
        status=status,
        overall_result=overall_result,
        policy_engine_version=policy_engine_version,
        criterion_results=criterion_results,
        error_code=error_code,
        error_message=error_message[:500] if error_message else None,
        completed_at=datetime.now(UTC),
    )
    db.add(evaluation)
    await db.flush()
    return evaluation


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

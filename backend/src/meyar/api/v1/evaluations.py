"""Slice 12 — thin REST wrappers over the accepted Slice 5/10 evaluation/
batch-ranking services. HTTP validates and presents; `meyar.evaluation.
service` and `meyar.scoring.batch` compute the deterministic score/rank —
no policy logic is duplicated here."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.core.auth import TenantContext, require_scope
from meyar.db import get_db
from meyar.evaluation.service import EvaluationInputError, evaluate_and_score_candidate
from meyar.models.evaluation import EVALUATION_STATUS_COMPLETED
from meyar.schemas.api_evaluation import (
    ApiBatchRankingResponse,
    ApiBatchRankRequest,
    ApiRankedCandidate,
    ApiScoreCandidateRequest,
    ApiScoreCandidateResponse,
)
from meyar.scoring.batch import BatchRankingError, rank_candidates_for_job
from meyar.scoring.schemas import ScoreExplanation
from meyar.services.candidate_profile_repo import get_current_profile_version
from meyar.services.job_criteria_repo import get_criteria_version
from meyar.services.job_repo import get_job
from meyar.ui.service import build_ranked_candidate_views

router = APIRouter(tags=["evaluations"])

_NOT_FOUND_CODES = frozenset({"PROFILE_NOT_FOUND", "CRITERIA_VERSION_NOT_FOUND"})


async def _get_job_or_404(db: AsyncSession, tenant_id: uuid.UUID, job_id: uuid.UUID):
    job = await get_job(db, tenant_id=tenant_id, job_id=job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return job


@router.post(
    "/jobs/{job_id}/criteria/{version_number}/score",
    response_model=ApiScoreCandidateResponse,
)
async def post_score_candidate(
    job_id: uuid.UUID,
    version_number: int,
    body: ApiScoreCandidateRequest,
    ctx: TenantContext = Depends(require_scope("evaluations:read")),
    db: AsyncSession = Depends(get_db),
) -> ApiScoreCandidateResponse:
    await _get_job_or_404(db, ctx.tenant_id, job_id)
    criteria_version = await get_criteria_version(
        db, tenant_id=ctx.tenant_id, job_id=job_id, version_number=version_number
    )
    if criteria_version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Criteria version not found."
        )
    profile_version = await get_current_profile_version(
        db, tenant_id=ctx.tenant_id, candidate_id=body.candidate_id
    )
    if profile_version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Candidate profile not found."
        )

    try:
        result = await evaluate_and_score_candidate(
            db,
            tenant_id=ctx.tenant_id,
            candidate_id=body.candidate_id,
            candidate_profile_version_id=profile_version.id,
            job_id=job_id,
            job_criteria_version_id=criteria_version.id,
            evaluation_as_of_date=body.evaluation_as_of_date,
            resolved_profile_version=profile_version,
            resolved_criteria_version=criteria_version,
        )
    except EvaluationInputError as exc:
        await db.rollback()
        code = (
            status.HTTP_404_NOT_FOUND
            if exc.code in _NOT_FOUND_CODES
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        raise HTTPException(status_code=code, detail=exc.code) from exc

    await db.commit()
    evaluation = result.evaluation

    if evaluation.status != EVALUATION_STATUS_COMPLETED:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=evaluation.error_code
        )

    explanation = ScoreExplanation.model_validate(evaluation.score_explanation)
    return ApiScoreCandidateResponse(
        candidate_id=body.candidate_id,
        candidate_profile_version_id=profile_version.id,
        job_id=job_id,
        job_criteria_version_id=criteria_version.id,
        evaluation_id=evaluation.id,
        numeric_score=explanation.numeric_score,
        fit_band=explanation.fit_band,
        evaluation_policy_version=explanation.evaluation_policy_version,
        scoring_policy_version=explanation.scoring_policy_version,
        evaluation_as_of_date=explanation.evaluation_as_of_date,
        reused=result.reused,
        explanation=explanation,
    )


@router.post(
    "/jobs/{job_id}/criteria/{version_number}/rank",
    response_model=ApiBatchRankingResponse,
)
async def post_rank_job(
    job_id: uuid.UUID,
    version_number: int,
    body: ApiBatchRankRequest,
    ctx: TenantContext = Depends(require_scope("evaluations:write")),
    db: AsyncSession = Depends(get_db),
) -> ApiBatchRankingResponse:
    await _get_job_or_404(db, ctx.tenant_id, job_id)
    criteria_version = await get_criteria_version(
        db, tenant_id=ctx.tenant_id, job_id=job_id, version_number=version_number
    )
    if criteria_version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Criteria version not found."
        )

    try:
        ranking = await rank_candidates_for_job(
            db,
            tenant_id=ctx.tenant_id,
            job_criteria_version_id=criteria_version.id,
            evaluation_as_of_date=body.evaluation_as_of_date,
        )
        views = await build_ranked_candidate_views(db, tenant_id=ctx.tenant_id, ranking=ranking)
        await db.commit()
    except BatchRankingError as exc:
        await db.rollback()
        code = (
            status.HTTP_404_NOT_FOUND
            if exc.code == "CRITERIA_VERSION_NOT_FOUND"
            else status.HTTP_422_UNPROCESSABLE_CONTENT
        )
        raise HTTPException(status_code=code, detail=exc.code) from exc

    results = [
        ApiRankedCandidate(
            rank=item.rank,
            candidate_id=item.candidate_id,
            full_name=view.full_name,
            candidate_profile_version_id=item.candidate_profile_version_id,
            evaluation_id=item.evaluation_id,
            numeric_score=item.score_explanation.numeric_score,
            fit_band=item.fit_band,
            evaluation_as_of_date=item.evaluation_as_of_date,
            evaluation_policy_version=item.evaluation_policy_version,
            scoring_policy_version=item.scoring_policy_version,
        )
        for item, view in zip(ranking.results, views, strict=True)
    ]
    return ApiBatchRankingResponse(
        job_criteria_version_id=criteria_version.id,
        evaluation_as_of_date=ranking.evaluation_as_of_date,
        evaluation_policy_version=ranking.evaluation_policy_version,
        scoring_policy_version=ranking.scoring_policy_version,
        evaluated_count=ranking.evaluated_count,
        reused_count=ranking.reused_count,
        skipped_count=ranking.skipped_count,
        skip_reason_counts=ranking.skip_reason_counts,
        results=results,
    )

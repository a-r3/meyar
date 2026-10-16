"""Deterministic scoring/ranking use cases (issue #45, D-116).

No scoring policy lives here: ``meyar.evaluation`` / ``meyar.scoring`` decide.
This module resolves the evaluation date ONCE (the boundary rule of
``meyar.application.business_date``), selects the exact inputs, and passes
everything explicitly. The LLM never participates, and no candidate identity is
an input — names are attached only after rank and score are final."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.application.business_date import effective_business_date
from meyar.application.errors import ApplicationError, ErrorCode
from meyar.application.principal import Principal
from meyar.config import Settings
from meyar.evaluation.service import EvaluationInputError, evaluate_and_score_candidate
from meyar.models.evaluation import EVALUATION_STATUS_COMPLETED
from meyar.schemas.api_evaluation import (
    ApiBatchRankingResponse,
    ApiEvaluationOut,
    ApiRankedCandidate,
    ApiScoreCandidateResponse,
)
from meyar.scoring.batch import BatchRankingError, rank_candidates_for_job
from meyar.scoring.schemas import ScoreExplanation
from meyar.services.candidate_profile_repo import (
    get_effective_profile_version,
    get_profile_version_by_id,
)
from meyar.services.evaluation_repo import get_evaluation
from meyar.services.job_criteria_repo import get_criteria_version
from meyar.services.job_repo import get_job
from meyar.services.profile_authority import get_authorized_profile_version_by_id
from meyar.ui.service import build_ranked_candidate_views

_NOT_FOUND_CODES = frozenset({"PROFILE_NOT_FOUND", "CRITERIA_VERSION_NOT_FOUND"})


def _not_found(detail: str, reason: str) -> ApplicationError:
    return ApplicationError(
        ErrorCode.NOT_FOUND, status_code=404, reason=reason, message=detail, legacy_detail=detail
    )


async def _require_criteria_version(
    db: AsyncSession, principal: Principal, *, job_id: uuid.UUID, version_number: int
):  # noqa: ANN202 - JobCriteriaVersion
    job = await get_job(db, tenant_id=principal.tenant_id, job_id=job_id)
    if job is None:
        raise _not_found("Job not found.", "JOB_NOT_FOUND")
    version = await get_criteria_version(
        db, tenant_id=principal.tenant_id, job_id=job_id, version_number=version_number
    )
    if version is None:
        raise _not_found("Criteria version not found.", "CRITERIA_VERSION_NOT_FOUND")
    return version


async def score_candidate(
    db: AsyncSession,
    principal: Principal,
    *,
    settings: Settings,
    job_id: uuid.UUID,
    version_number: int,
    candidate_id: uuid.UUID,
    evaluation_as_of_date,  # noqa: ANN001 - date | None
    candidate_profile_version_id: uuid.UUID | None,
) -> ApiScoreCandidateResponse:
    """Score ONE candidate against one immutable criteria version.

    With ``candidate_profile_version_id`` the score uses exactly that
    historical version, provided it belongs to ``candidate_id`` inside the
    principal's tenant (anything else is the same "not found"); without it, the
    candidate's current effective profile is used."""
    criteria_version = await _require_criteria_version(
        db, principal, job_id=job_id, version_number=version_number
    )
    if candidate_profile_version_id is not None:
        profile_version = await get_profile_version_by_id(
            db, tenant_id=principal.tenant_id, profile_version_id=candidate_profile_version_id
        )
        if profile_version is not None and profile_version.candidate_id != candidate_id:
            profile_version = None
    else:
        profile_version = await get_effective_profile_version(
            db, tenant_id=principal.tenant_id, candidate_id=candidate_id
        )
    if profile_version is None:
        raise _not_found("Candidate profile not found.", "PROFILE_NOT_FOUND")

    defaulted = evaluation_as_of_date is None
    effective_date = effective_business_date(
        evaluation_as_of_date, timezone_name=settings.business_timezone
    )
    try:
        result = await evaluate_and_score_candidate(
            db,
            tenant_id=principal.tenant_id,
            candidate_id=candidate_id,
            candidate_profile_version_id=profile_version.id,
            job_id=job_id,
            job_criteria_version_id=criteria_version.id,
            evaluation_as_of_date=effective_date,
            resolved_profile_version=profile_version,
            resolved_criteria_version=criteria_version,
        )
    except EvaluationInputError as exc:
        await db.rollback()
        if exc.code in _NOT_FOUND_CODES:
            raise _not_found(exc.code, exc.code) from None
        raise ApplicationError(
            ErrorCode.VALIDATION_FAILED, status_code=422, reason=exc.code,
            message="The evaluation inputs cannot be scored.", legacy_detail=exc.code,
        ) from None

    await db.commit()
    evaluation = result.evaluation
    if evaluation.status != EVALUATION_STATUS_COMPLETED:
        raise ApplicationError(
            ErrorCode.VALIDATION_FAILED, status_code=422, reason=evaluation.error_code,
            message="The candidate profile cannot be scored.",
            legacy_detail=evaluation.error_code,
        )
    explanation = ScoreExplanation.model_validate(evaluation.score_explanation)
    return ApiScoreCandidateResponse(
        candidate_id=candidate_id,
        candidate_profile_version_id=profile_version.id,
        job_id=job_id,
        job_criteria_version_id=criteria_version.id,
        evaluation_id=evaluation.id,
        numeric_score=explanation.numeric_score,
        fit_band=explanation.fit_band,
        evaluation_policy_version=explanation.evaluation_policy_version,
        scoring_policy_version=explanation.scoring_policy_version,
        evaluation_as_of_date=explanation.evaluation_as_of_date,
        evaluation_date_defaulted=defaulted,
        reused=result.reused,
        explanation=explanation,
    )


async def rank_candidates(
    db: AsyncSession,
    principal: Principal,
    *,
    settings: Settings,
    job_id: uuid.UUID,
    version_number: int,
    evaluation_as_of_date,  # noqa: ANN001 - date | None
) -> ApiBatchRankingResponse:
    """Rank the tenant's active library with each candidate's CURRENT effective
    profile (no historical-version mixing)."""
    criteria_version = await _require_criteria_version(
        db, principal, job_id=job_id, version_number=version_number
    )
    defaulted = evaluation_as_of_date is None
    effective_date = effective_business_date(
        evaluation_as_of_date, timezone_name=settings.business_timezone
    )
    try:
        ranking = await rank_candidates_for_job(
            db,
            tenant_id=principal.tenant_id,
            job_criteria_version_id=criteria_version.id,
            evaluation_as_of_date=effective_date,
        )
        views = await build_ranked_candidate_views(
            db, tenant_id=principal.tenant_id, ranking=ranking
        )
        await db.commit()
    except BatchRankingError as exc:
        await db.rollback()
        if exc.code == "CRITERIA_VERSION_NOT_FOUND":
            raise _not_found(exc.code, exc.code) from None
        if exc.code == "JOB_ARCHIVED":
            raise ApplicationError(
                ErrorCode.CONFLICT, status_code=409, reason=exc.code,
                message="The job is archived and can no longer be ranked.",
                legacy_detail=exc.code,
            ) from None
        raise ApplicationError(
            ErrorCode.VALIDATION_FAILED, status_code=422, reason=exc.code,
            message="The criteria version cannot be ranked.", legacy_detail=exc.code,
        ) from None

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
            explanation=item.score_explanation,
        )
        for item, view in zip(ranking.results, views, strict=True)
    ]
    return ApiBatchRankingResponse(
        job_criteria_version_id=criteria_version.id,
        evaluation_as_of_date=ranking.evaluation_as_of_date,
        evaluation_date_defaulted=defaulted,
        evaluation_policy_version=ranking.evaluation_policy_version,
        scoring_policy_version=ranking.scoring_policy_version,
        evaluated_count=ranking.evaluated_count,
        reused_count=ranking.reused_count,
        skipped_count=ranking.skipped_count,
        skip_reason_counts=ranking.skip_reason_counts,
        results=results,
    )


async def get_persisted_evaluation(
    db: AsyncSession, principal: Principal, *, evaluation_id: uuid.UUID
) -> ApiEvaluationOut | None:
    evaluation = await get_evaluation(
        db, tenant_id=principal.tenant_id, evaluation_id=evaluation_id
    )
    if evaluation is None:
        return None
    authorized = await get_authorized_profile_version_by_id(
        db,
        tenant_id=principal.tenant_id,
        profile_version_id=evaluation.candidate_profile_version_id,
    )
    visible = authorized is not None
    completed = (
        visible
        and evaluation.status == EVALUATION_STATUS_COMPLETED
        and evaluation.score_explanation is not None
    )
    return ApiEvaluationOut(
        evaluation_id=evaluation.id,
        candidate_id=evaluation.candidate_id,
        candidate_profile_version_id=evaluation.candidate_profile_version_id,
        job_id=evaluation.job_id,
        job_criteria_version_id=evaluation.job_criteria_version_id,
        status=evaluation.status if visible else "UNAVAILABLE",
        fit_band=evaluation.overall_result if visible else None,
        numeric_score=(
            f"{evaluation.numeric_score:.2f}"
            if visible and evaluation.numeric_score is not None
            else None
        ),
        evaluation_as_of_date=evaluation.evaluation_as_of_date,
        evaluation_policy_version=evaluation.policy_engine_version,
        scoring_policy_version=evaluation.scoring_policy_version,
        error_code=evaluation.error_code if visible else None,
        created_at=evaluation.created_at,
        explanation=(
            ScoreExplanation.model_validate(evaluation.score_explanation) if completed else None
        ),
    )

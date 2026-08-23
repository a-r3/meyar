import uuid
from datetime import date

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.evaluation.evaluators import evaluate_criterion
from meyar.evaluation.policy import POLICY_ENGINE_VERSION, compute_overall_result
from meyar.models.candidate_profile_version import (
    PROFILE_STATUS_COMPLETED,
    CandidateProfileVersion,
)
from meyar.models.evaluation import (
    EVALUATION_STATUS_COMPLETED,
    EVALUATION_STATUS_FAILED,
    Evaluation,
)
from meyar.models.job_criteria_version import JobCriteriaVersion
from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.schemas.criteria import CriterionIn
from meyar.scoring.policy import SCORING_POLICY_VERSION, ScoringPolicyError, score_results
from meyar.scoring.schemas import ScoredEvaluationResult
from meyar.services.audit_repo import record_event
from meyar.services.candidate_profile_repo import get_profile_version_by_id
from meyar.services.evaluation_repo import create_evaluation, get_scored_evaluation_by_provenance
from meyar.services.job_criteria_repo import get_criteria_version_by_id


class EvaluationInputError(Exception):
    """The tenant-scoped immutable input versions cannot be evaluated."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


async def evaluate_and_score_candidate(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    candidate_profile_version_id: uuid.UUID,
    job_id: uuid.UUID,
    job_criteria_version_id: uuid.UUID,
    evaluation_as_of_date: date,
    resolved_profile_version: CandidateProfileVersion | None = None,
    resolved_criteria_version: JobCriteriaVersion | None = None,
) -> ScoredEvaluationResult:
    """Evaluate and score one exact profile/criteria/date provenance tuple."""
    profile_version = resolved_profile_version
    if profile_version is None:
        profile_version = await get_profile_version_by_id(
            db, tenant_id=tenant_id, profile_version_id=candidate_profile_version_id
        )
    if (
        profile_version is None
        or profile_version.id != candidate_profile_version_id
        or profile_version.tenant_id != tenant_id
        or profile_version.candidate_id != candidate_id
    ):
        await _record_failure_audit(db, tenant_id, "PROFILE_NOT_FOUND")
        raise EvaluationInputError(
            "PROFILE_NOT_FOUND",
            "No candidate profile version found for this tenant/candidate.",
        )

    criteria_version = resolved_criteria_version
    if criteria_version is None:
        criteria_version = await get_criteria_version_by_id(
            db, tenant_id=tenant_id, criteria_version_id=job_criteria_version_id
        )
    if (
        criteria_version is None
        or criteria_version.id != job_criteria_version_id
        or criteria_version.tenant_id != tenant_id
        or criteria_version.job_id != job_id
    ):
        await _record_failure_audit(db, tenant_id, "CRITERIA_VERSION_NOT_FOUND")
        raise EvaluationInputError(
            "CRITERIA_VERSION_NOT_FOUND",
            "No job criteria version found for this tenant/job.",
        )

    existing = await get_scored_evaluation_by_provenance(
        db,
        tenant_id=tenant_id,
        candidate_profile_version_id=candidate_profile_version_id,
        job_criteria_version_id=job_criteria_version_id,
        evaluation_as_of_date=evaluation_as_of_date,
        policy_engine_version=POLICY_ENGINE_VERSION,
        scoring_policy_version=SCORING_POLICY_VERSION,
    )
    if existing is not None:
        await _record_score_audit(db, existing, reused=True)
        return ScoredEvaluationResult(evaluation=existing, reused=True)

    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="EVALUATION_STARTED",
        metadata={
            "candidate_id": str(candidate_id),
            "job_id": str(job_id),
            "candidate_profile_version_id": str(candidate_profile_version_id),
            "job_criteria_version_id": str(job_criteria_version_id),
            "evaluation_as_of_date": evaluation_as_of_date.isoformat(),
        },
    )

    if (
        profile_version.status != PROFILE_STATUS_COMPLETED
        or profile_version.profile_content is None
    ):
        await _record_failure_audit(db, tenant_id, "INSUFFICIENT_STRUCTURED_DATA")
        raise EvaluationInputError(
            "INSUFFICIENT_STRUCTURED_DATA",
            "Candidate profile version has no completed structured data to evaluate.",
        )

    try:
        profile = CandidateProfileExtraction.model_validate(profile_version.profile_content)
    except ValidationError as exc:
        evaluation = await _persist_failure(
            db,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            candidate_profile_version_id=candidate_profile_version_id,
            job_id=job_id,
            job_criteria_version_id=job_criteria_version_id,
            error_code="PROFILE_SCHEMA_UNSUPPORTED",
            error_message=str(exc),
        )
        return ScoredEvaluationResult(evaluation=evaluation, reused=False)

    try:
        criteria = [CriterionIn.model_validate(item) for item in criteria_version.criteria]
    except ValidationError as exc:
        evaluation = await _persist_failure(
            db,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            candidate_profile_version_id=candidate_profile_version_id,
            job_id=job_id,
            job_criteria_version_id=job_criteria_version_id,
            error_code="CRITERIA_SCHEMA_UNSUPPORTED",
            error_message=str(exc),
        )
        return ScoredEvaluationResult(evaluation=evaluation, reused=False)

    try:
        results = [
            evaluate_criterion(
                criterion, profile, evaluation_as_of_date=evaluation_as_of_date
            )
            for criterion in criteria
        ]
        overall = compute_overall_result(results)
    except Exception as exc:  # criterion policy must fail safely
        evaluation = await _persist_failure(
            db,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            candidate_profile_version_id=candidate_profile_version_id,
            job_id=job_id,
            job_criteria_version_id=job_criteria_version_id,
            error_code="POLICY_ENGINE_ERROR",
            error_message=str(exc),
        )
        return ScoredEvaluationResult(evaluation=evaluation, reused=False)

    evaluation_id = uuid.uuid4()
    try:
        numeric_score, explanation = score_results(
            results,
            fit_band=overall,
            evaluation_id=evaluation_id,
            candidate_profile_version_id=candidate_profile_version_id,
            job_criteria_version_id=job_criteria_version_id,
            evaluation_as_of_date=evaluation_as_of_date,
            evaluation_policy_version=POLICY_ENGINE_VERSION,
        )
    except ScoringPolicyError as exc:
        await _record_failure_audit(db, tenant_id, exc.code)
        raise

    try:
        async with db.begin_nested():
            evaluation = await create_evaluation(
                db,
                evaluation_id=evaluation_id,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                candidate_profile_version_id=candidate_profile_version_id,
                job_id=job_id,
                job_criteria_version_id=job_criteria_version_id,
                status=EVALUATION_STATUS_COMPLETED,
                policy_engine_version=POLICY_ENGINE_VERSION,
                evaluation_as_of_date=evaluation_as_of_date,
                numeric_score=numeric_score,
                scoring_policy_version=SCORING_POLICY_VERSION,
                score_explanation=explanation.model_dump(mode="json"),
                overall_result=overall,
                criterion_results=[result.model_dump(mode="json") for result in results],
            )
    except IntegrityError:
        existing = await get_scored_evaluation_by_provenance(
            db,
            tenant_id=tenant_id,
            candidate_profile_version_id=candidate_profile_version_id,
            job_criteria_version_id=job_criteria_version_id,
            evaluation_as_of_date=evaluation_as_of_date,
            policy_engine_version=POLICY_ENGINE_VERSION,
            scoring_policy_version=SCORING_POLICY_VERSION,
        )
        if existing is None:
            raise
        await _record_score_audit(db, existing, reused=True)
        return ScoredEvaluationResult(evaluation=existing, reused=True)

    completed_event_type = (
        "EVALUATION_MANUAL_REVIEW_REQUIRED"
        if overall == "MANUAL_REVIEW_REQUIRED"
        else "EVALUATION_COMPLETED"
    )
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type=completed_event_type,
        metadata={
            "evaluation_id": str(evaluation.id),
            "candidate_id": str(candidate_id),
            "job_id": str(job_id),
            "overall_result": overall,
            "policy_engine_version": POLICY_ENGINE_VERSION,
        },
    )
    await _record_score_audit(db, evaluation, reused=False)
    return ScoredEvaluationResult(evaluation=evaluation, reused=False)


async def evaluate_candidate(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    candidate_profile_version_id: uuid.UUID,
    job_id: uuid.UUID,
    job_criteria_version_id: uuid.UUID,
    evaluation_as_of_date: date,
) -> Evaluation:
    """Compatibility-shaped entry point backed by the one scored path."""
    result = await evaluate_and_score_candidate(
        db,
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_profile_version_id=candidate_profile_version_id,
        job_id=job_id,
        job_criteria_version_id=job_criteria_version_id,
        evaluation_as_of_date=evaluation_as_of_date,
    )
    return result.evaluation


async def _record_score_audit(db: AsyncSession, evaluation: Evaluation, *, reused: bool) -> None:
    await record_event(
        db,
        tenant_id=evaluation.tenant_id,
        event_type="CANDIDATE_SCORE_COMPUTED",
        metadata={
            "evaluation_id": str(evaluation.id),
            "candidate_id": str(evaluation.candidate_id),
            "candidate_profile_version_id": str(evaluation.candidate_profile_version_id),
            "job_criteria_version_id": str(evaluation.job_criteria_version_id),
            "evaluation_as_of_date": evaluation.evaluation_as_of_date.isoformat()
            if evaluation.evaluation_as_of_date
            else None,
            "numeric_score": str(evaluation.numeric_score),
            "fit_band": evaluation.overall_result,
            "evaluation_policy_version": evaluation.policy_engine_version,
            "scoring_policy_version": evaluation.scoring_policy_version,
            "reused": reused,
        },
    )


async def _persist_failure(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    candidate_profile_version_id: uuid.UUID,
    job_id: uuid.UUID,
    job_criteria_version_id: uuid.UUID,
    error_code: str,
    error_message: str,
) -> Evaluation:
    evaluation = await create_evaluation(
        db,
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_profile_version_id=candidate_profile_version_id,
        job_id=job_id,
        job_criteria_version_id=job_criteria_version_id,
        status=EVALUATION_STATUS_FAILED,
        policy_engine_version=POLICY_ENGINE_VERSION,
        error_code=error_code,
        error_message=error_message,
    )
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="EVALUATION_FAILED",
        metadata={
            "evaluation_id": str(evaluation.id),
            "candidate_id": str(candidate_id),
            "job_id": str(job_id),
            "error_code": error_code,
        },
    )
    return evaluation


async def _record_failure_audit(db: AsyncSession, tenant_id: uuid.UUID, error_code: str) -> None:
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="EVALUATION_FAILED",
        metadata={"error_code": error_code},
    )

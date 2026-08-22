import uuid

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.evaluation.evaluators import evaluate_criterion
from meyar.evaluation.policy import POLICY_ENGINE_VERSION, compute_overall_result
from meyar.models.candidate_profile_version import PROFILE_STATUS_COMPLETED
from meyar.models.evaluation import (
    EVALUATION_STATUS_COMPLETED,
    EVALUATION_STATUS_FAILED,
    Evaluation,
)
from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.schemas.criteria import CriterionIn
from meyar.services.audit_repo import record_event
from meyar.services.candidate_profile_repo import get_profile_version_by_id
from meyar.services.evaluation_repo import create_evaluation
from meyar.services.job_criteria_repo import get_criteria_version_by_id


class EvaluationInputError(Exception):
    """Raised when evaluation cannot even be attempted — the referenced
    profile/criteria version does not resolve for this tenant, or does not
    belong to the stated candidate/job. Nothing to version, so no
    Evaluation row is created for this case (mirrors
    ExtractionPreconditionError in Slice 4)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


async def evaluate_candidate(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    candidate_profile_version_id: uuid.UUID,
    job_id: uuid.UUID,
    job_criteria_version_id: uuid.UUID,
) -> Evaluation:
    """Runs the full Slice 5 pipeline: resolves the exact immutable
    CandidateProfileVersion + JobCriteriaVersion (tenant-scoped — a
    cross-tenant id simply fails to resolve), evaluates each criterion
    deterministically, computes the overall fit band, and persists an
    immutable Evaluation. Caller must not resolve "latest" versions
    itself and pass them in without keeping the exact ids — this function
    only ever operates on the concrete version ids given."""
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="EVALUATION_STARTED",
        metadata={
            "candidate_id": str(candidate_id),
            "job_id": str(job_id),
            "candidate_profile_version_id": str(candidate_profile_version_id),
            "job_criteria_version_id": str(job_criteria_version_id),
        },
    )

    profile_version = await get_profile_version_by_id(
        db, tenant_id=tenant_id, profile_version_id=candidate_profile_version_id
    )
    if profile_version is None or profile_version.candidate_id != candidate_id:
        await _record_failure_audit(db, tenant_id, "PROFILE_NOT_FOUND")
        raise EvaluationInputError(
            "PROFILE_NOT_FOUND",
            "No candidate profile version found for this tenant/candidate.",
        )

    criteria_version = await get_criteria_version_by_id(
        db, tenant_id=tenant_id, criteria_version_id=job_criteria_version_id
    )
    if criteria_version is None or criteria_version.job_id != job_id:
        await _record_failure_audit(db, tenant_id, "CRITERIA_VERSION_NOT_FOUND")
        raise EvaluationInputError(
            "CRITERIA_VERSION_NOT_FOUND",
            "No job criteria version found for this tenant/job.",
        )

    status_not_completed = profile_version.status != PROFILE_STATUS_COMPLETED
    content_missing = profile_version.profile_content is None
    if status_not_completed or content_missing:
        await _record_failure_audit(db, tenant_id, "INSUFFICIENT_STRUCTURED_DATA")
        raise EvaluationInputError(
            "INSUFFICIENT_STRUCTURED_DATA",
            "Candidate profile version has no completed structured data to evaluate.",
        )

    try:
        profile = CandidateProfileExtraction.model_validate(profile_version.profile_content)
    except ValidationError as exc:
        return await _persist_failure(
            db,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            candidate_profile_version_id=candidate_profile_version_id,
            job_id=job_id,
            job_criteria_version_id=job_criteria_version_id,
            error_code="PROFILE_SCHEMA_UNSUPPORTED",
            error_message=str(exc),
        )

    try:
        criteria = [CriterionIn.model_validate(c) for c in criteria_version.criteria]
    except ValidationError as exc:
        return await _persist_failure(
            db,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            candidate_profile_version_id=candidate_profile_version_id,
            job_id=job_id,
            job_criteria_version_id=job_criteria_version_id,
            error_code="CRITERIA_SCHEMA_UNSUPPORTED",
            error_message=str(exc),
        )

    try:
        results = [evaluate_criterion(criterion, profile) for criterion in criteria]
        overall = compute_overall_result(results)
    except Exception as exc:  # policy engine must never crash the request
        return await _persist_failure(
            db,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            candidate_profile_version_id=candidate_profile_version_id,
            job_id=job_id,
            job_criteria_version_id=job_criteria_version_id,
            error_code="POLICY_ENGINE_ERROR",
            error_message=str(exc),
        )

    evaluation = await create_evaluation(
        db,
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_profile_version_id=candidate_profile_version_id,
        job_id=job_id,
        job_criteria_version_id=job_criteria_version_id,
        status=EVALUATION_STATUS_COMPLETED,
        policy_engine_version=POLICY_ENGINE_VERSION,
        overall_result=overall,
        criterion_results=[r.model_dump(mode="json") for r in results],
    )
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
    return evaluation


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

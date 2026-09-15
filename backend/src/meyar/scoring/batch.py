import uuid
from collections import Counter
from datetime import date

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.evaluation.policy import POLICY_ENGINE_VERSION
from meyar.evaluation.service import EvaluationInputError, evaluate_and_score_candidate
from meyar.models.candidate import CANDIDATE_STATUS_ACTIVE
from meyar.models.candidate_profile_version import PROFILE_STATUS_COMPLETED
from meyar.models.job import JOB_STATUS_ACTIVE
from meyar.schemas.criteria import CriterionIn
from meyar.scoring.policy import (
    SCORING_POLICY_VERSION,
    validate_criteria_total_weight,
)
from meyar.scoring.schemas import BatchRankingResult, RankedCandidate, ScoreExplanation
from meyar.services.audit_repo import record_event
from meyar.services.candidate_profile_repo import list_current_profile_versions_for_tenant
from meyar.services.candidate_repo import list_candidates_for_tenant
from meyar.services.job_criteria_repo import get_criteria_version_by_id
from meyar.services.job_repo import get_job

FIT_TIERS = {
    "STRONG_MATCH": 0,
    "POTENTIAL_MATCH": 1,
    "MANUAL_REVIEW_REQUIRED": 2,
    "INSUFFICIENT_EVIDENCE": 3,
}


class BatchRankingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def fit_tier(fit_band: str) -> int:
    try:
        return FIT_TIERS[fit_band]
    except KeyError as exc:
        raise BatchRankingError("UNKNOWN_FIT_BAND", f"Unsupported fit band: {fit_band}") from exc


async def rank_candidates_for_job(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    job_criteria_version_id: uuid.UUID,
    evaluation_as_of_date: date,
) -> BatchRankingResult:
    """Score the tenant's active library using one current profile per candidate.

    Enforced here (the shared service every caller — UI, REST API, CLI, and
    any future agent tool — goes through) rather than only at a router/
    template layer: an ARCHIVED job is not a current, evaluable vacancy, so
    no new ranking/Evaluation may be created against any of its criteria
    versions, however the request reaches this function. See
    docs/DECISIONS.md D-033. This never touches historical Evaluation rows
    or JobCriteriaVersion data — archiving remains non-destructive, and
    reading past results is a separate code path unaffected by this check.
    """
    criteria_version = await get_criteria_version_by_id(
        db, tenant_id=tenant_id, criteria_version_id=job_criteria_version_id
    )
    if criteria_version is None:
        raise BatchRankingError(
            "CRITERIA_VERSION_NOT_FOUND", "No job criteria version found for this tenant."
        )
    job = await get_job(db, tenant_id=tenant_id, job_id=criteria_version.job_id)
    if job is None or job.status != JOB_STATUS_ACTIVE:
        raise BatchRankingError(
            "JOB_ARCHIVED",
            "The job for this criteria version is archived and can no longer be ranked.",
        )
    try:
        criteria = [CriterionIn.model_validate(item) for item in criteria_version.criteria]
    except ValidationError as exc:
        raise BatchRankingError(
            "CRITERIA_SCHEMA_UNSUPPORTED", "The criteria version schema is unsupported."
        ) from exc

    # Fail a legacy all-zero version before loading or iterating candidates.
    validate_criteria_total_weight(criteria)

    candidates = await list_candidates_for_tenant(db, tenant_id=tenant_id)
    current_profiles = await list_current_profile_versions_for_tenant(db, tenant_id=tenant_id)
    profiles_by_candidate = {profile.candidate_id: profile for profile in current_profiles}
    skipped: Counter[str] = Counter()
    ranked: list[RankedCandidate] = []
    reused_count = 0

    for candidate in candidates:
        if candidate.status != CANDIDATE_STATUS_ACTIVE:
            skipped["CANDIDATE_NOT_ACTIVE"] += 1
            continue
        profile = profiles_by_candidate.get(candidate.id)
        if profile is None:
            skipped["NO_CURRENT_PROFILE"] += 1
            continue
        if profile.status != PROFILE_STATUS_COMPLETED or profile.profile_content is None:
            skipped["CURRENT_PROFILE_NOT_COMPLETED"] += 1
            continue

        try:
            scored = await evaluate_and_score_candidate(
                db,
                tenant_id=tenant_id,
                candidate_id=candidate.id,
                candidate_profile_version_id=profile.id,
                job_id=criteria_version.job_id,
                job_criteria_version_id=criteria_version.id,
                evaluation_as_of_date=evaluation_as_of_date,
                resolved_profile_version=profile,
                resolved_criteria_version=criteria_version,
            )
        except EvaluationInputError as exc:
            skipped[exc.code] += 1
            continue

        evaluation = scored.evaluation
        if evaluation.status != "COMPLETED":
            skipped["CURRENT_PROFILE_INVALID"] += 1
            continue
        if (
            evaluation.numeric_score is None
            or evaluation.overall_result is None
            or evaluation.evaluation_as_of_date is None
            or evaluation.scoring_policy_version is None
            or evaluation.score_explanation is None
        ):
            raise BatchRankingError(
                "INCOMPLETE_SCORED_EVALUATION",
                "A completed scored evaluation is missing required provenance.",
            )
        tier = fit_tier(evaluation.overall_result)
        if scored.reused:
            reused_count += 1
        ranked.append(
            RankedCandidate(
                rank=1,
                candidate_id=candidate.id,
                candidate_profile_version_id=profile.id,
                evaluation_id=evaluation.id,
                numeric_score=evaluation.numeric_score,
                fit_band=evaluation.overall_result,
                fit_tier=tier,
                evaluation_as_of_date=evaluation.evaluation_as_of_date,
                evaluation_policy_version=evaluation.policy_engine_version,
                scoring_policy_version=evaluation.scoring_policy_version,
                score_explanation=ScoreExplanation.model_validate(
                    evaluation.score_explanation
                ),
            )
        )

    ranked.sort(key=lambda item: (item.fit_tier, -item.numeric_score, item.candidate_id.int))
    evaluated_count = len(ranked)
    eligible = [item for item in ranked if item.fit_band in ("STRONG_MATCH", "POTENTIAL_MATCH")]
    eligible_count = len(eligible)
    presented = eligible if criteria_version.eligible_only else ranked
    ranked = [
        item.model_copy(update={"rank": index})
        for index, item in enumerate(presented[: criteria_version.result_limit], 1)
    ]

    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="JOB_BATCH_RANKED",
        metadata={
            "job_criteria_version_id": str(job_criteria_version_id),
            "evaluation_as_of_date": evaluation_as_of_date.isoformat(),
            "evaluation_policy_version": POLICY_ENGINE_VERSION,
            "scoring_policy_version": SCORING_POLICY_VERSION,
            "evaluated_count": evaluated_count,
            "eligible_count": eligible_count,
            "result_limit": criteria_version.result_limit,
            "reused_count": reused_count,
            "skipped_count": sum(skipped.values()),
            "skip_reason_counts": dict(sorted(skipped.items())),
        },
    )
    return BatchRankingResult(
        tenant_id=tenant_id,
        job_criteria_version_id=job_criteria_version_id,
        evaluation_as_of_date=evaluation_as_of_date,
        evaluation_policy_version=POLICY_ENGINE_VERSION,
        scoring_policy_version=SCORING_POLICY_VERSION,
        result_limit=criteria_version.result_limit,
        eligible_count=eligible_count,
        eligible_only=criteria_version.eligible_only,
        evaluated_count=evaluated_count,
        reused_count=reused_count,
        skipped_count=sum(skipped.values()),
        skip_reason_counts=dict(sorted(skipped.items())),
        results=ranked,
    )

import uuid
from collections import Counter
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from meyar.schemas.criteria import CriterionIn
from meyar.schemas.evaluation import (
    CRITERION_STATUS_CONFLICTING_EVIDENCE,
    CRITERION_STATUS_MANUAL_REVIEW_REQUIRED,
    CRITERION_STATUS_MATCH,
    CRITERION_STATUS_NOT_MATCHED,
    CRITERION_STATUS_PARTIAL_MATCH,
    CRITERION_STATUS_UNKNOWN,
    CriterionResult,
)
from meyar.scoring.schemas import (
    CriterionScoreContribution,
    ScoreEvidenceRef,
    ScoreExplanation,
)

SCORING_POLICY_VERSION = "meyar-score-v1"

STATUS_FACTORS: dict[str, Decimal] = {
    CRITERION_STATUS_MATCH: Decimal("1.0"),
    CRITERION_STATUS_PARTIAL_MATCH: Decimal("0.5"),
    CRITERION_STATUS_NOT_MATCHED: Decimal("0.0"),
    CRITERION_STATUS_UNKNOWN: Decimal("0.0"),
    CRITERION_STATUS_CONFLICTING_EVIDENCE: Decimal("0.0"),
    CRITERION_STATUS_MANUAL_REVIEW_REQUIRED: Decimal("0.0"),
}

STATUS_MEANINGS: dict[str, str] = {
    CRITERION_STATUS_MATCH: "EVIDENCE_SUPPORTS_MATCH",
    CRITERION_STATUS_PARTIAL_MATCH: "EVIDENCE_SUPPORTS_PARTIAL_MATCH",
    CRITERION_STATUS_NOT_MATCHED: "EVIDENCE_SUPPORTS_NON_MATCH",
    CRITERION_STATUS_UNKNOWN: "INSUFFICIENT_EVIDENCE_TO_DETERMINE",
    CRITERION_STATUS_CONFLICTING_EVIDENCE: "EVIDENCE_CONFLICT_REQUIRES_REVIEW",
    CRITERION_STATUS_MANUAL_REVIEW_REQUIRED: "HUMAN_REVIEW_REQUIRED",
}


class ScoringPolicyError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def criterion_weight(value: float) -> Decimal:
    """Canonical float-to-Decimal conversion; never preserves binary artifacts."""
    return Decimal(str(value))


def validate_positive_total_weight(results: list[CriterionResult]) -> Decimal:
    total = sum((criterion_weight(result.configured_weight) for result in results), Decimal("0"))
    if total == 0:
        raise ScoringPolicyError(
            "ZERO_TOTAL_CRITERION_WEIGHT",
            "The criteria version has no positive scoring weight.",
        )
    return total


def validate_criteria_total_weight(criteria: list[CriterionIn]) -> Decimal:
    total = sum((criterion_weight(item.weight) for item in criteria), Decimal("0"))
    if total == 0:
        raise ScoringPolicyError(
            "ZERO_TOTAL_CRITERION_WEIGHT",
            "The criteria version has no positive scoring weight.",
        )
    return total


def _factor_for(status: str) -> Decimal:
    try:
        return STATUS_FACTORS[status]
    except KeyError as exc:
        raise ScoringPolicyError(
            "UNKNOWN_CRITERION_STATUS", f"Unsupported criterion status: {status}"
        ) from exc


def score_results(
    results: list[CriterionResult],
    *,
    fit_band: str,
    evaluation_id: uuid.UUID,
    candidate_profile_version_id: uuid.UUID,
    job_criteria_version_id: uuid.UUID,
    evaluation_as_of_date: date,
    evaluation_policy_version: str,
) -> tuple[Decimal, ScoreExplanation]:
    total_weight = validate_positive_total_weight(results)
    total_weighted_points = Decimal("0")
    contributions: list[CriterionScoreContribution] = []

    for result in results:
        weight = criterion_weight(result.configured_weight)
        factor = _factor_for(result.status)
        weighted_points = weight * factor
        total_weighted_points += weighted_points
        contributions.append(
            CriterionScoreContribution(
                criterion_id=result.criterion_id,
                criterion_kind=result.kind,
                criterion_type=result.type,
                weight=str(weight),
                status=result.status,
                status_meaning=STATUS_MEANINGS[result.status],
                factor=str(factor),
                weighted_points=str(weighted_points),
                reason_code=result.reason_code,
                evidence_state="REFERENCED" if result.evidence else "NONE",
                evidence_references=[
                    ScoreEvidenceRef(page=evidence.page, block_index=evidence.block_index)
                    for evidence in result.evidence
                ],
                manual_review_required=result.manual_review_required,
            )
        )

    raw_score = Decimal("100") * total_weighted_points / total_weight
    numeric_score = raw_score.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if not Decimal("0.00") <= numeric_score <= Decimal("100.00"):
        raise ScoringPolicyError("SCORE_OUT_OF_RANGE", "Computed score is outside 0–100.")

    counts = Counter(result.status for result in results)
    explanation = ScoreExplanation(
        numeric_score=format(numeric_score, ".2f"),
        total_weight=str(total_weight),
        total_weighted_points=str(total_weighted_points),
        fit_band=fit_band,
        evaluation_policy_version=evaluation_policy_version,
        scoring_policy_version=SCORING_POLICY_VERSION,
        evaluation_as_of_date=evaluation_as_of_date,
        evaluation_id=evaluation_id,
        candidate_profile_version_id=candidate_profile_version_id,
        job_criteria_version_id=job_criteria_version_id,
        criterion_count=len(results),
        status_counts={status: counts.get(status, 0) for status in STATUS_FACTORS},
        criteria=contributions,
    )
    return numeric_score, explanation

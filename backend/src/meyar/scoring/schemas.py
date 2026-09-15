import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from meyar.models.evaluation import Evaluation


class ScoreEvidenceRef(BaseModel):
    """Location-only evidence provenance; quotes are not duplicated into scores."""

    page: int = Field(ge=1)
    block_index: int = Field(ge=0)


class CriterionScoreContribution(BaseModel):
    model_config = {"extra": "forbid"}

    criterion_id: str
    criterion_kind: str
    criterion_type: str
    weight: str
    status: str
    status_meaning: str
    factor: str
    weighted_points: str
    reason_code: str
    explanation: str
    evidence_state: str
    evidence_references: list[ScoreEvidenceRef]
    manual_review_required: bool


class ScoreExplanation(BaseModel):
    model_config = {"extra": "forbid"}

    numeric_score: str
    total_weight: str
    total_weighted_points: str
    fit_band: str
    evaluation_policy_version: str
    scoring_policy_version: str
    evaluation_as_of_date: date
    evaluation_id: uuid.UUID
    candidate_profile_version_id: uuid.UUID
    job_criteria_version_id: uuid.UUID
    criterion_count: int = Field(ge=1)
    status_counts: dict[str, int]
    criteria: list[CriterionScoreContribution]


@dataclass(frozen=True)
class ScoredEvaluationResult:
    evaluation: Evaluation
    reused: bool


class RankedCandidate(BaseModel):
    model_config = {"extra": "forbid"}

    rank: int = Field(ge=1)
    candidate_id: uuid.UUID
    candidate_profile_version_id: uuid.UUID
    evaluation_id: uuid.UUID
    numeric_score: Decimal
    fit_band: str
    fit_tier: int = Field(ge=0, le=3)
    evaluation_as_of_date: date
    evaluation_policy_version: str
    scoring_policy_version: str
    score_explanation: ScoreExplanation


class BatchRankingResult(BaseModel):
    model_config = {"extra": "forbid"}

    tenant_id: uuid.UUID
    job_criteria_version_id: uuid.UUID
    evaluation_as_of_date: date
    evaluation_policy_version: str
    scoring_policy_version: str
    result_limit: int = Field(ge=1, le=100)
    eligible_count: int = Field(ge=0)
    eligible_only: bool
    evaluated_count: int = Field(ge=0)
    reused_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    skip_reason_counts: dict[str, int]
    results: list[RankedCandidate]

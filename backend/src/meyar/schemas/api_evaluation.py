"""Slice 12 — external-facing DTOs for `/api/v1/jobs/{job_id}/criteria/
{version_number}/score` and `/rank`. `numeric_score` is always the
canonical ScoreExplanation decimal string (e.g. "75.00"), never a bare
Decimal/float — see `meyar.scoring.schemas.ScoreExplanation`."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from meyar.scoring.schemas import ScoreExplanation


class ApiScoreCandidateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    candidate_id: uuid.UUID
    # Omitted → the configured business "today", resolved once at the boundary
    # and echoed back. Supplied → used unchanged (reproducible scoring).
    evaluation_as_of_date: date | None = None
    # Optional exact historical profile version of THIS candidate, for
    # reproducible single-candidate scoring. Batch ranking never accepts one.
    candidate_profile_version_id: uuid.UUID | None = None


class ApiScoreCandidateResponse(BaseModel):
    model_config = {"extra": "forbid"}

    candidate_id: uuid.UUID
    candidate_profile_version_id: uuid.UUID
    job_id: uuid.UUID
    job_criteria_version_id: uuid.UUID
    evaluation_id: uuid.UUID
    numeric_score: str
    fit_band: str
    evaluation_policy_version: str
    scoring_policy_version: str
    evaluation_as_of_date: date
    evaluation_date_defaulted: bool = False
    reused: bool
    explanation: ScoreExplanation


class ApiBatchRankRequest(BaseModel):
    model_config = {"extra": "forbid"}

    evaluation_as_of_date: date | None = None


class ApiRankedCandidate(BaseModel):
    model_config = {"extra": "forbid"}

    rank: int
    candidate_id: uuid.UUID
    full_name: str | None = None
    candidate_profile_version_id: uuid.UUID
    evaluation_id: uuid.UUID
    numeric_score: str
    fit_band: str
    evaluation_as_of_date: date
    evaluation_policy_version: str
    scoring_policy_version: str
    # The full deterministic explanation of this exact score.
    explanation: ScoreExplanation


class ApiBatchRankingResponse(BaseModel):
    model_config = {"extra": "forbid"}

    job_criteria_version_id: uuid.UUID
    evaluation_as_of_date: date
    evaluation_date_defaulted: bool = False
    evaluation_policy_version: str
    scoring_policy_version: str
    evaluated_count: int
    reused_count: int
    skipped_count: int
    skip_reason_counts: dict[str, int]
    # Bounded by the version's result_limit (1..100, enforced in the database).
    results: list[ApiRankedCandidate] = Field(max_length=100)


class ApiEvaluationOut(BaseModel):
    """One persisted, immutable evaluation. ``UNAVAILABLE`` means its profile
    version is no longer evidence-authorized: no score or explanation is shown
    (the same rule the candidate detail applies)."""

    model_config = {"extra": "forbid"}

    evaluation_id: uuid.UUID
    candidate_id: uuid.UUID
    candidate_profile_version_id: uuid.UUID
    job_id: uuid.UUID
    job_criteria_version_id: uuid.UUID
    status: str
    fit_band: str | None = None
    numeric_score: str | None = None
    evaluation_as_of_date: date | None = None
    evaluation_policy_version: str
    scoring_policy_version: str | None = None
    # Closed machine code of a recorded failed evaluation (never raw errors).
    error_code: str | None = None
    created_at: datetime
    explanation: ScoreExplanation | None = None

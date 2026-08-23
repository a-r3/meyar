"""Slice 12 — external-facing DTOs for `/api/v1/jobs/{job_id}/criteria/
{version_number}/score` and `/rank`. `numeric_score` is always the
canonical ScoreExplanation decimal string (e.g. "75.00"), never a bare
Decimal/float — see `meyar.scoring.schemas.ScoreExplanation`."""

import uuid
from datetime import date

from pydantic import BaseModel

from meyar.scoring.schemas import ScoreExplanation


class ApiScoreCandidateRequest(BaseModel):
    model_config = {"extra": "forbid"}

    candidate_id: uuid.UUID
    evaluation_as_of_date: date


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
    reused: bool
    explanation: ScoreExplanation


class ApiBatchRankRequest(BaseModel):
    model_config = {"extra": "forbid"}

    evaluation_as_of_date: date


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


class ApiBatchRankingResponse(BaseModel):
    model_config = {"extra": "forbid"}

    job_criteria_version_id: uuid.UUID
    evaluation_as_of_date: date
    evaluation_policy_version: str
    scoring_policy_version: str
    evaluated_count: int
    reused_count: int
    skipped_count: int
    skip_reason_counts: dict[str, int]
    results: list[ApiRankedCandidate]

"""Slice 12 — narrow external-facing DTOs for `/api/v1/search*`.

Deliberately distinct from `meyar.search.schemas.CandidateSearchRequest`:
no `embedding_config`/`structured_weight`/`semantic_weight` fields — those
stay trusted-server-only and are injected by the route, never accepted
from a client. `extra="forbid"` everywhere so a client cannot smuggle an
unreviewed field into the ranking path.
"""

import uuid
from datetime import date

from pydantic import BaseModel, Field

from meyar.search.planner_schemas import PlannerOutcome, PlannerReasonCode
from meyar.search.policy import MAX_SEARCH_LIMIT, MAX_SEMANTIC_QUERY_LENGTH, MIN_SEARCH_LIMIT
from meyar.search.schemas import (
    PreferredFilterMatch,
    PreferredFilters,
    RequiredFilterMatch,
    RequiredFilters,
    SearchMode,
)


class ApiCandidateSearchRequest(BaseModel):
    """Structured/hybrid search request DTO. `embedding_config` and the
    structured/semantic weights are never client-suppliable — the route
    injects trusted server-side values before building the internal
    `CandidateSearchRequest`."""

    model_config = {"extra": "forbid"}

    mode: SearchMode
    required_filters: RequiredFilters = Field(default_factory=RequiredFilters)
    preferred_filters: PreferredFilters = Field(default_factory=PreferredFilters)
    semantic_query: str | None = Field(default=None, max_length=MAX_SEMANTIC_QUERY_LENGTH)
    as_of_date: date | None = None
    limit: int = Field(default=20, ge=MIN_SEARCH_LIMIT, le=MAX_SEARCH_LIMIT)


class ApiNaturalLanguageSearchRequest(BaseModel):
    model_config = {"extra": "forbid"}

    query: str = Field(min_length=1, max_length=4000)
    as_of_date: date


class ApiCandidateSearchResultItem(BaseModel):
    """One ranked candidate, plus presentation-only identity (full_name
    only — never email/phone in a search result, per identity
    minimization: CandidateIdentity is never a scoring/search signal)."""

    model_config = {"extra": "forbid"}

    candidate_id: uuid.UUID
    full_name: str | None = None
    rank: int
    mode: SearchMode
    relevance_score: float
    structured_score: float | None = None
    semantic_score: float | None = None
    required_filters_matched: list[RequiredFilterMatch]
    preferred_filters_matched: list[PreferredFilterMatch]
    candidate_profile_version_id: uuid.UUID
    search_policy_version: str


class ApiCandidateSearchResponse(BaseModel):
    model_config = {"extra": "forbid"}

    mode: SearchMode
    policy_version: str
    results: list[ApiCandidateSearchResultItem]
    result_count: int
    limit: int


class ApiNaturalLanguageSearchResponse(BaseModel):
    """Preserves the exact 7-way typed planner outcome — no collapse to a
    generic error. `search` is populated only when `outcome ==
    EXECUTABLE` (mirrors `PlannedCandidateSearchResponse`'s own
    executable/search_response contract)."""

    model_config = {"extra": "forbid"}

    outcome: PlannerOutcome
    executable: bool
    reason_codes: list[PlannerReasonCode]
    search: ApiCandidateSearchResponse | None = None

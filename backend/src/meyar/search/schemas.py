"""Slice 8 — the strict internal search-request/result contract.

This is NOT the natural-language SearchPlan LLM parser (Slice 9 owns
that) — it is the validated, deterministic structure Slice 9 will
eventually produce and Slice 11/12 will eventually expose. `extra` is
forbidden everywhere so no client can smuggle an unreviewed dynamic
field into the ranking path. See docs/DECISIONS.md (meyar-search-v1)."""

import uuid
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from meyar.schemas.criteria import ProhibitedCriterionError, find_prohibited_term
from meyar.search.policy import (
    DEFAULT_SEMANTIC_WEIGHT,
    DEFAULT_STRUCTURED_WEIGHT,
    MAX_SEARCH_LIMIT,
    MAX_SEMANTIC_QUERY_LENGTH,
    MIN_SEARCH_LIMIT,
    WEIGHT_SUM_TOLERANCE,
)

_FILTER_MAX_ITEMS = 20


class NamedDurationFilter(BaseModel):
    model_config = {"extra": "forbid"}

    value: str = Field(min_length=1, max_length=200)
    min_years: float | None = Field(default=None, ge=0, le=60)


class LanguageLevelFilter(BaseModel):
    model_config = {"extra": "forbid"}

    value: str = Field(min_length=1, max_length=200)
    required_level: str | None = Field(default=None, min_length=1, max_length=50)


class SearchMode(StrEnum):
    STRUCTURED_ONLY = "STRUCTURED_ONLY"
    SEMANTIC_ONLY = "SEMANTIC_ONLY"
    HYBRID = "HYBRID"


class RequiredFilters(BaseModel):
    """Hard eligibility constraints. Only approved professional fields —
    the exact same denylist as job criteria (docs/SECURITY_PRIVACY.md)
    applies at the CandidateSearchRequest level below."""

    model_config = {"extra": "forbid"}

    skills: list[str] = Field(default_factory=list, max_length=_FILTER_MAX_ITEMS)
    certifications: list[str] = Field(default_factory=list, max_length=_FILTER_MAX_ITEMS)
    languages: list[str] = Field(default_factory=list, max_length=_FILTER_MAX_ITEMS)
    education: list[str] = Field(default_factory=list, max_length=_FILTER_MAX_ITEMS)
    min_total_experience_years: float | None = Field(default=None, ge=0, le=60)
    skill_experience: list[NamedDurationFilter] = Field(
        default_factory=list, max_length=_FILTER_MAX_ITEMS
    )
    domain_experience: list[NamedDurationFilter] = Field(
        default_factory=list, max_length=_FILTER_MAX_ITEMS
    )
    language_levels: list[LanguageLevelFilter] = Field(
        default_factory=list, max_length=_FILTER_MAX_ITEMS
    )


class PreferredFilters(RequiredFilters):
    """Same shape as RequiredFilters, but soft ranking signals rather than
    hard eligibility gates — kept as a distinct class (not a type alias)
    so the two concepts never get confused at a call site."""


class EmbeddingSearchConfig(BaseModel):
    """The ONE coherent embedding configuration semantic retrieval runs
    against — provider/model_name/model_revision/serializer_version/
    embedding_dimensions together identify a single compatible group of
    CandidateEmbeddingVersion rows (mirrors the Slice 7 seven-field
    provenance identity, minus the two fields that vary per-row:
    candidate_profile_version_id and source_sha256). Never "whatever
    embedding row is newest" — see docs/DECISIONS.md."""

    model_config = {"extra": "forbid"}

    provider: str = Field(min_length=1, max_length=32)
    model_name: str = Field(min_length=1, max_length=128)
    # "" is MODEL_REVISION_UNKNOWN (see
    # meyar.models.candidate_embedding_version) — an explicit, deliberate
    # value, never confused with "any revision".
    model_revision: str = Field(default="", max_length=64)
    serializer_version: str = Field(min_length=1, max_length=64)
    embedding_dimensions: int = Field(ge=1, le=8192)


class CandidateSearchRequest(BaseModel):
    """Strict internal search request. `extra="forbid"` — no dynamic
    field names, no client-suppliable tenant id (tenant_id is always an
    explicit parameter to the service, never a field here)."""

    model_config = {"extra": "forbid"}

    mode: SearchMode
    required_filters: RequiredFilters = Field(default_factory=RequiredFilters)
    preferred_filters: PreferredFilters = Field(default_factory=PreferredFilters)
    semantic_query: str | None = Field(default=None, max_length=MAX_SEMANTIC_QUERY_LENGTH)
    embedding_config: EmbeddingSearchConfig | None = None
    # Deterministic reference date for experience-duration filters — see
    # docs/DECISIONS.md: an "as of" date is required whenever a
    # min_total_experience_years filter is set, so identical persisted
    # search state never silently changes because the wall clock did.
    as_of_date: date | None = None
    limit: int = Field(default=20, ge=MIN_SEARCH_LIMIT, le=MAX_SEARCH_LIMIT)
    structured_weight: float = Field(default=DEFAULT_STRUCTURED_WEIGHT, ge=0.0, le=1.0)
    semantic_weight: float = Field(default=DEFAULT_SEMANTIC_WEIGHT, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_semantic_query_and_config(self) -> "CandidateSearchRequest":
        needs_semantic = self.mode in (SearchMode.SEMANTIC_ONLY, SearchMode.HYBRID)
        if needs_semantic:
            normalized = (self.semantic_query or "").strip()
            if not normalized:
                raise ValueError(
                    "semantic_query is required (non-empty after normalization) for "
                    "SEMANTIC_ONLY/HYBRID mode."
                )
            if not normalized.isprintable():
                raise ValueError("semantic_query must not contain control characters.")
            if self.embedding_config is None:
                raise ValueError("embedding_config is required for SEMANTIC_ONLY/HYBRID mode.")
        else:
            if self.semantic_query is not None:
                raise ValueError("semantic_query must not be set for STRUCTURED_ONLY mode.")
            if self.embedding_config is not None:
                raise ValueError("embedding_config must not be set for STRUCTURED_ONLY mode.")
        return self

    @model_validator(mode="after")
    def _validate_weights(self) -> "CandidateSearchRequest":
        if self.mode == SearchMode.HYBRID:
            total = self.structured_weight + self.semantic_weight
            if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
                raise ValueError(
                    f"structured_weight + semantic_weight must sum to 1.0 (got {total})."
                )
        return self

    @model_validator(mode="after")
    def _validate_experience_reproducibility(self) -> "CandidateSearchRequest":
        needs_as_of = (
            self.required_filters.min_total_experience_years is not None
            or self.preferred_filters.min_total_experience_years is not None
            or any(
                item.min_years is not None
                for item in (
                    *self.required_filters.skill_experience,
                    *self.required_filters.domain_experience,
                    *self.preferred_filters.skill_experience,
                    *self.preferred_filters.domain_experience,
                )
            )
        )
        if needs_as_of and self.as_of_date is None:
            raise ValueError(
                "as_of_date is required when a min_total_experience_years filter is set — "
                "otherwise the same persisted request would silently produce a different "
                "result as the wall clock advances."
            )
        return self

    @model_validator(mode="after")
    def _validate_not_sensitive(self) -> "CandidateSearchRequest":
        candidates: list[tuple[str, str]] = []
        if self.semantic_query:
            candidates.append(("semantic_query", self.semantic_query))
        for field_name, filters in (
            ("required_filters", self.required_filters),
            ("preferred_filters", self.preferred_filters),
        ):
            for category in ("skills", "certifications", "languages", "education"):
                for value in getattr(filters, category):
                    candidates.append((f"{field_name}.{category}", value))
            for category in ("skill_experience", "domain_experience", "language_levels"):
                for value in getattr(filters, category):
                    candidates.append((f"{field_name}.{category}", value.value))
        for label, text in candidates:
            term = find_prohibited_term(text)
            if term:
                raise ProhibitedCriterionError(label, term)
        return self


class RequiredFilterMatch(BaseModel):
    model_config = {"extra": "forbid"}
    category: str
    value: str


class PreferredFilterMatch(BaseModel):
    model_config = {"extra": "forbid"}
    category: str
    value: str


class CandidateSearchResult(BaseModel):
    """One ranked candidate. Deliberately excludes: embedding vectors,
    CandidateIdentity, raw CV/evidence text. `relevance_score` is search
    relevance (0-1) — never a hiring/JD/0-100 score (Slice 10 owns that).
    """

    model_config = {"extra": "forbid"}

    candidate_id: uuid.UUID
    rank: int
    mode: SearchMode
    relevance_score: float
    # None means "not evaluated in this mode" (e.g. structured_score in
    # SEMANTIC_ONLY) — distinct from a computed 0.0 (evaluated, no
    # preferred criteria configured or none matched).
    structured_score: float | None = None
    semantic_score: float | None = None
    required_filters_matched: list[RequiredFilterMatch] = Field(default_factory=list)
    preferred_filters_matched: list[PreferredFilterMatch] = Field(default_factory=list)
    candidate_profile_version_id: uuid.UUID
    candidate_embedding_version_id: uuid.UUID | None = None
    search_policy_version: str


class CandidateSearchResponse(BaseModel):
    """Safe aggregate metadata alongside results — no PII, ever."""

    model_config = {"extra": "forbid"}

    mode: SearchMode
    policy_version: str
    results: list[CandidateSearchResult]
    result_count: int
    eligible_profile_count: int
    compatible_embedding_count: int
    excluded_missing_embedding_count: int
    limit: int
    effective_structured_weight: float | None = None
    effective_semantic_weight: float | None = None
    embedding_config: EmbeddingSearchConfig | None = None

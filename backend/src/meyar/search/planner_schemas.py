"""Strict Slice 9 planner contracts.

``PlannerDraft`` is the only shape the LLM may produce. Trusted runtime
values (tenant, reference date, embedding provenance, and ranking weights)
are deliberately absent. ``SearchPlanResult`` is the safe application
result: either it contains a fully validated Slice 8 request or it is a
typed, non-executable outcome.
"""

from enum import StrEnum

from pydantic import BaseModel, Field, StrictInt, StrictStr, model_validator

from meyar.search.policy import MAX_SEARCH_LIMIT, MAX_SEMANTIC_QUERY_LENGTH, MIN_SEARCH_LIMIT
from meyar.search.schemas import (
    CandidateSearchRequest,
    CandidateSearchResponse,
    PreferredFilters,
    RequiredFilters,
    SearchMode,
)

PLANNER_SCHEMA_VERSION = "search-plan-schema-v1"


class PlannerOutcome(StrEnum):
    EXECUTABLE = "EXECUTABLE"
    PROHIBITED_REQUEST = "PROHIBITED_REQUEST"
    UNSUPPORTED_SEMANTICS = "UNSUPPORTED_SEMANTICS"
    AMBIGUOUS_REQUEST = "AMBIGUOUS_REQUEST"
    MALFORMED_MODEL_OUTPUT = "MALFORMED_MODEL_OUTPUT"
    PLANNER_PROVIDER_FAILURE = "PLANNER_PROVIDER_FAILURE"
    VALIDATION_FAILURE = "VALIDATION_FAILURE"


class PlannerReasonCode(StrEnum):
    PROTECTED_CRITERION = "PROTECTED_CRITERION"
    SKILL_SPECIFIC_EXPERIENCE_DURATION_UNSUPPORTED = (
        "SKILL_SPECIFIC_EXPERIENCE_DURATION_UNSUPPORTED"
    )
    LANGUAGE_PROFICIENCY_UNSUPPORTED = "LANGUAGE_PROFICIENCY_UNSUPPORTED"
    IDENTITY_SEARCH_UNSUPPORTED = "IDENTITY_SEARCH_UNSUPPORTED"
    CUSTOM_WEIGHTING_UNSUPPORTED = "CUSTOM_WEIGHTING_UNSUPPORTED"
    SALARY_FILTER_UNSUPPORTED = "SALARY_FILTER_UNSUPPORTED"
    LOCATION_FILTER_UNSUPPORTED = "LOCATION_FILTER_UNSUPPORTED"
    PROJECT_DURATION_UNSUPPORTED = "PROJECT_DURATION_UNSUPPORTED"
    PROMPT_INJECTION_UNSUPPORTED = "PROMPT_INJECTION_UNSUPPORTED"
    OTHER_UNSUPPORTED_SEMANTICS = "OTHER_UNSUPPORTED_SEMANTICS"
    EMPTY_REQUEST = "EMPTY_REQUEST"
    REQUEST_TOO_LONG = "REQUEST_TOO_LONG"
    REQUEST_CONTROL_CHARACTERS = "REQUEST_CONTROL_CHARACTERS"
    NO_SEARCH_CRITERIA = "NO_SEARCH_CRITERIA"
    MODEL_SCHEMA_INVALID = "MODEL_SCHEMA_INVALID"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_PROVENANCE_MISMATCH = "MODEL_PROVENANCE_MISMATCH"
    STRUCTURED_FILTER_NOT_SUPPORTED_BY_REQUEST = (
        "STRUCTURED_FILTER_NOT_SUPPORTED_BY_REQUEST"
    )
    NUMERIC_EXPERIENCE_NOT_SUPPORTED_BY_REQUEST = (
        "NUMERIC_EXPERIENCE_NOT_SUPPORTED_BY_REQUEST"
    )
    NUMERIC_EXPERIENCE_OMITTED = "NUMERIC_EXPERIENCE_OMITTED"
    RESULT_LIMIT_NOT_SUPPORTED_BY_REQUEST = "RESULT_LIMIT_NOT_SUPPORTED_BY_REQUEST"
    RESULT_LIMIT_OMITTED = "RESULT_LIMIT_OMITTED"
    RESULT_LIMIT_OUT_OF_RANGE = "RESULT_LIMIT_OUT_OF_RANGE"
    MANDATORY_REQUIREMENT_DOWNGRADED = "MANDATORY_REQUIREMENT_DOWNGRADED"
    PREFERRED_REQUIREMENT_UPGRADED = "PREFERRED_REQUIREMENT_UPGRADED"
    SEMANTIC_QUERY_NOT_SUPPORTED_BY_REQUEST = (
        "SEMANTIC_QUERY_NOT_SUPPORTED_BY_REQUEST"
    )
    SEMANTIC_QUERY_UNSAFE = "SEMANTIC_QUERY_UNSAFE"
    CANDIDATE_SEARCH_VALIDATION_FAILED = "CANDIDATE_SEARCH_VALIDATION_FAILED"


class PlannerDraft(BaseModel):
    """Strict model output. No mode or trusted runtime configuration."""

    model_config = {"extra": "forbid"}

    required_filters: RequiredFilters = Field(default_factory=RequiredFilters)
    preferred_filters: PreferredFilters = Field(default_factory=PreferredFilters)
    semantic_query: StrictStr | None = Field(
        default=None, max_length=MAX_SEMANTIC_QUERY_LENGTH
    )
    requested_limit: StrictInt | None = Field(
        default=None, ge=MIN_SEARCH_LIMIT, le=MAX_SEARCH_LIMIT
    )
    unsupported_reason_codes: list[PlannerReasonCode] = Field(
        default_factory=list, max_length=20
    )


class PlanInterpretationSummary(BaseModel):
    """Concise user-facing explanation, constructed deterministically.

    It is not chain-of-thought and is never written into audit metadata.
    """

    model_config = {"extra": "forbid"}

    required_criteria: list[str] = Field(default_factory=list, max_length=100)
    preferred_criteria: list[str] = Field(default_factory=list, max_length=100)
    semantic_intent_present: bool = False
    selected_mode: SearchMode | None = None
    result_limit: int | None = None
    used_default_limit: bool = False


class SearchPlanResult(BaseModel):
    model_config = {"extra": "forbid"}

    executable: bool
    outcome: PlannerOutcome
    search_request: CandidateSearchRequest | None = None
    planner_policy_version: str
    prompt_version: str
    schema_version: str
    model_provider: str
    model_name: str
    model_revision: str = ""
    attempt_count: int = Field(ge=0, le=2)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    interpretation: PlanInterpretationSummary = Field(
        default_factory=PlanInterpretationSummary
    )
    reason_codes: list[PlannerReasonCode] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _validate_executable_contract(self) -> "SearchPlanResult":
        if self.executable:
            if self.outcome != PlannerOutcome.EXECUTABLE or self.search_request is None:
                raise ValueError(
                    "Executable planner result requires EXECUTABLE outcome and search_request."
                )
        elif self.search_request is not None or self.outcome == PlannerOutcome.EXECUTABLE:
            raise ValueError(
                "Non-executable planner result must not contain a search_request or "
                "EXECUTABLE outcome."
            )
        return self


class PlannedCandidateSearchResponse(BaseModel):
    """Thin Slice 9 orchestration result; ranking remains a Slice 8 result."""

    model_config = {"extra": "forbid"}

    plan: SearchPlanResult
    search_response: CandidateSearchResponse | None = None

    @model_validator(mode="after")
    def _validate_execution_contract(self) -> "PlannedCandidateSearchResponse":
        if self.plan.executable != (self.search_response is not None):
            raise ValueError(
                "search_response must be present exactly when the plan is executable."
            )
        return self

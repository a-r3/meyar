"""Slice 2 — strict contracts for the bounded read-only local-AI agent.

``AgentDecision`` is the ONLY shape the local model may produce, mirroring
the discipline ``meyar.search.planner_schemas.PlannerDraft`` already
established (D-016/D-031): ``extra="forbid"``, no trusted-runtime fields
(tenant, candidate identity, database access), and a strict per-action
shape enforced by a validator so the model cannot mix an action with
arguments that do not belong to it. Every ``AgentDecision`` the LLM
produces is untrusted input — see meyar.agent.service for the same
tenant/schema/prohibited-attribute/evidence discipline applied to it as to
any other LLM-produced tool argument (docs/DECISIONS.md D-031 point 4).

Tool RESULT schemas below are never LLM-authored — they are the
deterministic, already-tenant-scoped output of existing services
(search/profile/evidence). They deliberately carry no
``CandidateIdentity`` field: only ``candidate_id`` (an opaque UUID, not
identity data) and ``CandidateProfileExtraction`` facts, exactly the same
boundary ``meyar.search.schemas.CandidateSearchResult`` already enforces.
"""

import uuid
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from meyar.schemas.candidate_profile import CandidateProfileExtraction, EvidenceRef
from meyar.search.planner_schemas import PlannedCandidateSearchResponse

AGENT_SCHEMA_VERSION = "agent-decision-schema-v1"
AGENT_POLICY_VERSION = "agent-policy-v1"

# Bound on how many ordinal candidate references a single search result
# set (and therefore a single candidate_ref) can carry — matches
# meyar.search.policy.MAX_SEARCH_LIMIT so a candidate_ref is never
# accepted for a rank the search policy itself could not have produced.
MAX_CANDIDATE_REF = 50

# Deliberately NOT 4000 (the raw HR message's own cap, meyar.ui.router's
# /ui/agent Form(max_length=4000)). Verified empirically against a real
# Ollama daemon (D-035): a Pydantic string field's `maxLength` above
# ~2000 in a `format`-constrained-decoding JSON schema causes Ollama's
# grammar/vocabulary compiler to fail outright with HTTP 500 ("failed to
# load model vocabulary required for format") on this hardware/model —
# not a validation-time concern, a request-time provider failure. 2000
# matches the already-proven-safe meyar.search.policy.MAX_SEMANTIC_QUERY_LENGTH
# bound used identically in PlannerDraft.semantic_query.
MAX_AGENT_SEARCH_QUERY_LENGTH = 2000


class AgentActionType(StrEnum):
    SEARCH_CANDIDATES = "SEARCH_CANDIDATES"
    GET_CANDIDATE_PROFILE = "GET_CANDIDATE_PROFILE"
    GET_CANDIDATE_EVIDENCE = "GET_CANDIDATE_EVIDENCE"
    FINAL_ANSWER = "FINAL_ANSWER"
    CLARIFY = "CLARIFY"


TOOL_ACTIONS = frozenset(
    {
        AgentActionType.SEARCH_CANDIDATES,
        AgentActionType.GET_CANDIDATE_PROFILE,
        AgentActionType.GET_CANDIDATE_EVIDENCE,
    }
)


class AgentDecision(BaseModel):
    """Strict model output for one orchestration step. No mode, no tenant,
    no candidate_id/identity, no database access — only a next action and
    the minimum typed argument that action needs. See module docstring."""

    model_config = {"extra": "forbid"}

    action: AgentActionType
    # SEARCH_CANDIDATES only: forwarded, unmodified, into the existing
    # frozen NL search-planner pipeline (D-031) — this module never
    # re-implements filter extraction.
    search_query: str | None = Field(
        default=None, min_length=1, max_length=MAX_AGENT_SEARCH_QUERY_LENGTH
    )
    # GET_CANDIDATE_PROFILE / GET_CANDIDATE_EVIDENCE only: an ORDINAL
    # position (1 = first result) into the conversation's own
    # server-held last-search-result list — never a raw candidate_id.
    # See meyar.agent.service._resolve_candidate_ref.
    candidate_ref: int | None = Field(default=None, ge=1, le=MAX_CANDIDATE_REF)
    # GET_CANDIDATE_EVIDENCE only, optional: a skill/fact name to narrow
    # which evidence items are returned (e.g. "Python"). Free text, but
    # never treated as instructions — matched as a case/diacritic-
    # insensitive substring against existing stored facts only.
    evidence_topic: str | None = Field(default=None, max_length=200)
    # FINAL_ANSWER / CLARIFY only: short natural-language framing shown
    # to the HR user. Never the sole source of a factual claim about a
    # candidate — see meyar.agent.service and docs/DECISIONS.md D-035.
    message: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _validate_shape(self) -> "AgentDecision":
        if self.action == AgentActionType.SEARCH_CANDIDATES:
            if self.search_query is None:
                raise ValueError("SEARCH_CANDIDATES requires search_query.")
            if self.candidate_ref is not None or self.message is not None:
                raise ValueError("SEARCH_CANDIDATES must not set candidate_ref or message.")
            if self.evidence_topic is not None:
                raise ValueError("SEARCH_CANDIDATES must not set evidence_topic.")
        elif self.action in (
            AgentActionType.GET_CANDIDATE_PROFILE,
            AgentActionType.GET_CANDIDATE_EVIDENCE,
        ):
            if self.candidate_ref is None:
                raise ValueError(f"{self.action} requires candidate_ref.")
            if self.search_query is not None or self.message is not None:
                raise ValueError(f"{self.action} must not set search_query or message.")
            if (
                self.action == AgentActionType.GET_CANDIDATE_PROFILE
                and self.evidence_topic is not None
            ):
                raise ValueError("GET_CANDIDATE_PROFILE must not set evidence_topic.")
        else:  # FINAL_ANSWER / CLARIFY
            if self.message is None:
                raise ValueError(f"{self.action} requires message.")
            if (
                self.search_query is not None
                or self.candidate_ref is not None
                or self.evidence_topic is not None
            ):
                raise ValueError(
                    f"{self.action} must not set search_query, candidate_ref, or evidence_topic."
                )
        return self


class AgentSearchToolResult(BaseModel):
    model_config = {"extra": "forbid"}

    response: PlannedCandidateSearchResponse


class AgentProfileToolResult(BaseModel):
    """``candidate_id`` is populated for the UI presentation layer's own
    use only (to resolve a human-facing display name) — it is never part
    of what reaches the model's own prompt context; see
    meyar.agent.service._summarize_tool_result, which deliberately omits
    it."""

    model_config = {"extra": "forbid"}

    candidate_ref: int
    candidate_id: uuid.UUID | None = None
    found: bool
    profile_status: str | None = None
    profile: CandidateProfileExtraction | None = None


class EvidenceMatchItem(BaseModel):
    model_config = {"extra": "forbid"}

    category: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=500)
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=10)


class AgentEvidenceToolResult(BaseModel):
    """See AgentProfileToolResult docstring — candidate_id is UI-only."""

    model_config = {"extra": "forbid"}

    candidate_ref: int
    candidate_id: uuid.UUID | None = None
    found: bool
    profile_status: str | None = None
    topic: str | None = None
    matches: list[EvidenceMatchItem] = Field(default_factory=list, max_length=100)


class AgentToolResult(BaseModel):
    """One executed tool call's typed result, tagged by which tool
    produced it. Exactly one of the payload fields is set, matching
    ``tool_name`` — enforced below, mirroring AgentDecision's discipline."""

    model_config = {"extra": "forbid"}

    tool_name: AgentActionType
    search: AgentSearchToolResult | None = None
    profile: AgentProfileToolResult | None = None
    evidence: AgentEvidenceToolResult | None = None

    @model_validator(mode="after")
    def _validate_payload_matches_tool(self) -> "AgentToolResult":
        expected = {
            AgentActionType.SEARCH_CANDIDATES: ("search",),
            AgentActionType.GET_CANDIDATE_PROFILE: ("profile",),
            AgentActionType.GET_CANDIDATE_EVIDENCE: ("evidence",),
        }.get(self.tool_name)
        if expected is None:
            raise ValueError(f"{self.tool_name} is not a valid tool result tag.")
        for field_name in ("search", "profile", "evidence"):
            populated = getattr(self, field_name) is not None
            should_be_populated = field_name in expected
            if populated != should_be_populated:
                raise ValueError(
                    f"AgentToolResult.{field_name} must be set only when "
                    f"tool_name == {expected[0].upper()}."
                )
        return self


class AgentTurnOutcome(StrEnum):
    ANSWERED = "ANSWERED"
    CLARIFICATION_REQUESTED = "CLARIFICATION_REQUESTED"
    CANDIDATE_REF_NOT_FOUND = "CANDIDATE_REF_NOT_FOUND"
    TOOL_CALL_LIMIT_EXCEEDED = "TOOL_CALL_LIMIT_EXCEEDED"
    AGENT_PROVIDER_FAILURE = "AGENT_PROVIDER_FAILURE"
    MALFORMED_MODEL_OUTPUT = "MALFORMED_MODEL_OUTPUT"


class AgentTurnResult(BaseModel):
    """The full, safe result of one bounded orchestration turn. ``message``
    is model-authored framing text only — every factual claim about a
    candidate lives in ``tool_results`` (deterministic, evidence-grounded,
    server-rendered), never in ``message`` alone. See D-035."""

    model_config = {"extra": "forbid"}

    outcome: AgentTurnOutcome
    message: str | None = None
    tool_results: list[AgentToolResult] = Field(default_factory=list)
    tool_call_count: int = Field(ge=0)
    agent_policy_version: str
    prompt_version: str
    model_provider: str
    model_name: str
    model_revision: str = ""

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
``FINAL_ANSWER``/``CLARIFY`` carry only a closed ``AgentResponseCode``;
the model has no free-text response field. The server owns every rendered
sentence for those outcomes.

Tool RESULT schemas below are never LLM-authored — they are the
deterministic, already-tenant-scoped output of existing services
(search/profile/evidence). They deliberately carry no
``CandidateIdentity`` field: only ``candidate_id`` (an opaque UUID, not
identity data) and ``CandidateProfileExtraction`` facts, exactly the same
boundary ``meyar.search.schemas.CandidateSearchResult`` already enforces.

``GroundedSelection`` (D-038, superseding the D-037 ``GroundedAnswer``
shape) is a SECOND, narrower LLM-output shape used to explain an
already-fetched profile/evidence tool result. The model NEVER authors any
part of the displayed sentence text — it only selects (and orders) which
already-supplied ``GroundedFact`` ids are relevant to the question, plus
an optional closed-enum caveat. The actual sentence is built entirely
server-side from those facts' own title/detail values (see
meyar.agent.service.render_grounded_answer), so no unsupported factual
claim — numeric or otherwise — can ever reach the user: there is no
free-text span in the final answer that did not come from a
GroundedFact.
"""

import hashlib
import re
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from meyar.core.result_count import DEFAULT_RESULT_LIMIT, MAX_RESULT_LIMIT, MIN_RESULT_LIMIT
from meyar.schemas.candidate_profile import CandidateProfileExtraction, EvidenceRef
from meyar.schemas.criteria import CriterionIn, CriterionType
from meyar.search.planner_schemas import PlannedCandidateSearchResponse
from meyar.search.schemas import CandidateSearchResponse

AGENT_SCHEMA_VERSION = "agent-decision-schema-v1"
AGENT_POLICY_VERSION = "agent-policy-v1"

# Bound on how many ordinal candidate references a single search result
# set (and therefore a single candidate_ref) can carry — matches
# meyar.search.policy.MAX_SEARCH_LIMIT so a candidate_ref is never
# accepted for a rank the search policy itself could not have produced.
MAX_CANDIDATE_REF = 50
MAX_JD_REQUIREMENT_SPANS = 64

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
    # issue #49 PR49-2: a follow-up that operates on the conversation's own
    # CURRENT active AgentResultSet ("ilk üçü", "bunlardan SQL bilənlər",
    # "5 nəfərə endir") rather than naming a new, independent search. The
    # model may only express bounded intent (filter_query/limit below) —
    # it never supplies a result_set_id/candidate_id/ordinal membership
    # list; the server alone resolves the active result set and computes
    # the derived membership/order (see
    # meyar.services.agent_result_set_repo.create_result_set_from_refinement).
    REFINE_CANDIDATE_RESULTS = "REFINE_CANDIDATE_RESULTS"
    # Slice 4's typed draft action remains the internal service/tool result
    # identity, but issue #79 makes its execution authority server-owned.
    # Confirmed JDs are routed before model orchestration; a model-produced
    # value by itself is rejected. Nothing is persisted by this action — see
    # AgentJobDraftToolResult.
    DRAFT_JOB_CRITERIA = "DRAFT_JOB_CRITERIA"
    FINAL_ANSWER = "FINAL_ANSWER"
    CLARIFY = "CLARIFY"


class AgentResponseCode(StrEnum):
    """Closed, non-factual conversational intents for FINAL_ANSWER/CLARIFY."""

    GREETING = "GREETING"
    ACKNOWLEDGEMENT = "ACKNOWLEDGEMENT"
    NEED_MORE_DETAIL = "NEED_MORE_DETAIL"
    CANDIDATE_REFERENCE_REQUIRED = "CANDIDATE_REFERENCE_REQUIRED"
    UNSUPPORTED_REQUEST = "UNSUPPORTED_REQUEST"
    HIRING_DECISION_REQUIRES_HUMAN = "HIRING_DECISION_REQUIRES_HUMAN"
    # issue #49 PR49-2: REFINE_CANDIDATE_RESULTS-shaped intent ("ilk üçü",
    # "bunlardan ...") with no active result set to operate on — distinct
    # from CANDIDATE_REFERENCE_REQUIRED (which is about naming ONE
    # candidate_ref), since a refinement request never names a single
    # candidate at all. Never used for a STALE/EXPIRED active result set —
    # those get their own truthful AgentTurnOutcome (RESULT_SET_STALE/
    # RESULT_SET_EXPIRED), same as candidate_ref resolution.
    RESULT_CONTEXT_REQUIRED = "RESULT_CONTEXT_REQUIRED"


_FINAL_RESPONSE_CODES = frozenset({AgentResponseCode.GREETING, AgentResponseCode.ACKNOWLEDGEMENT})
_CLARIFICATION_RESPONSE_CODES = frozenset(
    {
        AgentResponseCode.NEED_MORE_DETAIL,
        AgentResponseCode.CANDIDATE_REFERENCE_REQUIRED,
        AgentResponseCode.UNSUPPORTED_REQUEST,
        AgentResponseCode.HIRING_DECISION_REQUIRES_HUMAN,
        AgentResponseCode.RESULT_CONTEXT_REQUIRED,
    }
)


TOOL_ACTIONS = frozenset(
    {
        AgentActionType.SEARCH_CANDIDATES,
        AgentActionType.GET_CANDIDATE_PROFILE,
        AgentActionType.GET_CANDIDATE_EVIDENCE,
        AgentActionType.DRAFT_JOB_CRITERIA,
        AgentActionType.REFINE_CANDIDATE_RESULTS,
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
    # REFINE_CANDIDATE_RESULTS only, optional: the user's own natural-
    # language refinement criterion (e.g. "SQL bilənlər"), forwarded
    # UNMODIFIED into the existing frozen NL search-planner pipeline
    # (D-031) — exactly like SEARCH_CANDIDATES.search_query — to derive a
    # validated deterministic RequiredFilters shape. Never itself executed
    # as a tenant-wide search: meyar.agent.service._dispatch_refine
    # evaluates the resulting filters ONLY against the active
    # AgentResultSet's own current members. See AgentActionType.
    # REFINE_CANDIDATE_RESULTS.
    filter_query: str | None = Field(
        default=None, min_length=1, max_length=MAX_AGENT_SEARCH_QUERY_LENGTH
    )
    # REFINE_CANDIDATE_RESULTS only, optional: "ilk N" / "N nəfərə endir" —
    # applied server-side, after any filter, against the active result
    # set's OWN preserved order. Bounded to the same MAX_CANDIDATE_REF a
    # candidate_ref itself is bounded to (never a limit the search policy
    # itself could not have produced).
    limit: int | None = Field(default=None, ge=1, le=MAX_CANDIDATE_REF)
    # FINAL_ANSWER / CLARIFY only: a closed conversational intent. The
    # server maps this to fixed copy; no model-authored prose can reach HR.
    response_code: AgentResponseCode | None = None

    @model_validator(mode="after")
    def _validate_shape(self) -> "AgentDecision":
        if self.action == AgentActionType.SEARCH_CANDIDATES:
            if self.search_query is None:
                raise ValueError("SEARCH_CANDIDATES requires search_query.")
            if self.candidate_ref is not None or self.response_code is not None:
                raise ValueError("SEARCH_CANDIDATES must not set candidate_ref or response_code.")
            if self.evidence_topic is not None:
                raise ValueError("SEARCH_CANDIDATES must not set evidence_topic.")
            if self.filter_query is not None or self.limit is not None:
                raise ValueError("SEARCH_CANDIDATES must not set filter_query or limit.")
        elif self.action in (
            AgentActionType.GET_CANDIDATE_PROFILE,
            AgentActionType.GET_CANDIDATE_EVIDENCE,
        ):
            if self.candidate_ref is None:
                raise ValueError(f"{self.action} requires candidate_ref.")
            if self.search_query is not None or self.response_code is not None:
                raise ValueError(f"{self.action} must not set search_query or response_code.")
            if self.filter_query is not None or self.limit is not None:
                raise ValueError(f"{self.action} must not set filter_query or limit.")
            if (
                self.action == AgentActionType.GET_CANDIDATE_PROFILE
                and self.evidence_topic is not None
            ):
                raise ValueError("GET_CANDIDATE_PROFILE must not set evidence_topic.")
        elif self.action == AgentActionType.REFINE_CANDIDATE_RESULTS:
            # The model may express ONLY bounded intent — see
            # AgentActionType.REFINE_CANDIDATE_RESULTS and the filter_query/
            # limit field docstrings. At least one refinement operation
            # must be present: a bare "bunlardan"-shaped decision with
            # neither set is never accepted as this action (small-model
            # safety — see docs/DECISIONS.md D-084); the model must use
            # CLARIFY(NEED_MORE_DETAIL) instead.
            if self.search_query is not None or self.candidate_ref is not None:
                raise ValueError(
                    "REFINE_CANDIDATE_RESULTS must not set search_query or candidate_ref."
                )
            if self.evidence_topic is not None or self.response_code is not None:
                raise ValueError(
                    "REFINE_CANDIDATE_RESULTS must not set evidence_topic or response_code."
                )
            if self.filter_query is None and self.limit is None:
                raise ValueError(
                    "REFINE_CANDIDATE_RESULTS requires filter_query and/or limit."
                )
        elif self.action == AgentActionType.DRAFT_JOB_CRITERIA:
            # Bare action, no argument — see AgentActionType.DRAFT_JOB_CRITERIA
            # docstring for why the JD text itself is never round-tripped
            # through the model's own output.
            if (
                self.search_query is not None
                or self.candidate_ref is not None
                or self.evidence_topic is not None
                or self.response_code is not None
                or self.filter_query is not None
                or self.limit is not None
            ):
                raise ValueError(
                    "DRAFT_JOB_CRITERIA must not set search_query, candidate_ref, "
                    "evidence_topic, filter_query, limit, or response_code."
                )
        else:  # FINAL_ANSWER / CLARIFY
            if self.response_code is None:
                raise ValueError(f"{self.action} requires response_code.")
            if (
                self.search_query is not None
                or self.candidate_ref is not None
                or self.evidence_topic is not None
                or self.filter_query is not None
                or self.limit is not None
            ):
                raise ValueError(
                    f"{self.action} must not set search_query, candidate_ref, evidence_topic, "
                    "filter_query, or limit."
                )
            allowed = (
                _FINAL_RESPONSE_CODES
                if self.action == AgentActionType.FINAL_ANSWER
                else _CLARIFICATION_RESPONSE_CODES
            )
            if self.response_code not in allowed:
                raise ValueError(
                    f"{self.action} does not allow response_code={self.response_code}."
                )
        return self


class AgentSearchToolResult(BaseModel):
    model_config = {"extra": "forbid"}

    response: PlannedCandidateSearchResponse


class AgentRefineToolResult(BaseModel):
    """REFINE_CANDIDATE_RESULTS's tool result (issue #49 PR49-2) — NEVER
    LLM-authored: ``response`` is server-built, reusing the exact
    ``CandidateSearchResponse``/``CandidateSearchResult`` shape
    SEARCH_CANDIDATES already produces (so the existing candidate-card
    presentation layer renders it unchanged), except every result here is
    already a member of the source AgentResultSet — never a fresh tenant-
    wide search. ``source_result_count`` is the active result set's own
    size BEFORE this refinement; ``result_count`` (also
    ``response.result_count``) is the derived set's size. ``has_filter``/
    ``requested_limit``/``limit_truncated`` are safe, non-identity summary
    flags the presentation layer uses to build one deterministic HR-facing
    sentence — never a raw filter_query/limit echo of untrusted model
    input."""

    model_config = {"extra": "forbid"}

    response: CandidateSearchResponse
    source_result_count: int = Field(ge=0)
    has_filter: bool
    requested_limit: int | None = Field(default=None, ge=1, le=MAX_CANDIDATE_REF)
    # True only when a requested_limit was set AND the source (post-filter)
    # set already had fewer members than that limit — see
    # AgentActionType.REFINE_CANDIDATE_RESULTS section 16 precedent
    # (docs/DECISIONS.md D-084): a derived set is still created, truthfully
    # reporting the smaller count, never fabricating members to reach the
    # requested limit.
    limit_truncated: bool = False


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


class JDDraftCriterionKind(StrEnum):
    """The kinds a JD-drafting model may propose for one requirement —
    the evaluator's ``CriterionKind`` values, plus ``OTHER``: a requirement
    that is genuinely stated in the
    JD text and already confirmed non-sensitive, but does not fit any of
    the five evaluator-supported dimensions (for example: relocation
    willingness, driving license, availability for shift work). ``OTHER``
    never becomes a ``CriterionIn``: a model proposal of ``OTHER`` is
    rejected by the canonical boundary (meyar.agent.canonical_requirements,
    issue #84), and deterministic non-professional source text is
    UNSUPPORTED before any model call. Deliberately NOT
    meyar.schemas.criteria.CriterionKind itself: that enum is the
    deterministic evaluator's own persisted scoring vocabulary and must
    never grow a scoring-irrelevant member (PR #42 owner correction,
    issue #33, D-045 — see 'Inspect the actual evaluator capability
    boundary')."""

    SKILL = "SKILL"
    EXPERIENCE = "EXPERIENCE"
    CERTIFICATION = "CERTIFICATION"
    EDUCATION = "EDUCATION"
    LANGUAGE = "LANGUAGE"
    SKILL_EXPERIENCE = "SKILL_EXPERIENCE"
    DOMAIN_EXPERIENCE = "DOMAIN_EXPERIENCE"
    OTHER = "OTHER"


class RequirementSpan(BaseModel):
    """One server-segmented occurrence from the original JD.

    Identity and offsets are produced before the model is called.  ``text``
    is always the exact ``original_jd[start_offset:end_offset]`` slice;
    ``normalized`` is server-produced comparison data, never model output.
    """

    model_config = {"extra": "forbid"}

    span_id: str = Field(pattern=r"^req-\d{4}$")
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=4000)
    normalized: str = Field(min_length=1, max_length=4000)
    segmentation_needs_review: bool = False
    # Issue #84: spans produced by splitting ONE coordinated clause ("X və Y
    # ...", "X and Y ...") share a group id so their semantic authority is
    # decided symmetrically. Server-owned; never model-supplied.
    coordination_group: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_offsets(self) -> "RequirementSpan":
        if self.end_offset <= self.start_offset:
            raise ValueError("RequirementSpan end_offset must follow start_offset.")
        return self


class SupportedInputLanguage(StrEnum):
    AZERBAIJANI = "AZERBAIJANI"
    ENGLISH = "ENGLISH"
    MIXED_AZ_EN = "MIXED_AZ_EN"
    UNSUPPORTED = "UNSUPPORTED"


class SourceOccurrence(BaseModel):
    """One exact occurrence in the original HR text.

    Semantic values never become authority merely because the model emitted a
    plausible string.  Every material slot carries the exact source slice and
    absolute offsets that authorized it.
    """

    model_config = {"extra": "forbid"}

    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _validate_offsets(self) -> "SourceOccurrence":
        if self.end_offset <= self.start_offset:
            raise ValueError("SourceOccurrence end_offset must follow start_offset.")
        return self


class SourceSpanRole(StrEnum):
    """Server-observed grammatical role for one exact source occurrence."""

    SUBJECT = "SUBJECT"
    RELATION = "RELATION"
    QUANTITY = "QUANTITY"
    DURATION = "DURATION"
    PROFICIENCY = "PROFICIENCY"
    MODALITY = "MODALITY"
    CONTROL_RESULT_COUNT = "CONTROL_RESULT_COUNT"
    CONNECTIVE = "CONNECTIVE"
    RECRUITMENT_PREAMBLE = "RECRUITMENT_PREAMBLE"
    PROTECTED_CUE = "PROTECTED_CUE"
    GENERIC_PERSON_OR_RESULT_NOUN = "GENERIC_PERSON_OR_RESULT_NOUN"
    OTHER = "OTHER"


class SourceSpanOwner(StrEnum):
    """Terminal authority owner for a role occurrence.

    Role observations may be proposed in several ways while parsing, but the
    reconciled registry contains one non-conflicting owner per source byte.
    """

    WORKFLOW_CONTROL = "WORKFLOW_CONTROL"
    SCORABLE = "SCORABLE"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    UNSUPPORTED_VISIBLE = "UNSUPPORTED_VISIBLE"
    PROHIBITED = "PROHIBITED"
    NON_REQUIREMENT_TEXT = "NON_REQUIREMENT_TEXT"


class SourceRoleAssignment(BaseModel):
    """One exact, final role/ownership entry in the consumption registry."""

    model_config = {"extra": "forbid"}

    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=4000)
    role: SourceSpanRole
    owner: SourceSpanOwner
    requirement_span_id: str | None = Field(default=None, pattern=r"^req-\d{4}$")

    @model_validator(mode="after")
    def _validate_offsets(self) -> "SourceRoleAssignment":
        if self.end_offset <= self.start_offset:
            raise ValueError("SourceRoleAssignment end_offset must follow start_offset.")
        return self


class SemanticRequirementState(StrEnum):
    SCORABLE = "SCORABLE"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    UNSUPPORTED = "UNSUPPORTED"
    PROHIBITED = "PROHIBITED"


class SemanticReviewReason(StrEnum):
    """Why a material requirement stayed NEEDS_HUMAN_REVIEW (issue #84).

    Only ``SUBJECT_NOT_CANONICAL`` and ``RECRUITMENT_SUBJECT_WITHOUT_CUE``
    describe a subject-normalization gap that a server-validated local-model
    canonical proposal may close; every other reason is policy the model can
    never override."""

    SEGMENTATION = "SEGMENTATION"
    NEGATION_OR_COMPARATOR = "NEGATION_OR_COMPARATOR"
    RESULT_COUNT_ENTITY = "RESULT_COUNT_ENTITY"
    PARTICLE_COORDINATION = "PARTICLE_COORDINATION"
    MODALITY_UNKNOWN = "MODALITY_UNKNOWN"
    SUBJECT_MISSING = "SUBJECT_MISSING"
    PERSONAL_ELIGIBILITY = "PERSONAL_ELIGIBILITY"
    RECRUITMENT_SUBJECT_WITHOUT_CUE = "RECRUITMENT_SUBJECT_WITHOUT_CUE"
    FAMILY_UNAUTHORIZED = "FAMILY_UNAUTHORIZED"
    EXPERIENCE_WITHOUT_DURATION = "EXPERIENCE_WITHOUT_DURATION"
    CERTIFICATION_QUANTITY = "CERTIFICATION_QUANTITY"
    SUBJECT_NOT_CANONICAL = "SUBJECT_NOT_CANONICAL"
    COORDINATION_SYMMETRY = "COORDINATION_SYMMETRY"
    # Issue #84: the same canonical requirement appears in more than one
    # source span with different importance/duration/level. Never scored
    # twice and never auto-resolved; only an explicit HR choice decides.
    SEMANTIC_CONFLICT = "SEMANTIC_CONFLICT"


class SemanticRequirement(BaseModel):
    """Server-authorized semantic slots for one material source requirement.

    The local model may propose a family, but it cannot author a subject,
    number, level, modality, or result count.  Those fields are accepted only
    when this structure points to their exact occurrences in the canonical
    input.  ``result_limit`` is intentionally absent: workflow intent is parsed
    independently from requirements.
    """

    model_config = {"extra": "forbid"}

    requirement_span_id: str = Field(pattern=r"^req-\d{4}$")
    criterion_family: JDDraftCriterionKind | None = None
    subject: SourceOccurrence | None = None
    normalized_subject: str | None = Field(default=None, max_length=200)
    modality: SourceOccurrence | None = None
    criterion_type: CriterionType | None = None
    duration_or_number: SourceOccurrence | None = None
    min_years: float | None = Field(default=None, ge=0, le=60)
    proficiency: SourceOccurrence | None = None
    required_level: str | None = Field(default=None, max_length=50)
    state: SemanticRequirementState
    comparison: str | None = Field(default=None, max_length=16)
    review_reason: SemanticReviewReason | None = None


class JDDraftCriterionItem(BaseModel):
    """One MODEL-PRODUCED candidate requirement drafted from a JD's own
    text — untrusted input, exactly like every other LLM-produced tool
    argument (D-031 point 4). ``kind`` is restricted to
    ``JDDraftCriterionKind`` — evaluator kinds plus the explicit ``OTHER``
    escape hatch (D-045). The service still rejects any combination the
    current review form cannot round-trip without loss. Never persisted
    directly: every item
    is re-validated into a real ``CriterionIn`` (same prohibited-attribute
    denylist, same kind-specific shape rules) by
    meyar.agent.service._dispatch_draft_job_criteria before it is ever
    shown to HR — an item that fails that check never becomes a
    CriterionIn, but is disclosed (see AgentJobDraftToolResult), never
    silently discarded or weakened."""

    model_config = {"extra": "forbid"}

    kind: JDDraftCriterionKind
    # Both the HR-facing label AND the exact term the deterministic scorer
    # matches against candidate evidence — mirrors the single "Tələb"
    # field discipline the manual form already established (D-025) so a
    # drafted criterion can never disagree with its own displayed name.
    # Still the human-readable requirement text when kind is OTHER.
    requirement: str = Field(min_length=1, max_length=200)
    # The only source-authority reference. The server creates and supplies
    # these ids before inference; an unknown id is never resolved by text.
    span_id: str = Field(pattern=r"^req-\d{4}$")
    # Verbatim/near-verbatim attributable fragment copied from the JD. It
    # is an untrusted usability/debugging hint only. It never selects,
    # narrows, or otherwise authorizes the canonical RequirementSpan.
    source_text: str = Field(default="", max_length=500)
    min_years: float | None = Field(default=None, ge=0, le=60)
    required_level: str | None = Field(default=None, min_length=1, max_length=50)


# Bounds how many must-have/preferred rows one JD draft may propose per
# section — generous for real-world JDs while keeping the human review
# screen a fixed, scrollable-but-bounded table (no unbounded LLM output
# surface). A JD with more genuine requirements than this is still fully
# usable — HR can add further rows by hand after review, same as the
# manual form already allows.
MAX_JD_DRAFT_ROWS_PER_SECTION = 8


class JDCriteriaDraft(BaseModel):
    """Strict model output for Slice 4 JD-criteria drafting
    (meyar.llm.provider.LLMProvider.draft_job_criteria). The model drafts;
    it never assigns a final score and nothing here is persisted until an
    accountable human confirms via the existing job-creation path
    (D-030/D-032)."""

    model_config = {"extra": "forbid"}

    title: str = Field(min_length=1, max_length=255)
    must_have: list[JDDraftCriterionItem] = Field(
        default_factory=list, max_length=MAX_JD_DRAFT_ROWS_PER_SECTION
    )
    preferred: list[JDDraftCriterionItem] = Field(
        default_factory=list, max_length=MAX_JD_DRAFT_ROWS_PER_SECTION
    )


class UnsupportedJDCriterionItem(BaseModel):
    """One non-sensitive JD requirement that did NOT become a real
    CriterionIn — kept visible to HR (never persisted, never scored) so
    it is disclosed rather than silently lost. ``requirement`` is the
    model-drafted term itself, already confirmed non-sensitive (a
    PROHIBITED item never reaches this shape — see
    AgentJobDraftToolResult.prohibited_count)."""

    model_config = {"extra": "forbid"}

    requirement: str = Field(min_length=1, max_length=500)
    criterion_type: CriterionType


class SemanticParameters(BaseModel):
    """The semantic scoring parameters of one canonical requirement.

    Issue #84: two source spans with the same canonical identity (kind +
    canonical subject) are an exact duplicate only when these parameters
    are identical; otherwise they are a semantic conflict."""

    model_config = {"extra": "forbid", "frozen": True}

    criterion_type: CriterionType
    min_years: float | None = Field(default=None, ge=0, le=60)
    required_level: str | None = Field(default=None, min_length=1, max_length=50)


class NeedsReviewJDCriterionItem(BaseModel):
    """A source requirement whose material semantics could not be safely
    represented or whose model draft omitted/changed a source-bound field.
    It remains visible to HR but is never submitted as a scoring row.

    Issue #84:

    - ``kind``/``subject`` are present ONLY when the canonical criterion
      shape (family + canonical subject + duration/level) is already
      server-validated; only then may ``allowed_types`` offer a modality
      resolution. Arbitrary source text can never become a criterion shape.
    - ``blocking`` marks an explicit MUST_HAVE source requirement that is
      unresolved: it blocks confirmation until HR either resolves an allowed
      interpretation or explicitly acknowledges that it is excluded from
      automatic ranking (``acknowledged_excluded``). Exclusion never mints a
      criterion."""

    model_config = {"extra": "forbid"}

    requirement: str = Field(min_length=1, max_length=500)
    criterion_type: CriterionType | None = None
    span_id: str | None = Field(default=None, pattern=r"^req-\d{4}$")
    kind: JDDraftCriterionKind | None = None
    subject: str | None = Field(default=None, min_length=1, max_length=200)
    min_years: float | None = Field(default=None, ge=0, le=60)
    required_level: str | None = Field(default=None, min_length=1, max_length=50)
    allowed_types: list[CriterionType] = Field(default_factory=list, max_length=2)
    blocking: bool = False
    acknowledged_excluded: bool = False
    # Issue #84: a semantic conflict — the same canonical requirement in
    # several source spans with different parameters. ``span_id`` is the
    # first conflicting span; HR must choose one server-declared option.
    conflict_span_ids: list[str] = Field(default_factory=list, max_length=MAX_JD_REQUIREMENT_SPANS)
    conflict_options: list[SemanticParameters] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _validate_resolution_shape(self) -> "NeedsReviewJDCriterionItem":
        if self.conflict_options or self.conflict_span_ids:
            if (
                len(self.conflict_options) < 2
                or len(self.conflict_span_ids) < 2
                or self.span_id != self.conflict_span_ids[0]
                or len(set(self.conflict_span_ids)) != len(self.conflict_span_ids)
                or len(set(self.conflict_options)) != len(self.conflict_options)
                or self.kind is None
                or self.allowed_types
                or not self.blocking
            ):
                raise ValueError("Semantic conflict review metadata is inconsistent.")
        if (self.kind is None) != (self.subject is None):
            raise ValueError("Review resolution metadata must be complete.")
        if self.kind is not None and self.span_id is None:
            raise ValueError("A canonical review shape must be bound to a source span.")
        if self.allowed_types and self.kind is None:
            raise ValueError("Allowed review types require a complete canonical shape.")
        if len(set(self.allowed_types)) != len(self.allowed_types):
            raise ValueError("Allowed review types must be unique.")
        if (self.blocking or self.acknowledged_excluded) and self.span_id is None:
            raise ValueError("A blocking review item must be bound to a source span.")
        if self.acknowledged_excluded and not self.blocking:
            raise ValueError("Only a blocking review item can be acknowledged as excluded.")
        return self


class SemanticInterpretationSource(StrEnum):
    """Issue #84: which authority produced a canonical criterion shape.

    ``DETERMINISTIC`` — the server's own source analysis produced an already
    canonical subject. ``MODEL_VALIDATED`` — a local-model canonical proposal
    closed a subject-normalization gap and passed server grounding checks."""

    DETERMINISTIC = "DETERMINISTIC"
    MODEL_VALIDATED = "MODEL_VALIDATED"


class SemanticModelProvenance(BaseModel):
    """Identity of the local model whose JD semantic result was accepted.
    Never raw model output."""

    model_config = {"extra": "forbid", "frozen": True}

    provider: str = Field(min_length=1, max_length=64)
    model_name: str = Field(min_length=1, max_length=255)
    model_revision: str = Field(default="", max_length=255)


class SemanticReviewDecisionKind(StrEnum):
    MUST_HAVE = "MUST_HAVE"
    PREFERRED = "PREFERRED"
    EXCLUDED_BY_REVIEWER = "EXCLUDED_BY_REVIEWER"


class SemanticReviewDecision(BaseModel):
    """One explicit human semantic-review decision that affects ranking."""

    model_config = {"extra": "forbid", "frozen": True}

    span_id: str = Field(pattern=r"^req-\d{4}$")
    decision: SemanticReviewDecisionKind


class SemanticConflictResolution(BaseModel):
    """One explicit HR choice among the server-declared conflicting
    parameter sets of one canonical requirement (issue #84)."""

    model_config = {"extra": "forbid", "frozen": True}

    span_ids: list[str] = Field(min_length=2, max_length=MAX_JD_REQUIREMENT_SPANS)
    options: list[SemanticParameters] = Field(min_length=2, max_length=16)
    chosen_index: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_choice(self) -> "SemanticConflictResolution":
        if any(not re.fullmatch(r"req-\d{4}", span_id) for span_id in self.span_ids):
            raise ValueError("Conflict span ids must be server span ids.")
        if len(set(self.span_ids)) != len(self.span_ids):
            raise ValueError("Conflict span ids must be unique.")
        if len(set(self.options)) != len(self.options):
            raise ValueError("Conflict options must be distinct.")
        if self.chosen_index >= len(self.options):
            raise ValueError("Conflict choice is out of range.")
        return self

    @property
    def chosen(self) -> SemanticParameters:
        return self.options[self.chosen_index]


class SemanticCriterionAmendmentField(StrEnum):
    """The only semantic fields a bounded HR follow-up may amend."""

    CRITERION_TYPE = "CRITERION_TYPE"
    MIN_YEARS = "MIN_YEARS"
    REQUIRED_LEVEL = "REQUIRED_LEVEL"


_CEFR_OR_NAMED_LEVEL_RE = re.compile(r"^[A-Z][A-Z0-9 -]{0,49}$")


def encode_amendment_value(field: SemanticCriterionAmendmentField, value: object) -> str:
    """Canonical string form of one amended semantic value."""
    if field == SemanticCriterionAmendmentField.CRITERION_TYPE:
        return CriterionType(value).value  # type: ignore[arg-type]
    if field == SemanticCriterionAmendmentField.MIN_YEARS:
        return f"{float(value):g}"  # type: ignore[arg-type]
    return str(value).strip().upper()


class SemanticCriterionAmendment(BaseModel):
    """One immutable, server-created human amendment of one criterion field.

    Created only by the bounded pending-draft follow-up after it resolved
    exactly one target criterion. ``source_text`` is the bounded HR follow-up
    message that authorized the change (HR-authored instruction, never
    candidate data or model output); ``source_sha256`` is its digest."""

    model_config = {"extra": "forbid", "frozen": True}

    sequence: int = Field(ge=1)
    criterion_id: str = Field(pattern=r"^[a-z0-9_]{1,64}$")
    span_id: str = Field(pattern=r"^req-\d{4}$")
    field: SemanticCriterionAmendmentField
    previous_value: str = Field(min_length=1, max_length=50)
    new_value: str = Field(min_length=1, max_length=50)
    source_text: str = Field(min_length=1, max_length=4000)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_values(self) -> "SemanticCriterionAmendment":
        for value in (self.previous_value, self.new_value):
            if self.field == SemanticCriterionAmendmentField.CRITERION_TYPE:
                if value not in (CriterionType.MUST_HAVE.value, CriterionType.PREFERRED.value):
                    raise ValueError("Malformed criterion type amendment.")
            elif self.field == SemanticCriterionAmendmentField.MIN_YEARS:
                if not re.fullmatch(r"\d{1,2}(?:\.\d+)?", value) or float(value) > 60:
                    raise ValueError("Malformed min_years amendment.")
                if value != f"{float(value):g}":
                    raise ValueError("Non-canonical min_years amendment.")
            elif not _CEFR_OR_NAMED_LEVEL_RE.fullmatch(value):
                raise ValueError("Malformed language level amendment.")
        if self.previous_value == self.new_value:
            raise ValueError("An amendment must change the value.")
        if hashlib.sha256(self.source_text.encode("utf-8")).hexdigest() != self.source_sha256:
            raise ValueError("Amendment source digest does not match its source text.")
        return self


class RequirementSpanState(StrEnum):
    SCORABLE = "SCORABLE"
    UNSUPPORTED = "UNSUPPORTED"
    PROHIBITED = "PROHIBITED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"


class RequirementSpanResult(BaseModel):
    """Final explicit reconciliation state for one canonical occurrence."""

    model_config = {"extra": "forbid"}

    span_id: str = Field(pattern=r"^req-\d{4}$")
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    # Present for reviewable professional requirements. Prohibited source
    # text is redacted from the tool result/session authority payload.
    text: str | None = Field(default=None, max_length=4000)
    normalized: str | None = Field(default=None, max_length=4000)
    state: RequirementSpanState
    criterion_type: CriterionType | None = None
    criterion_id: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,64}$")
    # Issue #84: authority of the (possibly review-gated) canonical shape;
    # None when no canonical shape exists for this span.
    interpretation_source: SemanticInterpretationSource | None = None


class AgentJobDraftToolResult(BaseModel):
    """DRAFT_JOB_CRITERIA's tool result — NEVER LLM-authored directly:
    must_have/preferred are real, already-validated CriterionIn rows (same
    schema/denylist the manual form and the REST API use), built by
    meyar.agent.service from a JDCriteriaDraft. An item that fails
    CriterionIn validation is never silently dropped: a non-sensitive
    failure is disclosed verbatim in ``unsupported`` (HR sees exactly
    which requirement will not participate in deterministic scoring); a
    sensitive/prohibited-attribute match is counted in
    ``prohibited_count`` only — its own text is never redisplayed,
    matching the same denylist discipline the manual form and REST API
    already enforce. A requirement with no resolved server-owned span id
    (a model proposal with an unknown span id) is counted in
    ``ungrounded_count`` only, for the same
    reason: its own text was never confirmed to actually be in HR's JD,
    so redisplaying it would itself misattribute invented content to the
    source document. Carries no candidate/tenant data. Only ever attached
    on a genuine drafting success (possibly with zero criteria) — a
    drafting call that never produced a usable result at all is the
    distinct AgentTurnOutcome.JOB_DRAFT_FAILED outcome with no
    tool_results at all, mirroring the existing AGENT_PROVIDER_FAILURE/
    MALFORMED_MODEL_OUTPUT precedent (D-036)."""

    model_config = {"extra": "forbid"}

    title: str | None = None
    draft_id: uuid.UUID
    requested_result_limit: int | None = Field(default=None, ge=0, le=9999)
    result_limit: int = Field(
        default=DEFAULT_RESULT_LIMIT, ge=MIN_RESULT_LIMIT, le=MAX_RESULT_LIMIT
    )
    result_limit_was_bounded: bool = False
    must_have: list[CriterionIn] = Field(default_factory=list)
    preferred: list[CriterionIn] = Field(default_factory=list)
    unsupported: list[UnsupportedJDCriterionItem] = Field(default_factory=list)
    needs_review: list[NeedsReviewJDCriterionItem] = Field(default_factory=list)
    prohibited_count: int = Field(default=0, ge=0)
    ungrounded_count: int = Field(default=0, ge=0)
    unsupported_language: SupportedInputLanguage | None = None
    result_limit_needs_review: bool = False
    wrong_mode_guidance: bool = False
    modification_source_text: str | None = Field(default=None, max_length=500)
    # Issue #84: which JD semantic-interpretation policy produced this draft.
    # None only for drafts persisted before the policy was versioned.
    semantic_policy_version: str | None = Field(default=None, max_length=64)
    # Issue #84 durable-provenance inputs (all server-owned; never model text).
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    semantic_prompt_version: str | None = Field(default=None, max_length=64)
    semantic_model: SemanticModelProvenance | None = None
    rejected_proposal_count: int = Field(default=0, ge=0)
    review_decisions: list[SemanticReviewDecision] = Field(
        default_factory=list, max_length=MAX_JD_REQUIREMENT_SPANS
    )
    conflict_resolutions: list[SemanticConflictResolution] = Field(
        default_factory=list, max_length=MAX_JD_REQUIREMENT_SPANS
    )
    amendments: list[SemanticCriterionAmendment] = Field(default_factory=list, max_length=256)
    # Session-held only (issue #84): the exact analysed JD, so confirmation
    # can re-derive every source value deterministically instead of trusting
    # draft fields. It lives in the same bounded transcript as the HR turn
    # that supplied it; it is never audited, logged or persisted in the
    # durable provenance (only its sha256 is).
    # Excluded from every dump/render: only ``pending_draft_payload`` writes
    # it into the session-held transcript payload.
    source_jd_text: str | None = Field(default=None, max_length=20000, exclude=True)
    requirements: list[RequirementSpanResult] = Field(
        default_factory=list, max_length=MAX_JD_REQUIREMENT_SPANS
    )


def pending_draft_payload(draft: "AgentJobDraftToolResult") -> dict[str, Any]:
    """The server-held transcript payload of a pending draft (issue #84):
    the public draft plus the exact analysed JD, so confirmation can
    re-derive source values. The JD is already in the same transcript as
    the HR turn that supplied it; it is never rendered, audited or put in
    durable provenance."""
    payload = draft.model_dump(mode="json")
    payload["source_jd_text"] = draft.source_jd_text
    return payload


class ConfirmedAgentJobDraft(BaseModel):
    """Session UI state for one canonical draft confirmation.

    A copy may remain in bounded conversation JSON to redisplay safe unscored
    requirements. Confirmation identity and idempotency live independently in
    ``AgentDraftConfirmation``; ids in this payload are never authoritative.
    """

    model_config = {"extra": "forbid"}

    draft_id: uuid.UUID
    job_id: uuid.UUID
    criteria_version_id: uuid.UUID
    result_limit: int = Field(
        default=DEFAULT_RESULT_LIMIT, ge=MIN_RESULT_LIMIT, le=MAX_RESULT_LIMIT
    )
    unsupported_requirements: list[str] = Field(default_factory=list)
    needs_review_requirements: list[str] = Field(default_factory=list)


class AgentToolResult(BaseModel):
    """One executed tool call's typed result, tagged by which tool
    produced it. Exactly one of the payload fields is set, matching
    ``tool_name`` — enforced below, mirroring AgentDecision's discipline."""

    model_config = {"extra": "forbid"}

    tool_name: AgentActionType
    search: AgentSearchToolResult | None = None
    profile: AgentProfileToolResult | None = None
    evidence: AgentEvidenceToolResult | None = None
    job_draft: AgentJobDraftToolResult | None = None
    refine: AgentRefineToolResult | None = None

    @model_validator(mode="after")
    def _validate_payload_matches_tool(self) -> "AgentToolResult":
        expected = {
            AgentActionType.SEARCH_CANDIDATES: ("search",),
            AgentActionType.GET_CANDIDATE_PROFILE: ("profile",),
            AgentActionType.GET_CANDIDATE_EVIDENCE: ("evidence",),
            AgentActionType.DRAFT_JOB_CRITERIA: ("job_draft",),
            AgentActionType.REFINE_CANDIDATE_RESULTS: ("refine",),
        }.get(self.tool_name)
        if expected is None:
            raise ValueError(f"{self.tool_name} is not a valid tool result tag.")
        for field_name in ("search", "profile", "evidence", "job_draft", "refine"):
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
    # A tool (always SEARCH_CANDIDATES — the only tool that loops back for
    # another decision) already produced a real, grounded result, but the
    # SUBSEQUENT "what next" decision step failed (timeout/unavailable/
    # repeated schema-invalid output). This is never treated as a fatal
    # turn failure — the grounded tool_results are real and safe to show;
    # only the optional closing framing is missing. Distinct from
    # MALFORMED_MODEL_OUTPUT/AGENT_PROVIDER_FAILURE, which apply only when
    # tool_results is empty. See D-036.
    ANSWERED_FROM_TOOL_RESULT = "ANSWERED_FROM_TOOL_RESULT"
    CLARIFICATION_REQUESTED = "CLARIFICATION_REQUESTED"
    CANDIDATE_REF_NOT_FOUND = "CANDIDATE_REF_NOT_FOUND"
    # issue #49: the referenced AgentResultSet was found and otherwise
    # valid (tenant/session/context_epoch/expiry all matched), but the
    # tenant's searchable corpus has since drifted (a member's profile was
    # re-extracted, a new candidate became searchable, or — for semantic/
    # hybrid — the compatible embedding version changed) since it was
    # created. Distinct from CANDIDATE_REF_NOT_FOUND (which also covers an
    # ordinal that never resolves at all, e.g. no active result set) so HR
    # is told to re-run the search rather than that the candidate itself
    # was never found. See meyar.services.agent_result_set_repo.
    RESULT_SET_STALE = "RESULT_SET_STALE"
    # issue #49: the referenced AgentResultSet's own expires_at (bound to
    # the owning BrowserSession's expiry) has passed.
    RESULT_SET_EXPIRED = "RESULT_SET_EXPIRED"
    # DRAFT_JOB_CRITERIA's own drafting call never produced a usable
    # result (provider failure or repeated schema-invalid output) —
    # tool_results is always empty for this outcome, same as
    # AGENT_PROVIDER_FAILURE/MALFORMED_MODEL_OUTPUT below.
    JOB_DRAFT_FAILED = "JOB_DRAFT_FAILED"
    TOOL_CALL_LIMIT_EXCEEDED = "TOOL_CALL_LIMIT_EXCEEDED"
    AGENT_PROVIDER_FAILURE = "AGENT_PROVIDER_FAILURE"
    MALFORMED_MODEL_OUTPUT = "MALFORMED_MODEL_OUTPUT"


class GroundedFact(BaseModel):
    """One already-validated, already-extracted professional fact offered
    to the model for grounded-answer synthesis (D-037/D-038) — never raw
    CV text, never CandidateIdentity. Built server-side, purely from an
    already-fetched AgentProfileToolResult/AgentEvidenceToolResult; ``id``
    is this fact's position in the list given to the model for exactly
    this one call, not a database id."""

    model_config = {"extra": "forbid"}

    id: int = Field(ge=0)
    category: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=500)
    detail: str | None = Field(default=None, max_length=300)


class GroundedCaveat(StrEnum):
    """A closed, non-extensible set of caveat SENTENCES the server may
    append — never free text, so a caveat can never itself smuggle in an
    unsupported claim. Add a new member only when a genuinely new,
    deterministic caveat sentence is needed; never add a free-text
    caveat field."""

    # The question asked about a specific duration/count (e.g. "how many
    # years of Python") that the supplied facts cannot support — see the
    # skill-specific-duration precedent (D-027) this mirrors for the
    # agent path.
    DURATION_NOT_PROVEN = "DURATION_NOT_PROVEN"


class GroundedSelection(BaseModel):
    """Strict model output for D-038 grounded-answer synthesis. The model
    selects which of the supplied ``GroundedFact.id`` values are relevant
    to the question, in the order it judges most useful, and may set
    ``caveat`` to one fixed, closed-enum caveat — it authors no sentence
    text at all. meyar.agent.service.render_grounded_answer independently
    re-checks every id is one that was actually supplied before building
    the displayed sentence purely from those facts' own values; an
    invalid selection is discarded, never persisted or shown — see
    D-038."""

    model_config = {"extra": "forbid"}

    used_facts: list[int] = Field(default_factory=list, max_length=30)
    caveat: GroundedCaveat | None = None


class AgentTurnResult(BaseModel):
    """The full, safe result of one bounded orchestration turn. ``message``
    is fixed server-owned copy for FINAL_ANSWER/CLARIFY, or a server-built
    grounded-answer sentence assembled entirely from GroundedFact values
    the model only selected/ordered (D-038) — never model-authored prose.
    Every factual claim also always lives in ``tool_results`` itself
    (deterministic, evidence-grounded, server-rendered) regardless of
    what ``message`` says."""

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

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

import uuid
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from meyar.core.result_count import DEFAULT_RESULT_LIMIT, MAX_RESULT_LIMIT, MIN_RESULT_LIMIT
from meyar.schemas.candidate_profile import CandidateProfileExtraction, EvidenceRef
from meyar.schemas.criteria import CriterionIn, CriterionType
from meyar.search.planner_schemas import PlannedCandidateSearchResponse

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
    # Slice 4 (issue #33, D-030/D-032): the user pasted/described a JD or
    # role and wants candidate-evaluation criteria drafted from it. No
    # argument on the decision itself — the server uses the user's OWN
    # already-known message text as the JD input for a second, narrower
    # LLM call (meyar.agent.service._dispatch_draft_job_criteria), exactly
    # like GET_CANDIDATE_EVIDENCE never asks the model to restate CV text.
    # Nothing is persisted by this action — see AgentJobDraftToolResult.
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


_FINAL_RESPONSE_CODES = frozenset({AgentResponseCode.GREETING, AgentResponseCode.ACKNOWLEDGEMENT})
_CLARIFICATION_RESPONSE_CODES = frozenset(
    {
        AgentResponseCode.NEED_MORE_DETAIL,
        AgentResponseCode.CANDIDATE_REFERENCE_REQUIRED,
        AgentResponseCode.UNSUPPORTED_REQUEST,
        AgentResponseCode.HIRING_DECISION_REQUIRES_HUMAN,
    }
)


TOOL_ACTIONS = frozenset(
    {
        AgentActionType.SEARCH_CANDIDATES,
        AgentActionType.GET_CANDIDATE_PROFILE,
        AgentActionType.GET_CANDIDATE_EVIDENCE,
        AgentActionType.DRAFT_JOB_CRITERIA,
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
        elif self.action in (
            AgentActionType.GET_CANDIDATE_PROFILE,
            AgentActionType.GET_CANDIDATE_EVIDENCE,
        ):
            if self.candidate_ref is None:
                raise ValueError(f"{self.action} requires candidate_ref.")
            if self.search_query is not None or self.response_code is not None:
                raise ValueError(f"{self.action} must not set search_query or response_code.")
            if (
                self.action == AgentActionType.GET_CANDIDATE_PROFILE
                and self.evidence_topic is not None
            ):
                raise ValueError("GET_CANDIDATE_PROFILE must not set evidence_topic.")
        elif self.action == AgentActionType.DRAFT_JOB_CRITERIA:
            # Bare action, no argument — see AgentActionType.DRAFT_JOB_CRITERIA
            # docstring for why the JD text itself is never round-tripped
            # through the model's own output.
            if (
                self.search_query is not None
                or self.candidate_ref is not None
                or self.evidence_topic is not None
                or self.response_code is not None
            ):
                raise ValueError(
                    "DRAFT_JOB_CRITERIA must not set search_query, candidate_ref, "
                    "evidence_topic, or response_code."
                )
        else:  # FINAL_ANSWER / CLARIFY
            if self.response_code is None:
                raise ValueError(f"{self.action} requires response_code.")
            if (
                self.search_query is not None
                or self.candidate_ref is not None
                or self.evidence_topic is not None
            ):
                raise ValueError(
                    f"{self.action} must not set search_query, candidate_ref, or evidence_topic."
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
    never becomes a ``CriterionIn`` — it always maps to
    ``DroppedJDCriterionReason.UNSUPPORTED`` in
    meyar.agent.service._build_criterion_from_draft_item, deterministically,
    never via an incidental validation failure. Deliberately NOT
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

    @model_validator(mode="after")
    def _validate_offsets(self) -> "RequirementSpan":
        if self.end_offset <= self.start_offset:
            raise ValueError("RequirementSpan end_offset must follow start_offset.")
        return self


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


class DroppedJDCriterionReason(StrEnum):
    """Why a model-drafted JD requirement did not become a real
    CriterionIn — see PR #42 owner correction (issue #33): a drafted
    requirement must never simply vanish, but a PROHIBITED one's own text
    is exactly what security policy forbids re-displaying, so the reasons
    are surfaced very differently (see UnsupportedJDCriterionItem and
    AgentJobDraftToolResult.prohibited_count/ungrounded_count)."""

    # A non-sensitive requirement that failed some other CriterionIn rule
    # (for example an EXPERIENCE item the JD text gave no derivable
    # duration for). Safe to disclose verbatim — HR must see it, per the
    # ACAMS-style "not silently dropped" requirement.
    UNSUPPORTED = "UNSUPPORTED"
    # Matched the sensitive/irrelevant-attribute denylist
    # (meyar.schemas.criteria.find_prohibited_term). The matched
    # requirement's own text must never be re-displayed or persisted —
    # only a count and a safe, generic HR-facing explanation.
    PROHIBITED = "PROHIBITED"
    # Failed to resolve a server-owned canonical span id. The requirement's
    # own text therefore has no authoritative occurrence in the actual JD —
    # the structural replacement for D-046's lexical grounding check:
    # a short/underspecified JD reliably gets "filled in" with a
    # plausible-sounding but entirely unstated item (the reported
    # "Passing an exam" fabricated onto an unrelated travel-readiness
    # requirement). Never disclosed verbatim — unlike UNSUPPORTED, this
    # requirement was never confirmed to actually be in the JD, so
    # presenting its own text would itself misattribute invented content
    # to HR's own source document; only a safe count is exposed (see
    # AgentJobDraftToolResult.ungrounded_count).
    UNGROUNDED = "UNGROUNDED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"


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


class NeedsReviewJDCriterionItem(BaseModel):
    """A source requirement whose material semantics could not be safely
    represented or whose model draft omitted/changed a source-bound field.
    It remains visible to HR but is never submitted as a scoring row."""

    model_config = {"extra": "forbid"}

    requirement: str = Field(min_length=1, max_length=500)
    criterion_type: CriterionType | None = None


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
    (see DroppedJDCriterionReason.UNGROUNDED) is counted in
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
    requirements: list[RequirementSpanResult] = Field(
        default_factory=list, max_length=MAX_JD_REQUIREMENT_SPANS
    )


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

    @model_validator(mode="after")
    def _validate_payload_matches_tool(self) -> "AgentToolResult":
        expected = {
            AgentActionType.SEARCH_CANDIDATES: ("search",),
            AgentActionType.GET_CANDIDATE_PROFILE: ("profile",),
            AgentActionType.GET_CANDIDATE_EVIDENCE: ("evidence",),
            AgentActionType.DRAFT_JOB_CRITERIA: ("job_draft",),
        }.get(self.tool_name)
        if expected is None:
            raise ValueError(f"{self.tool_name} is not a valid tool result tag.")
        for field_name in ("search", "profile", "evidence", "job_draft"):
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

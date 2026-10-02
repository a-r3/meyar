"""Closed, server-owned contracts of the capability layer (issue #88 slices
B and C, D-092 §8–§11, implementation records D-094 / D-095).

Pure data only: no database, no model, no executor imports.

Two plan shapes reach the pure Layer-1 validator:

* the MODEL proposal ``agent-plan-v1`` (``AgentPlanProposal``): a closed
  PLAN / CLARIFY / CONVERSE contract whose steps carry only a
  ``CapabilityName`` and GROUNDED arguments — exact quotations of the
  user's own current message (§10.2). It has no field for a tenant /
  session / conversation / ResultSet / candidate / draft / task /
  clarification id, a scope, a confirmation flag, a score, a weight, a
  date, a module/function name, a free-text answer, or any model-authored
  search/filter/topic text or number;
* the SERVER plan ``capability-plan-v1``: built only by the server for the
  deterministic forced routes and resumed clarifications (grounded by
  construction: the whole server-owned source, a router span, or a
  server-parsed count).

Validation turns either into ``ValidatedStep``s that hold only
SERVER-resolved values (source slices, parsed integers) plus offsets/hashes
— the executors never see a model-authored argument."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meyar.agent.capabilities.grounding import (
    MAX_QUOTE_LENGTH,
    MAX_QUOTES_PER_SELECTION,
    GroundedSpan,
)
from meyar.agent.clarification_schemas import TaskPhase, TaskType
from meyar.agent.schemas import MAX_CANDIDATE_REF, AgentResponseCode

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from meyar.agent.schemas import AgentToolResult
    from meyar.agent.turn_boundary import TurnSessionState
    from meyar.embedding.provider import EmbeddingProvider
    from meyar.llm.provider import LLMProvider
    from meyar.schemas.candidate_profile import CandidateProfileExtraction
    from meyar.search.schemas import EmbeddingSearchConfig
    from meyar.services.agent_result_set_repo import ResultSetResolutionFailure

# Server-built plan shape (forced routes / resumed clarifications).
CAPABILITY_PLAN_SCHEMA_VERSION = "capability-plan-v1"
# D-092 §10.1: the model output contract.
AGENT_PLAN_SCHEMA_VERSION = "agent-plan-v1"
# D-092 §10.3: effective bound is min(MAX_PLAN_STEPS, agent_max_tool_calls).
MAX_PLAN_STEPS = 3


class CapabilityName(StrEnum):
    """Closed. Extending = one enum member + one registry entry."""

    SEARCH_CANDIDATES = "SEARCH_CANDIDATES"
    REFINE_RESULTS = "REFINE_RESULTS"
    GET_CANDIDATE_PROFILE = "GET_CANDIDATE_PROFILE"
    GET_CANDIDATE_EVIDENCE = "GET_CANDIDATE_EVIDENCE"
    ANALYZE_VACANCY = "ANALYZE_VACANCY"
    CREATE_JOB = "CREATE_JOB"
    RANK_JOB_CANDIDATES = "RANK_JOB_CANDIDATES"


class SideEffect(StrEnum):
    NONE = "NONE"
    SESSION_WORKING_STATE = "SESSION_WORKING_STATE"
    DERIVED_RECORDS = "DERIVED_RECORDS"
    BUSINESS_MUTATION = "BUSINESS_MUTATION"


class ExecutionMode(StrEnum):
    IN_TURN = "IN_TURN"
    HUMAN_ACTION_ONLY = "HUMAN_ACTION_ONLY"


class LiveContextReq(StrEnum):
    ACTIVE_RESULT_SET = "ACTIVE_RESULT_SET"
    PENDING_DRAFT = "PENDING_DRAFT"
    CONFIRMED_JOB_IN_SESSION = "CONFIRMED_JOB_IN_SESSION"


class ConfirmationPolicy(StrEnum):
    NONE = "NONE"
    EXPLICIT_HUMAN_ROUTE = "EXPLICIT_HUMAN_ROUTE"


class ContentPolicy(StrEnum):
    NONE = "NONE"
    PROFESSIONAL_LOCAL_ONLY = "PROFESSIONAL_LOCAL_ONLY"


class IdentityPolicy(StrEnum):
    # CandidateIdentity is display-only in the UI view layer; it is never a
    # capability input, model input, task state or audit value.
    NEVER_IN_INPUT_OR_MODEL = "NEVER_IN_INPUT_OR_MODEL"


class PlanOrigin(StrEnum):
    """Set by the SERVER when it hands a plan to the validator — never a
    field of the proposal itself, so a model can never claim SERVER."""

    SERVER = "SERVER"
    MODEL = "MODEL"


# ---------------------------------------------------------------------------
# Grounding primitives and per-capability argument schemas (strict)
# ---------------------------------------------------------------------------

_ARGS_CONFIG = ConfigDict(extra="forbid", strict=True, frozen=True)


class SourceQuote(BaseModel):
    """An exact, unique substring of the current user message. The server
    resolves it to offsets; its characters never become executable input
    except as that exact source slice."""

    model_config = _ARGS_CONFIG

    quote: str = Field(min_length=1, max_length=MAX_QUOTE_LENGTH)


class SourceSelection(BaseModel):
    """WHOLE_MESSAGE (default, always safe) or 1..4 exact QUOTES."""

    model_config = _ARGS_CONFIG

    mode: Literal["WHOLE_MESSAGE", "QUOTES"]
    quotes: list[SourceQuote] = Field(default_factory=list, max_length=MAX_QUOTES_PER_SELECTION)

    @model_validator(mode="after")
    def _mode_shape(self) -> SourceSelection:
        if self.mode == "WHOLE_MESSAGE" and self.quotes:
            raise ValueError("WHOLE_MESSAGE takes no quotes.")
        if self.mode == "QUOTES" and not self.quotes:
            raise ValueError("QUOTES requires at least one quote.")
        return self


class SearchArgs(BaseModel):
    model_config = _ARGS_CONFIG

    source: SourceSelection


class RefineArgs(BaseModel):
    """Grounded refinement: a filter SOURCE selection and/or a count QUOTE
    the server parses — no model-authored filter text or integer."""

    model_config = _ARGS_CONFIG

    filter_source: SourceSelection | None = None
    limit_quote: SourceQuote | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> RefineArgs:
        if self.filter_source is None and self.limit_quote is None:
            raise ValueError("REFINE_RESULTS requires filter_source or limit_quote.")
        return self


class ProfileArgs(BaseModel):
    """``ref_quote`` (e.g. "birincinin"): the server's closed ordinal parser
    reads it; there is no model-supplied integer."""

    model_config = _ARGS_CONFIG

    ref_quote: SourceQuote


class EvidenceArgs(BaseModel):
    """``topic_quote`` is an exact grounded slice or absent (all evidence)."""

    model_config = _ARGS_CONFIG

    ref_quote: SourceQuote
    topic_quote: SourceQuote | None = None


class NoArgs(BaseModel):
    """CREATE_JOB / RANK_JOB_CANDIDATES (every target is server-derived)
    and a model's ANALYZE_VACANCY proposal (never executable)."""

    model_config = _ARGS_CONFIG


class SourceSpan(BaseModel):
    model_config = _ARGS_CONFIG

    start: int = Field(ge=0)
    end: int = Field(gt=0)


class VacancyArgs(BaseModel):
    """SERVER-built only (ANALYZE_VACANCY is not model-proposable): exact
    offsets into the plan's server-owned source text."""

    model_config = _ARGS_CONFIG

    source: SourceSpan


class ServerLimitArgs(BaseModel):
    """SERVER-built only: FORCE_RESULT_LIMIT's server-parsed count."""

    model_config = _ARGS_CONFIG

    limit: int = Field(ge=1, le=MAX_CANDIDATE_REF)


class HumanActionAffordance(BaseModel):
    """A HUMAN_ACTION_ONLY step converted to a server-derived affordance:
    the existing authenticated CSRF route is the only executor. Carries no
    id — the route re-derives its live target from session authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: CapabilityName
    live_target: LiveContextReq


# ---------------------------------------------------------------------------
# agent-plan-v1 — the model output contract (D-092 §10.1)
# ---------------------------------------------------------------------------

_PROPOSAL_CONFIG = ConfigDict(extra="forbid", strict=True, frozen=True)


class PlanKind(StrEnum):
    PLAN = "PLAN"
    CLARIFY = "CLARIFY"
    CONVERSE = "CONVERSE"


class PlanGoal(StrEnum):
    """The goal TaskTypes a model plan may claim (§11.1 lane selection)."""

    CANDIDATE_SEARCH = TaskType.CANDIDATE_SEARCH.value
    RESULT_FOLLOWUP = TaskType.RESULT_FOLLOWUP.value
    VACANCY_ANALYSIS = TaskType.VACANCY_ANALYSIS.value


class ModelClarificationCode(StrEnum):
    """Closed CLARIFY codes. SEARCH_OR_VACANCY becomes the server-typed
    resumable clarification (§6.1) when the text is requirement-shaped; the
    rest stay non-resumable fixed copy."""

    NEED_MORE_DETAIL = "NEED_MORE_DETAIL"
    CANDIDATE_REFERENCE_REQUIRED = "CANDIDATE_REFERENCE_REQUIRED"
    RESULT_CONTEXT_REQUIRED = "RESULT_CONTEXT_REQUIRED"
    UNSUPPORTED_REQUEST = "UNSUPPORTED_REQUEST"
    HIRING_DECISION_REQUIRES_HUMAN = "HIRING_DECISION_REQUIRES_HUMAN"
    SEARCH_OR_VACANCY = "SEARCH_OR_VACANCY"


# CONVERSE codes (no factual content; the server owns the copy).
CONVERSE_RESPONSE_CODES = frozenset(
    {AgentResponseCode.GREETING, AgentResponseCode.ACKNOWLEDGEMENT}
)


class SearchStep(BaseModel):
    model_config = _PROPOSAL_CONFIG

    capability: Literal[CapabilityName.SEARCH_CANDIDATES]
    args: SearchArgs


class RefineStep(BaseModel):
    model_config = _PROPOSAL_CONFIG

    capability: Literal[CapabilityName.REFINE_RESULTS]
    args: RefineArgs


class ProfileStep(BaseModel):
    model_config = _PROPOSAL_CONFIG

    capability: Literal[CapabilityName.GET_CANDIDATE_PROFILE]
    args: ProfileArgs


class EvidenceStep(BaseModel):
    model_config = _PROPOSAL_CONFIG

    capability: Literal[CapabilityName.GET_CANDIDATE_EVIDENCE]
    args: EvidenceArgs


class VacancyProposalStep(BaseModel):
    """Parses only so Layer 1 can reject it as NOT_MODEL_PROPOSABLE and the
    server can turn it into the SEARCH_OR_VACANCY clarification."""

    model_config = _PROPOSAL_CONFIG

    capability: Literal[CapabilityName.ANALYZE_VACANCY]
    args: NoArgs


class CreateJobStep(BaseModel):
    model_config = _PROPOSAL_CONFIG

    capability: Literal[CapabilityName.CREATE_JOB]
    args: NoArgs


class RankStep(BaseModel):
    model_config = _PROPOSAL_CONFIG

    capability: Literal[CapabilityName.RANK_JOB_CANDIDATES]
    args: NoArgs


PlanStep = Annotated[
    SearchStep
    | RefineStep
    | ProfileStep
    | EvidenceStep
    | VacancyProposalStep
    | CreateJobStep
    | RankStep,
    Field(discriminator="capability"),
]
STEP_MODEL_BY_CAPABILITY: dict[CapabilityName, type[BaseModel]] = {
    CapabilityName.SEARCH_CANDIDATES: SearchStep,
    CapabilityName.REFINE_RESULTS: RefineStep,
    CapabilityName.GET_CANDIDATE_PROFILE: ProfileStep,
    CapabilityName.GET_CANDIDATE_EVIDENCE: EvidenceStep,
    CapabilityName.ANALYZE_VACANCY: VacancyProposalStep,
    CapabilityName.CREATE_JOB: CreateJobStep,
    CapabilityName.RANK_JOB_CANDIDATES: RankStep,
}


class AgentPlanProposal(BaseModel):
    """Strict model output. The kind/field combination is checked by the
    pure Layer 1 (SHAPE_INVALID), not here, so a well-typed but incoherent
    proposal is an audited rejection rather than a repair."""

    model_config = _PROPOSAL_CONFIG

    schema_version: Literal["agent-plan-v1"]
    kind: PlanKind
    goal: PlanGoal | None = None
    steps: list[PlanStep] = Field(default_factory=list, max_length=MAX_PLAN_STEPS)
    clarification_code: ModelClarificationCode | None = None
    response_code: AgentResponseCode | None = None


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _quote_schema() -> dict[str, Any]:
    return _object(
        {"quote": {"type": "string", "minLength": 1, "maxLength": MAX_QUOTE_LENGTH}}, ["quote"]
    )


def _selection_schema() -> dict[str, Any]:
    """WHOLE_MESSAGE takes no quotes; QUOTES takes 1..4 (mode-specific)."""
    return {
        "anyOf": [
            _object({"mode": {"const": "WHOLE_MESSAGE"}}, ["mode"]),
            _object(
                {
                    "mode": {"const": "QUOTES"},
                    "quotes": {
                        "type": "array",
                        "items": _quote_schema(),
                        "minItems": 1,
                        "maxItems": MAX_QUOTES_PER_SELECTION,
                    },
                },
                ["mode", "quotes"],
            ),
        ]
    }


def _args_schema(name: CapabilityName) -> dict[str, Any]:
    if name == CapabilityName.SEARCH_CANDIDATES:
        return _object({"source": _selection_schema()}, ["source"])
    if name == CapabilityName.REFINE_RESULTS:
        properties = {"filter_source": _selection_schema(), "limit_quote": _quote_schema()}
        # At least one of the two operations.
        return {
            "anyOf": [
                _object(properties, ["filter_source"]),
                _object(properties, ["limit_quote"]),
            ]
        }
    if name == CapabilityName.GET_CANDIDATE_PROFILE:
        return _object({"ref_quote": _quote_schema()}, ["ref_quote"])
    if name == CapabilityName.GET_CANDIDATE_EVIDENCE:
        return _object(
            {"ref_quote": _quote_schema(), "topic_quote": _quote_schema()}, ["ref_quote"]
        )
    return _object({}, [])


def agent_plan_json_schema(
    offered: Sequence[CapabilityName], *, max_steps: int = MAX_PLAN_STEPS
) -> dict[str, Any]:
    """The per-call JSON schema sent to the local model for constrained
    decoding (D-092 §8.1). Server-owned and kind-specific: a PLAN needs a
    goal and 1..max_steps steps whose capability is ONE OF THE OFFERED
    names; CLARIFY needs one closed clarification code; CONVERSE one of
    GREETING / ACKNOWLEDGEMENT. It narrows what a small model can emit; the
    response is still parsed against the full strict ``AgentPlanProposal``
    and re-checked by Layer 1 (UNKNOWN_CAPABILITY, SHAPE_INVALID, ...)."""
    version = {"const": AGENT_PLAN_SCHEMA_VERSION}
    variants: list[dict[str, Any]] = []
    if offered and max_steps >= 1:
        steps = [
            _object({"capability": {"const": name.value}, "args": _args_schema(name)},
                    ["capability", "args"])
            for name in offered
        ]
        variants.append(
            _object(
                {
                    "schema_version": version,
                    "kind": {"const": PlanKind.PLAN.value},
                    "goal": {"enum": [goal.value for goal in PlanGoal]},
                    "steps": {
                        "type": "array",
                        "items": steps[0] if len(steps) == 1 else {"anyOf": steps},
                        "minItems": 1,
                        "maxItems": max_steps,
                    },
                },
                ["schema_version", "kind", "goal", "steps"],
            )
        )
    variants.append(
        _object(
            {
                "schema_version": version,
                "kind": {"const": PlanKind.CLARIFY.value},
                "clarification_code": {"enum": [code.value for code in ModelClarificationCode]},
            },
            ["schema_version", "kind", "clarification_code"],
        )
    )
    variants.append(
        _object(
            {
                "schema_version": version,
                "kind": {"const": PlanKind.CONVERSE.value},
                "response_code": {
                    "enum": sorted(code.value for code in CONVERSE_RESPONSE_CODES)
                },
            },
            ["schema_version", "kind", "response_code"],
        )
    )
    return {"anyOf": variants}


# ---------------------------------------------------------------------------
# Typed model-context projection (D-092 §15) — an ALLOW-LIST
# ---------------------------------------------------------------------------


class ContextTurn(BaseModel):
    """One bounded transcript entry. Assistant entries carry only their
    closed outcome code (never stored display text, which may name a
    candidate); user entries carry the HR user's own text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["user", "assistant"]
    text: str = Field(max_length=4000)


class OfferedCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: CapabilityName
    description: str = Field(min_length=1, max_length=300)


class WaitingClarification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_type: TaskType
    phase: TaskPhase


class AgentPlanContext(BaseModel):
    """The ENTIRE input of ``propose_agent_plan``. No field can hold an id,
    identity, scope, token, score, date or audit data: everything is a
    closed code, a bounded integer/boolean, or user/fixed text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recent_turns: list[ContextTurn] = Field(max_length=100)
    available_capabilities: list[OfferedCapability] = Field(max_length=len(CapabilityName))
    max_plan_steps: int = Field(ge=1, le=MAX_PLAN_STEPS)
    active_result_context_present: bool
    available_candidate_refs: list[int] = Field(max_length=MAX_CANDIDATE_REF)
    waiting_clarification: WaitingClarification | None = None
    pending_vacancy_confirmation: bool


# ---------------------------------------------------------------------------
# Layer 1 (pure) validation inputs/outputs
# ---------------------------------------------------------------------------


class ResultSetStatus(StrEnum):
    """D-092 §11.1: the pre-existing active ResultSet as read (read-only) by
    the server just before validation."""

    NONE = "NONE"
    VALID = "VALID"
    STALE = "STALE"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class ResultSetContext:
    """Issue #86 makes staleness MEMBER-scoped, so STALE is judged for what
    a step consumes: ``stale_ordinals`` are the referenced members whose own
    snapshot is no longer authoritative (an unrelated changed member never
    affects a reference), and ``snapshot_stale`` covers a whole-snapshot
    consumer (refinement). Set-level ``STALE`` is an unsupported snapshot
    policy. The server inspects exactly the consumed ordinals / snapshot
    (``pre_existing_result_set_requirements``)."""

    status: ResultSetStatus
    member_count: int | None = None
    stale_ordinals: frozenset[int] = frozenset()
    snapshot_stale: bool = False


@dataclass(frozen=True)
class ValidationContext:
    """Assembled by the server (read-only) just before validation."""

    principal_scopes: frozenset[str]
    pre_existing_result_set: ResultSetContext
    pending_draft_live: bool
    confirmed_job_in_session: bool
    max_tool_calls: int
    # Candidate-content capabilities run only against the local model.
    candidate_content_local_only: bool = True


class PlanRejectionCode(StrEnum):
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    SHAPE_INVALID = "SHAPE_INVALID"
    UNKNOWN_CAPABILITY = "UNKNOWN_CAPABILITY"
    NOT_MODEL_PROPOSABLE = "NOT_MODEL_PROPOSABLE"
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    PLAN_TOO_LONG = "PLAN_TOO_LONG"
    DUPLICATE_STEP = "DUPLICATE_STEP"
    SCOPE_MISSING = "SCOPE_MISSING"
    TASK_TYPE_CONFLICT = "TASK_TYPE_CONFLICT"
    SOURCE_SELECTION_FORBIDDEN = "SOURCE_SELECTION_FORBIDDEN"
    SOURCE_NOT_GROUNDED = "SOURCE_NOT_GROUNDED"
    REFERENCE_NOT_GROUNDED = "REFERENCE_NOT_GROUNDED"
    SOURCE_COVERAGE_INCOMPLETE = "SOURCE_COVERAGE_INCOMPLETE"
    PROHIBITED_ATTRIBUTE = "PROHIBITED_ATTRIBUTE"
    DEPENDENCY_INVALID = "DEPENDENCY_INVALID"
    RESULT_CONTEXT_REQUIRED = "RESULT_CONTEXT_REQUIRED"
    RESULT_SET_STALE = "RESULT_SET_STALE"
    RESULT_SET_EXPIRED = "RESULT_SET_EXPIRED"
    CANDIDATE_REF_OUT_OF_RANGE = "CANDIDATE_REF_OUT_OF_RANGE"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    CANDIDATE_CONTENT_POLICY = "CANDIDATE_CONTENT_POLICY"


class GroundedField(StrEnum):
    """Which argument a grounding record backs (closed; audited/hashed)."""

    SOURCE = "SOURCE"
    FILTER = "FILTER"
    LIMIT = "LIMIT"
    REFERENCE = "REFERENCE"
    TOPIC = "TOPIC"
    VACANCY_SOURCE = "VACANCY_SOURCE"


class GroundingMode(StrEnum):
    WHOLE_MESSAGE = "WHOLE_MESSAGE"
    QUOTES = "QUOTES"
    SERVER_SPAN = "SERVER_SPAN"
    SERVER_VALUE = "SERVER_VALUE"


@dataclass(frozen=True)
class GroundingRecord:
    field: GroundedField
    mode: GroundingMode
    spans: tuple[GroundedSpan, ...] = ()


@dataclass(frozen=True)
class PlanRejection:
    """Whole-plan rejection: zero capabilities execute. Codes go to audit
    only; HR sees fixed copy. ``grounded_field`` / ``candidate_ref`` carry
    only the closed field code and the server-parsed ordinal needed to
    reproduce today's truthful outward copy/card."""

    code: PlanRejectionCode
    step_index: int | None = None
    grounded_field: GroundedField | None = None
    candidate_ref: int | None = None


@dataclass(frozen=True)
class ValidatedStep:
    """Only SERVER-resolved values: an exact source slice (or slices joined
    by the server separator), server-parsed integers, and the grounding
    offsets/hashes. No model-authored text or number."""

    index: int
    capability: CapabilityName
    policy_version: str
    # Search source / refine filter / vacancy span.
    resolved_text: str | None = None
    candidate_ref: int | None = None
    limit: int | None = None
    topic: str | None = None
    grounding: tuple[GroundingRecord, ...] = ()


@dataclass(frozen=True)
class ExecutablePlan:
    schema_version: str
    origin: PlanOrigin
    goal: TaskType
    steps: tuple[ValidatedStep, ...]
    affordances: tuple[HumanActionAffordance, ...] = ()


# ---------------------------------------------------------------------------
# Execution (Layer 2 + executors)
# ---------------------------------------------------------------------------


@dataclass
class ExecutionContext:
    """Everything an executor may touch. ``session_context`` is the in-turn
    working copy (never the ORM row): a produced ResultSet becomes its
    working pointer only for later steps; Phase B activates it only for a
    completed plan (§11.3, §12.1)."""

    db: AsyncSession
    llm: LLMProvider
    tenant_id: uuid.UUID
    session_context: TurnSessionState
    as_of_date: date
    embedding_config: EmbeddingSearchConfig
    embedding_provider: EmbeddingProvider | None


@dataclass(frozen=True)
class CapabilityOutcome:
    """One executor's truthful result. ``succeeded`` is False for a
    truthful non-success (non-executable search, unresolved reference,
    rejected refinement, failed draft) — never for an infrastructure
    error, which propagates and abandons the turn (#85)."""

    capability: CapabilityName
    succeeded: bool
    tool_result: AgentToolResult | None = None
    produced_result_set_id: uuid.UUID | None = None
    matched_profile: CandidateProfileExtraction | None = None
    resolution_failure: ResultSetResolutionFailure | None = None
    rejection_message: str | None = None


class PlanStatus(StrEnum):
    COMPLETED = "COMPLETED"
    # Single-step plan: the step's own truthful outcome is the plan outcome,
    # exactly as today (§11.3).
    STEP_FAILED = "STEP_FAILED"
    # Multi-step plan with a failed precondition or non-success step.
    INCOMPLETE = "INCOMPLETE"


class StepFailureReason(StrEnum):
    PRODUCER_FAILED = "PRODUCER_FAILED"
    RESULT_SET_MISSING = "RESULT_SET_MISSING"
    RESULT_SET_FOREIGN = "RESULT_SET_FOREIGN"
    RESULT_SET_EXPIRED = "RESULT_SET_EXPIRED"
    RESULT_SET_EMPTY = "RESULT_SET_EMPTY"
    CANDIDATE_REF_OUT_OF_RANGE = "CANDIDATE_REF_OUT_OF_RANGE"
    STEP_NOT_SUCCESSFUL = "STEP_NOT_SUCCESSFUL"


@dataclass
class PlanExecution:
    status: PlanStatus
    outcomes: list[CapabilityOutcome] = field(default_factory=list)
    failed_step_index: int | None = None
    failure_reason: StepFailureReason | None = None
    # The ResultSet pointer this plan activates — set ONLY for a COMPLETED
    # plan; otherwise the previous active pointer is kept exactly.
    activated_result_set_id: uuid.UUID | None = None

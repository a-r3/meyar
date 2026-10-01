"""Closed, server-owned contracts of the capability layer (issue #88 slice B,
D-092 §8–§11, implementation record D-094).

Pure data only: no database, no model, no executor imports. The model can
only ever produce a ``CapabilityName`` value (through today's transitional
``AgentDecision`` adapter in slice B); it never names a Python module or
function and never supplies a tenant/session/ResultSet/candidate id, scope,
confirmation flag, score or date.

Slice B scope note: ``SourceSelection`` admits only ``WHOLE_MESSAGE``. The
``QUOTES`` grounding, coverage and the closed ordinal parsers of §10.2 are
slice C; the refine filter/limit, candidate ordinal and evidence topic stay
the transitional ``AgentDecision`` fields (D-092 §23)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meyar.agent.clarification_schemas import TaskType
from meyar.agent.schemas import MAX_AGENT_SEARCH_QUERY_LENGTH, MAX_CANDIDATE_REF

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from meyar.agent.schemas import AgentToolResult
    from meyar.agent.turn_boundary import TurnSessionState
    from meyar.embedding.provider import EmbeddingProvider
    from meyar.llm.provider import LLMProvider
    from meyar.schemas.candidate_profile import CandidateProfileExtraction
    from meyar.search.schemas import EmbeddingSearchConfig
    from meyar.services.agent_result_set_repo import ResultSetResolutionFailure

# Server-side plan contract version (NOT the slice-C model output
# ``agent-plan-v1``): the shape the transitional adapter and future
# server-built plans hand to ``validate_plan``.
CAPABILITY_PLAN_SCHEMA_VERSION = "capability-plan-v1"
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
# Per-capability input schemas (strict, extra="forbid")
# ---------------------------------------------------------------------------

_ARGS_CONFIG = ConfigDict(extra="forbid", strict=True, frozen=True)


class SourceSelection(BaseModel):
    """Slice B: WHOLE_MESSAGE only. It resolves to the plan's server-owned
    source text exactly (the canonical current message, or a resumed
    clarification's bound source span) — the same input today's
    FORCE_CANDIDATE_SEARCH gives the planner. QUOTES is slice C."""

    model_config = _ARGS_CONFIG

    mode: Literal["WHOLE_MESSAGE"]


class SearchArgs(BaseModel):
    model_config = _ARGS_CONFIG

    source: SourceSelection


class RefineArgs(BaseModel):
    """Transitional (slice B): today's model-supplied AgentDecision
    ``filter_query``/``limit``, same bounds. Replaced by grounded quotes in
    slice C."""

    model_config = _ARGS_CONFIG

    filter_query: str | None = Field(
        default=None, min_length=1, max_length=MAX_AGENT_SEARCH_QUERY_LENGTH
    )
    limit: int | None = Field(default=None, ge=1, le=MAX_CANDIDATE_REF)

    @model_validator(mode="after")
    def _at_least_one(self) -> RefineArgs:
        if self.filter_query is None and self.limit is None:
            raise ValueError("REFINE_RESULTS requires filter_query or limit.")
        return self


class ProfileArgs(BaseModel):
    """Transitional: today's AgentDecision ordinal ``candidate_ref``."""

    model_config = _ARGS_CONFIG

    candidate_ref: int = Field(ge=1, le=MAX_CANDIDATE_REF)


class EvidenceArgs(BaseModel):
    """Transitional: today's ``candidate_ref`` + optional ``evidence_topic``."""

    model_config = _ARGS_CONFIG

    candidate_ref: int = Field(ge=1, le=MAX_CANDIDATE_REF)
    evidence_topic: str | None = Field(default=None, max_length=200)


class SourceSpan(BaseModel):
    model_config = _ARGS_CONFIG

    start: int = Field(ge=0)
    end: int = Field(gt=0)


class VacancyArgs(BaseModel):
    """Server-built only (ANALYZE_VACANCY is not model-proposable): exact
    offsets into the plan's server-owned source text."""

    model_config = _ARGS_CONFIG

    source: SourceSpan


class NoArgs(BaseModel):
    """CREATE_JOB / RANK_JOB_CANDIDATES: every target is server-derived."""

    model_config = _ARGS_CONFIG


class HumanActionAffordance(BaseModel):
    """A HUMAN_ACTION_ONLY step converted to a server-derived affordance:
    the existing authenticated CSRF route is the only executor. Carries no
    id — the route re-derives its live target from session authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability: CapabilityName
    live_target: LiveContextReq


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
    PROHIBITED_ATTRIBUTE = "PROHIBITED_ATTRIBUTE"
    DEPENDENCY_INVALID = "DEPENDENCY_INVALID"
    RESULT_CONTEXT_REQUIRED = "RESULT_CONTEXT_REQUIRED"
    RESULT_SET_STALE = "RESULT_SET_STALE"
    RESULT_SET_EXPIRED = "RESULT_SET_EXPIRED"
    CANDIDATE_REF_OUT_OF_RANGE = "CANDIDATE_REF_OUT_OF_RANGE"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    CANDIDATE_CONTENT_POLICY = "CANDIDATE_CONTENT_POLICY"


@dataclass(frozen=True)
class PlanRejection:
    """Whole-plan rejection: zero capabilities execute. Codes go to audit
    only; HR sees fixed copy."""

    code: PlanRejectionCode
    step_index: int | None = None


@dataclass(frozen=True)
class ValidatedStep:
    index: int
    capability: CapabilityName
    policy_version: str
    args: BaseModel
    # Server-resolved grounded text (search source / vacancy span); never
    # model-authored. ``None`` for capabilities without a text source.
    resolved_text: str | None = None


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

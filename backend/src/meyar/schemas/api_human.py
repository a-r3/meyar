"""Typed, bounded DTOs for the accountable-human JSON surface (issue #45).

Every request model forbids extra fields, so a client cannot smuggle
``tenant_id``/``user_id``/``session_id`` (or any other authority) into a
mutation: identity is only ever derived from the authenticated session."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from meyar.agent.schemas import AgentActionType, AgentTurnOutcome
from meyar.models.agent_conversation import AgentConversationTitleKind
from meyar.schemas.criteria import CriterionIn
from meyar.schemas.read_models import (
    AgentCandidateProfileView,
    AgentEvidenceView,
    AgentJobDraftView,
    AgentRefineSummaryView,
    AgentTurnLogView,
    CandidateSearchResultView,
)
from meyar.search.planner_schemas import PlannerOutcome

MAX_TURN_MESSAGE_TRANSPORT_CHARS = 8192


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionOut(_Strict):
    """Who the server believes the caller is, plus the CSRF token to echo in
    ``X-CSRF-Token`` on every state-changing request."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    membership_id: uuid.UUID
    role: str
    scopes: list[str]
    csrf_token: str


class ConversationSummaryOut(_Strict):
    id: uuid.UUID
    title_kind: AgentConversationTitleKind
    updated_at: datetime


class ConversationListOut(_Strict):
    items: list[ConversationSummaryOut]
    page: int
    page_size: int
    has_next: bool


class ChoiceOut(_Strict):
    value: str
    label: str


class ActiveClarificationOut(_Strict):
    """The ONE answerable clarification (closed choices)."""

    id: uuid.UUID
    choices: list[ChoiceOut]


class ConversationDetailOut(_Strict):
    id: uuid.UUID
    title_kind: AgentConversationTitleKind
    updated_at: datetime
    # Bounded server-side (MAX_PERSISTED_AGENT_TURNS); oldest entries dropped.
    turns: list[AgentTurnLogView] = Field(max_length=100)
    active_clarification: ActiveClarificationOut | None = None
    active_draft: AgentJobDraftView | None = None


class SubmissionOut(_Strict):
    """Single-use identity for ONE turn submission (replay-safe)."""

    submission_id: uuid.UUID
    conversation_id: uuid.UUID
    expires_at: datetime


class TurnRequest(_Strict):
    message: str = Field(min_length=1, max_length=MAX_TURN_MESSAGE_TRANSPORT_CHARS)
    submission_id: uuid.UUID
    clarification_id: uuid.UUID | None = None
    clarification_choice: str | None = Field(default=None, max_length=64)


class SearchOutcomeOut(_Strict):
    """A planner outcome is an ANSWER, never an HTTP error. Only the closed
    outcome enum and fixed text are public; internal reason codes are not."""

    outcome: PlannerOutcome
    executable: bool
    message: str
    result_count: int | None = None
    infrastructure_error: bool = False


class TurnResultOut(_Strict):
    tool: AgentActionType
    search: SearchOutcomeOut | None = None
    candidates: list[CandidateSearchResultView] = Field(default_factory=list)
    profile: AgentCandidateProfileView | None = None
    evidence: AgentEvidenceView | None = None
    job_draft: AgentJobDraftView | None = None
    refine: AgentRefineSummaryView | None = None
    candidate_reference_not_found: int | None = None


class TurnStatus(StrEnum):
    COMPLETED = "COMPLETED"
    REPLAYED = "REPLAYED"


class TurnResponse(_Strict):
    status: TurnStatus
    conversation_id: uuid.UUID
    # Absent on REPLAYED: the completed turn is read from the conversation.
    outcome: AgentTurnOutcome | None = None
    message: str | None = None
    results: list[TurnResultOut] = Field(default_factory=list, max_length=16)


class ResolveDraftRequest(_Strict):
    """Exactly one of ``criterion_type`` / ``decision`` / ``option``."""

    span_id: str = Field(pattern=r"^req-\d{4}$")
    criterion_type: Literal["MUST_HAVE", "PREFERRED"] | None = None
    decision: Literal["exclude"] | None = None
    option: int | None = Field(default=None, ge=0, le=15)


class ReviewedCriterion(_Strict):
    span_id: str = Field(pattern=r"^req-\d{4}$")
    criterion: CriterionIn


class ReviewedDraft(_Strict):
    """OPTIONAL: what the caller says it reviewed. A proposal only — it must
    equal the live server-held draft exactly or the confirmation is refused;
    it can never add, remove or weaken a criterion."""

    title: str = Field(min_length=1, max_length=255)
    criteria: list[ReviewedCriterion] = Field(max_length=64)


class ConfirmDraftRequest(_Strict):
    reviewed: ReviewedDraft | None = None


class ResolveDraftResponse(_Strict):
    draft: AgentJobDraftView


class ConfirmDraftResponse(_Strict):
    """Confirmation is complete once this returns. Ranking is a separate call."""

    job_id: uuid.UUID
    criteria_version_id: uuid.UUID
    criteria_version_number: int | None = None
    replayed: bool
    result_limit: int | None = None
    unsupported_requirements: list[str] = Field(default_factory=list)
    needs_review_requirements: list[str] = Field(default_factory=list)

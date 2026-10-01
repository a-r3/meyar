"""Closed vocabulary for resumable clarification (issue #88 slice A,
docs/AGENT_CORE_V2_DESIGN.md §4.3/§6, D-092 + Amendments A1/A2).

Pure enums and strict schemas only: no DB, no service imports, so the
LLM provider boundary and the dialogue logic can share them without cycles.
Every value here is server-owned; the local model may only pick one of the
closed ``ClarificationProposalValue`` codes it is offered."""

from enum import StrEnum

from pydantic import BaseModel

ANSWER_SCHEMA_VERSION = "clarification-answers-v1"
TASK_POLICY_VERSION = "agent-core-v2-task-v1"
TASK_STATE_SCHEMA_VERSION = 1
MAX_CLARIFICATION_ATTEMPTS = 2
CLARIFICATION_PROMPT_VERSION = "clarification-classifier-prompt-v1"


class ClarificationType(StrEnum):
    SEARCH_OR_VACANCY = "SEARCH_OR_VACANCY"
    VACANCY_SOURCE_REQUIRED = "VACANCY_SOURCE_REQUIRED"


class ClarificationAnswer(StrEnum):
    CANDIDATE_SEARCH = "CANDIDATE_SEARCH"
    VACANCY_ANALYSIS = "VACANCY_ANALYSIS"


class ClarificationProposalValue(StrEnum):
    """Classifier output: an allowed answer, or one of two closed signals."""

    CANDIDATE_SEARCH = "CANDIDATE_SEARCH"
    VACANCY_ANALYSIS = "VACANCY_ANALYSIS"
    NEW_REQUEST = "NEW_REQUEST"
    UNCLEAR = "UNCLEAR"


class ClarificationStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"


class SupersededReason(StrEnum):
    """A2: only real persisted transitions of the clarification itself.
    A rejected button and a classifier failure transition nothing."""

    UNCLEAR = "UNCLEAR"
    NEW_TASK = "NEW_TASK"


class ResolutionSource(StrEnum):
    BUTTON = "BUTTON"
    LABEL = "LABEL"
    MODEL = "MODEL"
    SOURCE_MESSAGE = "SOURCE_MESSAGE"


class ExpiryReason(StrEnum):
    """Audit-only reason code for an EXPIRED clarification (§19.2)."""

    TTL = "TTL"
    STALE = "STALE"
    VERSION = "VERSION"
    ATTEMPTS = "ATTEMPTS"
    SOURCE_MISMATCH = "SOURCE_MISMATCH"


class RejectionReason(StrEnum):
    NOT_ACTIVE = "NOT_ACTIVE"
    INVALID_CHOICE = "INVALID_CHOICE"


class TaskType(StrEnum):
    UNDETERMINED = "UNDETERMINED"
    CANDIDATE_SEARCH = "CANDIDATE_SEARCH"
    RESULT_FOLLOWUP = "RESULT_FOLLOWUP"
    VACANCY_ANALYSIS = "VACANCY_ANALYSIS"


class TaskStatus(StrEnum):
    WAITING_CLARIFICATION = "WAITING_CLARIFICATION"
    WAITING_CONFIRMATION = "WAITING_CONFIRMATION"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED_SAFE = "FAILED_SAFE"


TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.EXPIRED, TaskStatus.FAILED_SAFE}
)


class TaskPhase(StrEnum):
    NEEDS_INTENT_CHOICE = "NEEDS_INTENT_CHOICE"
    NEEDS_SOURCE = "NEEDS_SOURCE"
    DRAFT_REVIEW = "DRAFT_REVIEW"
    DONE = "DONE"


ALLOWED_ANSWERS: dict[ClarificationType, tuple[ClarificationAnswer, ...]] = {
    ClarificationType.SEARCH_OR_VACANCY: (
        ClarificationAnswer.CANDIDATE_SEARCH,
        ClarificationAnswer.VACANCY_ANALYSIS,
    ),
    # Slot fill (§6.3): the next qualifying message is the source; no choice.
    ClarificationType.VACANCY_SOURCE_REQUIRED: (),
}


class ClarificationAnswerProposal(BaseModel):
    """Strict local-model output for ``resolve_clarification_answer``. The
    server still checks the value against the allowed set of the live
    clarification's type before using it."""

    model_config = {"extra": "forbid"}

    value: ClarificationProposalValue

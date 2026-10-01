"""Resumable clarification: pure, deterministic dialogue-state logic
(issue #88 slice A, docs/AGENT_CORE_V2_DESIGN.md §6, D-092 + A1/A2).

No DB, no model calls. The orchestrator (meyar.agent.service) reads rows,
calls these functions on immutable snapshots, and stages the resulting
transitions; Phase B (meyar.services.agent_task_repo) re-verifies them under
row locks before writing anything.

Liveness (§6.2) never consults ``turn_version``: it is the A1 append-position
binding with the A2 exchange chain, validated from at most four transcript
tail entries plus one persisted predecessor row."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import ValidationError

from meyar.agent.canonical_requirements import JD_SEMANTIC_POLICY_VERSION
from meyar.agent.clarification_schemas import (
    ALLOWED_ANSWERS,
    ANSWER_SCHEMA_VERSION,
    ClarificationAnswer,
    ClarificationStatus,
    ClarificationType,
    ExpiryReason,
    RejectionReason,
    ResolutionSource,
    SupersededReason,
    TaskPhase,
    TaskStatus,
    TaskType,
)
from meyar.agent.intent_routing import (
    ENTRY_ROUTING_POLICY_VERSION,
    AgentEntryRoute,
    AgentEntryRoutingResult,
)
from meyar.agent.schemas import SemanticRequirementState
from meyar.agent.semantic_requirements import analyze_hr_text
from meyar.core.text import fold_az_ascii, normalize_azerbaijani_case

MAX_SOURCE_OFFSET = 4000

# Fixed server copy (§20). Never model text, never ids or reason codes.
CLARIFICATION_STALE_COPY = (
    "Bu sual artıq aktiv deyil. Zəhmət olmasa əvvəlki sorğunu yenidən göndərin."
)
CLARIFICATION_REJECTED_COPY = "Bu sual artıq aktiv deyil."
CLARIFICATION_ATTEMPTS_EXHAUSTED_COPY = (
    "Sorğunu başa düşə bilmədim. Zəhmət olmasa sorğunu daha aydın ifadə edərək "
    "yenidən göndərin."
)
CHOICE_LABELS: dict[ClarificationAnswer, str] = {
    ClarificationAnswer.CANDIDATE_SEARCH: "Namizəd axtarışı",
    ClarificationAnswer.VACANCY_ANALYSIS: "Vakansiya tələbi kimi",
}


class ClarificationRejectedError(Exception):
    """A stale/foreign/mismatched clarification button (A2, T8b): a rejected
    request, never a clarification transition. The turn is abandoned."""

    def __init__(self, reason: RejectionReason) -> None:
        self.reason = reason
        super().__init__(f"Clarification button rejected: {reason.value}.")


class ClarificationClassifierError(Exception):
    """Classifier infrastructure/contract failure (A2, T8a). NOT ``UNCLEAR``:
    the turn is abandoned; no attempt is consumed and nothing transitions."""


# ---------------------------------------------------------------------------
# Closed-choice label table `clarification-answers-v1` (§6.4 step 2, §6.5)
# ---------------------------------------------------------------------------

_SEARCH_NOUNS = frozenset({"namized", "namizedler", "candidate", "candidates"})
_SEARCH_QUALIFIERS = frozenset({"axtaris", "axtarisi", "axtar", "search"})
_VACANCY_NOUNS = frozenset({"vakansiya", "vacancy", "job"})
_VACANCY_BARE_NOUNS = frozenset({"vakansiya", "vacancy"})
_VACANCY_QUALIFIERS = frozenset(
    {"kimi", "teleb", "telebi", "requirement", "requirements", "analiz", "analysis"}
)
_FILLER = frozenset({"kimi", "as", "please", "zehmet", "olmasa", "et", "edin", "a", "the"})
_MAX_LABEL_TOKENS = 8
_TOKEN_RE = re.compile(r"\w+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(fold_az_ascii(normalize_azerbaijani_case(text)))


def match_answer_label(text: str) -> ClarificationAnswer | None:
    """Strict whole-message match of the server's own button labels.

    Every token must belong to exactly one answer's label vocabulary or the
    bounded filler set; anything else (a technology, a number, another
    request) is no match and falls through to new-task detection."""
    tokens = _tokens(text)
    if not tokens or len(tokens) > _MAX_LABEL_TOKENS:
        return None
    vocabulary = (
        _SEARCH_NOUNS | _SEARCH_QUALIFIERS | _VACANCY_NOUNS | _VACANCY_QUALIFIERS | _FILLER
    )
    if any(token not in vocabulary for token in tokens):
        return None
    present = set(tokens)
    search = bool(present & _SEARCH_NOUNS) and bool(present & _SEARCH_QUALIFIERS)
    vacancy_tokens = present & (_VACANCY_NOUNS | (_VACANCY_QUALIFIERS - _FILLER))
    vacancy = (
        bool(present & _VACANCY_NOUNS)
        and (bool(present & _VACANCY_QUALIFIERS) or bool(present & _VACANCY_BARE_NOUNS))
    )
    if search and not vacancy_tokens:
        return ClarificationAnswer.CANDIDATE_SEARCH
    if vacancy and not present & (_SEARCH_NOUNS | _SEARCH_QUALIFIERS):
        return ClarificationAnswer.VACANCY_ANALYSIS
    return None


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClarificationSnapshot:
    """Immutable copy of one clarification row (+ its task's status)."""

    id: uuid.UUID
    task_id: uuid.UUID
    session_context_id: uuid.UUID
    context_epoch: int
    clarification_type: str
    answer_schema_version: str
    question_turn_id: uuid.UUID
    created_from_turn_id: uuid.UUID
    source_turn_id: uuid.UUID | None
    source_sha256: str | None
    source_start: int | None
    source_end: int | None
    semantic_policy_version: str
    routing_policy_version: str
    status: str
    attempt: int
    expires_at: datetime
    superseded_by_id: uuid.UUID | None
    superseded_reason: str | None
    task_status: str

    def same_source_binding(self, other: ClarificationSnapshot) -> bool:
        return (
            self.source_turn_id,
            self.source_sha256,
            self.source_start,
            self.source_end,
        ) == (other.source_turn_id, other.source_sha256, other.source_start, other.source_end)


def source_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entry(turns: Sequence[dict], index: int) -> dict | None:
    return turns[index] if len(turns) >= -index else None


def _is(entry: dict | None, turn_id: uuid.UUID | None, role: str) -> bool:
    return (
        entry is not None
        and turn_id is not None
        and entry.get("role") == role
        and entry.get("turn_id") == str(turn_id)
    )


def _source_requirements_ok(text: str) -> bool:
    try:
        requirements = analyze_hr_text(text).requirements
    except ValidationError:
        return False
    return bool(requirements) and not any(
        item.state == SemanticRequirementState.PROHIBITED for item in requirements
    )


def eligible_search_or_vacancy_source(message: str) -> bool:
    """§6.1: a resumable SEARCH_OR_VACANCY clarification needs ≥1 material
    requirement, no PROHIBITED span and offsets inside the 4000 bound."""
    return 0 < len(message) <= MAX_SOURCE_OFFSET and _source_requirements_ok(message)


def bound_source_text(turns: Sequence[dict], clarification: ClarificationSnapshot) -> str:
    """The exact bound span of the SOURCE turn. Call only after
    ``liveness_failure`` returned ``None`` for this clarification."""
    for turn in turns[-4:]:
        if turn.get("turn_id") == str(clarification.source_turn_id):
            text = str(turn.get("text", ""))
            assert clarification.source_start is not None
            assert clarification.source_end is not None
            return text[clarification.source_start : clarification.source_end]
    raise ValueError("Source turn is not inside the validated exchange chain.")


def liveness_failure(
    turns: Sequence[dict],
    clarification: ClarificationSnapshot,
    predecessor: ClarificationSnapshot | None,
    *,
    now: datetime,
    context_id: uuid.UUID,
    context_epoch: int,
) -> ExpiryReason | None:
    """§6.2 rules 1–5 (A1 append position + A2 exchange chain). ``None``
    means live. The caller has already checked that the live pointer equals
    ``clarification.id``. Never reads beyond ``turns[-4:]``."""
    if clarification.status != ClarificationStatus.OPEN.value:
        return ExpiryReason.STALE
    if clarification.expires_at <= now:
        return ExpiryReason.TTL
    if (
        clarification.session_context_id != context_id
        or clarification.context_epoch != context_epoch
        or clarification.task_status != TaskStatus.WAITING_CLARIFICATION.value
    ):
        return ExpiryReason.STALE
    if (
        clarification.answer_schema_version != ANSWER_SCHEMA_VERSION
        or clarification.semantic_policy_version != JD_SEMANTIC_POLICY_VERSION
        or clarification.routing_policy_version != ENTRY_ROUTING_POLICY_VERSION
    ):
        return ExpiryReason.VERSION
    search_or_vacancy = clarification.clarification_type == ClarificationType.SEARCH_OR_VACANCY
    if not _is(_entry(turns, -1), clarification.question_turn_id, "assistant") or not _is(
        _entry(turns, -2), clarification.created_from_turn_id, "user"
    ):
        return ExpiryReason.STALE
    if clarification.attempt == 1:
        if predecessor is not None:
            return ExpiryReason.STALE
        if search_or_vacancy and clarification.created_from_turn_id != clarification.source_turn_id:
            return ExpiryReason.STALE
        source_entry = _entry(turns, -2)
    elif clarification.attempt == 2:
        if (
            predecessor is None
            or predecessor.superseded_by_id != clarification.id
            or predecessor.status != ClarificationStatus.SUPERSEDED.value
            or predecessor.superseded_reason != SupersededReason.UNCLEAR.value
            or predecessor.attempt != 1
            or predecessor.task_id != clarification.task_id
            or predecessor.session_context_id != clarification.session_context_id
            or predecessor.clarification_type != clarification.clarification_type
            or not _is(_entry(turns, -4), predecessor.created_from_turn_id, "user")
            or not _is(_entry(turns, -3), predecessor.question_turn_id, "assistant")
        ):
            return ExpiryReason.STALE
        if search_or_vacancy and (
            not predecessor.same_source_binding(clarification)
            or predecessor.created_from_turn_id != predecessor.source_turn_id
        ):
            return ExpiryReason.STALE
        source_entry = _entry(turns, -4)
    else:
        return ExpiryReason.STALE
    if not search_or_vacancy:
        # VACANCY_SOURCE_REQUIRED: no source binding at any attempt (§6.2.2).
        if clarification.source_turn_id is not None:
            return ExpiryReason.STALE
        return None
    assert source_entry is not None
    start, end = clarification.source_start, clarification.source_end
    text = str(source_entry.get("text", ""))
    if (
        start is None
        or end is None
        or not 0 <= start < end <= min(len(text), MAX_SOURCE_OFFSET)
        or source_entry.get("turn_id") != str(clarification.source_turn_id)
        or source_sha256(text[start:end]) != clarification.source_sha256
    ):
        return ExpiryReason.SOURCE_MISMATCH
    if not _source_requirements_ok(text[start:end]):
        return ExpiryReason.SOURCE_MISMATCH
    return None


# ---------------------------------------------------------------------------
# §6.4 step 3: deterministic new-task / slot-fill detection
# ---------------------------------------------------------------------------


def _requirement_count(message: str, routing: AgentEntryRoutingResult) -> int:
    if routing.route == AgentEntryRoute.CLARIFY_INPUT_STRUCTURE:
        return 0
    try:
        return len(analyze_hr_text(message).requirements)
    except ValidationError:
        return 0


def is_clear_new_task(message: str, routing: AgentEntryRoutingResult) -> bool:
    """SEARCH_OR_VACANCY step 3: a forced route or ≥1 material requirement."""
    return routing.route in {
        AgentEntryRoute.FORCE_JOB_DRAFT,
        AgentEntryRoute.FORCE_CANDIDATE_SEARCH,
        AgentEntryRoute.FORCE_RESULT_LIMIT,
    } or bool(_requirement_count(message, routing))


class SourceSlotOutcome(StrEnum):
    SOURCE = "SOURCE"
    NEW_TASK = "NEW_TASK"
    UNCLEAR = "UNCLEAR"


def classify_vacancy_source_message(
    message: str, routing: AgentEntryRoutingResult
) -> tuple[SourceSlotOutcome, str | None]:
    """VACANCY_SOURCE_REQUIRED step 3 (A2), deterministic, no model.

    An explicit new search / count follow-up is a new task first; then the
    current qualifying message (or the router's exact FORCE_JOB_DRAFT span)
    is the slot fill; anything else is UNCLEAR."""
    if routing.route in {
        AgentEntryRoute.FORCE_CANDIDATE_SEARCH,
        AgentEntryRoute.FORCE_RESULT_LIMIT,
    }:
        return SourceSlotOutcome.NEW_TASK, None
    if routing.route == AgentEntryRoute.FORCE_JOB_DRAFT:
        return SourceSlotOutcome.SOURCE, routing.draft_source(message)
    if routing.route == AgentEntryRoute.CLARIFY_INPUT_STRUCTURE:
        # Not representable as bounded source occurrences, so it can never
        # be a qualifying (analyzable) vacancy source.
        return SourceSlotOutcome.UNCLEAR, None
    nonempty_lines = [line for line in message.splitlines() if line.strip()]
    if _requirement_count(message, routing) or len(nonempty_lines) >= 2:
        return SourceSlotOutcome.SOURCE, message
    return SourceSlotOutcome.UNCLEAR, None


# ---------------------------------------------------------------------------
# Staged changes (applied only in Phase B)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClarificationTransition:
    """Transition of the live clarification (and its task). ``task_status``
    is ``None`` for a resolution until the resumed capability's outcome is
    known (see ``finalize_resolution``)."""

    clarification_id: uuid.UUID
    # ``None`` only for a defensive pointer-clear of a row that cannot be read.
    task_id: uuid.UUID | None
    status: ClarificationStatus
    task_status: TaskStatus | None
    requires_live: bool
    superseded_reason: SupersededReason | None = None
    expiry_reason: ExpiryReason | None = None
    resolved_value: ClarificationAnswer | None = None
    resolution_source: ResolutionSource | None = None
    task_type: TaskType | None = None


@dataclass(frozen=True)
class NewClarification:
    id: uuid.UUID
    clarification_type: ClarificationType
    attempt: int
    question_turn_id: uuid.UUID
    created_from_turn_id: uuid.UUID
    task_type: TaskType
    task_phase: TaskPhase
    reuse_task_id: uuid.UUID | None = None
    predecessor_id: uuid.UUID | None = None
    source_turn_id: uuid.UUID | None = None
    source_sha256: str | None = None
    source_start: int | None = None
    source_end: int | None = None


class LaneBChange(StrEnum):
    NONE = "NONE"
    AMENDED = "AMENDED"  # T9: deterministic follow-up minted a new draft id
    NEW_DRAFT = "NEW_DRAFT"  # T3 / T11: a new vacancy analysis moved the pointer


@dataclass(frozen=True)
class DialogueCommit:
    transition: ClarificationTransition | None = None
    new_clarification: NewClarification | None = None
    lane_b: LaneBChange = LaneBChange.NONE

    @property
    def touches_lane_a(self) -> bool:
        return self.transition is not None or self.new_clarification is not None


def search_or_vacancy_clarification(
    *, message: str, user_turn_id: uuid.UUID, question_turn_id: uuid.UUID
) -> NewClarification:
    return NewClarification(
        id=uuid.uuid4(),
        clarification_type=ClarificationType.SEARCH_OR_VACANCY,
        attempt=1,
        question_turn_id=question_turn_id,
        created_from_turn_id=user_turn_id,
        task_type=TaskType.UNDETERMINED,
        task_phase=TaskPhase.NEEDS_INTENT_CHOICE,
        source_turn_id=user_turn_id,
        source_sha256=source_sha256(message),
        source_start=0,
        source_end=len(message),
    )


def vacancy_source_clarification(
    *, user_turn_id: uuid.UUID, question_turn_id: uuid.UUID
) -> NewClarification:
    return NewClarification(
        id=uuid.uuid4(),
        clarification_type=ClarificationType.VACANCY_SOURCE_REQUIRED,
        attempt=1,
        question_turn_id=question_turn_id,
        created_from_turn_id=user_turn_id,
        task_type=TaskType.VACANCY_ANALYSIS,
        task_phase=TaskPhase.NEEDS_SOURCE,
    )


def retry_clarification(
    current: ClarificationSnapshot, *, user_turn_id: uuid.UUID, question_turn_id: uuid.UUID
) -> NewClarification:
    """Attempt 2 (T5): same task, same type, the SAME original source
    binding; ``created_from_turn_id`` is the UNCLEAR answer U2."""
    search_or_vacancy = current.clarification_type == ClarificationType.SEARCH_OR_VACANCY
    return NewClarification(
        id=uuid.uuid4(),
        clarification_type=ClarificationType(current.clarification_type),
        attempt=current.attempt + 1,
        question_turn_id=question_turn_id,
        created_from_turn_id=user_turn_id,
        task_type=(
            TaskType.UNDETERMINED if search_or_vacancy else TaskType.VACANCY_ANALYSIS
        ),
        task_phase=(
            TaskPhase.NEEDS_INTENT_CHOICE if search_or_vacancy else TaskPhase.NEEDS_SOURCE
        ),
        reuse_task_id=current.task_id,
        predecessor_id=current.id,
        source_turn_id=current.source_turn_id,
        source_sha256=current.source_sha256,
        source_start=current.source_start,
        source_end=current.source_end,
    )


def clarification_payload(clarification_type: ClarificationType) -> dict[str, object]:
    """Display-only transcript payload (question code + choice codes)."""
    return {
        "question_code": clarification_type.value,
        "choices": [answer.value for answer in ALLOWED_ANSWERS[clarification_type]],
    }

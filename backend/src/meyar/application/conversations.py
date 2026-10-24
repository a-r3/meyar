"""Conversation use cases shared by the Jinja UI and the human JSON API
(issue #45, D-116).

This is the former ``POST /ui/agent`` route body, extracted without changing
behavior: submission identity/idempotent replay (#87), the two-phase
reservation with transaction release/revalidation around local inference (#85),
clarification state (#88) and the durable-conversation ownership (#80) are the
same code paths. Adapters only authenticate, validate transport shape and map
``TurnSubmissionResult`` to HTML or JSON."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.dialogue import (
    CLARIFICATION_REJECTED_COPY,
    ClarificationClassifierError,
    ClarificationRejectedError,
)
from meyar.agent.service import (
    AgentPlanProviderError,
    ClarificationButton,
    apply_agent_turn_commit,
    execute_agent_turn,
)
from meyar.agent.turn_boundary import (
    AgentInferenceBusyError,
    BoundaryEmbedding,
    BoundaryLLM,
    ClientDisconnectedError,
    ConversationSnapshot,
    ConversationTurnInProgressError,
    DisconnectProbe,
    TurnAuthorityLostError,
    TurnBoundary,
    TurnSessionState,
    TurnStaleReason,
    abandon_reserved_turn,
    clear_turn_reservation,
    reserve_agent_turn,
    run_until_client_disconnects,
)
from meyar.application.business_date import effective_business_date
from meyar.application.principal import HumanPrincipal
from meyar.config import Settings
from meyar.embedding.provider import EmbeddingProvider, EmbeddingProviderError
from meyar.llm.provider import LLMProvider
from meyar.models.agent_conversation import AgentConversation
from meyar.schemas.read_models import AgentTurnLogView, AgentTurnView
from meyar.search.schemas import EmbeddingSearchConfig
from meyar.search.service import SearchRequestError
from meyar.services.agent_conversation_repo import (
    ASSISTANT_TEXT_AUTHORITY_SERVER,
    ASSISTANT_TEXT_AUTHORITY_VERSION,
    ConversationPage,
    create_conversation,
    get_or_create_session_context,
    get_owned_conversation,
    list_owned_conversations,
    record_conversation_access_rejected,
    resolve_or_create_current_conversation,
    sync_last_turn_display_text,
)
from meyar.services.agent_submission_repo import (
    claim_submission,
    complete_submission,
    get_bound_submission,
    request_hash,
)
from meyar.services.agent_task_repo import lock_dialogue_rows, stamp_created_turn_version
from meyar.services.audit_repo import record_event
from meyar.ui.presentation import agent_turn_outcome_message
from meyar.ui.service import build_agent_turn_view


class NotRunReason(StrEnum):
    """Why an accepted request produced no new turn. These are retryable
    request outcomes, never planner outcomes and never silent successes."""

    TURN_IN_PROGRESS = "TURN_IN_PROGRESS"
    STALE = "STALE"
    BUSY = "BUSY"
    CLARIFICATION_NOT_ACTIVE = "CLARIFICATION_NOT_ACTIVE"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"


@dataclass(frozen=True)
class NotRunPresentation:
    outcome: str
    copy: str
    status_code: int
    retryable: bool


# Issue #85 (D-089): truthful HR copy for a turn that did not run or could not
# be committed. Never exposes queue/pool/lock internals or reason codes.
_PROVIDER_COPY = "MEYAR AI xidməti hazırda əlçatan deyil. Bir qədər sonra yenidən cəhd edin."
NOT_RUN_PRESENTATION: dict[NotRunReason, NotRunPresentation] = {
    NotRunReason.TURN_IN_PROGRESS: NotRunPresentation(
        "AGENT_TURN_IN_PROGRESS",
        "Bu söhbətdə əvvəlki sorğu hələ emal olunur. Cavabı gözləyin, sonra yenidən göndərin.",
        409,
        True,
    ),
    NotRunReason.STALE: NotRunPresentation(
        "AGENT_TURN_STALE",
        "Sorğu emal edilərkən söhbətin vəziyyəti dəyişdi, ona görə nəticə tətbiq edilmədi. "
        "Sorğunu yenidən göndərin.",
        409,
        True,
    ),
    NotRunReason.BUSY: NotRunPresentation(
        "AGENT_BUSY",
        "MEYAR hazırda digər sorğuları emal edir. Bir qədər sonra yenidən cəhd edin.",
        503,
        True,
    ),
    NotRunReason.CLARIFICATION_NOT_ACTIVE: NotRunPresentation(
        "CLARIFICATION_NOT_ACTIVE", CLARIFICATION_REJECTED_COPY, 409, False
    ),
    NotRunReason.PROVIDER_FAILURE: NotRunPresentation(
        "AGENT_PROVIDER_FAILURE", _PROVIDER_COPY, 503, True
    ),
}


class TurnDisposition(StrEnum):
    COMPLETED = "COMPLETED"
    REPLAYED = "REPLAYED"
    NOT_FOUND = "NOT_FOUND"
    NOT_RUN = "NOT_RUN"
    CLIENT_DISCONNECTED = "CLIENT_DISCONNECTED"
    PRINCIPAL_REVOKED = "PRINCIPAL_REVOKED"


@dataclass(frozen=True)
class TurnSubmissionResult:
    disposition: TurnDisposition
    conversation_id: uuid.UUID | None = None
    conversation: AgentConversation | None = None
    latest: AgentTurnView | None = None
    not_run: NotRunReason | None = None


@dataclass(frozen=True)
class ClarificationChoiceView:
    value: str
    label: str


@dataclass(frozen=True)
class ActiveClarificationView:
    """Closed-choice buttons for the ONE answerable clarification (§20).
    Shown only while the live pointer names an OPEN, unexpired question that
    is still the last transcript entry — never for history alone."""

    id: uuid.UUID
    choices: tuple[ClarificationChoiceView, ...]


async def active_clarification_view(
    db: AsyncSession, *, conversation: AgentConversation, session_context: object
) -> ActiveClarificationView | None:
    from meyar.agent.clarification_schemas import (
        ALLOWED_ANSWERS,
        ClarificationStatus,
        ClarificationType,
    )
    from meyar.agent.dialogue import CHOICE_LABELS
    from meyar.models.agent_task import AgentClarification

    pointer = getattr(session_context, "active_clarification_id", None)
    if pointer is None or not conversation.turns:
        return None
    row = await db.scalar(
        select(AgentClarification).where(
            AgentClarification.id == pointer,
            AgentClarification.tenant_id == conversation.tenant_id,
            AgentClarification.session_context_id == session_context.id,  # type: ignore[attr-defined]
        )
    )
    if (
        row is None
        or row.status != ClarificationStatus.OPEN.value
        or row.expires_at <= datetime.now(UTC)
        or conversation.turns[-1].get("turn_id") != str(row.question_turn_id)
    ):
        return None
    answers = ALLOWED_ANSWERS[ClarificationType(row.clarification_type)]
    return ActiveClarificationView(
        id=row.id,
        choices=tuple(
            ClarificationChoiceView(value=answer.value, label=CHOICE_LABELS[answer])
            for answer in answers
        ),
    )




MAX_TURN_MESSAGE_CHARS = 4000


def canonical_turn_message(value: str) -> str | None:
    """Canonical (LF-normalized) HR text, or ``None`` when out of bounds."""
    from meyar.agent.service import normalize_message_newlines

    canonical = normalize_message_newlines(value)
    if not 1 <= len(canonical) <= MAX_TURN_MESSAGE_CHARS:
        return None
    return canonical


def turn_log_entries(conversation: Any) -> list[AgentTurnLogView]:
    """Replay only text rendered under the currently accepted server policy.

    Older SERVER_VALIDATED markers do not prove current display authority.
    Their text is replaced by fixed outcome copy, without mutating history.
    User turns remain the HR user's own untrusted text.
    """
    views = []
    for turn in conversation.turns:
        role = turn.get("role", "user")
        if role == "assistant":
            trusted_text = (
                turn.get("text")
                if (
                    turn.get("text_authority") == ASSISTANT_TEXT_AUTHORITY_SERVER
                    and turn.get("text_authority_version") == ASSISTANT_TEXT_AUTHORITY_VERSION
                )
                else None
            )
            text = agent_turn_outcome_message(turn.get("outcome", "ANSWERED"), trusted_text or None)
        else:
            text = turn.get("text", "")
        views.append(AgentTurnLogView(role=role, text=text))
    return views


async def _audit_turn_not_committed(
    db: AsyncSession, principal: HumanPrincipal, event_type: str, reason_code: str
) -> None:
    """Bounded structural audit only: a closed reason code — never prompt,
    model output, message text, or queue/pool figures."""
    try:
        await record_event(
            db,
            tenant_id=principal.tenant_id,
            event_type=event_type,
            metadata={"reason_code": reason_code},
            actor_type=principal.actor_type,
            actor_id=principal.actor_id,
        )
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()


async def open_conversation(
    db: AsyncSession, principal: HumanPrincipal, *, conversation_id: uuid.UUID | None
) -> AgentConversation | None:
    """Select an owned conversation (explicit id) or this session's current one.

    ``None`` is returned identically for a foreign-tenant, foreign-user,
    foreign-membership or nonexistent id: no existence/ownership/title/timestamp
    distinction is ever revealed."""
    owner = principal.owner()
    if conversation_id is not None:
        selected = await get_owned_conversation(db, owner=owner, conversation_id=conversation_id)
        if selected is None:
            await record_conversation_access_rejected(db, owner=owner)
            await db.commit()
            return None
        await record_event(
            db,
            tenant_id=principal.tenant_id,
            event_type="agent.conversation.opened",
            metadata={"conversation_id": str(selected.id), "title_kind": selected.title_kind},
            actor_type=principal.actor_type,
            actor_id=principal.actor_id,
        )
    else:
        selected = await resolve_or_create_current_conversation(
            db, owner=owner, browser_session_id=principal.session_id
        )
        if selected is None:
            await db.rollback()
            return None
    await db.commit()
    return selected


async def start_new_conversation(db: AsyncSession, principal: HumanPrincipal) -> AgentConversation:
    """A NEW durable conversation plus a clean live context for THIS
    BrowserSession. The previous conversation is never deleted or cleared and
    another session's current conversation is never switched."""
    conversation = await create_conversation(db, owner=principal.owner())
    await get_or_create_session_context(
        db, conversation=conversation, browser_session_id=principal.session_id
    )
    await db.commit()
    return conversation


async def list_conversations(
    db: AsyncSession, principal: HumanPrincipal, *, page: int, page_size: int
) -> ConversationPage:
    return await list_owned_conversations(
        db, owner=principal.owner(), page=page, page_size=page_size
    )


async def submit_conversation_turn(
    db: AsyncSession,
    principal: HumanPrincipal,
    *,
    request: DisconnectProbe,
    settings: Settings,
    llm: LLMProvider,
    embedding_provider: EmbeddingProvider,
    embedding_config: EmbeddingSearchConfig,
    message: str,
    conversation_id: uuid.UUID | None,
    submission_id: uuid.UUID,
    clarification_button: ClarificationButton | None,
) -> TurnSubmissionResult:
    """Run (or idempotently replay) one conversation turn.

    ``message`` is already canonical, bounded text. Identity comes only from
    ``principal``; ``conversation_id``/``submission_id``/clarification ids are
    untrusted references that must resolve inside the principal's own
    owner + BrowserSession scope."""
    owner = principal.owner()
    explicit_selector = conversation_id is not None
    if conversation_id is None:
        # Legacy form without a selector: resolve (or race-safely create)
        # this session's current conversation in its OWN short transaction,
        # so the per-owner membership lock is released before the turn.
        current = await resolve_or_create_current_conversation(
            db, owner=owner, browser_session_id=principal.session_id
        )
        if current is None:
            await db.rollback()
            return TurnSubmissionResult(TurnDisposition.NOT_FOUND)
        conversation_id = current.id
        await db.commit()
    # #87 replay evidence over the WHOLE semantic request (D-092 §13): the
    # message plus any clarification id/choice. Never authorization.
    message_sha256 = request_hash(
        message,
        clarification_id=clarification_button.clarification_id if clarification_button else None,
        clarification_choice=clarification_button.choice if clarification_button else None,
    )
    submission = await get_bound_submission(
        db, submission_id=submission_id, owner=owner,
        browser_session_id=principal.session_id, conversation_id=conversation_id,
    )
    if submission is None or submission.expires_at <= datetime.now(UTC):
        await db.rollback()
        return TurnSubmissionResult(TurnDisposition.NOT_FOUND)
    if submission.request_sha256 is not None and submission.request_sha256 != message_sha256:
        await db.rollback()
        return TurnSubmissionResult(TurnDisposition.NOT_FOUND)
    if submission.status == "COMPLETED":
        await db.rollback()
        return TurnSubmissionResult(TurnDisposition.REPLAYED, conversation_id=conversation_id)
    if submission.status == "PROCESSING" and submission.lease_expires_at and (
        submission.lease_expires_at > datetime.now(UTC)
    ):
        await db.rollback()
        return TurnSubmissionResult(
            TurnDisposition.NOT_RUN, conversation_id=conversation_id,
            not_run=NotRunReason.TURN_IN_PROGRESS,
        )
    # issue #85 (D-089) PHASE A: lock the durable conversation row briefly,
    # authorize, refuse (immediately, 409) when another turn on this same
    # conversation is still in flight, and write the server-owned
    # reservation. The row lock is NOT held across local inference any more:
    # TurnBoundary commits before every model/embedding wait and re-locks +
    # revalidates afterwards. Same-conversation serialization (#80) is kept
    # by the reservation + turn_version, never by a lock held across Ollama.
    try:
        reserved = await reserve_agent_turn(
            db,
            owner=owner,
            conversation_id=conversation_id,
            browser_session_id=principal.session_id,
            ttl_seconds=settings.agent_turn_reservation_seconds,
        )
    except TurnAuthorityLostError:
        await db.rollback()
        return TurnSubmissionResult(TurnDisposition.PRINCIPAL_REVOKED)
    except ConversationTurnInProgressError:
        await db.rollback()
        unused_submission = await get_bound_submission(
            db, submission_id=submission_id, owner=owner,
            browser_session_id=principal.session_id, conversation_id=conversation_id,
            for_update=True,
        )
        if unused_submission is not None and unused_submission.status == "ISSUED":
            unused_submission.status = "ABANDONED"
        await record_event(
            db,
            tenant_id=principal.tenant_id,
            event_type="agent.turn.rejected",
            metadata={"reason_code": "TURN_IN_PROGRESS"},
            actor_type=principal.actor_type,
            actor_id=principal.actor_id,
        )
        await db.commit()
        return TurnSubmissionResult(
            TurnDisposition.NOT_RUN, conversation_id=conversation_id,
            not_run=NotRunReason.TURN_IN_PROGRESS,
        )
    if reserved is None:
        await db.rollback()
        if explicit_selector:
            await record_conversation_access_rejected(db, owner=owner)
            await db.commit()
        return TurnSubmissionResult(TurnDisposition.NOT_FOUND)
    reservation = reserved.reservation
    submission = await get_bound_submission(
        db, submission_id=submission_id, owner=owner,
        browser_session_id=principal.session_id, conversation_id=conversation_id,
        for_update=True,
    )
    claim_outcome = (
        claim_submission(
            submission, reservation=reservation, message_sha256=message_sha256,
            ttl_seconds=settings.agent_turn_reservation_seconds,
        ) if submission is not None else "INVALID"
    )
    if claim_outcome != "CLAIMED":
        await db.rollback()
        if claim_outcome == "COMPLETED":
            return TurnSubmissionResult(TurnDisposition.REPLAYED, conversation_id=conversation_id)
        return TurnSubmissionResult(TurnDisposition.NOT_FOUND)
    await db.flush()
    boundary = TurnBoundary(
        db, reservation, ttl_seconds=settings.agent_turn_reservation_seconds
    )
    # "Current date is a trusted-runtime value, never user/model supplied"
    # (docs/DECISIONS.md D-023): resolved once here, passed explicitly down.
    as_of_date = effective_business_date(None, timezone_name=settings.business_timezone)
    not_run: NotRunReason | None = None
    try:
        commit = await run_until_client_disconnects(
            request,
            execute_agent_turn(
                db,
                BoundaryLLM(llm, boundary),
                tenant_id=principal.tenant_id,
                # Plain snapshots cross the inference gap — never live ORM
                # authority (Phase B re-reads and revalidates everything).
                conversation=ConversationSnapshot.of(reserved.conversation),
                session_context=TurnSessionState.of(reserved.session_context),
                user_message=message,
                as_of_date=as_of_date,
                embedding_config=embedding_config,
                embedding_provider=BoundaryEmbedding(embedding_provider, boundary),
                max_tool_calls=settings.agent_max_tool_calls,
                max_context_turns=settings.agent_max_context_turns,
                clarification_button=clarification_button,
                # Issue #88 slice B: Layer-1 SCOPE_MISSING uses the live
                # principal's scopes (re-derived per request, never client).
                principal_scopes=principal.scopes,
            ),
        )
        # PHASE B: ALWAYS a fresh transaction (issue #88, D-092 §12.2) — end
        # Phase A / the last re-entry first, so no Phase A conversation/
        # context/submission lock is held when the principal is re-locked.
        # Then re-lock and revalidate principal, reservation, turn_version and
        # live context; only then persist the outcome and clear the
        # reservation in the same commit.
        conversation, session_context = await boundary.enter_phase_b()
        # Issue #88 (§12.2): clarification/task rows are locked and their
        # staged preconditions re-verified AFTER conversation/context and
        # BEFORE the submission row (fixed lock order).
        locked_dialogue = await lock_dialogue_rows(
            db, conversation=conversation, session_context=session_context,
            dialogue=commit.dialogue,
        )
        phase_b_submission = await get_bound_submission(
            db, submission_id=submission_id, owner=owner,
            browser_session_id=principal.session_id, conversation_id=conversation_id,
            for_update=True,
        )
        if phase_b_submission is None:
            raise TurnAuthorityLostError(TurnStaleReason.CONTEXT_CHANGED)
        if (
            phase_b_submission.status != "PROCESSING"
            or phase_b_submission.reservation_id != reservation.token
            or phase_b_submission.request_sha256 != message_sha256
        ):
            raise TurnAuthorityLostError(TurnStaleReason.CONTEXT_CHANGED)
        result = await apply_agent_turn_commit(
            db, conversation, session_context, tenant_id=principal.tenant_id, commit=commit,
            submission_id=submission_id, locked_dialogue=locked_dialogue,
            clarification_ttl_seconds=settings.agent_clarification_ttl_seconds,
        )
        clear_turn_reservation(conversation)
        latest = await build_agent_turn_view(db, tenant_id=principal.tenant_id, result=result)
        # D-045 (PR #42 owner correction, issue #33): make the persisted
        # turn text and the just-rendered live headline the same value, so
        # a later history re-render is byte-for-byte identical to what HR
        # saw live instead of falling back to a generic per-outcome
        # message — see sync_last_turn_display_text's own docstring.
        await sync_last_turn_display_text(db, conversation, text=latest.headline)
        # A1: provenance = the FINAL committed turn_version (after D-045).
        await stamp_created_turn_version(
            db, session_context=session_context, conversation=conversation,
            submission_id=submission_id,
        )
        complete_submission(
            phase_b_submission, reservation=reservation, message_sha256=message_sha256,
            completed_turn_version=conversation.turn_version,
        )
        await db.commit()
    except TurnAuthorityLostError as exc:
        await abandon_reserved_turn(db, reservation, submission_id=submission_id)
        await _audit_turn_not_committed(db, principal, "agent.turn.stale", exc.reason.value)
        if exc.reason == TurnStaleReason.PRINCIPAL_REVOKED:
            # Same outcome as any request made after revocation.
            return TurnSubmissionResult(TurnDisposition.PRINCIPAL_REVOKED)
        not_run = NotRunReason.STALE
    except AgentInferenceBusyError as exc:
        await abandon_reserved_turn(db, reservation, submission_id=submission_id)
        await _audit_turn_not_committed(db, principal, "agent.turn.busy", exc.reason)
        not_run = NotRunReason.BUSY
    except ClarificationRejectedError as exc:
        # A2 (T8b): a rejected request, not a clarification transition —
        # nothing appended, pointer/attempt untouched, submission ABANDONED.
        await abandon_reserved_turn(db, reservation, submission_id=submission_id)
        # Rejected before any inference: the rollback above also undid the
        # PROCESSING claim, so retire the still-ISSUED token explicitly.
        rejected_submission = await get_bound_submission(
            db, submission_id=submission_id, owner=owner,
            browser_session_id=principal.session_id, conversation_id=reservation.conversation_id,
            for_update=True,
        )
        if rejected_submission is not None and rejected_submission.status == "ISSUED":
            rejected_submission.status = "ABANDONED"
        await _audit_turn_not_committed(
            db, principal, "agent.clarification.rejected", exc.reason.value
        )
        not_run = NotRunReason.CLARIFICATION_NOT_ACTIVE
    except ClarificationClassifierError:
        # A2 (T8a): classifier infrastructure/contract failure is NOT
        # UNCLEAR — the turn is abandoned and the clarification stays live.
        await abandon_reserved_turn(db, reservation, submission_id=submission_id)
        await _audit_turn_not_committed(
            db, principal, "agent.turn.abandoned", "CLARIFICATION_CLASSIFIER_FAILURE"
        )
        not_run = NotRunReason.PROVIDER_FAILURE
    except AgentPlanProviderError:
        # Issue #88 slice C (#85): the one plan-proposal call (or its
        # repair) failed on infrastructure — the turn is abandoned: no
        # transcript, no plan execution, no task/pointer/clarification change.
        await abandon_reserved_turn(db, reservation, submission_id=submission_id)
        await _audit_turn_not_committed(
            db, principal, "agent.turn.abandoned", "AGENT_PLAN_PROVIDER_FAILURE"
        )
        not_run = NotRunReason.PROVIDER_FAILURE
    except ClientDisconnectedError:
        await abandon_reserved_turn(db, reservation, submission_id=submission_id)
        await _audit_turn_not_committed(
            db, principal, "agent.turn.abandoned", "CLIENT_DISCONNECTED"
        )
        # Nobody is listening; any minimal response is fine.
        return TurnSubmissionResult(TurnDisposition.CLIENT_DISCONNECTED)
    except (EmbeddingProviderError, SearchRequestError, SQLAlchemyError):
        await abandon_reserved_turn(db, reservation, submission_id=submission_id)
        not_run = NotRunReason.PROVIDER_FAILURE
    except BaseException:
        # Cancellation or an unexpected error: never leave this turn's
        # reservation behind (shielded, best-effort; TTL is the backstop).
        await abandon_reserved_turn(db, reservation, submission_id=submission_id)
        raise
    if not_run is not None:
        # D-044: nothing was persisted for this attempt; never a fabricated
        # answer. The adapter re-reads the owned conversation for display.
        return TurnSubmissionResult(
            TurnDisposition.NOT_RUN, conversation_id=reservation.conversation_id,
            not_run=not_run,
        )

    for tool_result in latest.tool_results:
        if tool_result.job_draft is not None:
            tool_result.job_draft.can_act = (
                session_context.active_pending_draft_id == tool_result.job_draft.draft_id
            )
    return TurnSubmissionResult(
        TurnDisposition.COMPLETED, conversation_id=conversation_id,
        conversation=conversation, latest=latest,
    )


async def issue_turn_submission(
    db: AsyncSession, principal: HumanPrincipal, *, conversation_id: uuid.UUID
):  # noqa: ANN201 - AgentTurnSubmission
    """Issue a single-use submission identity for an owned conversation."""
    from meyar.services.agent_submission_repo import issue_submission

    conversation = await get_owned_conversation(
        db, owner=principal.owner(), conversation_id=conversation_id
    )
    if conversation is None:
        await record_conversation_access_rejected(db, owner=principal.owner())
        await db.commit()
        return None
    submission = await issue_submission(
        db, owner=principal.owner(), browser_session_id=principal.session_id,
        conversation=conversation,
    )
    await db.commit()
    return submission


@dataclass(frozen=True)
class ConversationState:
    """Live, BrowserSession-bound state of one owned conversation."""

    conversation: AgentConversation
    active_clarification: ActiveClarificationView | None
    active_draft: Any  # AgentJobDraftToolResult | None


async def conversation_state(
    db: AsyncSession, principal: HumanPrincipal, conversation: AgentConversation
) -> ConversationState:
    """Read-only. A missing BrowserSession context never creates authority."""
    from meyar.services.agent_conversation_repo import (
        get_active_pending_job_draft,
        get_session_context,
    )

    session_context = await get_session_context(
        db, conversation=conversation, browser_session_id=principal.session_id
    )
    active_draft = get_active_pending_job_draft(conversation, session_context)
    clarification = (
        await active_clarification_view(
            db, conversation=conversation, session_context=session_context
        )
        if session_context is not None
        else None
    )
    return ConversationState(
        conversation=conversation, active_clarification=clarification, active_draft=active_draft
    )

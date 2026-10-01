"""Agent-turn transaction boundary around local inference (issue #85,
docs/DECISIONS.md D-089).

Invariant: NO pooled PostgreSQL connection, SQL transaction, or row lock is
held by an agent turn while it waits for an inference slot or runs a local
model/embedding call.

Lifecycle of one ``POST /ui/agent`` turn::

    PHASE A (short, locked)   reserve_agent_turn: lock the durable
                              conversation row, check ownership, refuse a
                              second in-flight turn, get/create this
                              BrowserSession's live context, write the
                              server-owned reservation (active_turn_id).
    orchestration             run on plain snapshots (ConversationSnapshot /
                              TurnSessionState), never live ORM authority.
      before EVERY model or   TurnBoundary.leave_db(): COMMIT (reservation,
      embedding call          audit rows, orphan-safe ResultSet rows) —
                              the connection returns to the pool.
      after the call          TurnBoundary.reenter(): new transaction,
                              re-lock + revalidate the live principal,
                              reservation, turn_version and session context.
    PHASE B (short, locked)   TurnBoundary.enter_phase_b(): ALWAYS a fresh
                              transaction — commit whatever DB phase is
                              still open (Phase A for a deterministic,
                              no-inference turn; the post-inference
                              re-entry otherwise), then re-lock principal
                              -> conversation -> context, then (router)
                              clarification -> task -> submission, apply
                              the transcript/pointer outcome and clear the
                              reservation in the same COMMIT.

Issue #88 (D-092 §12.2): the consequential Phase B transaction therefore
never inherits Phase A's conversation/context/submission row locks, whether
or not the turn called a model, and its FIRST lock is always the principal's
``FOR SHARE``. The commit before it can only persist what any #85 leave_db
already persists: the reservation and #87 PROCESSING claim, attempt-
provenance audit rows, and inert (pointer-less) ResultSet rows. Transcript,
pointers, task and clarification state are written only inside Phase B.

Same-conversation serialization (#80) is preserved by the reservation: at
most ONE accepted in-flight turn per conversation. A second turn on a
conversation with a live reservation is refused IMMEDIATELY with a truthful
"still processing" outcome (no hidden wait queue, so no ordering that depends
on poll timing), and a turn whose reservation or transcript version changed
can never commit.
Different conversations never share a reservation, so they stay
independent; they only contend for the GLOBAL inference admission gate
(meyar.llm.concurrency), which is intentional."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.clarification_schemas import ClarificationAnswerProposal
from meyar.agent.schemas import (
    AgentDecision,
    GroundedFact,
    GroundedSelection,
    JDCriteriaDraft,
    RequirementSpan,
)
from meyar.core.roles import permissions_for_role
from meyar.embedding.provider import EmbeddingBusyError, EmbeddingProvider, EmbeddingResult
from meyar.extraction.view import ProfessionalDocumentView
from meyar.llm.provider import InferenceBusyError, LLMProvider, LLMResultProvenance
from meyar.models.agent_conversation import (
    AgentConversation,
    AgentConversationSessionContext,
)
from meyar.models.agent_turn_submission import AgentTurnSubmission
from meyar.models.browser_session import BrowserSession
from meyar.models.tenant_membership import TenantMembership
from meyar.models.user import User
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.search.planner_schemas import PlannerDraft
from meyar.services.agent_conversation_repo import (
    OwnerPrincipal,
    get_or_create_session_context,
    get_owned_conversation_for_update,
    get_session_context_for_update,
    touch_session_context,
)

AGENT_TURN_REQUIRED_SCOPE = "candidates:read"


class TurnStaleReason(StrEnum):
    """Closed, audit-safe reason a reserved turn could not commit."""

    PRINCIPAL_REVOKED = "PRINCIPAL_REVOKED"
    CONVERSATION_UNAVAILABLE = "CONVERSATION_UNAVAILABLE"
    RESERVATION_LOST = "RESERVATION_LOST"
    CONVERSATION_CHANGED = "CONVERSATION_CHANGED"
    CONTEXT_CHANGED = "CONTEXT_CHANGED"


class TurnAuthorityLostError(Exception):
    """Authority captured at reservation time no longer holds. The turn
    fails closed: nothing it produced is committed."""

    def __init__(self, reason: TurnStaleReason) -> None:
        self.reason = reason
        super().__init__(f"Agent turn authority lost: {reason.value}.")


class AgentInferenceBusyError(Exception):
    """The global local-inference gate refused this turn's model call
    (queue full / queue wait expired). Deliberately NOT an
    ``LLMProviderError``: no agent/planner fallback may turn it into a
    fabricated or degraded "successful" answer — the whole turn is
    abandoned and the router renders the HR-safe BUSY outcome."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Agent inference busy: {reason}.")


class ConversationTurnInProgressError(Exception):
    """Another turn on the same conversation holds a live reservation."""


class ClientDisconnectedError(Exception):
    """The HTTP client went away; the turn was cancelled."""


# ---------------------------------------------------------------------------
# Snapshots that may safely cross the inference gap
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConversationSnapshot:
    id: uuid.UUID
    tenant_id: uuid.UUID
    title_kind: str
    turns: tuple[dict, ...]

    @classmethod
    def of(cls, conversation: AgentConversation) -> ConversationSnapshot:
        return cls(
            id=conversation.id,
            tenant_id=conversation.tenant_id,
            title_kind=conversation.title_kind,
            turns=tuple(dict(turn) for turn in conversation.turns),
        )


@dataclass
class TurnSessionState:
    """In-turn working copy of one BrowserSession-bound live context. The
    orchestration mutates only this plain object (never the ORM row); the
    final pointers are written in Phase B after revalidation."""

    tenant_id: uuid.UUID
    conversation_id: uuid.UUID
    browser_session_id: uuid.UUID
    context_epoch: int
    active_result_set_id: uuid.UUID | None
    active_pending_draft_id: uuid.UUID | None
    # Issue #88 slice A: the context row id (task/clarification scope) and
    # the dialogue-lane pointer as reserved. Read-only for orchestration:
    # clarification state changes are staged and applied only in Phase B.
    context_id: uuid.UUID | None = None
    active_clarification_id: uuid.UUID | None = None

    @classmethod
    def of(cls, context: AgentConversationSessionContext) -> TurnSessionState:
        return cls(
            tenant_id=context.tenant_id,
            conversation_id=context.conversation_id,
            browser_session_id=context.browser_session_id,
            context_epoch=context.context_epoch,
            active_result_set_id=context.active_result_set_id,
            active_pending_draft_id=context.active_pending_draft_id,
            context_id=context.id,
            active_clarification_id=context.active_clarification_id,
        )


@dataclass(frozen=True)
class TurnReservation:
    """Everything Phase B re-checks. Server-owned; never client-supplied."""

    token: uuid.UUID
    owner: OwnerPrincipal
    browser_session_id: uuid.UUID
    conversation_id: uuid.UUID
    turn_version: int
    context_id: uuid.UUID
    context_epoch: int
    active_result_set_id: uuid.UUID | None
    active_pending_draft_id: uuid.UUID | None
    active_clarification_id: uuid.UUID | None = None


@dataclass
class ReservedTurn:
    conversation: AgentConversation
    session_context: AgentConversationSessionContext
    reservation: TurnReservation


# ---------------------------------------------------------------------------
# Phase A — reserve
# ---------------------------------------------------------------------------


def _reservation_is_live(conversation: AgentConversation, now: datetime) -> bool:
    return (
        conversation.active_turn_id is not None
        and conversation.active_turn_expires_at is not None
        and conversation.active_turn_expires_at > now
    )


async def reserve_agent_turn(
    db: AsyncSession,
    *,
    owner: OwnerPrincipal,
    conversation_id: uuid.UUID,
    browser_session_id: uuid.UUID,
    ttl_seconds: int,
) -> ReservedTurn | None:
    """Phase A. Returns ``None`` for a foreign/nonexistent conversation
    (indistinguishable). Raises ``ConversationTurnInProgressError`` when
    another turn already holds a live reservation. Does not commit: the
    caller's transaction (and the row lock) continue until the first
    ``TurnBoundary.leave_db`` or the Phase B commit."""
    conversation = await get_owned_conversation_for_update(
        db, owner=owner, conversation_id=conversation_id
    )
    if conversation is None:
        return None
    now = datetime.now(UTC)
    if _reservation_is_live(conversation, now):
        raise ConversationTurnInProgressError()
    session_context = await get_or_create_session_context(
        db, conversation=conversation, browser_session_id=browser_session_id
    )
    touch_session_context(session_context)
    token = uuid.uuid4()
    conversation.active_turn_id = token
    conversation.active_turn_expires_at = now + timedelta(seconds=ttl_seconds)
    await db.flush()
    return ReservedTurn(
        conversation=conversation,
        session_context=session_context,
        reservation=TurnReservation(
            token=token,
            owner=owner,
            browser_session_id=browser_session_id,
            conversation_id=conversation.id,
            turn_version=conversation.turn_version,
            context_id=session_context.id,
            context_epoch=session_context.context_epoch,
            active_result_set_id=session_context.active_result_set_id,
            active_pending_draft_id=session_context.active_pending_draft_id,
            active_clarification_id=session_context.active_clarification_id,
        ),
    )


# ---------------------------------------------------------------------------
# Re-entry / Phase B — revalidate
# ---------------------------------------------------------------------------


async def _principal_is_live(db: AsyncSession, reservation: TurnReservation) -> bool:
    """Same live checks as meyar.ui.auth.get_ui_context, re-derived from
    the database (never from the request's earlier UIContext).

    issue #87 (D-091): the authority rows are row-locked ``FOR SHARE`` for
    the rest of this short re-entry/Phase B transaction, in the same
    User -> TenantMembership -> BrowserSession order every security mutator
    (set_password, set_user_active, set_membership_active, logout) writes
    them. A credential/session revocation therefore either commits first
    (this check then waits for it and sees the revoked state) or waits
    until this transaction ends — it can never commit between this check
    and the consequential commit. ``FOR SHARE`` (not ``FOR UPDATE``) blocks
    those UPDATEs but stays compatible with the ``FOR KEY SHARE`` locks FK
    inserts take (new conversations/submissions/result sets referencing
    the same user/membership/session) and with other turns' Phase B, so it
    introduces no lock-order inversion with conversation-row locks. Never
    held across inference: ``TurnBoundary.leave_db`` commits first."""
    owner = reservation.owner
    user = await db.scalar(
        select(User)
        .where(User.id == owner.user_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if user is None or not user.is_active:
        return False
    membership = await db.scalar(
        select(TenantMembership)
        .where(TenantMembership.id == owner.membership_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if (
        membership is None
        or not membership.is_active
        or membership.user_id != owner.user_id
        or membership.tenant_id != owner.tenant_id
        or AGENT_TURN_REQUIRED_SCOPE not in permissions_for_role(membership.role)
    ):
        return False
    session = await db.scalar(
        select(BrowserSession)
        .where(BrowserSession.id == reservation.browser_session_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    return (
        session is not None
        and session.revoked_at is None
        and session.expires_at > datetime.now(UTC)
        and session.user_id == owner.user_id
        and session.tenant_membership_id == owner.membership_id
    )


async def revalidate_reserved_turn(
    db: AsyncSession, reservation: TurnReservation, *, ttl_seconds: int
) -> tuple[AgentConversation, AgentConversationSessionContext]:
    """Re-lock and re-validate EVERY piece of consequential authority the
    turn was reserved under. On success the conversation and context rows
    are locked (until the next commit) and the reservation is refreshed.
    On any mismatch raises ``TurnAuthorityLostError`` — never silently
    replays against new context, never falls back to tenant-wide access."""
    if not await _principal_is_live(db, reservation):
        raise TurnAuthorityLostError(TurnStaleReason.PRINCIPAL_REVOKED)
    conversation = await get_owned_conversation_for_update(
        db, owner=reservation.owner, conversation_id=reservation.conversation_id
    )
    if conversation is None:
        raise TurnAuthorityLostError(TurnStaleReason.CONVERSATION_UNAVAILABLE)
    if conversation.active_turn_id != reservation.token:
        raise TurnAuthorityLostError(TurnStaleReason.RESERVATION_LOST)
    if conversation.turn_version != reservation.turn_version:
        raise TurnAuthorityLostError(TurnStaleReason.CONVERSATION_CHANGED)
    session_context = await get_session_context_for_update(
        db, conversation=conversation, browser_session_id=reservation.browser_session_id
    )
    if (
        session_context is None
        or session_context.id != reservation.context_id
        or session_context.context_epoch != reservation.context_epoch
        or session_context.active_result_set_id != reservation.active_result_set_id
        or session_context.active_pending_draft_id != reservation.active_pending_draft_id
        or session_context.active_clarification_id != reservation.active_clarification_id
    ):
        raise TurnAuthorityLostError(TurnStaleReason.CONTEXT_CHANGED)
    conversation.active_turn_expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    await db.flush()
    return conversation, session_context


def clear_turn_reservation(conversation: AgentConversation) -> None:
    conversation.active_turn_id = None
    conversation.active_turn_expires_at = None


async def abandon_reserved_turn(
    db: AsyncSession, reservation: TurnReservation,
    *, submission_id: uuid.UUID | None = None,
) -> None:
    """Best-effort, cancellation-shielded cleanup for a turn that will not
    commit: roll back its open transaction and clear ONLY its own
    reservation. If this itself fails (DB unreachable), the reservation
    simply expires after ``agent_turn_reservation_seconds``."""
    with anyio.CancelScope(shield=True):
        with anyio.move_on_after(10):
            try:
                await db.rollback()
                conversation = await db.scalar(
                    select(AgentConversation)
                    .where(
                        AgentConversation.id == reservation.conversation_id,
                        AgentConversation.tenant_id == reservation.owner.tenant_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if conversation is not None and conversation.active_turn_id == reservation.token:
                    clear_turn_reservation(conversation)
                if submission_id is not None:
                    submission = await db.scalar(
                        select(AgentTurnSubmission)
                        .where(AgentTurnSubmission.id == submission_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if (
                        submission is not None and submission.status == "PROCESSING"
                        and submission.reservation_id == reservation.token
                    ):
                        submission.status = "ABANDONED"
                        submission.reservation_id = None
                        submission.lease_expires_at = None
                await db.commit()
            except Exception:  # noqa: BLE001 - best-effort cleanup; TTL is the backstop
                # A connection broken by the cancellation itself must never
                # replace the original outcome/exception of the turn.
                try:
                    await db.rollback()
                except Exception:  # noqa: BLE001
                    pass


# ---------------------------------------------------------------------------
# The boundary itself
# ---------------------------------------------------------------------------


class TurnBoundary:
    """Owns the DB-phase transitions of one reserved turn."""

    def __init__(self, db: AsyncSession, reservation: TurnReservation, *, ttl_seconds: int):
        self._db = db
        self.reservation = reservation
        self._ttl_seconds = ttl_seconds
        self.model_calls = 0

    async def leave_db(self) -> None:
        """End the current short DB phase: commit and return the pooled
        connection BEFORE any inference wait/call. The first call also makes
        the Phase A reservation durable before the row lock is released."""
        await self._db.commit()
        assert not self._db.in_transaction()

    async def reenter(self) -> tuple[AgentConversation, AgentConversationSessionContext]:
        return await revalidate_reserved_turn(
            self._db, self.reservation, ttl_seconds=self._ttl_seconds
        )

    async def enter_phase_b(self) -> tuple[AgentConversation, AgentConversationSessionContext]:
        """Start the final consequential Phase B from a FRESH transaction.

        Unconditional: a deterministic turn is still inside its Phase A
        transaction (conversation/context/submission locked), and a turn that
        called a model is inside its last post-inference re-entry. Either is
        committed first (only reservation/claim, attempt audit and inert
        ResultSet rows can be pending — see the module docstring), so the
        principal ``FOR SHARE`` locks taken by revalidation are the first
        locks of the transaction that writes consequential state (D-092
        §12.2 order)."""
        await self.leave_db()
        return await self.reenter()

    async def around_inference[T](self, call: Callable[[], Awaitable[T]]) -> T:
        await self.leave_db()
        self.model_calls += 1
        try:
            result = await call()
        except (InferenceBusyError, EmbeddingBusyError) as exc:
            # Shared LLM/embedding admission gate refused: abandon the turn.
            raise AgentInferenceBusyError(exc.reason) from None
        except Exception:
            # A model failure is a normal handled outcome for the
            # orchestration, which continues with DB work — on revalidated
            # authority only. (Cancellation propagates untouched.)
            await self.reenter()
            raise
        await self.reenter()
        return result


class BoundaryLLM:
    """``LLMProvider`` that routes every model call through a TurnBoundary.
    Delegates to the real local provider; adds no prompt content."""

    def __init__(self, inner: LLMProvider, boundary: TurnBoundary) -> None:
        self._inner = inner
        self._boundary = boundary
        self.provider_name = inner.provider_name
        self.model_name = inner.model_name
        self.model_revision = inner.model_revision

    async def extract_candidate_profile(
        self, view: ProfessionalDocumentView
    ) -> tuple[CandidateProfileExtraction, str]:
        return await self._boundary.around_inference(
            lambda: self._inner.extract_candidate_profile(view)
        )

    async def extract_candidate_identity(
        self, view: ProfessionalDocumentView
    ) -> tuple[CandidateIdentityExtraction, str]:
        return await self._boundary.around_inference(
            lambda: self._inner.extract_candidate_identity(view)
        )

    async def plan_candidate_search(
        self, natural_language_request: str, *, repair: bool = False
    ) -> tuple[PlannerDraft, LLMResultProvenance]:
        return await self._boundary.around_inference(
            lambda: self._inner.plan_candidate_search(natural_language_request, repair=repair)
        )

    async def decide_agent_action(
        self,
        *,
        recent_turns: list[tuple[str, str]],
        last_tool_result_summary: dict[str, Any] | None,
        active_result_context_present: bool,
        available_candidate_refs: list[int],
        repair: bool = False,
    ) -> tuple[AgentDecision, LLMResultProvenance]:
        return await self._boundary.around_inference(
            lambda: self._inner.decide_agent_action(
                recent_turns=recent_turns,
                last_tool_result_summary=last_tool_result_summary,
                active_result_context_present=active_result_context_present,
                available_candidate_refs=available_candidate_refs,
                repair=repair,
            )
        )

    async def select_grounded_facts(
        self, *, question: str, facts: list[GroundedFact], repair: bool = False
    ) -> tuple[GroundedSelection, LLMResultProvenance]:
        return await self._boundary.around_inference(
            lambda: self._inner.select_grounded_facts(
                question=question, facts=facts, repair=repair
            )
        )

    async def draft_job_criteria(
        self,
        jd_text: str,
        *,
        requirement_spans: list[RequirementSpan],
        span_hints: dict[str, dict[str, str]] | None = None,
        repair: bool = False,
    ) -> tuple[JDCriteriaDraft, LLMResultProvenance]:
        return await self._boundary.around_inference(
            lambda: self._inner.draft_job_criteria(
                jd_text, requirement_spans=requirement_spans, span_hints=span_hints, repair=repair
            )
        )

    async def resolve_clarification_answer(
        self,
        *,
        clarification_type: str,
        allowed_answers: list[str],
        answer_text: str,
        repair: bool = False,
    ) -> tuple[ClarificationAnswerProposal, LLMResultProvenance]:
        return await self._boundary.around_inference(
            lambda: self._inner.resolve_clarification_answer(
                clarification_type=clarification_type,
                allowed_answers=allowed_answers,
                answer_text=answer_text,
                repair=repair,
            )
        )

    async def health(self) -> dict:
        return await self._inner.health()


class BoundaryEmbedding:
    """``EmbeddingProvider`` whose local query-embedding call also runs
    outside any transaction (it is Ollama HTTP too)."""

    def __init__(self, inner: EmbeddingProvider, boundary: TurnBoundary) -> None:
        self._inner = inner
        self._boundary = boundary
        self.provider_name = inner.provider_name
        self.model_name = inner.model_name
        self.model_revision = inner.model_revision

    async def embed(self, text: str) -> EmbeddingResult:
        return await self._boundary.around_inference(lambda: self._inner.embed(text))

    async def health(self) -> dict:
        return await self._inner.health()


# ---------------------------------------------------------------------------
# Abandoned clients
# ---------------------------------------------------------------------------


class DisconnectProbe(Protocol):
    async def is_disconnected(self) -> bool: ...


async def run_until_client_disconnects[T](
    request: DisconnectProbe,
    work: Awaitable[T],
    *,
    poll_interval_seconds: float = 0.5,
) -> T:
    """Run ``work`` while polling the ASGI client connection. When the
    client disconnects, the work is CANCELLED (queued admission is left,
    an in-flight local HTTP call is cancelled, admission slots are released
    by their context managers) and ``ClientDisconnectedError`` is raised. No
    detached task can outlive this call: both tasks are cancelled and
    awaited in ``finally``, including when the caller itself is cancelled."""
    work_task = asyncio.ensure_future(work)

    async def _watch() -> None:
        while True:
            await asyncio.sleep(poll_interval_seconds)
            if await request.is_disconnected():
                return

    watch_task = asyncio.ensure_future(_watch())
    try:
        done, _pending = await asyncio.wait(
            {work_task, watch_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if work_task in done:
            return work_task.result()
        work_task.cancel()
        await asyncio.gather(work_task, return_exceptions=True)
        raise ClientDisconnectedError()
    finally:
        for task in (work_task, watch_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(work_task, watch_task, return_exceptions=True)

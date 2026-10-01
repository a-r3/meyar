"""Phase-B persistence for agent tasks and resumable clarifications
(issue #88 slice A, docs/AGENT_CORE_V2_DESIGN.md §4.4/§7/§12.2/§19,
D-092 + A1/A2).

Every query is scoped by ``tenant_id`` AND the BrowserSession context id at
the data-access layer. Rows are locked FOR UPDATE only after the
conversation and session-context rows (lock order: principal →
conversation → session context → clarification → task → submission), and
only inside a short Phase-B transaction — never across inference."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.canonical_requirements import JD_SEMANTIC_POLICY_VERSION
from meyar.agent.clarification_schemas import (
    ANSWER_SCHEMA_VERSION,
    TASK_POLICY_VERSION,
    TASK_STATE_SCHEMA_VERSION,
    TERMINAL_TASK_STATUSES,
    ClarificationStatus,
    TaskPhase,
    TaskStatus,
    TaskType,
)
from meyar.agent.dialogue import (
    ClarificationSnapshot,
    DialogueCommit,
    LaneBChange,
    liveness_failure,
)
from meyar.agent.intent_routing import ENTRY_ROUTING_POLICY_VERSION
from meyar.agent.turn_boundary import TurnAuthorityLostError, TurnStaleReason
from meyar.models.agent_conversation import (
    AgentConversation,
    AgentConversationSessionContext,
)
from meyar.models.agent_task import AgentClarification, AgentTask
from meyar.models.browser_session import BrowserSession
from meyar.services.audit_repo import record_event

TERMINAL_TASKS_KEPT_PER_CONTEXT = 10
TASK_RETENTION_BATCH = 20


def snapshot_of(row: AgentClarification, *, task_status: str) -> ClarificationSnapshot:
    return ClarificationSnapshot(
        id=row.id,
        task_id=row.task_id,
        session_context_id=row.session_context_id,
        context_epoch=row.context_epoch,
        clarification_type=row.clarification_type,
        answer_schema_version=row.answer_schema_version,
        question_turn_id=row.question_turn_id,
        created_from_turn_id=row.created_from_turn_id,
        source_turn_id=row.source_turn_id,
        source_sha256=row.source_sha256,
        source_start=row.source_start,
        source_end=row.source_end,
        semantic_policy_version=row.semantic_policy_version,
        routing_policy_version=row.routing_policy_version,
        status=row.status,
        attempt=row.attempt,
        expires_at=row.expires_at,
        superseded_by_id=row.superseded_by_id,
        superseded_reason=row.superseded_reason,
        task_status=task_status,
    )


@dataclass(frozen=True)
class LiveClarificationRead:
    current: ClarificationSnapshot
    # A2: at most one predecessor; ``None`` when there is none. More than one
    # row claiming this clarification as successor is a forged chain.
    predecessor: ClarificationSnapshot | None
    predecessor_ambiguous: bool = False


async def _clarification(
    db: AsyncSession, *, tenant_id: uuid.UUID, context_id: uuid.UUID,
    clarification_id: uuid.UUID, for_update: bool = False,
) -> AgentClarification | None:
    stmt = select(AgentClarification).where(
        AgentClarification.id == clarification_id,
        AgentClarification.tenant_id == tenant_id,
        AgentClarification.session_context_id == context_id,
    ).execution_options(populate_existing=True)
    if for_update:
        stmt = stmt.with_for_update()
    return await db.scalar(stmt)


async def _task(
    db: AsyncSession, *, tenant_id: uuid.UUID, context_id: uuid.UUID, task_id: uuid.UUID,
    for_update: bool = False,
) -> AgentTask | None:
    stmt = select(AgentTask).where(
        AgentTask.id == task_id,
        AgentTask.tenant_id == tenant_id,
        AgentTask.session_context_id == context_id,
    ).execution_options(populate_existing=True)
    if for_update:
        stmt = stmt.with_for_update()
    return await db.scalar(stmt)


async def _predecessors(
    db: AsyncSession, *, tenant_id: uuid.UUID, context_id: uuid.UUID,
    clarification_id: uuid.UUID,
) -> list[AgentClarification]:
    return list(
        (
            await db.scalars(
                select(AgentClarification)
                .where(
                    AgentClarification.superseded_by_id == clarification_id,
                    AgentClarification.tenant_id == tenant_id,
                    AgentClarification.session_context_id == context_id,
                )
                .limit(2)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def read_live_clarification(
    db: AsyncSession, *, tenant_id: uuid.UUID, context_id: uuid.UUID,
    clarification_id: uuid.UUID,
) -> LiveClarificationRead | None:
    """Bounded read (one row + its task + ≤2 predecessor rows) of the
    clarification the live pointer names. ``None`` for any id outside this
    tenant + BrowserSession context — foreign and missing are identical."""
    row = await _clarification(
        db, tenant_id=tenant_id, context_id=context_id, clarification_id=clarification_id
    )
    if row is None:
        return None
    task = await _task(db, tenant_id=tenant_id, context_id=context_id, task_id=row.task_id)
    if task is None:
        return None
    predecessors = await _predecessors(
        db, tenant_id=tenant_id, context_id=context_id, clarification_id=row.id
    )
    predecessor = None
    if len(predecessors) == 1:
        predecessor_task = await _task(
            db, tenant_id=tenant_id, context_id=context_id, task_id=predecessors[0].task_id
        )
        predecessor = snapshot_of(
            predecessors[0],
            task_status=predecessor_task.status if predecessor_task else "",
        )
    return LiveClarificationRead(
        current=snapshot_of(row, task_status=task.status),
        predecessor=predecessor,
        predecessor_ambiguous=len(predecessors) > 1,
    )


@dataclass
class LockedDialogue:
    clarification: AgentClarification | None = None
    task: AgentTask | None = None
    lane_b_tasks: list[AgentTask] = field(default_factory=list)
    # An observed-stale pointer whose row is already terminal/unreadable:
    # nothing to transition, only the dangling pointer is cleared (T8).
    pointer_only: bool = False


def _lost() -> TurnAuthorityLostError:
    return TurnAuthorityLostError(TurnStaleReason.CONTEXT_CHANGED)


async def lock_dialogue_rows(
    db: AsyncSession,
    *,
    conversation: AgentConversation,
    session_context: AgentConversationSessionContext,
    dialogue: DialogueCommit,
    now: datetime | None = None,
) -> LockedDialogue:
    """Phase B (§12.2 steps 1–2): lock the referenced clarification, its task
    and the lane-B waiting task FOR UPDATE — after the conversation/context
    locks, before the submission lock — and re-verify every staged
    transition's precondition. Any mismatch fails the whole turn closed."""
    now = now or datetime.now(UTC)
    tenant_id, context_id = session_context.tenant_id, session_context.id
    locked = LockedDialogue()
    transition = dialogue.transition
    if transition is not None:
        if session_context.active_clarification_id != transition.clarification_id:
            raise _lost()
        row = await _clarification(
            db, tenant_id=tenant_id, context_id=context_id,
            clarification_id=transition.clarification_id, for_update=True,
        )
        task = (
            await _task(
                db, tenant_id=tenant_id, context_id=context_id, task_id=row.task_id,
                for_update=True,
            )
            if row is not None else None
        )
        consistent = (
            row is not None
            and row.status == ClarificationStatus.OPEN.value
            and task is not None
            and task.id == transition.task_id
            and task.status == TaskStatus.WAITING_CLARIFICATION.value
        )
        if not consistent:
            if transition.requires_live or transition.status != ClarificationStatus.EXPIRED:
                raise _lost()
            locked.pointer_only = True
            return await _lock_lane_b(db, locked, dialogue, tenant_id, context_id)
        assert row is not None and task is not None
        if transition.requires_live:
            predecessors = await _predecessors(
                db, tenant_id=tenant_id, context_id=context_id, clarification_id=row.id
            )
            predecessor = None
            if len(predecessors) == 1:
                predecessor = snapshot_of(predecessors[0], task_status=task.status)
            if len(predecessors) > 1 or liveness_failure(
                conversation.turns,
                snapshot_of(row, task_status=task.status),
                predecessor,
                now=now,
                context_id=context_id,
                context_epoch=session_context.context_epoch,
            ) is not None:
                raise _lost()
        locked.clarification, locked.task = row, task
    elif dialogue.new_clarification is not None and session_context.active_clarification_id:
        # A turn that saw no live clarification may not open one over a
        # pointer that appeared meanwhile (also guarded by revalidation).
        raise _lost()
    return await _lock_lane_b(db, locked, dialogue, tenant_id, context_id)


async def _lock_lane_b(
    db: AsyncSession, locked: LockedDialogue, dialogue: DialogueCommit,
    tenant_id: uuid.UUID, context_id: uuid.UUID,
) -> LockedDialogue:
    if dialogue.lane_b != LaneBChange.NONE:
        locked.lane_b_tasks = list(
            (
                await db.scalars(
                    select(AgentTask)
                    .where(
                        AgentTask.tenant_id == tenant_id,
                        AgentTask.session_context_id == context_id,
                        AgentTask.status == TaskStatus.WAITING_CONFIRMATION.value,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
    return locked


async def _session_expiry(
    db: AsyncSession, session_context: AgentConversationSessionContext
) -> datetime:
    expires_at = await db.scalar(
        select(BrowserSession.expires_at).where(
            BrowserSession.id == session_context.browser_session_id
        )
    )
    if expires_at is None:
        raise _lost()
    return expires_at


async def _audit(db: AsyncSession, tenant_id: uuid.UUID, event_type: str, **metadata: object):
    await record_event(db, tenant_id=tenant_id, event_type=event_type, metadata=metadata)


async def _set_task_status(
    db: AsyncSession, task: AgentTask, *, status: TaskStatus, now: datetime,
    reason_code: str, task_type: TaskType | None = None,
    pending_draft_id: uuid.UUID | None = None, expires_at: datetime | None = None,
) -> None:
    previous = task.status
    task.status = status.value
    if task_type is not None:
        task.task_type = task_type.value
    if status == TaskStatus.WAITING_CONFIRMATION:
        task.phase = TaskPhase.DRAFT_REVIEW.value
        task.pending_draft_id = pending_draft_id
        if expires_at is not None:
            task.expires_at = expires_at
    else:
        task.pending_draft_id = None
    if status in TERMINAL_TASK_STATUSES:
        task.phase = TaskPhase.DONE.value
        task.terminal_at = now
    task.updated_at = now
    await db.flush()
    await _audit(
        db, task.tenant_id, "agent.task.state_changed",
        from_status=previous, to_status=status.value, reason_code=reason_code,
    )


async def _retire_terminal_tasks(
    db: AsyncSession, *, tenant_id: uuid.UUID, context_id: uuid.UUID
) -> None:
    """§19.1: on task creation keep the newest 10 terminal tasks per context
    (bounded batch ≤20). Clarifications cascade; history and audit stay."""
    stale_ids = (
        select(AgentTask.id)
        .where(
            AgentTask.tenant_id == tenant_id,
            AgentTask.session_context_id == context_id,
            AgentTask.terminal_at.is_not(None),
        )
        .order_by(AgentTask.terminal_at.desc(), AgentTask.id.desc())
        .offset(TERMINAL_TASKS_KEPT_PER_CONTEXT)
        .limit(TASK_RETENTION_BATCH)
    )
    await db.execute(delete(AgentTask).where(AgentTask.id.in_(stale_ids)))


async def _create_task(
    db: AsyncSession, *, conversation: AgentConversation,
    session_context: AgentConversationSessionContext, submission_id: uuid.UUID,
    task_type: TaskType, status: TaskStatus, phase: TaskPhase, expires_at: datetime,
    pending_draft_id: uuid.UUID | None = None, now: datetime,
) -> AgentTask:
    task = AgentTask(
        id=uuid.uuid4(),
        tenant_id=session_context.tenant_id,
        conversation_id=conversation.id,
        session_context_id=session_context.id,
        owner_user_id=conversation.owner_user_id,
        owner_membership_id=conversation.owner_membership_id,
        task_type=task_type.value,
        status=status.value,
        phase=phase.value,
        state_schema_version=TASK_STATE_SCHEMA_VERSION,
        state={"unresolved_slots": [], "assumptions": []},
        pending_draft_id=pending_draft_id,
        policy_version=TASK_POLICY_VERSION,
        created_by_submission_id=submission_id,
        created_at=now,
        updated_at=now,
        expires_at=expires_at,
    )
    db.add(task)
    await db.flush()
    await _audit(
        db, task.tenant_id, "agent.task.created",
        task_type=task.task_type, status=task.status, policy_version=TASK_POLICY_VERSION,
    )
    await _retire_terminal_tasks(db, tenant_id=task.tenant_id, context_id=session_context.id)
    return task


async def apply_dialogue_commit(
    db: AsyncSession,
    *,
    conversation: AgentConversation,
    session_context: AgentConversationSessionContext,
    locked: LockedDialogue,
    dialogue: DialogueCommit,
    submission_id: uuid.UUID,
    clarification_ttl_seconds: int,
    now: datetime | None = None,
) -> None:
    """Phase B (§12.2 step 3): write the staged lane-A/lane-B changes onto
    rows locked by ``lock_dialogue_rows``. Order respects every partial
    unique index: the old row leaves its waiting status before a new one
    enters it. ``session_context.active_pending_draft_id`` must already hold
    this turn's committed lane-B pointer."""
    now = now or datetime.now(UTC)
    tenant_id = session_context.tenant_id
    session_expires_at = await _session_expiry(db, session_context)
    draft_pointer = session_context.active_pending_draft_id
    transition = dialogue.transition
    lane_b_moved_by_resolution = (
        transition is not None and transition.task_status == TaskStatus.WAITING_CONFIRMATION
    )

    if dialogue.lane_b == LaneBChange.NEW_DRAFT:
        for task in locked.lane_b_tasks:
            # T11: only a replacing pending draft ends the old lane-B task.
            await _set_task_status(
                db, task, status=TaskStatus.CANCELLED, now=now, reason_code="DRAFT_REPLACED"
            )

    if transition is not None and not locked.pointer_only:
        row, live_task = locked.clarification, locked.task
        assert row is not None and live_task is not None
        row.status = transition.status.value
        if transition.status == ClarificationStatus.RESOLVED:
            assert transition.resolved_value is not None
            assert transition.resolution_source is not None
            row.resolved_value = transition.resolved_value.value
            row.resolution_source = transition.resolution_source.value
            row.resolved_by_submission_id = submission_id
            row.resolved_at = now
        elif transition.status == ClarificationStatus.SUPERSEDED:
            assert transition.superseded_reason is not None
            row.superseded_reason = transition.superseded_reason.value
        await db.flush()
        if transition.status == ClarificationStatus.RESOLVED:
            await _audit(
                db, tenant_id, "agent.clarification.resolved",
                clarification_type=row.clarification_type,
                resolved_value=row.resolved_value,
                resolution_source=row.resolution_source,
            )
        elif transition.status == ClarificationStatus.SUPERSEDED:
            await _audit(
                db, tenant_id, "agent.clarification.superseded",
                reason_code=row.superseded_reason,
            )
        else:
            assert transition.expiry_reason is not None
            await _audit(
                db, tenant_id, "agent.clarification.expired",
                reason_code=transition.expiry_reason.value,
            )
        if (
            transition.task_status is not None
            and transition.task_status.value != live_task.status
        ):
            reason_code = (
                transition.superseded_reason.value
                if transition.superseded_reason is not None
                else transition.expiry_reason.value
                if transition.expiry_reason is not None
                else "CLARIFICATION_RESOLVED"
            )
            await _set_task_status(
                db, live_task, status=transition.task_status, now=now, reason_code=reason_code,
                task_type=transition.task_type,
                pending_draft_id=draft_pointer if lane_b_moved_by_resolution else None,
                expires_at=session_expires_at,
            )

    if dialogue.lane_b == LaneBChange.NEW_DRAFT and not lane_b_moved_by_resolution:
        assert draft_pointer is not None
        await _create_task(
            db, conversation=conversation, session_context=session_context,
            submission_id=submission_id, task_type=TaskType.VACANCY_ANALYSIS,
            status=TaskStatus.WAITING_CONFIRMATION, phase=TaskPhase.DRAFT_REVIEW,
            expires_at=session_expires_at, pending_draft_id=draft_pointer, now=now,
        )
    elif dialogue.lane_b == LaneBChange.AMENDED:
        for task in locked.lane_b_tasks:
            # T9: same lane-B task, the deterministic amendment's new draft id.
            task.pending_draft_id = draft_pointer
            task.updated_at = now
            await db.flush()
            await _audit(
                db, tenant_id, "agent.task.state_changed",
                from_status=task.status, to_status=task.status, reason_code="DRAFT_MODIFIED",
            )

    new = dialogue.new_clarification
    if new is not None:
        expires_at = min(now + timedelta(seconds=clarification_ttl_seconds), session_expires_at)
        if new.reuse_task_id is not None:
            assert locked.task is not None and locked.task.id == new.reuse_task_id
            task_id = new.reuse_task_id
            # T5: the same waiting task now waits on the retry's own TTL.
            locked.task.expires_at = expires_at
            locked.task.updated_at = now
        else:
            task_id = (
                await _create_task(
                    db, conversation=conversation, session_context=session_context,
                    submission_id=submission_id, task_type=new.task_type,
                    status=TaskStatus.WAITING_CLARIFICATION, phase=new.task_phase,
                    expires_at=expires_at, now=now,
                )
            ).id
        db.add(
            AgentClarification(
                id=new.id,
                tenant_id=tenant_id,
                conversation_id=conversation.id,
                session_context_id=session_context.id,
                task_id=task_id,
                context_epoch=session_context.context_epoch,
                clarification_type=new.clarification_type.value,
                answer_schema_version=ANSWER_SCHEMA_VERSION,
                question_turn_id=new.question_turn_id,
                created_from_turn_id=new.created_from_turn_id,
                source_turn_id=new.source_turn_id,
                source_sha256=new.source_sha256,
                source_start=new.source_start,
                source_end=new.source_end,
                semantic_policy_version=JD_SEMANTIC_POLICY_VERSION,
                routing_policy_version=ENTRY_ROUTING_POLICY_VERSION,
                # Re-stamped after the D-045 display sync (A1 provenance).
                created_turn_version=conversation.turn_version,
                status=ClarificationStatus.OPEN.value,
                attempt=new.attempt,
                expires_at=expires_at,
                created_by_submission_id=submission_id,
                created_at=now,
            )
        )
        await db.flush()
        if new.predecessor_id is not None:
            assert locked.clarification is not None
            assert locked.clarification.id == new.predecessor_id
            locked.clarification.superseded_by_id = new.id
            await db.flush()
        await _audit(
            db, tenant_id, "agent.clarification.created",
            clarification_type=new.clarification_type.value, attempt=new.attempt,
            answer_schema_version=ANSWER_SCHEMA_VERSION,
        )
        session_context.active_clarification_id = new.id
    elif transition is not None:
        session_context.active_clarification_id = None
    await db.flush()


async def stamp_created_turn_version(
    db: AsyncSession, *, session_context: AgentConversationSessionContext,
    conversation: AgentConversation, submission_id: uuid.UUID,
) -> None:
    """A1: ``created_turn_version`` = the conversation's FINAL committed
    ``turn_version`` of the creating Phase B, i.e. after the D-045 sync.
    Provenance only; never read for liveness."""
    row = await db.scalar(
        select(AgentClarification).where(
            AgentClarification.tenant_id == session_context.tenant_id,
            AgentClarification.session_context_id == session_context.id,
            AgentClarification.created_by_submission_id == submission_id,
        )
    )
    if row is not None:
        row.created_turn_version = conversation.turn_version
        await db.flush()


async def complete_confirmed_draft_task(
    db: AsyncSession, *, session_context: AgentConversationSessionContext,
    draft_id: uuid.UUID,
) -> None:
    """T10: the separate HR confirm route completes the lane-B task waiting
    on exactly this draft (if one exists — legacy drafts have none). Never
    touches lane A. Called with the conversation/context rows locked."""
    task = await db.scalar(
        select(AgentTask)
        .where(
            AgentTask.tenant_id == session_context.tenant_id,
            AgentTask.session_context_id == session_context.id,
            AgentTask.status == TaskStatus.WAITING_CONFIRMATION.value,
            AgentTask.pending_draft_id == draft_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is not None:
        await _set_task_status(
            db, task, status=TaskStatus.COMPLETED, now=datetime.now(UTC),
            reason_code="DRAFT_CONFIRMED",
        )

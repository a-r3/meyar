"""Durable conversation + BrowserSession-bound live context authority
(issue #80, docs/DECISIONS.md D-086).

``AgentConversation`` is durable history owned by (tenant, user,
membership). ``AgentConversationSessionContext`` is the ONLY live authority
for the active ResultSet and pending JD-draft mutation authority, and is
bound to exactly one (conversation, BrowserSession) pair. A caller must
always pass the live UIContext principal's own tenant/user/membership ids —
never a stored or client-supplied owner — and every lookup here filters on
all of them at the data-access layer. A foreign/missing conversation id is
indistinguishable from a nonexistent one (both return ``None``)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.schemas import (
    AgentActionType,
    AgentJobDraftToolResult,
    AgentTurnOutcome,
    AgentTurnResult,
    ConfirmedAgentJobDraft,
    pending_draft_payload,
)
from meyar.models.agent_conversation import (
    AgentConversation,
    AgentConversationSessionContext,
    AgentConversationTitleKind,
)
from meyar.models.tenant_membership import TenantMembership
from meyar.services.audit_repo import ACTOR_HUMAN_USER, record_event

ASSISTANT_TEXT_AUTHORITY_SERVER = "SERVER_VALIDATED"
ASSISTANT_TEXT_AUTHORITY_VERSION = "candidate-factuality-v2"

# issue #80: durable transcript storage bound, deliberately SEPARATE from
# Settings.agent_max_context_turns (the LLM context window). Raising this
# never increases model input — the model only ever receives the last
# agent_max_context_turns entries (see meyar.agent.service.run_agent_turn).
# Oldest entries are dropped once exceeded; there is no unbounded memory.
MAX_PERSISTED_AGENT_TURNS = 100

# History listing bound (sidebar/history in PR80-2). Never an unbounded
# "select every conversation for this user".
DEFAULT_HISTORY_PAGE_SIZE = 20
MAX_HISTORY_PAGE_SIZE = 50


@dataclass(frozen=True)
class OwnerPrincipal:
    """The live, re-derived UIContext principal a conversation lookup is
    authorized against. Built only from meyar.ui.auth.UIContext."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    membership_id: uuid.UUID


@dataclass(frozen=True)
class ConversationPage:
    items: list["ConversationSummary"]
    page: int
    page_size: int
    has_next: bool


@dataclass(frozen=True)
class ConversationSummary:
    """Sidebar projection: never loads transcript or live session authority."""

    id: uuid.UUID
    title_kind: str
    updated_at: datetime


def _owned(owner: OwnerPrincipal):  # noqa: ANN202 - SQLAlchemy boolean clause list
    return (
        AgentConversation.tenant_id == owner.tenant_id,
        AgentConversation.owner_user_id == owner.user_id,
        AgentConversation.owner_membership_id == owner.membership_id,
    )


async def create_conversation(db: AsyncSession, *, owner: OwnerPrincipal) -> AgentConversation:
    """A brand-new, empty durable conversation (title_kind NEW). Never
    copies any transcript, ResultSet, ordinal, pending draft, or scoring
    state from another conversation."""
    conversation = AgentConversation(
        tenant_id=owner.tenant_id,
        owner_user_id=owner.user_id,
        owner_membership_id=owner.membership_id,
        title_kind=AgentConversationTitleKind.NEW.value,
        turns=[],
    )
    db.add(conversation)
    await db.flush()
    await record_event(
        db,
        tenant_id=owner.tenant_id,
        event_type="agent.conversation.created",
        metadata={
            "conversation_id": str(conversation.id),
            "title_kind": conversation.title_kind,
        },
        actor_type=ACTOR_HUMAN_USER,
        actor_id=owner.user_id,
    )
    return conversation


async def get_owned_conversation(
    db: AsyncSession, *, owner: OwnerPrincipal, conversation_id: uuid.UUID
) -> AgentConversation | None:
    return await db.scalar(
        select(AgentConversation)
        .where(AgentConversation.id == conversation_id, *_owned(owner))
        .execution_options(populate_existing=True)
    )


async def get_owned_conversation_for_update(
    db: AsyncSession, *, owner: OwnerPrincipal, conversation_id: uuid.UUID
) -> AgentConversation | None:
    """Holds the DURABLE conversation row's PostgreSQL ``SELECT ... FOR
    UPDATE`` lock until the caller's commit/rollback — the whole
    state-changing turn is serialized per conversation (two tabs or two
    BrowserSessions on the same conversation can never lose a transcript
    update), across worker processes, while different conversations stay
    independently concurrent. ``populate_existing`` guarantees the caller
    sees the committed post-lock state, not a stale identity-map copy."""
    return await db.scalar(
        select(AgentConversation)
        .where(AgentConversation.id == conversation_id, *_owned(owner))
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def get_most_recent_owned_conversation(
    db: AsyncSession, *, owner: OwnerPrincipal
) -> AgentConversation | None:
    """Legacy single-workspace ``/ui/agent`` default (PR80-1 compatibility
    only): the owner's most recently updated conversation."""
    return await db.scalar(
        select(AgentConversation)
        .where(*_owned(owner))
        .order_by(AgentConversation.updated_at.desc(), AgentConversation.id.desc())
        .limit(1)
    )


async def get_current_conversation_for_session(
    db: AsyncSession, *, owner: OwnerPrincipal, browser_session_id: uuid.UUID
) -> AgentConversation | None:
    """Legacy single-workspace ``/ui/agent`` default (PR80-1 compatibility):
    the owned conversation this BrowserSession most recently used (its own
    session context), falling back to the owner's most recent conversation
    — so a relogin shows durable history, while "Yeni söhbət" in one session
    never switches another session's current conversation. Ownership is
    still enforced on the conversation row itself."""
    current = await db.scalar(
        select(AgentConversation)
        .join(
            AgentConversationSessionContext,
            AgentConversationSessionContext.conversation_id == AgentConversation.id,
        )
        .where(
            *_owned(owner),
            AgentConversationSessionContext.tenant_id == owner.tenant_id,
            AgentConversationSessionContext.browser_session_id == browser_session_id,
        )
        .order_by(
            AgentConversationSessionContext.updated_at.desc(),
            AgentConversation.updated_at.desc(),
            AgentConversation.id.desc(),
        )
        .limit(1)
    )
    if current is not None:
        return current
    return await get_most_recent_owned_conversation(db, owner=owner)


async def resolve_or_create_current_conversation(
    db: AsyncSession, *, owner: OwnerPrincipal, browser_session_id: uuid.UUID
) -> AgentConversation | None:
    """Race-safe legacy default resolution for a request that names no
    conversation. Briefly locks the owner's OWN membership row (row-level,
    per owner — never global) so two concurrent first requests cannot each
    create a separate "current" conversation; the new conversation also
    gets this BrowserSession's clean context, making it the session's
    current one. The caller commits right away (releasing the membership
    lock) BEFORE starting any long-running turn, which then locks only the
    durable conversation row. Returns ``None`` when the live membership no
    longer matches the principal."""
    membership = await db.scalar(
        select(TenantMembership)
        .where(
            TenantMembership.id == owner.membership_id,
            TenantMembership.user_id == owner.user_id,
            TenantMembership.tenant_id == owner.tenant_id,
        )
        .with_for_update()
    )
    if membership is None:
        return None
    current = await get_current_conversation_for_session(
        db, owner=owner, browser_session_id=browser_session_id
    )
    if current is not None:
        return current
    conversation = await create_conversation(db, owner=owner)
    await get_or_create_session_context(
        db, conversation=conversation, browser_session_id=browser_session_id
    )
    return conversation


async def list_owned_conversations(
    db: AsyncSession,
    *,
    owner: OwnerPrincipal,
    page: int = 1,
    page_size: int = DEFAULT_HISTORY_PAGE_SIZE,
) -> ConversationPage:
    """Bounded, deterministic history page: ``updated_at DESC, id DESC``.
    ``page >= 1`` and ``1 <= page_size <= MAX_HISTORY_PAGE_SIZE`` are
    enforced here (ValueError), not trusted from a caller."""
    if page < 1:
        raise ValueError("page must be >= 1")
    if page_size < 1 or page_size > MAX_HISTORY_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_HISTORY_PAGE_SIZE}")
    rows = await db.execute(
        select(AgentConversation.id, AgentConversation.title_kind, AgentConversation.updated_at)
        .where(*_owned(owner))
        .order_by(AgentConversation.updated_at.desc(), AgentConversation.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size + 1)
    )
    items = [ConversationSummary(*row) for row in rows.all()]
    return ConversationPage(
        items=items[:page_size],
        page=page,
        page_size=page_size,
        has_next=len(items) > page_size,
    )


async def record_conversation_access_rejected(
    db: AsyncSession, *, owner: OwnerPrincipal, reason_code: str = "NOT_FOUND"
) -> None:
    """Audit a rejected explicit conversation selector. Never records the
    presented id's ownership, title, timestamp, or any transcript text —
    foreign and nonexistent ids are audited identically."""
    await record_event(
        db,
        tenant_id=owner.tenant_id,
        event_type="agent.conversation.access_rejected",
        metadata={"reason_code": reason_code},
        actor_type=ACTOR_HUMAN_USER,
        actor_id=owner.user_id,
    )


# ---------------------------------------------------------------------------
# BrowserSession-bound live session context
# ---------------------------------------------------------------------------


def _context_scope(
    *, tenant_id: uuid.UUID, conversation_id: uuid.UUID, browser_session_id: uuid.UUID
):  # noqa: ANN202 - SQLAlchemy boolean clause list
    return (
        AgentConversationSessionContext.tenant_id == tenant_id,
        AgentConversationSessionContext.conversation_id == conversation_id,
        AgentConversationSessionContext.browser_session_id == browser_session_id,
    )


async def get_session_context(
    db: AsyncSession,
    *,
    conversation: AgentConversation,
    browser_session_id: uuid.UUID,
) -> AgentConversationSessionContext | None:
    """``conversation`` must already be an owner-validated row (from
    get_owned_conversation*); scoping still repeats tenant + conversation +
    session at the data-access layer."""
    return await db.scalar(
        select(AgentConversationSessionContext)
        .where(
            *_context_scope(
                tenant_id=conversation.tenant_id,
                conversation_id=conversation.id,
                browser_session_id=browser_session_id,
            )
        )
        .execution_options(populate_existing=True)
    )


async def get_session_context_for_update(
    db: AsyncSession,
    *,
    conversation: AgentConversation,
    browser_session_id: uuid.UUID,
) -> AgentConversationSessionContext | None:
    return await db.scalar(
        select(AgentConversationSessionContext)
        .where(
            *_context_scope(
                tenant_id=conversation.tenant_id,
                conversation_id=conversation.id,
                browser_session_id=browser_session_id,
            )
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


async def get_or_create_session_context(
    db: AsyncSession,
    *,
    conversation: AgentConversation,
    browser_session_id: uuid.UUID,
) -> AgentConversationSessionContext:
    """Returns this BrowserSession's live context for ``conversation``,
    creating a CLEAN one (no ResultSet, no pending draft) when absent — a
    new BrowserSession never inherits another session's live authority.

    A new context's ``context_epoch`` is one past the highest epoch any
    context of this conversation has used, so epochs are monotonic per
    durable conversation (defense in depth on top of the session and
    conversation ResultSet checks).

    Callers performing a state-changing turn hold the owning conversation's
    row lock first (get_owned_conversation_for_update), which already
    serializes creation for the same conversation. Creation is additionally
    race-safe on its own: a concurrent duplicate INSERT loses on
    ``uq_agent_conversation_session_context`` inside a SAVEPOINT and the
    loser re-reads the winner's row — never two rows, never a 500."""
    existing = await get_session_context_for_update(
        db, conversation=conversation, browser_session_id=browser_session_id
    )
    if existing is not None:
        return existing
    max_epoch = await db.scalar(
        select(func.max(AgentConversationSessionContext.context_epoch)).where(
            AgentConversationSessionContext.tenant_id == conversation.tenant_id,
            AgentConversationSessionContext.conversation_id == conversation.id,
        )
    )
    now = datetime.now(UTC)
    context = AgentConversationSessionContext(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        browser_session_id=browser_session_id,
        context_epoch=(max_epoch or 0) + 1,
        active_result_set_id=None,
        active_pending_draft_id=None,
        created_at=now,
        updated_at=now,
    )
    try:
        async with db.begin_nested():
            db.add(context)
            await db.flush()
    except IntegrityError:
        winner = await get_session_context_for_update(
            db, conversation=conversation, browser_session_id=browser_session_id
        )
        if winner is None:
            raise
        return winner
    return context


def touch_session_context(session_context: AgentConversationSessionContext) -> None:
    """Marks this context as the session's most recently used one (see
    get_current_conversation_for_session)."""
    session_context.updated_at = datetime.now(UTC)


async def find_session_context_by_pending_draft(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    browser_session_id: uuid.UUID,
    draft_id: uuid.UUID,
) -> AgentConversationSessionContext | None:
    """Locate the one live context (for THIS BrowserSession) whose pending
    authority currently points at ``draft_id``. Only a locator: callers must
    still re-validate conversation ownership and re-read the context under
    the conversation lock (see resolve_pending_draft_authority)."""
    return await db.scalar(
        select(AgentConversationSessionContext).where(
            AgentConversationSessionContext.tenant_id == tenant_id,
            AgentConversationSessionContext.browser_session_id == browser_session_id,
            AgentConversationSessionContext.active_pending_draft_id == draft_id,
        )
    )


@dataclass(frozen=True)
class PendingDraftAuthority:
    conversation: AgentConversation
    session_context: AgentConversationSessionContext
    draft: AgentJobDraftToolResult


async def resolve_pending_draft_authority(
    db: AsyncSession,
    *,
    owner: OwnerPrincipal,
    browser_session_id: uuid.UUID,
    draft_id: uuid.UUID,
) -> PendingDraftAuthority | None:
    """A pending JD draft is actionable ONLY when ALL of:

    1. the current live owner may access the conversation;
    2. the current BrowserSession resolves that conversation's context;
    3. ``session_context.active_pending_draft_id == draft_id``;
    4. the matching payload exists in the conversation's server-held
       transcript.

    Historical transcript alone is never authority: a relogin, another
    conversation's context, or a superseded (modified) draft id all return
    ``None``. Holds the conversation and context row locks on success."""
    locator = await find_session_context_by_pending_draft(
        db, tenant_id=owner.tenant_id, browser_session_id=browser_session_id, draft_id=draft_id
    )
    if locator is None:
        return None
    conversation = await get_owned_conversation_for_update(
        db, owner=owner, conversation_id=locator.conversation_id
    )
    if conversation is None:
        return None
    session_context = await get_session_context_for_update(
        db, conversation=conversation, browser_session_id=browser_session_id
    )
    if session_context is None or session_context.active_pending_draft_id != draft_id:
        return None
    draft = get_active_pending_job_draft(conversation, session_context)
    if draft is None:
        return None
    return PendingDraftAuthority(
        conversation=conversation, session_context=session_context, draft=draft
    )


# ---------------------------------------------------------------------------
# Closed title category
# ---------------------------------------------------------------------------

_TITLE_KIND_BY_TOOL = {
    AgentActionType.SEARCH_CANDIDATES: AgentConversationTitleKind.CANDIDATE_SEARCH,
    AgentActionType.DRAFT_JOB_CRITERIA: AgentConversationTitleKind.VACANCY_ANALYSIS,
    AgentActionType.REFINE_CANDIDATE_RESULTS: AgentConversationTitleKind.RESULT_REFINEMENT,
    AgentActionType.GET_CANDIDATE_PROFILE: AgentConversationTitleKind.GENERAL,
    AgentActionType.GET_CANDIDATE_EVIDENCE: AgentConversationTitleKind.GENERAL,
}


def title_kind_for_turn(result: AgentTurnResult) -> AgentConversationTitleKind | None:
    """Closed title category derived ONLY from the completed, server-
    validated turn result — the first executed tool's type, or GENERAL for
    a zero-tool closed-response answer. Clarifications/failures are not a
    meaningful action (``None``: stay NEW). Never inspects message, query,
    JD, or candidate text; never model-generated."""
    for tool_result in result.tool_results:
        kind = _TITLE_KIND_BY_TOOL.get(tool_result.tool_name)
        if kind is not None:
            return kind
    if result.outcome == AgentTurnOutcome.ANSWERED:
        return AgentConversationTitleKind.GENERAL
    return None


def apply_title_kind_transition(
    conversation: AgentConversation, result: AgentTurnResult
) -> None:
    """``NEW`` may transition exactly once; any other kind is final."""
    if conversation.title_kind != AgentConversationTitleKind.NEW.value:
        return
    kind = title_kind_for_turn(result)
    if kind is not None:
        conversation.title_kind = kind.value


# ---------------------------------------------------------------------------
# Transcript state
# ---------------------------------------------------------------------------


def bound_persisted_turns(turns: list[dict]) -> list[dict]:
    return turns[-MAX_PERSISTED_AGENT_TURNS:]


async def save_conversation_turns(
    db: AsyncSession, conversation: AgentConversation, *, turns: list[dict]
) -> None:
    conversation.turns = bound_persisted_turns(turns)
    await db.flush()


async def sync_last_turn_display_text(
    db: AsyncSession, conversation: AgentConversation, *, text: str | None
) -> None:
    """Overwrites the just-persisted assistant turn's stored ``text`` with
    the exact HR-facing headline that was actually rendered for the live
    turn (meyar.ui.service._agent_turn_headline) — PR #42 owner correction
    (issue #33, D-045), so a later history re-render is identical to what
    HR saw live. A no-op when there is no headline or the last turn is not
    the assistant turn just written."""
    if not text or not conversation.turns:
        return
    last = conversation.turns[-1]
    if last.get("role") != "assistant":
        return
    turns = [
        *conversation.turns[:-1],
        {
            **last,
            "text": text,
            "text_authority": ASSISTANT_TEXT_AUTHORITY_SERVER,
            "text_authority_version": ASSISTANT_TEXT_AUTHORITY_VERSION,
        },
    ]
    conversation.turns = turns
    await db.flush()


def _find_pending_payload(
    conversation: AgentConversation, *, draft_id: uuid.UUID
) -> AgentJobDraftToolResult | None:
    for turn in reversed(conversation.turns):
        payload = turn.get("pending_job_draft")
        if not isinstance(payload, dict) or payload.get("draft_id") != str(draft_id):
            continue
        try:
            draft = AgentJobDraftToolResult.model_validate(payload)
        except ValueError:
            continue
        if draft.draft_id == draft_id:
            return draft
    return None


def get_active_pending_job_draft(
    conversation: AgentConversation,
    session_context: AgentConversationSessionContext | None,
) -> AgentJobDraftToolResult | None:
    """The ONLY pending-draft lookup used for authority. Requires the live
    session context to belong to this conversation and to point at a draft
    id; the transcript only supplies the server-held payload for that exact
    id. Never "the latest pending draft found in history"."""
    if (
        session_context is None
        or session_context.conversation_id != conversation.id
        or session_context.tenant_id != conversation.tenant_id
        or session_context.active_pending_draft_id is None
    ):
        return None
    return _find_pending_payload(conversation, draft_id=session_context.active_pending_draft_id)


async def replace_pending_job_draft(
    db: AsyncSession,
    authority: PendingDraftAuthority,
    *,
    draft: AgentJobDraftToolResult,
) -> None:
    """Replace the one ACTIVE pending draft payload after a server-authorized
    review resolution (same draft id — review resolution never mints one)."""
    if draft.draft_id != authority.session_context.active_pending_draft_id:
        raise ValueError("Pending draft was not available for review.")
    changed = False
    turns: list[dict] = []
    for turn in authority.conversation.turns:
        current = dict(turn)
        payload = current.get("pending_job_draft")
        if isinstance(payload, dict) and payload.get("draft_id") == str(draft.draft_id):
            current["pending_job_draft"] = pending_draft_payload(draft)
            changed = True
        turns.append(current)
    if not changed:
        raise ValueError("Pending draft was not available for review.")
    await save_conversation_turns(db, authority.conversation, turns=turns)


async def mark_pending_job_draft_confirmed(
    db: AsyncSession,
    authority: PendingDraftAuthority,
    *,
    confirmation: ConfirmedAgentJobDraft,
) -> None:
    """Consume live pending authority (``active_pending_draft_id = NULL``)
    and keep optional confirmation UI state in the transcript. Replay and
    idempotency are owned exclusively by AgentDraftConfirmation — never by
    this transcript state."""
    if confirmation.draft_id != authority.session_context.active_pending_draft_id:
        raise ValueError("Pending draft was not available for confirmation.")
    changed = False
    turns: list[dict] = []
    for turn in authority.conversation.turns:
        current = dict(turn)
        payload = current.get("pending_job_draft")
        if isinstance(payload, dict) and payload.get("draft_id") == str(confirmation.draft_id):
            current.pop("pending_job_draft", None)
            current["confirmed_job_draft"] = confirmation.model_dump(mode="json")
            changed = True
        turns.append(current)
    if not changed:
        raise ValueError("Pending draft was not available for confirmation.")
    await save_conversation_turns(db, authority.conversation, turns=turns)
    authority.session_context.active_pending_draft_id = None
    await db.flush()

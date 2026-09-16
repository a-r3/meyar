import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.schemas import AgentJobDraftToolResult, ConfirmedAgentJobDraft
from meyar.models.agent_conversation import AgentConversation

ASSISTANT_TEXT_AUTHORITY_SERVER = "SERVER_VALIDATED"
ASSISTANT_TEXT_AUTHORITY_VERSION = "candidate-factuality-v2"


async def get_conversation_by_session(
    db: AsyncSession, *, tenant_id: uuid.UUID, browser_session_id: uuid.UUID
) -> AgentConversation | None:
    result = await db.execute(
        select(AgentConversation).where(
            AgentConversation.tenant_id == tenant_id,
            AgentConversation.browser_session_id == browser_session_id,
        )
    )
    return result.scalar_one_or_none()


async def get_conversation_for_update_by_session(
    db: AsyncSession, *, tenant_id: uuid.UUID, browser_session_id: uuid.UUID
) -> AgentConversation | None:
    result = await db.execute(
        select(AgentConversation)
        .where(
            AgentConversation.tenant_id == tenant_id,
            AgentConversation.browser_session_id == browser_session_id,
        )
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def get_or_create_conversation(
    db: AsyncSession, *, tenant_id: uuid.UUID, browser_session_id: uuid.UUID
) -> AgentConversation:
    """Tenant-scoped, 1:1 with BrowserSession — a session belonging to
    another tenant's membership never resolves here (defense in depth:
    the caller's UIContext.tenant_id is itself already re-derived live
    from the session's own membership, see meyar.ui.auth)."""
    conversation = await get_conversation_by_session(
        db, tenant_id=tenant_id, browser_session_id=browser_session_id
    )
    if conversation is not None:
        return conversation
    conversation = AgentConversation(
        tenant_id=tenant_id,
        browser_session_id=browser_session_id,
        turns=[],
        last_search_candidate_ids=[],
    )
    db.add(conversation)
    await db.flush()
    return conversation


async def save_conversation_state(
    db: AsyncSession,
    conversation: AgentConversation,
    *,
    turns: list[dict],
    last_search_candidate_ids: list[str],
) -> None:
    conversation.turns = turns
    conversation.last_search_candidate_ids = last_search_candidate_ids
    await db.flush()


async def sync_last_turn_display_text(
    db: AsyncSession, conversation: AgentConversation, *, text: str | None
) -> None:
    """Overwrites the just-persisted assistant turn's stored ``text`` with
    the exact HR-facing headline that was actually rendered for the live
    turn (meyar.ui.service._agent_turn_headline) — PR #42 owner correction
    (issue #33, D-045): before this, run_agent_turn's own _finish_turn persisted
    only ``result.message`` (often empty for a plain tool-result turn),
    while the live render used a richer, separately computed per-tool
    headline. A later history re-render
    (meyar.ui.presentation.agent_turn_outcome_message) then fell back to a
    generic per-outcome message instead of reproducing what HR actually
    saw live. Storing the live headline verbatim makes both renders
    identical by construction — no second, divergent text-derivation path.
    A no-op when there is no headline (never overwrites with an empty
    value) or the last turn is not the assistant turn just written."""
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


async def reset_conversation(db: AsyncSession, conversation: AgentConversation) -> None:
    """Slice 4 (issue #33): the "Yeni söhbət" capability — clears this
    session's own server-held conversation state (turns and the ordinal
    candidate_ref resolution table) without touching any other tenant/
    candidate/job row. Same tenant/session scoping as every other call
    site here; the caller resolves ``conversation`` via
    get_or_create_conversation first, so it is always already this
    request's own row."""
    await save_conversation_state(db, conversation, turns=[], last_search_candidate_ids=[])


def get_pending_job_draft(
    conversation: AgentConversation, *, draft_id: uuid.UUID
) -> AgentJobDraftToolResult | None:
    """Resolve confirmation authority only from this server-held session."""
    for turn in reversed(conversation.turns):
        payload = turn.get("pending_job_draft")
        if not isinstance(payload, dict):
            continue
        try:
            draft = AgentJobDraftToolResult.model_validate(payload)
        except ValueError:
            continue
        if draft.draft_id == draft_id:
            return draft
    return None


async def replace_pending_job_draft(
    db: AsyncSession,
    conversation: AgentConversation,
    *,
    draft: AgentJobDraftToolResult,
) -> None:
    """Replace one pending draft after a server-authorized review resolution."""
    changed = False
    turns: list[dict] = []
    for turn in conversation.turns:
        current = dict(turn)
        payload = current.get("pending_job_draft")
        if isinstance(payload, dict) and payload.get("draft_id") == str(draft.draft_id):
            current["pending_job_draft"] = draft.model_dump(mode="json")
            changed = True
        turns.append(current)
    if not changed:
        raise ValueError("Pending draft was not available for review.")
    await save_conversation_state(
        db,
        conversation,
        turns=turns,
        last_search_candidate_ids=conversation.last_search_candidate_ids,
    )


def get_confirmed_job_draft(
    conversation: AgentConversation, *, draft_id: uuid.UUID
) -> ConfirmedAgentJobDraft | None:
    """Read optional confirmation UI state from this session transcript.

    Confirmation identity/idempotency is owned by AgentDraftConfirmation; a
    caller must never treat ids found only here as authoritative.
    """
    for turn in reversed(conversation.turns):
        payload = turn.get("confirmed_job_draft")
        if not isinstance(payload, dict):
            continue
        try:
            confirmed = ConfirmedAgentJobDraft.model_validate(payload)
        except ValueError:
            continue
        if confirmed.draft_id == draft_id:
            return confirmed
    return None


async def mark_pending_job_draft_confirmed(
    db: AsyncSession,
    conversation: AgentConversation,
    *,
    confirmation: ConfirmedAgentJobDraft,
) -> None:
    """Consume pending authority while retaining optional confirmation UI state."""
    changed = False
    turns: list[dict] = []
    for turn in conversation.turns:
        current = dict(turn)
        payload = current.get("pending_job_draft")
        if (
            isinstance(payload, dict)
            and payload.get("draft_id") == str(confirmation.draft_id)
        ):
            current.pop("pending_job_draft", None)
            current["confirmed_job_draft"] = confirmation.model_dump(mode="json")
            changed = True
        turns.append(current)
    if not changed:
        raise ValueError("Pending draft was not available for confirmation.")
    await save_conversation_state(
        db,
        conversation,
        turns=turns,
        last_search_candidate_ids=conversation.last_search_candidate_ids,
    )

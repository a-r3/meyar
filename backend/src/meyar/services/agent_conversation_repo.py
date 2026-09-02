import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.agent_conversation import AgentConversation


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


async def reset_conversation(db: AsyncSession, conversation: AgentConversation) -> None:
    """Slice 4 (issue #33): the "Yeni söhbət" capability — clears this
    session's own server-held conversation state (turns and the ordinal
    candidate_ref resolution table) without touching any other tenant/
    candidate/job row. Same tenant/session scoping as every other call
    site here; the caller resolves ``conversation`` via
    get_or_create_conversation first, so it is always already this
    request's own row."""
    await save_conversation_state(db, conversation, turns=[], last_search_candidate_ids=[])

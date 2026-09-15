import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.agent_draft_confirmation import (
    AGENT_DRAFT_CONFIRMATION_STATUS_CONFIRMED,
    AgentDraftConfirmation,
)


async def get_draft_confirmation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    browser_session_id: uuid.UUID,
    draft_id: uuid.UUID,
) -> AgentDraftConfirmation | None:
    """Resolve only the confirmation owned by this tenant and browser session."""
    result = await db.execute(
        select(AgentDraftConfirmation).where(
            AgentDraftConfirmation.tenant_id == tenant_id,
            AgentDraftConfirmation.browser_session_id == browser_session_id,
            AgentDraftConfirmation.draft_id == draft_id,
            AgentDraftConfirmation.status == AGENT_DRAFT_CONFIRMATION_STATUS_CONFIRMED,
        )
    )
    return result.scalar_one_or_none()


async def create_draft_confirmation(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    browser_session_id: uuid.UUID,
    draft_id: uuid.UUID,
    job_id: uuid.UUID,
    criteria_version_id: uuid.UUID,
) -> AgentDraftConfirmation:
    confirmation = AgentDraftConfirmation(
        tenant_id=tenant_id,
        browser_session_id=browser_session_id,
        draft_id=draft_id,
        job_id=job_id,
        criteria_version_id=criteria_version_id,
        status=AGENT_DRAFT_CONFIRMATION_STATUS_CONFIRMED,
    )
    db.add(confirmation)
    await db.flush()
    return confirmation

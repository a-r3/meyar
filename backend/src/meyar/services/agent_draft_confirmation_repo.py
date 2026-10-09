import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.agent_draft_confirmation import (
    AGENT_DRAFT_CONFIRMATION_STATUS_CONFIRMED,
    AgentDraftConfirmation,
)
from meyar.models.browser_session import BrowserSession
from meyar.models.tenant_membership import TenantMembership


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
    # Preserve creation-time referential/tenant authority despite the nullable
    # retained link. SHARE also serializes session retirement/revocation.
    now = datetime.now(UTC)
    session_id = await db.scalar(
        select(BrowserSession.id)
        .join(TenantMembership, BrowserSession.tenant_membership_id == TenantMembership.id)
        .where(
            BrowserSession.id == browser_session_id,
            TenantMembership.tenant_id == tenant_id,
            TenantMembership.user_id == BrowserSession.user_id,
            BrowserSession.expires_at > now,
            BrowserSession.revoked_at.is_(None),
        )
        .with_for_update(read=True, of=BrowserSession)
    )
    if session_id is None:
        raise ValueError("Confirmation requires an owned live browser session.")
    confirmation = AgentDraftConfirmation(
        tenant_id=tenant_id,
        browser_session_id=browser_session_id,
        historical_browser_session_id=browser_session_id,
        draft_id=draft_id,
        job_id=job_id,
        criteria_version_id=criteria_version_id,
        status=AGENT_DRAFT_CONFIRMATION_STATUS_CONFIRMED,
    )
    db.add(confirmation)
    await db.flush()
    return confirmation

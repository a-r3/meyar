import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.browser_session import BrowserSession


def hash_session_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


async def create_browser_session(
    db: AsyncSession, *, user_id: uuid.UUID, tenant_membership_id: uuid.UUID, ttl_hours: int
) -> tuple[BrowserSession, str]:
    """Session fixation prevention: called only after a fresh successful
    login (see meyar.ui.router.login) — a new random token is issued and
    the caller is responsible for setting it on a fresh cookie, never
    reusing a client-presented value."""
    if ttl_hours <= 0:
        raise ValueError("Browser-session TTL must be positive.")
    raw_token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    session = BrowserSession(
        user_id=user_id,
        tenant_membership_id=tenant_membership_id,
        session_token_hash=hash_session_token(raw_token),
        csrf_secret=secrets.token_hex(32),
        created_at=now,
        expires_at=now + timedelta(hours=ttl_hours),
    )
    db.add(session)
    await db.flush()
    return session, raw_token


async def get_browser_session_by_token(
    db: AsyncSession, raw_token: str
) -> BrowserSession | None:
    token_hash = hash_session_token(raw_token)
    result = await db.execute(
        select(BrowserSession)
        .where(BrowserSession.session_token_hash == token_hash)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def revoke_browser_session(
    db: AsyncSession, session: BrowserSession, *, revoked_at: datetime | None = None
) -> None:
    if session.revoked_at is None:
        session.revoked_at = revoked_at or datetime.now(UTC)
        await db.flush()


async def revoke_browser_session_by_id(
    db: AsyncSession, *, session_id: uuid.UUID, revoked_at: datetime | None = None
) -> None:
    await db.execute(
        update(BrowserSession)
        .where(BrowserSession.id == session_id, BrowserSession.revoked_at.is_(None))
        .values(revoked_at=revoked_at or datetime.now(UTC))
    )


async def get_browser_session_by_id(
    db: AsyncSession, *, browser_session_id: uuid.UUID
) -> BrowserSession | None:
    """BrowserSession has no tenant_id column of its own (tenant is always
    re-derived live via its TenantMembership — see the model docstring),
    matching every other lookup in this module. Used by
    meyar.services.agent_result_set_repo to bind a new AgentResultSet's
    ``expires_at`` to its owning session's own expiry — the caller's own
    ``browser_session_id`` always comes from an already tenant-scoped
    AgentConversation row, never a client-supplied value."""
    result = await db.execute(select(BrowserSession).where(BrowserSession.id == browser_session_id))
    return result.scalar_one_or_none()

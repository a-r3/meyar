"""Live tenant suspension authority (issue #46 S1).

Only tenant IDs, never active-state decisions, cross a processing gap. An
outer commit re-reads and SHARE-locks every registered tenant. SQLAlchemy's
sync session events run in AsyncSession's greenlet, so these are real DB
queries using the committing transaction, not a second connection.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from meyar.models.browser_session import BrowserSession
from meyar.models.tenant import Tenant
from meyar.models.tenant_membership import TenantMembership
from meyar.models.user import User

_COMMIT_TENANTS = "meyar_tenant_commit_checks"


class TenantInactiveError(Exception):
    """Closed application refusal; contains no tenant or candidate data."""

    def __init__(self) -> None:
        super().__init__("Tenant application access is unavailable.")


async def tenant_is_active(db: AsyncSession, tenant_id: uuid.UUID, *, lock: bool = False) -> bool:
    stmt = select(Tenant.is_active).where(Tenant.id == tenant_id)
    if lock:
        stmt = stmt.with_for_update(read=True)
    # Never flush generated data merely to decide whether it is authorized.
    with db.no_autoflush:
        return await db.scalar(stmt) is True


async def require_active_tenant(
    db: AsyncSession, tenant_id: uuid.UUID, *, lock: bool = False
) -> None:
    if not await tenant_is_active(db, tenant_id, lock=lock):
        await db.rollback()
        raise TenantInactiveError()
    db.sync_session.info.setdefault(_COMMIT_TENANTS, set()).add(tenant_id)


@event.listens_for(Session, "before_commit")
def _revalidate_tenant_commit(session: Session) -> None:
    # Savepoint release is not a durable commit. Keep the outer obligation.
    if session.in_nested_transaction():
        return
    for tenant_id in sorted(session.info.get(_COMMIT_TENANTS, ())):
        with session.no_autoflush:
            active = session.scalar(
                select(Tenant.is_active).where(Tenant.id == tenant_id).with_for_update(read=True)
            )
        if active is not True:
            # Rollback is the caller's responsibility after a failed commit.
            # The obligation remains until rollback, so catching this error
            # cannot bypass the guard with a second commit.
            raise TenantInactiveError()


@event.listens_for(Session, "after_transaction_end")
def _clear_tenant_commit_checks(session: Session, transaction: SessionTransaction) -> None:
    if transaction.parent is None:
        session.info.pop(_COMMIT_TENANTS, None)


async def set_tenant_active(db: AsyncSession, *, tenant_id: uuid.UUID, is_active: bool) -> None:
    """Operator security boundary; caller commits. Suspension keeps API keys.

    Order: all affected Users (UUID order) -> Tenant -> Memberships ->
    BrowserSessions. NO KEY UPDATE is compatible with FK KEY SHARE locks
    taken by application inserts. Agent/login take User -> Tenant ->
    Membership -> BrowserSession, so neither side can invert the order.
    Only membership stamps rotate; unrelated pending choices remain valid.
    """
    user_ids = select(TenantMembership.user_id).where(TenantMembership.tenant_id == tenant_id)
    await db.execute(
        select(User.id)
        .where(User.id.in_(user_ids))
        .order_by(User.id)
        .with_for_update(key_share=True)
    )
    tenant = await db.scalar(
        select(Tenant)
        .where(Tenant.id == tenant_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if tenant is None:
        raise TenantInactiveError()
    tenant.is_active = is_active
    if not is_active:
        memberships = list(
            (
                await db.scalars(
                    select(TenantMembership)
                    .where(TenantMembership.tenant_id == tenant_id)
                    .order_by(TenantMembership.id)
                    .with_for_update(key_share=True)
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        for membership in memberships:
            membership.security_version = uuid.uuid4()
        await db.execute(
            update(BrowserSession)
            .where(
                BrowserSession.tenant_membership_id.in_([m.id for m in memberships]),
                BrowserSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )
    await db.flush()

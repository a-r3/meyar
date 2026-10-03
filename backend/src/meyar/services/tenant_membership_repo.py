import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.tenant import Tenant
from meyar.models.tenant_membership import TenantMembership
from meyar.models.user import User
from meyar.services.browser_session_repo import revoke_sessions_for_membership


async def create_membership(
    db: AsyncSession, *, user_id: uuid.UUID, tenant_id: uuid.UUID, role: str
) -> TenantMembership:
    membership = TenantMembership(user_id=user_id, tenant_id=tenant_id, role=role)
    db.add(membership)
    await db.flush()
    return membership


async def get_membership_by_id(
    db: AsyncSession, membership_id: uuid.UUID, *, for_update: bool = False
) -> TenantMembership | None:
    stmt = (
        select(TenantMembership)
        .where(TenantMembership.id == membership_id)
        .execution_options(populate_existing=True)
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_membership_for_user_and_tenant(
    db: AsyncSession, *, user_id: uuid.UUID, tenant_id: uuid.UUID
) -> TenantMembership | None:
    """Used at login/tenant-selection time to live-check `is_active`;
    `populate_existing=True` for the same reason as
    meyar.services.user_repo.get_user_by_username."""
    result = await db.execute(
        select(TenantMembership)
        .where(TenantMembership.user_id == user_id, TenantMembership.tenant_id == tenant_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def list_active_memberships_for_user(
    db: AsyncSession, *, user_id: uuid.UUID
) -> list[TenantMembership]:
    """Every currently-active membership for user_id, across all tenants —
    the server-side authority for login-time tenant selection/
    auto-selection. Never trusts a client-supplied tenant id."""
    result = await db.execute(
        select(TenantMembership).join(Tenant, Tenant.id == TenantMembership.tenant_id).where(
            TenantMembership.user_id == user_id, TenantMembership.is_active.is_(True),
            Tenant.is_active.is_(True),
        ).execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def set_membership_active(
    db: AsyncSession, *, membership_id: uuid.UUID, is_active: bool
) -> None:
    values: dict[str, bool | uuid.UUID] = {"is_active": is_active}
    if not is_active:
        values["security_version"] = uuid.uuid4()
        user_id = await db.scalar(
            select(TenantMembership.user_id).where(TenantMembership.id == membership_id)
        )
        if user_id is not None:
            # Match login's User -> Membership row-lock order.
            await db.execute(
                update(User).where(User.id == user_id).values(security_version=uuid.uuid4())
            )
    await db.execute(
        update(TenantMembership)
        .where(TenantMembership.id == membership_id)
        .values(**values)
    )
    if not is_active:
        # User stamp invalidates all pending claims; other memberships'
        # BrowserSessions remain live.
        await revoke_sessions_for_membership(db, membership_id=membership_id)

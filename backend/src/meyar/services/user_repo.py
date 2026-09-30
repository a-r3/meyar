import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.core.password import hash_password
from meyar.models.user import User
from meyar.services.browser_session_repo import revoke_sessions_for_user


async def create_user(db: AsyncSession, *, username: str, plaintext_password: str) -> User:
    """Caller is responsible for prompting the plaintext outside normal
    CLI arguments (see meyar.cli's `create-user`/`set-password` commands)
    — this function only ever receives it in-process and immediately
    discards it after hashing."""
    user = User(username=username, password_hash=hash_password(plaintext_password))
    db.add(user)
    await db.flush()
    return user


async def get_user_by_username(
    db: AsyncSession, username: str, *, for_update: bool = False
) -> User | None:
    """Not tenant-scoped: a User exists independently of any tenant —
    tenant authorization is decided separately via TenantMembership.
    `populate_existing=True` matters here: this is the live login-time
    check of `is_active`, so a row already in this session's identity map
    (e.g. re-queried after a bulk UPDATE) must never return stale data."""
    stmt = select(User).where(User.username == username).execution_options(populate_existing=True)
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_user_by_id(
    db: AsyncSession, user_id: uuid.UUID, *, for_update: bool = False
) -> User | None:
    stmt = select(User).where(User.id == user_id).execution_options(populate_existing=True)
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def set_password(db: AsyncSession, *, user_id: uuid.UUID, plaintext_password: str) -> None:
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            password_hash=hash_password(plaintext_password),
            security_version=uuid.uuid4(), updated_at=datetime.now(UTC),
        )
    )
    await revoke_sessions_for_user(db, user_id=user_id)


async def set_user_active(db: AsyncSession, *, user_id: uuid.UUID, is_active: bool) -> None:
    values = {"is_active": is_active, "updated_at": datetime.now(UTC)}
    if not is_active:
        values["security_version"] = uuid.uuid4()
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(**values)
    )
    if not is_active:
        await revoke_sessions_for_user(db, user_id=user_id)

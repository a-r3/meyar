import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.core.password import hash_password
from meyar.models.user import User


async def create_user(db: AsyncSession, *, username: str, plaintext_password: str) -> User:
    """Caller is responsible for prompting the plaintext outside normal
    CLI arguments (see meyar.cli's `create-user`/`set-password` commands)
    — this function only ever receives it in-process and immediately
    discards it after hashing."""
    user = User(username=username, password_hash=hash_password(plaintext_password))
    db.add(user)
    await db.flush()
    return user


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    """Not tenant-scoped: a User exists independently of any tenant —
    tenant authorization is decided separately via TenantMembership.
    `populate_existing=True` matters here: this is the live login-time
    check of `is_active`, so a row already in this session's identity map
    (e.g. re-queried after a bulk UPDATE) must never return stale data."""
    result = await db.execute(
        select(User).where(User.username == username).execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await db.execute(
        select(User).where(User.id == user_id).execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def set_password(db: AsyncSession, *, user_id: uuid.UUID, plaintext_password: str) -> None:
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(password_hash=hash_password(plaintext_password), updated_at=datetime.now(UTC))
    )


async def set_user_active(db: AsyncSession, *, user_id: uuid.UUID, is_active: bool) -> None:
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(is_active=is_active, updated_at=datetime.now(UTC))
    )

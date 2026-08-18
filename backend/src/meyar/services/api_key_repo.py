import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.core.security import generate_api_key, hash_api_key
from meyar.models.api_key import ApiKey

DEFAULT_SCOPES = [
    "jobs:read",
    "jobs:write",
    "candidates:read",
    "candidates:write",
    "evaluations:read",
    "evaluations:write",
]


async def create_api_key(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    env: str,
    scopes: list[str] | None = None,
    expires_at: datetime | None = None,
) -> tuple[ApiKey, str]:
    """Create a new API key for tenant_id. Returns (record, plaintext).
    The plaintext must be shown to the caller exactly once and never
    stored or logged by anything downstream of this function."""
    plaintext, prefix, key_hash = generate_api_key(env)
    api_key = ApiKey(
        tenant_id=tenant_id,
        prefix=prefix,
        key_hash=key_hash,
        scopes=scopes or DEFAULT_SCOPES,
        created_at=datetime.now(UTC),
        expires_at=expires_at,
    )
    db.add(api_key)
    await db.flush()
    return api_key, plaintext


async def get_api_key_by_plaintext(db: AsyncSession, plaintext: str) -> ApiKey | None:
    """The only lookup path that is intentionally not tenant_id-scoped:
    at authentication time the tenant is not yet known — it is derived
    from the matched key itself, not supplied by the client."""
    key_hash = hash_api_key(plaintext)
    result = await db.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
    return result.scalar_one_or_none()


async def touch_last_used(db: AsyncSession, api_key_id: uuid.UUID) -> None:
    await db.execute(
        update(ApiKey).where(ApiKey.id == api_key_id).values(last_used_at=datetime.now(UTC))
    )

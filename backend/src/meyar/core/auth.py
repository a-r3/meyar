import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.db import get_db
from meyar.models.api_key import ApiKey
from meyar.services.api_key_repo import get_api_key_by_plaintext, touch_last_used

bearer_scheme = HTTPBearer(
    scheme_name="ApiKeyBearer",
    description="MEYAR internal API key, presented as a Bearer token.",
    auto_error=False,
)


@dataclass(frozen=True)
class TenantContext:
    tenant_id: uuid.UUID
    api_key_id: uuid.UUID
    scopes: list[str]


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def api_key_is_active(api_key: ApiKey, *, now: datetime | None = None) -> bool:
    """One authority for API-key expiry/revocation semantics."""
    checked_at = now or datetime.now(UTC)
    return api_key.revoked_at is None and (
        api_key.expires_at is None or api_key.expires_at > checked_at
    )


async def authenticate_raw_api_key(
    db: AsyncSession, plaintext: str, *, update_last_used: bool = True
) -> ApiKey | None:
    """Validate a presented raw key for both Bearer auth and UI login.

    The plaintext is used only for the one-way lookup and is never returned,
    persisted, logged, or attached to an exception.
    """
    api_key = await get_api_key_by_plaintext(db, plaintext)
    if api_key is None or not api_key_is_active(api_key):
        return None
    if update_last_used:
        await touch_last_used(db, api_key.id)
    return api_key


async def get_current_tenant(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> TenantContext:
    if credentials is None or not credentials.credentials.strip():
        raise _unauthorized()
    presented_key = credentials.credentials.strip()

    api_key = await authenticate_raw_api_key(db, presented_key)
    if api_key is None:
        raise _unauthorized()
    await db.commit()

    return TenantContext(
        tenant_id=api_key.tenant_id, api_key_id=api_key.id, scopes=list(api_key.scopes)
    )


def require_scope(scope: str):
    async def _dependency(
        ctx: TenantContext = Depends(get_current_tenant),
    ) -> TenantContext:
        if scope not in ctx.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key is missing required scope: {scope}",
            )
        return ctx

    return _dependency

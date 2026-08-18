import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.db import get_db
from meyar.services.api_key_repo import get_api_key_by_plaintext, touch_last_used


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


async def get_current_tenant(
    request: Request, db: AsyncSession = Depends(get_db)
) -> TenantContext:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise _unauthorized()
    presented_key = auth_header.removeprefix("Bearer ").strip()
    if not presented_key:
        raise _unauthorized()

    api_key = await get_api_key_by_plaintext(db, presented_key)
    if api_key is None:
        raise _unauthorized()
    if api_key.revoked_at is not None:
        raise _unauthorized()
    if api_key.expires_at is not None and api_key.expires_at <= datetime.now(UTC):
        raise _unauthorized()

    await touch_last_used(db, api_key.id)
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

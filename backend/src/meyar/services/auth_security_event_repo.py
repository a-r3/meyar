"""Closed structural outcomes only; no submitted identifiers or request content."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.auth_security_event import AuthSecurityEvent

LOGIN_REJECTED = "LOGIN_REJECTED"
NO_ACTIVE_MEMBERSHIP = "NO_ACTIVE_MEMBERSHIP"
PENDING_TOKEN_INVALID = "PENDING_TOKEN_INVALID"
TENANT_SELECTION_INVALID = "TENANT_SELECTION_INVALID"
_CODES = frozenset({
    LOGIN_REJECTED, NO_ACTIVE_MEMBERSHIP, PENDING_TOKEN_INVALID,
    TENANT_SELECTION_INVALID,
})


async def record_auth_failure(
    db: AsyncSession, *, outcome_code: str, user_id: uuid.UUID | None = None
) -> None:
    if outcome_code not in _CODES:
        raise ValueError("Unreviewed authentication outcome code")
    db.add(AuthSecurityEvent(outcome_code=outcome_code, user_id=user_id))
    await db.flush()

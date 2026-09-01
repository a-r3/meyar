import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.core.roles import permissions_for_role
from meyar.db import get_db
from meyar.services.browser_session_repo import get_browser_session_by_token
from meyar.services.tenant_membership_repo import get_membership_by_id
from meyar.services.user_repo import get_user_by_id

UI_SESSION_COOKIE = "meyar_ui_session"


class UIAccessError(Exception):
    def __init__(self, status_code: int, *, clear_cookie: bool = False) -> None:
        self.status_code = status_code
        self.clear_cookie = clear_cookie
        super().__init__("UI access denied.")


@dataclass(frozen=True)
class UIContext:
    """The accountable human principal for one authenticated UI request.
    Every field below is re-derived live from the User + TenantMembership
    rows on every request (see get_ui_context) — a disabled user or a
    deactivated/revoked membership takes effect immediately, without
    waiting for the session to expire or for re-login."""

    session_id: uuid.UUID
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    membership_id: uuid.UUID
    role: str
    scopes: frozenset[str]
    csrf_token: str


def derive_csrf_token(*, csrf_secret: str, raw_session_token: str) -> str:
    return hmac.new(
        bytes.fromhex(csrf_secret), raw_session_token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_csrf(expected: str, presented: str | None) -> None:
    if presented is None or not hmac.compare_digest(expected, presented):
        raise UIAccessError(status.HTTP_403_FORBIDDEN)


async def get_ui_context(
    request: Request, db: AsyncSession = Depends(get_db)
) -> UIContext:
    raw_token = request.cookies.get(UI_SESSION_COOKIE)
    if not raw_token:
        raise UIAccessError(status.HTTP_303_SEE_OTHER)
    session = await get_browser_session_by_token(db, raw_token)
    now = datetime.now(UTC)
    if session is None or session.revoked_at is not None or session.expires_at <= now:
        raise UIAccessError(status.HTTP_303_SEE_OTHER, clear_cookie=True)

    user = await get_user_by_id(db, session.user_id)
    if user is None or not user.is_active:
        raise UIAccessError(status.HTTP_303_SEE_OTHER, clear_cookie=True)

    membership = await get_membership_by_id(db, session.tenant_membership_id)
    if membership is None or not membership.is_active or membership.user_id != user.id:
        raise UIAccessError(status.HTTP_303_SEE_OTHER, clear_cookie=True)

    return UIContext(
        session_id=session.id,
        user_id=user.id,
        tenant_id=membership.tenant_id,
        membership_id=membership.id,
        role=membership.role,
        scopes=permissions_for_role(membership.role),
        csrf_token=derive_csrf_token(
            csrf_secret=session.csrf_secret, raw_session_token=raw_token
        ),
    )


def require_ui_scopes(*required_scopes: str):
    async def _dependency(ctx: UIContext = Depends(get_ui_context)) -> UIContext:
        if not set(required_scopes).issubset(ctx.scopes):
            raise UIAccessError(status.HTTP_403_FORBIDDEN)
        return ctx

    return _dependency

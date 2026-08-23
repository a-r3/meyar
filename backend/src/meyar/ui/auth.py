import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.core.auth import api_key_is_active
from meyar.db import get_db
from meyar.services.api_key_repo import get_api_key_by_id
from meyar.services.browser_session_repo import get_browser_session_by_token

UI_SESSION_COOKIE = "meyar_ui_session"


class UIAccessError(Exception):
    def __init__(self, status_code: int, *, clear_cookie: bool = False) -> None:
        self.status_code = status_code
        self.clear_cookie = clear_cookie
        super().__init__("UI access denied.")


@dataclass(frozen=True)
class UIContext:
    session_id: uuid.UUID
    api_key_id: uuid.UUID
    tenant_id: uuid.UUID
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
    api_key = await get_api_key_by_id(db, session.api_key_id)
    if api_key is None or not api_key_is_active(api_key, now=now):
        raise UIAccessError(status.HTTP_303_SEE_OTHER, clear_cookie=True)
    return UIContext(
        session_id=session.id,
        api_key_id=api_key.id,
        tenant_id=api_key.tenant_id,
        scopes=frozenset(api_key.scopes),
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

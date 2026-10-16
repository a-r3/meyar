"""Human HTTP/JSON authentication (issue #45, D-116).

There is deliberately no second human auth system. A human JSON request is
authenticated by the SAME BrowserSession cookie the Jinja UI issues, and the
principal is re-derived on every request through ``resolve_ui_context`` (live
User + TenantMembership + BrowserSession + active Tenant, with the same lock
order). Differences from the HTML adapter are presentation only:

- failures are JSON ``ApplicationError`` (401/403) instead of 303 redirects;
- a state-changing request must carry the per-session CSRF token in the
  ``X-CSRF-Token`` header (the same HMAC the HTML forms embed), compared in
  constant time, after authentication and scope checks;
- the cookie is ``Path=/ui`` so it is never sent to the machine ``/api/v1``
  surface; human JSON therefore lives under ``/ui/api/v1``.

API keys are never accepted here, and a cookie session is never accepted on
``/api/v1``. Client-supplied tenant/user/session identifiers are not read."""

import hmac

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyCookie, APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.application.errors import ApplicationError, ErrorCode
from meyar.db import get_db
from meyar.ui.auth import UI_SESSION_COOKIE, UIAccessError, UIContext, resolve_ui_context

CSRF_HEADER = "X-CSRF-Token"

# These two schemes exist so OpenAPI states the real mechanism. The actual
# checks below read the request directly; the values extracted here are unused.
_session_cookie_scheme = APIKeyCookie(
    name=UI_SESSION_COOKIE,
    scheme_name="HumanSessionCookie",
    description=(
        "Opaque BrowserSession cookie issued by the interactive MEYAR login "
        "(/ui/login). HttpOnly, SameSite=Lax, Path=/ui. Not an API key."
    ),
    auto_error=False,
)
_csrf_header_scheme = APIKeyHeader(
    name=CSRF_HEADER,
    scheme_name="HumanCsrfToken",
    description=(
        "Per-session CSRF token from GET /ui/api/v1/session. Required on every "
        "state-changing human request."
    ),
    auto_error=False,
)


def _unauthenticated(*, clear_cookie: bool) -> ApplicationError:
    return ApplicationError(
        ErrorCode.AUTHENTICATION_REQUIRED,
        status_code=401,
        clear_session_cookie=clear_cookie,
    )


async def get_human_context(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _cookie: str | None = Security(_session_cookie_scheme),
) -> UIContext:
    try:
        return await resolve_ui_context(request, db)
    except UIAccessError as exc:
        await db.rollback()
        raise _unauthenticated(clear_cookie=exc.clear_cookie) from None


def require_human_scopes(*required_scopes: str):
    async def _dependency(ctx: UIContext = Depends(get_human_context)) -> UIContext:
        if not set(required_scopes).issubset(ctx.scopes):
            raise ApplicationError(ErrorCode.AUTHORIZATION_DENIED, status_code=403)
        return ctx

    return _dependency


def require_human_mutation(*required_scopes: str):
    """Authenticate → authorize → verify CSRF, in that order."""

    async def _dependency(
        request: Request,
        ctx: UIContext = Depends(require_human_scopes(*required_scopes)),
        _csrf: str | None = Security(_csrf_header_scheme),
    ) -> UIContext:
        presented = request.headers.get(CSRF_HEADER)
        if presented is None or not hmac.compare_digest(
            ctx.csrf_token.encode("utf-8"), presented.encode("utf-8")
        ):
            raise ApplicationError(ErrorCode.CSRF_REJECTED, status_code=403)
        return ctx

    return _dependency

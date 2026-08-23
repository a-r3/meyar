import re
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakeLLMProvider
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.config import Settings, get_settings
from meyar.llm.dependency import get_llm_provider
from meyar.main import app
from meyar.models.api_key import ApiKey
from meyar.models.browser_session import BrowserSession
from meyar.search.planner_schemas import PlannerDraft
from meyar.services.api_key_repo import create_api_key
from meyar.services.browser_session_repo import hash_session_token


@pytest.fixture
def local_ui_settings() -> Settings:
    settings = Settings(ui_cookie_secure=False)
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


async def _login(client: AsyncClient, plaintext: str):
    return await client.post(
        "/ui/login", data={"api_key": plaintext}, follow_redirects=False
    )


async def _csrf(client: AsyncClient) -> str:
    response = await client.get("/ui")
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', response.text)
    assert match is not None
    return match.group(1)


async def test_valid_api_key_login_creates_hashed_server_session(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    _tenant, api_key, plaintext = tenant_and_key
    response = await _login(client, plaintext)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui"
    raw_cookie = response.cookies.get("meyar_ui_session")
    assert raw_cookie is not None

    session = (await db_session.execute(select(BrowserSession))).scalar_one()
    assert session.api_key_id == api_key.id
    assert session.session_token_hash == hash_session_token(raw_cookie)
    assert session.session_token_hash != raw_cookie
    assert plaintext not in session.__dict__.values()
    assert not hasattr(session, "raw_api_key")
    assert not hasattr(session, "tenant_id")
    assert not hasattr(session, "scopes")
    assert session.expires_at - session.created_at == timedelta(hours=8)


async def test_invalid_login_is_generic_and_never_echoes_key(
    client: AsyncClient, local_ui_settings: Settings
) -> None:
    submitted = "meyar_test_invalid-secret-value"
    response = await _login(client, submitted)
    assert response.status_code == 401
    assert "Daxilolma məlumatı etibarlı deyil" in response.text
    assert submitted not in response.text
    assert "revoked" not in response.text.lower()
    assert "expired" not in response.text.lower()


async def test_login_cookie_policy_local_and_production(
    client: AsyncClient, tenant_and_key, local_ui_settings: Settings
) -> None:
    _tenant, _api_key, plaintext = tenant_and_key
    local = await _login(client, plaintext)
    header = local.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "path=/ui" in header
    assert "secure" not in header

    app.dependency_overrides[get_settings] = lambda: Settings(ui_cookie_secure=True)
    production = await _login(client, plaintext)
    assert "secure" in production.headers["set-cookie"].lower()


def test_production_rejects_insecure_ui_cookie_configuration() -> None:
    with pytest.raises(ValueError, match="Production UI cookies must be Secure"):
        Settings(env="production", ui_cookie_secure=False)


async def test_session_fixation_cookie_is_replaced_with_fresh_token(
    client: AsyncClient, tenant_and_key, local_ui_settings: Settings
) -> None:
    _tenant, _api_key, plaintext = tenant_and_key
    client.cookies.set("meyar_ui_session", "attacker-controlled", path="/ui")
    response = await _login(client, plaintext)
    issued = response.cookies.get("meyar_ui_session")
    assert issued is not None
    assert issued != "attacker-controlled"
    assert issued != plaintext


@pytest.mark.parametrize("field", ["expires_at", "revoked_at"])
async def test_expired_or_revoked_session_is_rejected(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
    field: str,
) -> None:
    _tenant, _api_key, plaintext = tenant_and_key
    await _login(client, plaintext)
    session = (await db_session.execute(select(BrowserSession))).scalars().first()
    assert session is not None
    setattr(session, field, datetime.now(UTC) - timedelta(seconds=1))
    await db_session.commit()

    response = await client.get("/ui", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    assert "max-age=0" in response.headers["set-cookie"].lower()


@pytest.mark.parametrize("field", ["revoked_at", "expires_at"])
async def test_live_api_key_revocation_or_expiry_invalidates_existing_session(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
    field: str,
) -> None:
    _tenant, api_key, plaintext = tenant_and_key
    await _login(client, plaintext)
    await db_session.execute(
        ApiKey.__table__.update()
        .where(ApiKey.id == api_key.id)
        .values(**{field: datetime.now(UTC) - timedelta(seconds=1)})
    )
    await db_session.commit()
    response = await client.get("/ui/library", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


async def test_ui_scope_enforcement_reuses_live_api_key_scopes(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    tenant, _api_key, _plaintext = tenant_and_key
    _key, plaintext = await create_api_key(
        db_session, tenant_id=tenant.id, env="test", scopes=["candidates:read"]
    )
    await db_session.commit()
    await _login(client, plaintext)
    assert (await client.get("/ui/library")).status_code == 200
    forbidden = await client.get("/ui/jobs")
    assert forbidden.status_code == 403
    assert "icazəniz yoxdur" in forbidden.text


async def test_logout_requires_correct_csrf_revokes_session_and_clears_cookie(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
) -> None:
    _tenant, _api_key, plaintext = tenant_and_key
    await _login(client, plaintext)
    token = await _csrf(client)
    response = await client.post(
        "/ui/logout", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    assert "max-age=0" in response.headers["set-cookie"].lower()
    session = (await db_session.execute(select(BrowserSession))).scalars().first()
    assert session is not None and session.revoked_at is not None


@pytest.mark.parametrize("csrf_data", [{}, {"csrf_token": "0" * 64}])
async def test_logout_rejects_missing_or_wrong_csrf_before_revocation(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_key,
    local_ui_settings: Settings,
    csrf_data: dict[str, str],
) -> None:
    _tenant, _api_key, plaintext = tenant_and_key
    await _login(client, plaintext)
    response = await client.post("/ui/logout", data=csrf_data, follow_redirects=False)
    assert response.status_code in (403, 422)
    session = (await db_session.execute(select(BrowserSession))).scalars().first()
    assert session is not None and session.revoked_at is None


@pytest.mark.parametrize("csrf_data", [{}, {"csrf_token": "f" * 64}])
async def test_search_rejects_missing_or_wrong_csrf_before_planner(
    client: AsyncClient,
    tenant_and_key,
    local_ui_settings: Settings,
    csrf_data: dict[str, str],
) -> None:
    _tenant, _api_key, plaintext = tenant_and_key
    fake = FakeLLMProvider(planner_draft=PlannerDraft())
    app.dependency_overrides[get_llm_provider] = lambda: fake
    await _login(client, plaintext)
    response = await client.post(
        "/ui/search",
        data={"query": "Python bilən namizəd", "as_of_date": "2026-01-01", **csrf_data},
    )
    assert response.status_code in (403, 422)
    assert fake.call_count == 0


@pytest.mark.parametrize("csrf_data", [{}, {"csrf_token": "e" * 64}])
async def test_ranking_rejects_missing_or_wrong_csrf_before_service(
    client: AsyncClient,
    tenant_and_key,
    local_ui_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    csrf_data: dict[str, str],
) -> None:
    _tenant, _api_key, plaintext = tenant_and_key
    calls = 0

    async def forbidden_rank(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("ranking must not run before CSRF")

    monkeypatch.setattr("meyar.ui.router.rank_candidates_for_job", forbidden_rank)
    await _login(client, plaintext)
    response = await client.post(
        "/ui/jobs/00000000-0000-0000-0000-000000000001/rank",
        data={"evaluation_as_of_date": "2026-01-01", **csrf_data},
    )
    assert response.status_code in (403, 422)
    assert calls == 0


async def test_ui_security_headers_apply_to_authenticated_and_error_html(
    client: AsyncClient, tenant_and_key, local_ui_settings: Settings
) -> None:
    _tenant, _api_key, plaintext = tenant_and_key
    await _login(client, plaintext)
    for response in (
        await client.get("/ui"),
        await client.get("/ui/candidates/00000000-0000-0000-0000-000000000001"),
    ):
        assert response.headers["cache-control"] == "no-store"
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert "unsafe-inline" not in response.headers["content-security-policy"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"

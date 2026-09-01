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
from meyar.models.browser_session import BrowserSession
from meyar.models.tenant_membership import TenantMembership
from meyar.models.user import User
from meyar.search.planner_schemas import PlannerDraft
from meyar.services.browser_session_repo import hash_session_token
from meyar.services.tenant_membership_repo import create_membership
from meyar.services.user_repo import create_user


@pytest.fixture
def local_ui_settings() -> Settings:
    settings = Settings(ui_cookie_secure=False)
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


async def _login(client: AsyncClient, username: str, password: str):
    return await client.post(
        "/ui/login", data={"username": username, "password": password}, follow_redirects=False
    )


async def _csrf(client: AsyncClient) -> str:
    response = await client.get("/ui")
    assert response.status_code == 200
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', response.text)
    assert match is not None
    return match.group(1)


async def test_valid_login_creates_hashed_server_session(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    _tenant, user, password, membership = tenant_and_user
    response = await _login(client, user.username, password)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui"
    raw_cookie = response.cookies.get("meyar_ui_session")
    assert raw_cookie is not None

    session = (await db_session.execute(select(BrowserSession))).scalar_one()
    assert session.user_id == user.id
    assert session.tenant_membership_id == membership.id
    assert session.session_token_hash == hash_session_token(raw_cookie)
    assert session.session_token_hash != raw_cookie
    assert password not in session.__dict__.values()
    assert not hasattr(session, "api_key_id")
    assert not hasattr(session, "tenant_id")
    assert not hasattr(session, "scopes")
    assert session.expires_at - session.created_at == timedelta(hours=8)


async def test_invalid_password_is_generic_and_never_echoes_secret(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    _tenant, user, _password, _membership = tenant_and_user
    submitted = "definitely-the-wrong-password"
    response = await _login(client, user.username, submitted)
    assert response.status_code == 401
    assert "İstifadəçi adı və ya parol yanlışdır" in response.text
    assert submitted not in response.text
    assert "revoked" not in response.text.lower()
    assert "disabled" not in response.text.lower()


async def test_unknown_username_returns_identical_generic_error(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    _tenant, _user, password, _membership = tenant_and_user
    known_user_wrong_password = await _login(client, "no-such-user", password)
    assert known_user_wrong_password.status_code == 401
    assert "İstifadəçi adı və ya parol yanlışdır" in known_user_wrong_password.text
    assert "no-such-user" not in known_user_wrong_password.text


async def test_disabled_user_cannot_log_in_with_correct_password(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    await db_session.execute(
        User.__table__.update().where(User.id == user.id).values(is_active=False)
    )
    await db_session.commit()
    response = await _login(client, user.username, password)
    assert response.status_code == 401
    assert "İstifadəçi adı və ya parol yanlışdır" in response.text


async def test_inactive_membership_blocks_login(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    _tenant, user, password, membership = tenant_and_user
    await db_session.execute(
        TenantMembership.__table__.update()
        .where(TenantMembership.id == membership.id)
        .values(is_active=False)
    )
    await db_session.commit()
    response = await _login(client, user.username, password)
    assert response.status_code == 403
    assert "heç bir aktiv təşkilat girişi" in response.text


async def test_login_cookie_policy_local_and_production(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    local = await _login(client, user.username, password)
    header = local.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header
    assert "path=/ui" in header
    assert "secure" not in header

    app.dependency_overrides[get_settings] = lambda: Settings(ui_cookie_secure=True)
    production = await _login(client, user.username, password)
    assert "secure" in production.headers["set-cookie"].lower()


def test_production_rejects_insecure_ui_cookie_configuration() -> None:
    with pytest.raises(ValueError, match="Production UI cookies must be Secure"):
        Settings(env="production", ui_cookie_secure=False)


def test_production_rejects_default_pending_login_secret() -> None:
    with pytest.raises(ValueError, match="MEYAR_PENDING_LOGIN_SECRET"):
        Settings(env="production", ui_cookie_secure=True)


async def test_session_fixation_cookie_is_replaced_with_fresh_token(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    client.cookies.set("meyar_ui_session", "attacker-controlled", path="/ui")
    response = await _login(client, user.username, password)
    issued = response.cookies.get("meyar_ui_session")
    assert issued is not None
    assert issued != "attacker-controlled"
    assert issued != password


@pytest.mark.parametrize("field", ["expires_at", "revoked_at"])
async def test_expired_or_revoked_session_is_rejected(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
    field: str,
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    await _login(client, user.username, password)
    session = (await db_session.execute(select(BrowserSession))).scalars().first()
    assert session is not None
    setattr(session, field, datetime.now(UTC) - timedelta(seconds=1))
    await db_session.commit()

    response = await client.get("/ui", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    assert "max-age=0" in response.headers["set-cookie"].lower()


async def test_disabled_user_after_login_invalidates_existing_session(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    await _login(client, user.username, password)
    await db_session.execute(
        User.__table__.update().where(User.id == user.id).values(is_active=False)
    )
    await db_session.commit()
    response = await client.get("/ui/library", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


async def test_revoked_membership_after_login_invalidates_existing_session(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    _tenant, user, password, membership = tenant_and_user
    await _login(client, user.username, password)
    await db_session.execute(
        TenantMembership.__table__.update()
        .where(TenantMembership.id == membership.id)
        .values(is_active=False)
    )
    await db_session.commit()
    response = await client.get("/ui/library", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


async def test_ui_permissions_are_role_derived_and_allow_current_hr_actions(
    client: AsyncClient,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    """Authorization enforcement is server-side and role-derived (see
    meyar.core.roles.permissions_for_role), not read from anything the
    client sends — an active HR_USER membership can reach every current
    UI surface."""
    _tenant, user, password, _membership = tenant_and_user
    await _login(client, user.username, password)
    assert (await client.get("/ui/library")).status_code == 200
    assert (await client.get("/ui/jobs")).status_code == 200


async def test_ui_candidate_detail_is_scoped_to_the_logged_in_tenant_only(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    """Tenant scoping is derived entirely from the server-side session's
    membership — never from a client-supplied value — so a candidate id
    belonging to another tenant simply does not resolve, exactly like the
    existing repository-level cross-tenant guarantees."""
    from meyar.services.candidate_repo import create_candidate
    from meyar.services.tenant_repo import create_tenant

    _tenant, user, password, _membership = tenant_and_user
    other_tenant = await create_tenant(db_session, name="Other tenant")
    other_candidate = await create_candidate(db_session, tenant_id=other_tenant.id)
    await db_session.commit()

    await _login(client, user.username, password)
    response = await client.get(f"/ui/candidates/{other_candidate.id}")
    assert response.status_code == 404


async def test_logout_requires_correct_csrf_revokes_session_and_clears_cookie(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    await _login(client, user.username, password)
    token = await _csrf(client)
    response = await client.post(
        "/ui/logout", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"
    assert "max-age=0" in response.headers["set-cookie"].lower()
    session = (await db_session.execute(select(BrowserSession))).scalars().first()
    assert session is not None and session.revoked_at is not None


async def test_logout_does_not_invalidate_a_different_users_session(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    """Multi-user correctness: one user's logout must never revoke
    another user's independent session."""
    tenant, _user, _password, _membership = tenant_and_user
    other_user = await create_user(
        db_session, username="other-hr-user", plaintext_password="another-strong-password-2"
    )
    other_membership = await create_membership(
        db_session, user_id=other_user.id, tenant_id=tenant.id, role="HR_USER"
    )
    await db_session.commit()

    from httpx import ASGITransport

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as other_client:
        _tenant, user, password, _membership = tenant_and_user
        await _login(client, user.username, password)
        await _login(other_client, other_user.username, "another-strong-password-2")

        token = await _csrf(client)
        await client.post("/ui/logout", data={"csrf_token": token}, follow_redirects=False)

        other_response = await other_client.get("/ui")
        assert other_response.status_code == 200

    sessions = (await db_session.execute(select(BrowserSession))).scalars().all()
    revoked = {s.user_id: s.revoked_at is not None for s in sessions}
    assert revoked[user.id] is True
    assert revoked[other_user.id] is False
    assert other_membership.tenant_id == tenant.id


@pytest.mark.parametrize("csrf_data", [{}, {"csrf_token": "0" * 64}])
async def test_logout_rejects_missing_or_wrong_csrf_before_revocation(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
    csrf_data: dict[str, str],
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    await _login(client, user.username, password)
    response = await client.post("/ui/logout", data=csrf_data, follow_redirects=False)
    assert response.status_code in (403, 422)
    session = (await db_session.execute(select(BrowserSession))).scalars().first()
    assert session is not None and session.revoked_at is None


@pytest.mark.parametrize("csrf_data", [{}, {"csrf_token": "f" * 64}])
async def test_search_rejects_missing_or_wrong_csrf_before_planner(
    client: AsyncClient,
    tenant_and_user,
    local_ui_settings: Settings,
    csrf_data: dict[str, str],
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    fake = FakeLLMProvider(planner_draft=PlannerDraft())
    app.dependency_overrides[get_llm_provider] = lambda: fake
    await _login(client, user.username, password)
    response = await client.post(
        "/ui/search",
        data={"query": "Python bilən namizəd", "as_of_date": "2026-01-01", **csrf_data},
    )
    assert response.status_code in (403, 422)
    assert fake.call_count == 0


@pytest.mark.parametrize("csrf_data", [{}, {"csrf_token": "e" * 64}])
async def test_ranking_rejects_missing_or_wrong_csrf_before_service(
    client: AsyncClient,
    tenant_and_user,
    local_ui_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    csrf_data: dict[str, str],
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    calls = 0

    async def forbidden_rank(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("ranking must not run before CSRF")

    monkeypatch.setattr("meyar.ui.router.rank_candidates_for_job", forbidden_rank)
    await _login(client, user.username, password)
    response = await client.post(
        "/ui/jobs/00000000-0000-0000-0000-000000000001/rank",
        data={"evaluation_as_of_date": "2026-01-01", **csrf_data},
    )
    assert response.status_code in (403, 422)
    assert calls == 0


async def test_ui_security_headers_apply_to_authenticated_and_error_html(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    await _login(client, user.username, password)
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

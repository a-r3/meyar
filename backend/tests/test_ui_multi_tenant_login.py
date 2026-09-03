"""Slice 1 — Human Identity & Dual Access (issue #30): a user with more
than one active TenantMembership must make an explicit, server-validated
tenant choice at login — never a silent pick, and never a client-supplied
tenant/membership id trusted without live re-validation. See
meyar.ui.pending_login and meyar.ui.router.select_tenant."""

import re

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.config import Settings, get_settings
from meyar.main import app
from meyar.services.tenant_membership_repo import create_membership
from meyar.services.tenant_repo import create_tenant
from meyar.services.user_repo import create_user
from meyar.ui.pending_login import issue_pending_login_token, verify_pending_login_token


@pytest.fixture
def local_ui_settings() -> Settings:
    settings = Settings(ui_cookie_secure=False)
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


async def _two_tenant_user(db_session: AsyncSession):
    tenant_a = await create_tenant(db_session, name="Multi-A")
    tenant_b = await create_tenant(db_session, name="Multi-B")
    user = await create_user(
        db_session, username="multi-membership-user", plaintext_password="password-value-1"
    )
    membership_a = await create_membership(
        db_session, user_id=user.id, tenant_id=tenant_a.id, role="HR_USER"
    )
    membership_b = await create_membership(
        db_session, user_id=user.id, tenant_id=tenant_b.id, role="HR_USER"
    )
    await db_session.commit()
    return tenant_a, tenant_b, user, membership_a, membership_b


async def test_multiple_active_memberships_show_a_selection_screen_not_auto_pick(
    client: AsyncClient, db_session: AsyncSession, local_ui_settings: Settings
) -> None:
    tenant_a, tenant_b, user, _membership_a, _membership_b = await _two_tenant_user(db_session)

    response = await client.post(
        "/ui/login",
        data={"username": user.username, "password": "password-value-1"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert response.cookies.get("meyar_ui_session") is None
    assert tenant_a.name in response.text
    assert tenant_b.name in response.text
    assert 'name="membership_id"' in response.text
    assert 'name="token"' in response.text


async def test_selecting_a_valid_membership_completes_login(
    client: AsyncClient, db_session: AsyncSession, local_ui_settings: Settings
) -> None:
    tenant_a, _tenant_b, user, membership_a, _membership_b = await _two_tenant_user(db_session)
    # Captured before the login POST below — it shares db_session with the
    # app via the client fixture's dependency override, and the route's
    # own rollback would otherwise expire this already-loaded instance.
    membership_a_id = str(membership_a.id)

    first = await client.post(
        "/ui/login",
        data={"username": user.username, "password": "password-value-1"},
        follow_redirects=False,
    )
    token_match = re.search(r'name="token" value="([^"]+)"', first.text)
    assert token_match is not None
    token = token_match.group(1)

    response = await client.post(
        "/ui/login/select-tenant",
        data={"token": token, "membership_id": membership_a_id},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/agent"
    assert response.cookies.get("meyar_ui_session") is not None

    home = await client.get("/ui/library")
    assert home.status_code == 200


async def test_selecting_a_membership_belonging_to_another_user_is_rejected(
    client: AsyncClient, db_session: AsyncSession, local_ui_settings: Settings
) -> None:
    """Server-side re-validation: a tampered membership_id that does not
    belong to the token's authenticated user must never resolve, even
    though the membership id itself is a real, valid, active row."""
    tenant_a, _tenant_b, user, _membership_a, _membership_b = await _two_tenant_user(db_session)
    other_tenant = await create_tenant(db_session, name="Someone-Else-Tenant")
    other_user = await create_user(
        db_session, username="someone-else", plaintext_password="another-password-1"
    )
    other_membership = await create_membership(
        db_session, user_id=other_user.id, tenant_id=other_tenant.id, role="HR_USER"
    )
    await db_session.commit()
    other_membership_id = str(other_membership.id)

    first = await client.post(
        "/ui/login",
        data={"username": user.username, "password": "password-value-1"},
        follow_redirects=False,
    )
    token_match = re.search(r'name="token" value="([^"]+)"', first.text)
    assert token_match is not None
    token = token_match.group(1)

    response = await client.post(
        "/ui/login/select-tenant",
        data={"token": token, "membership_id": other_membership_id},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert response.cookies.get("meyar_ui_session") is None


async def test_tampered_pending_login_token_is_rejected(
    client: AsyncClient, db_session: AsyncSession, local_ui_settings: Settings
) -> None:
    _tenant_a, _tenant_b, _user, membership_a, _membership_b = await _two_tenant_user(db_session)

    response = await client.post(
        "/ui/login/select-tenant",
        data={"token": "not-a-real-token", "membership_id": str(membership_a.id)},
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert response.cookies.get("meyar_ui_session") is None


def test_pending_login_token_signature_is_verified() -> None:
    import uuid

    user_id = uuid.uuid4()
    token = issue_pending_login_token(secret="secret-a", user_id=user_id)
    assert verify_pending_login_token(secret="secret-a", token=token) == user_id
    assert verify_pending_login_token(secret="secret-b", token=token) is None
    assert verify_pending_login_token(secret="secret-a", token=token + "x") is None


async def test_single_active_membership_still_auto_selects(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    response = await client.post(
        "/ui/login",
        data={"username": user.username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/agent"

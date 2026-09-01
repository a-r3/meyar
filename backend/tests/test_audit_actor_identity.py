"""Slice 1 — Human Identity & Dual Access (issue #30): structured audit
actor identity (meyar.services.audit_repo.ACTOR_HUMAN_USER /
ACTOR_API_KEY, AuditEvent.actor_type/actor_id). Proves a human UI action
and a machine API action are both attributed, and distinguishable, using
only an id reference — never a name/email/password/API-key secret."""

import re

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.config import Settings, get_settings
from meyar.main import app
from meyar.models.audit_event import AuditEvent
from meyar.services.audit_repo import ACTOR_API_KEY, ACTOR_HUMAN_USER


async def _login_and_csrf(client: AsyncClient, username: str, password: str) -> str:
    response = await client.post(
        "/ui/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    home = await client.get("/ui")
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', home.text)
    assert match is not None
    return match.group(1)


async def test_ui_job_creation_is_attributed_to_the_human_actor(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user
) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(ui_cookie_secure=False)
    tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)

    data = {"title": "Attributed HR Job", "csrf_token": csrf}
    for prefix in ("must", "pref"):
        for i in range(3):
            data[f"{prefix}_kind_{i}"] = "SKILL"
            data[f"{prefix}_requirement_{i}"] = ""
            data[f"{prefix}_min_years_{i}"] = ""
            data[f"{prefix}_weight_{i}"] = ""
    data["must_requirement_0"] = "Python"

    response = await client.post("/ui/jobs", data=data, follow_redirects=False)
    assert response.status_code == 303

    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant.id, AuditEvent.event_type == "job.created"
            )
        )
    ).scalar_one()
    assert event.actor_type == ACTOR_HUMAN_USER
    assert event.actor_id == user.id


async def test_api_job_creation_is_attributed_to_the_api_key_actor(
    client: AsyncClient, db_session: AsyncSession, tenant_and_key
) -> None:
    tenant, api_key, plaintext = tenant_and_key
    body = {
        "title": "Attributed API Job",
        "criteria": [
            {
                "id": "python",
                "kind": "SKILL",
                "type": "MUST_HAVE",
                "label": "Python",
                "value": "Python",
            }
        ],
    }
    response = await client.post(
        "/api/v1/jobs", json=body, headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert response.status_code == 201

    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.tenant_id == tenant.id, AuditEvent.event_type == "job.created"
            )
        )
    ).scalar_one()
    assert event.actor_type == ACTOR_API_KEY
    assert event.actor_id == api_key.id


async def test_login_and_logout_are_attributed_to_the_human_actor(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user
) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(ui_cookie_secure=False)
    tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)
    await client.post("/ui/logout", data={"csrf_token": csrf})

    events = (
        await db_session.execute(
            select(AuditEvent)
            .where(AuditEvent.tenant_id == tenant.id)
            .order_by(AuditEvent.created_at)
        )
    ).scalars().all()
    event_types = {e.event_type: e for e in events}
    assert event_types["ui.login.succeeded"].actor_type == ACTOR_HUMAN_USER
    assert event_types["ui.login.succeeded"].actor_id == user.id
    assert event_types["ui.logout"].actor_type == ACTOR_HUMAN_USER
    assert event_types["ui.logout"].actor_id == user.id
    # Never a name/email/password/API-key secret anywhere in the metadata.
    for event in events:
        assert "password" not in event.event_metadata
        assert user.username not in str(event.event_metadata)

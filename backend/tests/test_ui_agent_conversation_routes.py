"""issue #80 PR80-1 — real /ui/agent route behavior over the durable
conversation / BrowserSession-bound live context split (D-086): relogin
history without inherited authority, "Yeni söhbət" as a NEW durable
conversation, existence-private explicit selectors, live owner revocation,
and pending-draft authority across sessions."""

import re
import uuid

from conftest import BrowserTestClient as AsyncClient
from fakes import FakeLLMProvider
from httpx import ASGITransport
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from test_ui_agent_routes import (
    _login_and_csrf,
    _python_confirmation_data,
    _render_python_draft,
    local_ui_settings,  # noqa: F401 - pytest fixture re-export
)

from meyar.agent.schemas import AgentActionType, AgentDecision, AgentResponseCode
from meyar.config import Settings
from meyar.core.roles import ROLE_HR_USER
from meyar.llm.dependency import get_llm_provider
from meyar.main import app
from meyar.models.agent_conversation import AgentConversation, AgentConversationSessionContext
from meyar.models.audit_event import AuditEvent
from meyar.models.job import Job
from meyar.models.tenant_membership import TenantMembership
from meyar.models.user import User
from meyar.services.agent_conversation_repo import OwnerPrincipal, create_conversation
from meyar.services.tenant_membership_repo import create_membership
from meyar.services.tenant_repo import create_tenant
from meyar.services.user_repo import create_user

GREETING = FakeLLMProvider(
    agent_decision=AgentDecision(
        action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.GREETING
    )
)


def _conversation_id(html: str) -> str:
    match = re.search(r'name="conversation_id" value="([0-9a-f-]{36})"', html)
    assert match is not None
    return match.group(1)


async def _conversations(db_session: AsyncSession, tenant_id) -> list[AgentConversation]:
    return list(
        (
            await db_session.scalars(
                select(AgentConversation)
                .where(AgentConversation.tenant_id == tenant_id)
                .order_by(AgentConversation.created_at)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def test_new_conversation_creates_a_new_durable_row_and_keeps_history(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _membership = tenant_and_user
    app.dependency_overrides[get_llm_provider] = lambda: GREETING
    csrf = await _login_and_csrf(client, user.username, password)
    first_page = await client.get("/ui/agent")
    first_id = _conversation_id(first_page.text)
    await client.post(
        "/ui/agent",
        data={"message": "Salam köhnə söhbət", "csrf_token": csrf, "conversation_id": first_id},
    )

    reset = await client.post(
        "/ui/agent/reset", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert reset.status_code == 303
    assert reset.headers["location"].startswith("/ui/agent?conversation=")
    current = await client.get("/ui/agent")
    assert "Salam köhnə söhbət" not in current.text
    new_id = _conversation_id(current.text)
    assert new_id != first_id

    rows = await _conversations(db_session, tenant.id)
    assert [str(row.id) for row in rows] == [first_id, new_id]
    old, new = rows
    assert [turn["text"] for turn in old.turns][0] == "Salam köhnə söhbət"
    assert new.turns == [] and new.title_kind == "NEW"
    assert old.title_kind == "GENERAL"
    new_context = await db_session.scalar(
        select(AgentConversationSessionContext).where(
            AgentConversationSessionContext.conversation_id == new.id
        )
    )
    assert new_context is not None
    assert new_context.active_result_set_id is None
    assert new_context.active_pending_draft_id is None
    assert new_context.context_epoch == 1
    assert current.text.count('action="/ui/agent/reset"') == 1
    assert 'href="/ui/library"' in current.text
    assert 'href="/ui/jobs"' in current.text
    assert 'aria-current="page"' in current.text
    assert "Ümumi söhbət" in current.text
    assert "Bu gün," in current.text or "sen," in current.text
    assert "Salam köhnə söhbət</span>" not in current.text

    # The historical conversation stays reachable through the minimal
    # explicit selector (PR80-2 renders the sidebar).
    reopened = await client.get(f"/ui/agent?conversation={first_id}")
    assert reopened.status_code == 200
    assert "Salam köhnə söhbət" in reopened.text
    opened = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "agent.conversation.opened")
    )
    assert opened is not None
    assert opened.event_metadata == {"conversation_id": first_id, "title_kind": "GENERAL"}


async def test_relogin_shows_history_without_result_or_pending_draft_authority(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    _csrf, confirm_path, _html = await _render_python_draft(
        client, username=user.username, password=password
    )
    conversation_id = _conversation_id(_html)
    await client.post("/ui/logout", data={"csrf_token": _csrf})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as relogin:
        csrf = await _login_and_csrf(relogin, user.username, password)
        before = await db_session.scalar(
            select(func.count()).select_from(AgentConversationSessionContext)
        )
        workspace = await relogin.get(f"/ui/agent?conversation={conversation_id}")
        assert "Python required" in workspace.text  # durable history visible
        assert "Vakansiya analizi" in workspace.text
        assert confirm_path not in workspace.text
        assert "/resolve\"" not in workspace.text
        assert await db_session.scalar(
            select(func.count()).select_from(AgentConversationSessionContext)
        ) == before

        # Old pending draft is NOT live authority in the new BrowserSession.
        stale = await relogin.post(
            confirm_path, data={"csrf_token": csrf}, follow_redirects=False
        )
        assert stale.status_code == 422
        assert await db_session.scalar(select(func.count()).select_from(Job)) == 0

        app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider()
        submission_match = re.search(
            r'name="submission_id" value="([0-9a-f-]{36})"', workspace.text
        )
        assert submission_match is not None
        response = await relogin.post(
            "/ui/agent", data={
                "message": "ilk 3", "csrf_token": csrf,
                "conversation_id": conversation_id,
                "submission_id": submission_match.group(1),
            }
        )
        assert response.status_code == 200
        assert "Əvvəlcə namizəd axtarışı" in response.text


async def test_confirm_clears_live_pending_authority_and_replay_uses_durable_row(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    csrf, confirm_path, html = await _render_python_draft(
        client, username=user.username, password=password
    )
    conversation_id = _conversation_id(html)
    draft_id = uuid.UUID(confirm_path.split("/")[-2])
    context = await db_session.scalar(select(AgentConversationSessionContext))
    assert context is not None and context.active_pending_draft_id == draft_id

    confirmed = await client.post(confirm_path, data=_python_confirmation_data(html, csrf))
    assert confirmed.status_code == 200
    await db_session.refresh(context)
    assert context.active_pending_draft_id is None
    reloaded = await client.get(f"/ui/agent?conversation={conversation_id}")
    assert reloaded.status_code == 200
    assert confirm_path not in reloaded.text
    assert "/resolve\"" not in reloaded.text
    replay = await client.post(confirm_path, data={"csrf_token": csrf})
    assert replay.status_code == 200
    assert await db_session.scalar(select(func.count()).select_from(Job)) == 1


async def test_same_session_reload_and_switch_restore_only_current_draft_controls(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    csrf, confirm_path, html = await _render_python_draft(
        client, username=user.username, password=password
    )
    first_id = _conversation_id(html)
    draft_id = uuid.UUID(confirm_path.split("/")[-2])
    assert html.count(confirm_path) == 1
    assert html.count("Python required") == 1
    before = await db_session.scalar(
        select(func.count()).select_from(AgentConversationSessionContext)
    )

    reloaded = await client.get(f"/ui/agent?conversation={first_id}")
    assert reloaded.status_code == 200
    assert reloaded.text.count(confirm_path) == 1
    assert reloaded.text.count("Python required") == 1
    assert reloaded.text.count('class="agent-turn agent-turn-assistant"') == 1
    assert await db_session.scalar(
        select(func.count()).select_from(AgentConversationSessionContext)
    ) == before
    context = await db_session.scalar(
        select(AgentConversationSessionContext).where(
            AgentConversationSessionContext.conversation_id == uuid.UUID(first_id)
        )
    )
    assert context is not None and context.active_pending_draft_id == draft_id

    switched = await client.post("/ui/agent/reset", data={"csrf_token": csrf})
    second_id = switched.headers["location"].split("=")[-1]
    second = await client.get(switched.headers["location"])
    assert second.status_code == 200 and second_id != first_id
    assert confirm_path not in second.text and "Python required" not in second.text
    reopened = await client.get(f"/ui/agent?conversation={first_id}")
    assert reopened.status_code == 200
    assert reopened.text.count(confirm_path) == 1
    await db_session.refresh(context)
    assert context.active_pending_draft_id == draft_id


async def test_superseded_draft_reload_shows_only_new_draft_controls(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    csrf, old_path, html = await _render_python_draft(
        client, username=user.username, password=password
    )
    conversation_id = _conversation_id(html)
    app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider()
    updated = await client.post(
        "/ui/agent",
        data={
            "message": "20 yox, 5 nəfər göstər.",
            "csrf_token": csrf,
            "conversation_id": conversation_id,
        },
    )
    assert updated.status_code == 200
    new_path = re.search(r'action="(/ui/agent/drafts/[0-9a-f-]+/confirm)"', updated.text)
    assert new_path is not None and new_path.group(1) != old_path
    assert updated.text.count(new_path.group(1)) == 1
    reloaded = await client.get(f"/ui/agent?conversation={conversation_id}")
    assert reloaded.status_code == 200
    assert old_path not in reloaded.text
    assert reloaded.text.count(new_path.group(1)) == 1
    context = await db_session.scalar(
        select(AgentConversationSessionContext).where(
            AgentConversationSessionContext.conversation_id == uuid.UUID(conversation_id)
        )
    )
    assert context is not None
    assert context.active_pending_draft_id == uuid.UUID(new_path.group(1).split("/")[-2])


async def test_foreign_and_missing_selectors_are_indistinguishable_and_audited(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, _membership = tenant_and_user
    tenant_id = tenant.id
    other_user = await create_user(
        db_session, username=f"hr-{uuid.uuid4().hex[:8]}", plaintext_password="correct-horse-9"
    )
    other_membership = await create_membership(
        db_session, user_id=other_user.id, tenant_id=tenant.id, role=ROLE_HR_USER
    )
    colleague = await create_conversation(
        db_session,
        owner=OwnerPrincipal(
            tenant_id=tenant.id, user_id=other_user.id, membership_id=other_membership.id
        ),
    )
    colleague.turns = [{"role": "user", "text": "Həmkarın gizli sorğusu"}]
    foreign_tenant = await create_tenant(db_session, name=f"Other-{uuid.uuid4().hex[:8]}")
    foreign_user = await create_user(
        db_session, username=f"hr-{uuid.uuid4().hex[:8]}", plaintext_password="correct-horse-9"
    )
    foreign_membership = await create_membership(
        db_session, user_id=foreign_user.id, tenant_id=foreign_tenant.id, role=ROLE_HR_USER
    )
    foreign = await create_conversation(
        db_session,
        owner=OwnerPrincipal(
            tenant_id=foreign_tenant.id,
            user_id=foreign_user.id,
            membership_id=foreign_membership.id,
        ),
    )
    await db_session.commit()
    app.dependency_overrides[get_llm_provider] = lambda: GREETING
    csrf = await _login_and_csrf(client, user.username, password)

    bodies = []
    for conversation_id in (colleague.id, foreign.id, uuid.uuid4()):
        response = await client.get(f"/ui/agent?conversation={conversation_id}")
        assert response.status_code == 404
        assert "Həmkarın gizli sorğusu" not in response.text
        bodies.append(response.text)
        post = await client.post(
            "/ui/agent",
            data={"message": "salam", "csrf_token": csrf, "conversation_id": str(conversation_id)},
        )
        assert post.status_code == 404
        bodies.append(post.text)
    assert len(set(bodies)) == 1

    await db_session.refresh(colleague)
    await db_session.refresh(foreign)
    assert colleague.turns == [{"role": "user", "text": "Həmkarın gizli sorğusu"}]
    assert foreign.turns == []
    rejected = (
        await db_session.scalars(
            select(AuditEvent).where(
                AuditEvent.event_type == "agent.conversation.access_rejected"
            )
        )
    ).all()
    assert len(rejected) == 6
    assert all(event.event_metadata == {"reason_code": "NOT_FOUND"} for event in rejected)
    assert all(event.tenant_id == tenant_id for event in rejected)
    malformed = await client.get("/ui/agent?conversation=not-a-uuid")
    assert malformed.status_code in (404, 422)


async def test_membership_or_user_revocation_blocks_conversation_access_immediately(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, membership = tenant_and_user
    app.dependency_overrides[get_llm_provider] = lambda: GREETING
    csrf = await _login_and_csrf(client, user.username, password)
    page = await client.get("/ui/agent")
    conversation_id = _conversation_id(page.text)
    await client.post(
        "/ui/agent",
        data={"message": "Salam", "csrf_token": csrf, "conversation_id": conversation_id},
    )

    for table, key, row_id in (
        (TenantMembership.__table__, TenantMembership.id, membership.id),
        (User.__table__, User.id, user.id),
    ):
        await db_session.execute(table.update().where(key == row_id).values(is_active=False))
        await db_session.commit()
        for response in (
            await client.get("/ui/agent", follow_redirects=False),
            await client.get(f"/ui/agent?conversation={conversation_id}", follow_redirects=False),
            await client.post(
                "/ui/agent",
                data={"message": "x", "csrf_token": csrf, "conversation_id": conversation_id},
                follow_redirects=False,
            ),
        ):
            assert response.status_code == 303
            assert response.headers["location"] == "/ui/login"
            assert "Salam" not in response.text
        await db_session.execute(table.update().where(key == row_id).values(is_active=True))
        await db_session.commit()
        csrf = await _login_and_csrf(client, user.username, password)

    rows = await _conversations(db_session, tenant.id)
    assert len(rows) == 1
    assert [turn["role"] for turn in rows[0].turns] == ["user", "assistant"]
    restored = await client.get(f"/ui/agent?conversation={conversation_id}")
    assert restored.status_code == 200 and "Salam" in restored.text

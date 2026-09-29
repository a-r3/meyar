"""Issue #80 workspace presentation over durable, owner-scoped history."""

import re
import uuid
from datetime import UTC, datetime

from fakes import FakeLLMProvider
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from test_ui_agent_routes import _login_and_csrf, local_ui_settings  # noqa: F401

from meyar.agent.schemas import AgentActionType, AgentDecision, AgentResponseCode
from meyar.config import Settings
from meyar.llm.dependency import get_llm_provider
from meyar.main import app
from meyar.services.agent_conversation_repo import (
    ConversationSummary,
    OwnerPrincipal,
    create_conversation,
)
from meyar.ui.agent_workspace import history_item_view


def _selected_id(html: str) -> str:
    match = re.search(r'name="conversation_id" value="([0-9a-f-]{36})"', html)
    assert match is not None
    return match.group(1)


def test_history_labels_are_closed_and_timestamps_are_localized() -> None:
    item = ConversationSummary(
        id=uuid.uuid4(), title_kind="CANDIDATE_SEARCH",
        updated_at=datetime(2026, 9, 29, 10, 32, tzinfo=UTC),
    )
    view = history_item_view(
        item, current_id=item.id, timezone="Asia/Baku",
        now=datetime(2026, 9, 29, 11, tzinfo=UTC),
    )
    assert view.title == "Namizəd axtarışı"
    assert view.updated_label == "Bu gün, 14:32"
    assert view.is_active
    assert history_item_view(
        ConversationSummary(item.id, "UNEXPECTED", item.updated_at),
        current_id=uuid.uuid4(), timezone="Asia/Baku",
        now=datetime(2026, 9, 30, tzinfo=UTC),
    ).title == "Ümumi söhbət"


async def test_workspace_has_one_composer_new_action_and_separate_history(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings,  # noqa: F811
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)
    empty = await client.get("/ui/agent")
    assert empty.status_code == 200
    assert empty.text.count('action="/ui/agent/reset"') == 1
    assert empty.text.count('id="agent-composer"') == 1
    assert empty.text.count('name="message"') == 1
    assert "Söhbətlər" in empty.text and "Namizədlər" in empty.text
    assert 'href="/ui/jobs"' in empty.text
    assert 'aria-expanded="false"' in empty.text
    assert "Söhbətə başlayın" in empty.text
    first = _selected_id(empty.text)
    invalid = await client.post("/ui/agent/reset", data={"csrf_token": "bad"})
    assert invalid.status_code == 403

    app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider(
        agent_decision=AgentDecision(
            action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.GREETING
        )
    )
    await client.post(
        "/ui/agent", data={"csrf_token": csrf, "conversation_id": first, "message": "Məxfi sorğu"}
    )
    new = await client.post("/ui/agent/reset", data={"csrf_token": csrf})
    second = new.headers["location"].split("=")[-1]
    assert second != first
    page = await client.get(new.headers["location"])
    assert 'aria-current="page"' in page.text
    assert "Ümumi söhbət" in page.text
    assert "Məxfi sorğu" not in page.text
    old = await client.get(f"/ui/agent?conversation={first}")
    assert "Məxfi sorğu" in old.text
    assert _selected_id(old.text) == first
    assert old.text.count('aria-current="page"') == 1


async def test_sidebar_history_is_bounded_and_loads_no_transcript_column(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,  # noqa: F811
) -> None:
    tenant, user, password, membership = tenant_and_user
    owner = OwnerPrincipal(tenant.id, user.id, membership.id)
    for _ in range(24):
        await create_conversation(db_session, owner=owner)
    await db_session.commit()
    await _login_and_csrf(client, user.username, password)
    sql: list[str] = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        if "FROM agent_conversations" in statement and "OFFSET" in statement:
            sql.append(statement)

    event.listen(db_session.bind.sync_engine, "before_cursor_execute", capture)
    try:
        first = await client.get("/ui/agent?page=1")
        second = await client.get("/ui/agent?page=2")
    finally:
        event.remove(db_session.bind.sync_engine, "before_cursor_execute", capture)
    assert first.status_code == second.status_code == 200
    assert first.text.count('class="agent-sidebar__item') == 20
    assert second.text.count('class="agent-sidebar__item') >= 4
    assert "Növbəti" in first.text and "Əvvəlki" in second.text
    assert sql and all("agent_conversations.turns" not in statement for statement in sql)
    malformed = await client.get("/ui/agent?page=wrong")
    assert malformed.status_code == 422

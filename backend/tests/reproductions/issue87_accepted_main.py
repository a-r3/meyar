"""Synthetic vulnerability reproductions against immutable accepted main."""

import re
import uuid

import pytest
from agent_plans import converse, vacancy_proposal
from fakes import FakeLLMProvider
from sqlalchemy import select
from test_demo_human_login_e2e import _seed

from meyar.agent.schemas import (
    JDCriteriaDraft,
    JDDraftCriterionItem,
)
from meyar.config import Settings, get_settings
from meyar.llm.dependency import get_llm_provider
from meyar.main import app
from meyar.models.agent_conversation import AgentConversation, AgentConversationSessionContext
from meyar.models.audit_event import AuditEvent
from meyar.services.tenant_membership_repo import set_membership_active
from meyar.services.user_repo import set_password, set_user_active


@pytest.fixture
def local_ui():
    app.dependency_overrides[get_settings] = lambda: Settings(ui_cookie_secure=False)


def hidden(page, name):
    match = re.search(r'name="' + name + r'" value="([^"]+)"', page)
    assert match is not None
    return match.group(1)


async def login(client, username, password):
    response = await client.post(
        "/ui/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert response.status_code == 303


@pytest.mark.parametrize("change", ["password", "user", "membership"])
async def test_abc_old_cookie_survives_security_change(
    client, db_session, tenant_and_user, local_ui, change
):
    _, user, password, membership = tenant_and_user
    await login(client, user.username, password)
    cookie = client.cookies.get("meyar_ui_session")
    user_id, membership_id = user.id, membership.id
    if change == "password":
        await set_password(db_session, user_id=user_id, plaintext_password="new-synthetic-password")
    elif change == "user":
        await set_user_active(db_session, user_id=user_id, is_active=False)
        await db_session.commit()
        assert (await client.get("/ui/agent", follow_redirects=False)).status_code == 303
        await set_user_active(db_session, user_id=user_id, is_active=True)
    else:
        await set_membership_active(db_session, membership_id=membership_id, is_active=False)
        await db_session.commit()
        assert (await client.get("/ui/agent", follow_redirects=False)).status_code == 303
        await set_membership_active(db_session, membership_id=membership_id, is_active=True)
    await db_session.commit()
    client.cookies.set("meyar_ui_session", cookie, path="/ui")
    assert (await client.get("/ui/agent")).status_code == 200


async def test_d_demo_rotation_old_cookie_survives(client, db_session, tmp_path, local_ui):
    first = await _seed(db_session, tmp_path)
    await db_session.commit()
    await login(client, first.human_username, first.human_temp_password)
    cookie = client.cookies.get("meyar_ui_session")
    second = await _seed(db_session, tmp_path)
    await db_session.commit()
    assert second.human_temp_password != first.human_temp_password
    client.cookies.set("meyar_ui_session", cookie, path="/ui")
    assert (await client.get("/ui/library")).status_code == 200


async def test_e_failed_login_has_no_durable_event(client, db_session, tenant_and_user, local_ui):
    _, user, _, _ = tenant_and_user
    response = await client.post(
        "/ui/login", data={"username": user.username, "password": "wrong-synthetic-password"}
    )
    assert response.status_code == 401
    assert (await db_session.scalars(select(AuditEvent))).all() == []


async def test_f_crlf_browser_valid_4000_rejected(client, tenant_and_user, local_ui):
    _, user, password, _ = tenant_and_user
    await login(client, user.username, password)
    page = await client.get("/ui/agent")
    message = "x\r\n" * 1000 + "y" * 2000
    assert len(message.replace("\r\n", "\n")) == 4000
    response = await client.post(
        "/ui/agent", data={"csrf_token": hidden(page.text, "csrf_token"), "message": message}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("jd", [False, True])
async def test_gh_completed_post_reexecutes(client, db_session, tenant_and_user, local_ui, jd):
    _, user, password, _ = tenant_and_user
    fake = FakeLLMProvider(
        agent_plan=vacancy_proposal()
        if jd
        else converse("ACKNOWLEDGEMENT"),
        jd_draft=JDCriteriaDraft(
            title="Backend",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind="SKILL",
                    requirement="Python",
                    source_text="Python tələb olunur",
                )
            ],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    await login(client, user.username, password)
    page = await client.get("/ui/agent")
    cid = uuid.UUID(hidden(page.text, "conversation_id"))
    data = {
        "csrf_token": hidden(page.text, "csrf_token"),
        "conversation_id": str(cid),
        "message": "Analyze this job description:\nBackend.\nPython tələb olunur."
        if jd
        else "Kömək et",
    }
    assert (await client.post("/ui/agent", data=data)).status_code == 200
    conversation = await db_session.get(AgentConversation, cid)
    await db_session.refresh(conversation)
    assert len(conversation.turns) == 2
    context = await db_session.scalar(
        select(AgentConversationSessionContext).where(
            AgentConversationSessionContext.conversation_id == cid
        )
    )
    d1 = context.active_pending_draft_id
    calls = fake.agent_call_count + fake.jd_draft_call_count
    assert (await client.post("/ui/agent", data=data)).status_code == 200
    await db_session.refresh(conversation)
    await db_session.refresh(context)
    assert len(conversation.turns) == 4
    assert fake.agent_call_count + fake.jd_draft_call_count > calls
    if jd:
        assert d1 is not None and context.active_pending_draft_id != d1

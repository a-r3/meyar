"""Synthetic route regressions for issue #87."""

import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fakes import FakeLLMProvider
from httpx import AsyncClient
from search_helpers import seed_candidate_with_profile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.schemas import (
    AgentActionType,
    AgentDecision,
    AgentResponseCode,
    JDCriteriaDraft,
    JDDraftCriterionItem,
)
from meyar.config import Settings, get_settings
from meyar.llm.dependency import get_llm_provider
from meyar.llm.provider import InferenceBusyError
from meyar.main import app
from meyar.models.agent_conversation import (
    AgentConversation,
    AgentConversationSessionContext,
)
from meyar.models.agent_result_set import AgentResultSet
from meyar.models.agent_turn_submission import AgentTurnSubmission
from meyar.models.auth_security_event import AuthSecurityEvent
from meyar.models.browser_session import BrowserSession
from meyar.search.planner_schemas import PlannerDraft
from meyar.search.schemas import RequiredFilters
from meyar.services.tenant_membership_repo import create_membership, set_membership_active
from meyar.services.tenant_repo import create_tenant
from meyar.services.user_repo import create_user, set_password, set_user_active


@pytest.fixture
def local_ui_settings() -> Settings:
    settings = Settings(ui_cookie_secure=False)
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


async def _login(client: AsyncClient, username: str, password: str) -> None:
    response = await client.post(
        "/ui/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert response.status_code == 303


@pytest.mark.parametrize("change", ["password", "user", "membership"])
async def test_security_change_never_revives_old_cookie(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings, change: str,
) -> None:
    _, user, password, membership = tenant_and_user
    await _login(client, user.username, password)
    old_cookie = client.cookies.get("meyar_ui_session")
    assert old_cookie is not None
    assert (await client.get("/ui/agent")).status_code == 200
    if change == "password":
        await set_password(db_session, user_id=user.id, plaintext_password="new-synthetic-password")
    elif change == "user":
        await set_user_active(db_session, user_id=user.id, is_active=False)
        await db_session.commit()
        assert (await client.get("/ui/agent", follow_redirects=False)).status_code == 303
        await set_user_active(db_session, user_id=user.id, is_active=True)
    else:
        await set_membership_active(db_session, membership_id=membership.id, is_active=False)
        await db_session.commit()
        assert (await client.get("/ui/agent", follow_redirects=False)).status_code == 303
        await set_membership_active(db_session, membership_id=membership.id, is_active=True)
    await db_session.commit()
    client.cookies.set("meyar_ui_session", old_cookie, path="/ui")
    assert (await client.get("/ui/agent", follow_redirects=False)).status_code == 303


async def test_revocation_scope_preserves_other_user_and_fresh_login_recovers(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, membership = tenant_and_user
    other = await create_user(
        db_session, username=f"other-{uuid.uuid4()}",
        plaintext_password="other-synthetic-password",
    )
    await create_membership(
        db_session, user_id=other.id, tenant_id=tenant.id, role="HR_USER"
    )
    await db_session.commit()
    own_username, own_user_id, own_membership_id = user.username, user.id, membership.id
    other_username = other.username
    await _login(client, own_username, password)
    old_cookie = client.cookies.get("meyar_ui_session")
    assert old_cookie is not None
    await _login(client, other_username, "other-synthetic-password")
    other_cookie = client.cookies.get("meyar_ui_session")
    assert other_cookie is not None
    await set_password(
        db_session, user_id=own_user_id, plaintext_password="new-synthetic-password"
    )
    await set_membership_active(
        db_session, membership_id=own_membership_id, is_active=False
    )
    await db_session.commit()
    client.cookies.set("meyar_ui_session", other_cookie, path="/ui")
    assert (await client.get("/ui/agent")).status_code == 200
    await set_membership_active(
        db_session, membership_id=own_membership_id, is_active=True
    )
    await db_session.commit()
    client.cookies.set("meyar_ui_session", old_cookie, path="/ui")
    assert (await client.get("/ui/agent", follow_redirects=False)).status_code == 303
    fresh = await client.post(
        "/ui/login",
        data={"username": own_username, "password": "new-synthetic-password"},
        follow_redirects=False,
    )
    assert fresh.status_code == 303
    assert fresh.cookies.get("meyar_ui_session") != old_cookie


async def test_crlf_4000_is_accepted(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings,
) -> None:
    _, user, password, _ = tenant_and_user
    app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider(
        agent_decision=AgentDecision(
            action=AgentActionType.FINAL_ANSWER,
            response_code=AgentResponseCode.ACKNOWLEDGEMENT,
        )
    )
    await _login(client, user.username, password)
    page = await client.get("/ui/agent")
    csrf = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page.text)
    assert csrf is not None
    message = "x\r\n" * 1000 + "y" * 2000
    assert len(message.replace("\r\n", "\n")) == 4000
    response = await client.post(
        "/ui/agent", data={"csrf_token": csrf.group(1), "message": message}
    )
    assert response.status_code == 200


async def test_membership_revocation_preserves_same_users_other_tenant_session(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    _, user, password, first_membership = tenant_and_user
    username, first_membership_id = user.username, first_membership.id
    await _login(client, username, password)
    first_cookie = client.cookies.get("meyar_ui_session")
    assert first_cookie is not None
    second_tenant = await create_tenant(db_session, name=f"Synthetic-B-{uuid.uuid4()}")
    second_membership = await create_membership(
        db_session, user_id=user.id, tenant_id=second_tenant.id, role="HR_USER",
    )
    second_membership_id = second_membership.id
    await db_session.commit()
    pending = await client.post(
        "/ui/login", data={"username": username, "password": password},
    )
    chosen = await client.post(
        "/ui/login/select-tenant", data={
            "token": _hidden(pending.text, "token"),
            "membership_id": str(second_membership_id),
        }, follow_redirects=False,
    )
    assert chosen.status_code == 303
    await set_membership_active(
        db_session, membership_id=first_membership_id, is_active=False,
    )
    await set_membership_active(
        db_session, membership_id=first_membership_id, is_active=True,
    )
    await db_session.commit()
    assert (await client.get("/ui/agent")).status_code == 200
    client.cookies.set("meyar_ui_session", first_cookie, path="/ui")
    assert (await client.get("/ui/agent", follow_redirects=False)).status_code == 303


async def test_classic_search_uses_same_canonical_lf_limit(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings,
) -> None:
    _, user, password, _ = tenant_and_user
    fake = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"]))
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    await _login(client, user.username, password)
    home = await client.get("/ui")
    csrf = _hidden(home.text, "csrf_token")
    exactly = "x\r\n" * 1000 + "y" * 2000
    accepted = await client.post(
        "/ui/search", data={"csrf_token": csrf, "query": exactly}
    )
    assert accepted.status_code == 200
    too_long = exactly + "z"
    rejected = await client.post(
        "/ui/search", data={"csrf_token": csrf, "query": too_long}
    )
    assert rejected.status_code == 422
    assert "Çıxış" in rejected.text


async def test_prohibited_control_character_still_fails_before_planner_model(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings,
) -> None:
    _, user, password, _ = tenant_and_user
    fake = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"]))
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    await _login(client, user.username, password)
    home = await client.get("/ui")
    response = await client.post(
        "/ui/search", data={
            "csrf_token": _hidden(home.text, "csrf_token"),
            "query": "Python\x01",
        },
    )
    assert response.status_code == 200
    assert fake.call_count == 0


async def test_lf_and_crlf_agent_requests_have_same_hash_and_transcript_text(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    _, user, password, _ = tenant_and_user
    fake = FakeLLMProvider(agent_decision=AgentDecision(
        action=AgentActionType.FINAL_ANSWER,
        response_code=AgentResponseCode.ACKNOWLEDGEMENT,
    ))
    app.dependency_overrides[get_llm_provider] = lambda: fake
    await _login(client, user.username, password)
    first_page = await client.get("/ui/agent")
    conversation_id = _hidden(first_page.text, "conversation_id")
    first_token = _hidden(first_page.text, "submission_id")
    base = {
        "csrf_token": _hidden(first_page.text, "csrf_token"),
        "conversation_id": conversation_id,
    }
    lf = "Mənə kömək et\nx"
    assert (await client.post("/ui/agent", data={
        **base, "submission_id": first_token, "message": lf,
    })).status_code == 200
    second_page = await client.get(f"/ui/agent?conversation={conversation_id}")
    second_token = _hidden(second_page.text, "submission_id")
    assert (await client.post("/ui/agent", data={
        **base, "submission_id": second_token, "message": lf.replace("\n", "\r\n"),
    })).status_code == 200
    first_row = await db_session.get(AgentTurnSubmission, uuid.UUID(first_token))
    second_row = await db_session.get(AgentTurnSubmission, uuid.UUID(second_token))
    assert first_row is not None and second_row is not None
    assert first_row.request_sha256 == second_row.request_sha256
    conversation = await db_session.get(AgentConversation, uuid.UUID(conversation_id))
    assert conversation is not None
    await db_session.refresh(conversation)
    assert [turn["text"] for turn in conversation.turns if turn["role"] == "user"] == [
        lf, lf,
    ]


def _hidden(html: str, name: str) -> str:
    match = re.search(rf'name="{name}" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


async def test_completed_submission_replay_and_altered_bytes_fail_closed(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    _, user, password, _ = tenant_and_user
    fake = FakeLLMProvider(agent_decision=AgentDecision(
        action=AgentActionType.FINAL_ANSWER,
        response_code=AgentResponseCode.ACKNOWLEDGEMENT,
    ))
    app.dependency_overrides[get_llm_provider] = lambda: fake
    await _login(client, user.username, password)
    page = await client.get("/ui/agent")
    data = {
        "csrf_token": _hidden(page.text, "csrf_token"),
        "conversation_id": _hidden(page.text, "conversation_id"),
        "submission_id": _hidden(page.text, "submission_id"),
        "message": "Mənə kömək et",
    }
    first = await client.post("/ui/agent", data=data, follow_redirects=False)
    assert first.status_code == 200
    conversation_id = uuid.UUID(data["conversation_id"])
    conversation = await db_session.get(AgentConversation, conversation_id)
    assert conversation is not None
    await db_session.refresh(conversation)
    first_turns = list(conversation.turns)
    calls = fake.agent_call_count
    completed = await db_session.get(AgentTurnSubmission, uuid.UUID(data["submission_id"]))
    assert completed is not None and completed.status == "COMPLETED"
    assert completed.completed_turn_version == conversation.turn_version
    replay = await client.post("/ui/agent", data=data, follow_redirects=False)
    assert replay.status_code == 303
    await db_session.refresh(conversation)
    assert conversation.turns == first_turns
    assert fake.agent_call_count == calls
    assert 'data-completed-url=' in first.text
    altered = await client.post(
        "/ui/agent", data={**data, "message": "Başqa bir istək"}, follow_redirects=False
    )
    assert altered.status_code in {403, 404}
    await db_session.refresh(conversation)
    assert conversation.turns == first_turns
    assert fake.agent_call_count == calls
    fresh = await client.get(f"/ui/agent?conversation={conversation_id}")
    new_data = {**data, "submission_id": _hidden(fresh.text, "submission_id")}
    second = await client.post("/ui/agent", data=new_data, follow_redirects=False)
    assert second.status_code == 200
    await db_session.refresh(conversation)
    assert len(conversation.turns) == len(first_turns) + 2


async def test_jd_draft_completed_replay_does_not_supersede_pending_pointer(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    _, user, password, _ = tenant_and_user
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title="Backend",
            must_have=[JDDraftCriterionItem(
                span_id="req-0001", kind="SKILL", requirement="Python",
                source_text="Python tələb olunur",
            )],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    await _login(client, user.username, password)
    page = await client.get("/ui/agent")
    data = {
        "csrf_token": _hidden(page.text, "csrf_token"),
        "conversation_id": _hidden(page.text, "conversation_id"),
        "submission_id": _hidden(page.text, "submission_id"),
        "message": "Analyze this job description:\nBackend.\nPython tələb olunur.",
    }
    first = await client.post("/ui/agent", data=data, follow_redirects=False)
    assert first.status_code == 200
    conversation_id = uuid.UUID(data["conversation_id"])
    conversation = await db_session.get(AgentConversation, conversation_id)
    assert conversation is not None
    context = await db_session.scalar(select(AgentConversationSessionContext).where(
        AgentConversationSessionContext.conversation_id == conversation_id
    ))
    assert context is not None and context.active_pending_draft_id is not None
    d1 = context.active_pending_draft_id
    await db_session.refresh(conversation)
    first_turns = list(conversation.turns)
    calls = fake.jd_draft_call_count
    replay = await client.post("/ui/agent", data=data, follow_redirects=False)
    assert replay.status_code == 303
    await db_session.refresh(context)
    await db_session.refresh(conversation)
    assert context.active_pending_draft_id == d1
    assert conversation.turns == first_turns
    assert fake.jd_draft_call_count == calls
    fresh = await client.get(f"/ui/agent?conversation={conversation_id}")
    second = await client.post(
        "/ui/agent", data={**data, "submission_id": _hidden(fresh.text, "submission_id")},
        follow_redirects=False,
    )
    assert second.status_code == 200
    await db_session.refresh(context)
    await db_session.refresh(conversation)
    assert len(conversation.turns) == len(first_turns) + 2
    assert context.active_pending_draft_id is not None
    assert context.active_pending_draft_id != d1


async def test_search_submission_replay_does_not_create_second_result_set(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _ = tenant_and_user
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id,
        profile_content={
            "skills": [{"name": "Python", "category": None, "evidence": [
                {"page": 1, "block_index": 0, "quote": "Synthetic Python evidence"}
            ]}],
            "employment_history": [], "education": [], "certifications": [],
            "languages": [], "projects": [],
        },
    )
    await db_session.commit()
    fake = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query="Python bilən namizədləri göstər",
            ),
            AgentDecision(
                action=AgentActionType.FINAL_ANSWER,
                response_code=AgentResponseCode.ACKNOWLEDGEMENT,
            ),
        ],
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    await _login(client, user.username, password)
    page = await client.get("/ui/agent")
    data = {
        "csrf_token": _hidden(page.text, "csrf_token"),
        "conversation_id": _hidden(page.text, "conversation_id"),
        "submission_id": _hidden(page.text, "submission_id"),
        "message": "Python bilən namizədləri göstər",
    }
    first = await client.post("/ui/agent", data=data, follow_redirects=False)
    assert first.status_code == 200
    count = await db_session.scalar(select(func.count()).select_from(AgentResultSet))
    assert count == 1
    calls = fake.call_count + fake.agent_call_count
    replay = await client.post("/ui/agent", data=data, follow_redirects=False)
    assert replay.status_code == 303
    assert await db_session.scalar(select(func.count()).select_from(AgentResultSet)) == count
    assert fake.call_count + fake.agent_call_count == calls


async def test_invalid_csrf_does_not_consume_issued_submission(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    _, user, password, _ = tenant_and_user
    fake = FakeLLMProvider(agent_decision=AgentDecision(
        action=AgentActionType.FINAL_ANSWER,
        response_code=AgentResponseCode.ACKNOWLEDGEMENT,
    ))
    app.dependency_overrides[get_llm_provider] = lambda: fake
    await _login(client, user.username, password)
    page = await client.get("/ui/agent")
    data = {
        "csrf_token": "invalid", "conversation_id": _hidden(page.text, "conversation_id"),
        "submission_id": _hidden(page.text, "submission_id"), "message": "Kömək et",
    }
    bad = await client.post("/ui/agent", data=data, follow_redirects=False)
    assert bad.status_code == 403
    assert fake.agent_call_count == 0
    submission = await db_session.get(AgentTurnSubmission, uuid.UUID(data["submission_id"]))
    conversation = await db_session.get(AgentConversation, uuid.UUID(data["conversation_id"]))
    assert submission is not None and submission.status == "ISSUED"
    assert submission.request_sha256 is None and submission.reservation_id is None
    assert conversation is not None and conversation.active_turn_id is None
    good = await client.post(
        "/ui/agent", data={**data, "csrf_token": _hidden(page.text, "csrf_token")},
        follow_redirects=False,
    )
    assert good.status_code == 200


async def test_busy_attempt_abandons_old_token_and_renders_safe_retry_token(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    _, user, password, _ = tenant_and_user
    app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider(
        agent_error=InferenceBusyError("QUEUE_FULL")
    )
    await _login(client, user.username, password)
    page = await client.get("/ui/agent")
    data = {
        "csrf_token": _hidden(page.text, "csrf_token"),
        "conversation_id": _hidden(page.text, "conversation_id"),
        "submission_id": _hidden(page.text, "submission_id"),
        "message": "Mənə kömək et",
    }
    busy = await client.post("/ui/agent", data=data, follow_redirects=False)
    assert busy.status_code == 503
    retry_token = _hidden(busy.text, "submission_id")
    assert retry_token != data["submission_id"]
    conversation = await db_session.get(
        AgentConversation, uuid.UUID(data["conversation_id"])
    )
    assert conversation is not None and conversation.turns == []
    app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider(
        agent_decision=AgentDecision(
            action=AgentActionType.FINAL_ANSWER,
            response_code=AgentResponseCode.ACKNOWLEDGEMENT,
        )
    )
    old = await client.post("/ui/agent", data=data, follow_redirects=False)
    assert old.status_code in {403, 404}
    new = await client.post(
        "/ui/agent", data={**data, "submission_id": retry_token},
        follow_redirects=False,
    )
    assert new.status_code == 200
    await db_session.refresh(conversation)
    assert len(conversation.turns) == 2


async def test_failed_login_audit_has_no_unknown_identifier_or_secret(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    _, user, _, _ = tenant_and_user
    known_username = user.username
    known_user_id = user.id
    unknown = "unknown-synthetic-identifier"
    secret = "wrong-synthetic-password"
    a = await client.post("/ui/login", data={"username": unknown, "password": secret})
    b = await client.post("/ui/login", data={"username": known_username, "password": secret})
    assert a.status_code == b.status_code == 401
    assert a.text == b.text
    events = (await db_session.scalars(select(AuthSecurityEvent))).all()
    assert len(events) == 2
    assert events[0].user_id is None
    assert events[1].user_id == known_user_id
    persisted = repr([
        {column.name: getattr(e, column.name) for column in AuthSecurityEvent.__table__.columns}
        for e in events
    ])
    assert unknown not in persisted and secret not in persisted
    assert set(AuthSecurityEvent.__table__.columns.keys()) == {
        "id", "outcome_code", "user_id", "created_at",
    }


async def test_disabled_user_and_bad_pending_token_are_audited_with_generic_copy(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    _, user, password, membership = tenant_and_user
    username, user_id, membership_id = user.username, user.id, membership.id
    unknown = await client.post(
        "/ui/login", data={"username": "unknown-audit-fixture", "password": password}
    )
    await set_user_active(db_session, user_id=user_id, is_active=False)
    await db_session.commit()
    disabled = await client.post(
        "/ui/login", data={"username": username, "password": password}
    )
    assert disabled.status_code == 401 and disabled.text == unknown.text
    bad_token = await client.post(
        "/ui/login/select-tenant",
        data={"token": "invalid-synthetic-token", "membership_id": str(membership_id)},
    )
    assert bad_token.status_code == 401
    assert "İstifadəçi adı və ya parol yanlışdır" in bad_token.text
    events = (await db_session.scalars(select(AuthSecurityEvent))).all()
    assert [e.outcome_code for e in events] == [
        "LOGIN_REJECTED", "LOGIN_REJECTED", "PENDING_TOKEN_INVALID"
    ]
    assert [e.user_id for e in events] == [None, user_id, None]


async def test_over_limit_agent_message_preserves_canonical_text_and_auth_chrome(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    _, user, password, _ = tenant_and_user
    fake = FakeLLMProvider(agent_decision=AgentDecision(
        action=AgentActionType.FINAL_ANSWER,
        response_code=AgentResponseCode.ACKNOWLEDGEMENT,
    ))
    app.dependency_overrides[get_llm_provider] = lambda: fake
    await _login(client, user.username, password)
    page = await client.get("/ui/agent")
    message = "x\r\n" * 1000 + "y" * 2001
    assert len(message.replace("\r\n", "\n")) == 4001
    response = await client.post(
        "/ui/agent", data={
            "message": message, "csrf_token": _hidden(page.text, "csrf_token"),
            "conversation_id": _hidden(page.text, "conversation_id"),
            "submission_id": _hidden(page.text, "submission_id"),
        }, follow_redirects=False,
    )
    assert response.status_code == 422
    assert "Mətn 1–4000 simvol olmalıdır" in response.text
    assert message.replace("\r\n", "\n") in response.text
    assert 'name="submission_id"' in response.text
    assert "MEYAR AI" in response.text
    assert fake.agent_call_count == 0
    conversation = await db_session.get(
        AgentConversation, uuid.UUID(_hidden(page.text, "conversation_id"))
    )
    assert conversation is not None and conversation.turns == []


async def test_empty_agent_message_uses_canonical_validation_and_fresh_composer(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings,
) -> None:
    _, user, password, _ = tenant_and_user
    fake = FakeLLMProvider(agent_decision=AgentDecision(
        action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT,
    ))
    app.dependency_overrides[get_llm_provider] = lambda: fake
    await _login(client, user.username, password)
    page = await client.get("/ui/agent")
    response = await client.post("/ui/agent", data={
        "csrf_token": _hidden(page.text, "csrf_token"),
        "conversation_id": _hidden(page.text, "conversation_id"),
        "submission_id": _hidden(page.text, "submission_id"), "message": "",
    }, follow_redirects=False)
    assert response.status_code == 422
    assert "Mətn 1–4000 simvol olmalıdır" in response.text and "Çıxış" in response.text
    assert _hidden(response.text, "submission_id") != _hidden(page.text, "submission_id")
    assert fake.agent_call_count == 0


async def test_other_session_cannot_use_submission_token(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from conftest import BrowserTestClient
    from httpx import ASGITransport

    _, user, password, _ = tenant_and_user
    await _login(client, user.username, password)
    page = await client.get("/ui/agent")
    token = _hidden(page.text, "submission_id")
    async with BrowserTestClient(transport=ASGITransport(app=app), base_url="http://test") as other:
        await _login(other, user.username, password)
        other_page = await other.get("/ui/agent")
        wrong = await other.post(
            "/ui/agent", data={
                "message": "Kömək et", "csrf_token": _hidden(other_page.text, "csrf_token"),
                "conversation_id": _hidden(other_page.text, "conversation_id"),
                "submission_id": token,
            }, follow_redirects=False,
        )
        assert wrong.status_code in {403, 404}


async def test_global_ui_validation_uses_live_session_authority(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings,
) -> None:
    _, user, password, _ = tenant_and_user
    public_error = await client.get("/ui/jobs/not-a-uuid/ranking")
    assert public_error.status_code == 303
    assert 'name="csrf_token"' not in public_error.text
    await _login(client, user.username, password)
    private_error = await client.get("/ui/jobs/not-a-uuid/ranking")
    assert private_error.status_code == 422
    assert "Çıxış" in private_error.text


@pytest.mark.parametrize("change", ["password", "user", "membership"])
async def test_pending_login_security_stamp_blocks_old_selection(
    client: AsyncClient, db_session: AsyncSession, local_ui_settings: Settings,
    change: str,
) -> None:
    first_tenant = await create_tenant(db_session, name=f"Synthetic-A-{uuid.uuid4()}")
    second_tenant = await create_tenant(db_session, name=f"Synthetic-B-{uuid.uuid4()}")
    user = await create_user(
        db_session, username=f"synthetic-{uuid.uuid4()}",
        plaintext_password="synthetic-password-1",
    )
    first_membership = await create_membership(
        db_session, user_id=user.id, tenant_id=first_tenant.id, role="HR_USER"
    )
    await create_membership(
        db_session, user_id=user.id, tenant_id=second_tenant.id, role="HR_USER"
    )
    await db_session.commit()
    username = user.username
    user_id = user.id
    first_membership_id = first_membership.id
    login = await client.post(
        "/ui/login", data={"username": username, "password": "synthetic-password-1"}
    )
    assert login.status_code == 200
    token = _hidden(login.text, "token")
    if change == "password":
        await set_password(db_session, user_id=user_id, plaintext_password="synthetic-password-2")
    elif change == "user":
        await set_user_active(db_session, user_id=user_id, is_active=False)
        await set_user_active(db_session, user_id=user_id, is_active=True)
    else:
        await set_membership_active(db_session, membership_id=first_membership_id, is_active=False)
        await set_membership_active(db_session, membership_id=first_membership_id, is_active=True)
    await db_session.commit()
    selected = await client.post(
        "/ui/login/select-tenant",
        data={"token": token, "membership_id": str(first_membership_id)},
        follow_redirects=False,
    )
    assert selected.status_code == 401
    assert selected.cookies.get("meyar_ui_session") is None
    assert (await db_session.scalars(select(BrowserSession))).all() == []


async def test_malformed_membership_and_expired_pending_token_are_audited(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings, monkeypatch,
) -> None:
    from meyar.ui import pending_login

    _, user, _, membership = tenant_and_user
    user_id, membership_id = user.id, membership.id
    token = pending_login.issue_pending_login_token(
        secret=local_ui_settings.pending_login_secret, user_id=user.id,
        user_security_version=user.security_version,
        membership_versions={membership.id: membership.security_version},
    )
    malformed = await client.post(
        "/ui/login/select-tenant", data={"token": token, "membership_id": "not-a-uuid"},
        follow_redirects=False,
    )
    assert malformed.status_code == 401
    now = pending_login.time.time()
    monkeypatch.setattr(pending_login.time, "time", lambda: now + 301)
    expired = await client.post(
        "/ui/login/select-tenant", data={"token": token, "membership_id": str(membership_id)},
        follow_redirects=False,
    )
    assert expired.status_code == 401 and expired.text == malformed.text
    events = (await db_session.scalars(select(AuthSecurityEvent))).all()
    assert [e.outcome_code for e in events] == [
        "TENANT_SELECTION_INVALID", "PENDING_TOKEN_INVALID",
    ]
    assert [e.user_id for e in events] == [user_id, None]

    non_ascii = await client.post(
        "/ui/login/select-tenant",
        data={"token": token.rsplit(":", 1)[0] + ":ü", "membership_id": str(membership_id)},
        follow_redirects=False,
    )
    assert non_ascii.status_code == 401 and non_ascii.text == malformed.text
    events = (await db_session.scalars(select(AuthSecurityEvent))).all()
    assert events[-1].outcome_code == "PENDING_TOKEN_INVALID" and events[-1].user_id is None


async def test_token_cannot_select_another_owned_conversation(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings,
) -> None:
    _, user, password, _ = tenant_and_user
    fake = FakeLLMProvider(agent_decision=AgentDecision(
        action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT,
    ))
    app.dependency_overrides[get_llm_provider] = lambda: fake
    await _login(client, user.username, password)
    original = await client.get("/ui/agent")
    csrf = _hidden(original.text, "csrf_token")
    created = await client.post(
        "/ui/agent/reset", data={"csrf_token": csrf}, follow_redirects=False,
    )
    other = await client.get(created.headers["location"])
    data = {
        "csrf_token": csrf, "submission_id": _hidden(original.text, "submission_id"),
        "conversation_id": _hidden(other.text, "conversation_id"), "message": "Kömək et",
    }
    foreign_binding = await client.post("/ui/agent", data=data, follow_redirects=False)
    nonexistent = await client.post(
        "/ui/agent", data={**data, "submission_id": str(uuid.uuid4())},
        follow_redirects=False,
    )
    assert foreign_binding.status_code == nonexistent.status_code == 404
    assert foreign_binding.text == nonexistent.text
    assert fake.agent_call_count == 0


async def test_multi_tenant_password_rehash_claim_uses_committed_security_stamp(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from argon2 import PasswordHasher

    from meyar.core.password import needs_rehash
    from meyar.models.user import User

    _, user, password, membership = tenant_and_user
    user_id, username, membership_id = user.id, user.username, membership.id
    old_stamp = user.security_version
    user.password_hash = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1).hash(password)
    assert needs_rehash(user.password_hash)
    other_tenant = await create_tenant(db_session, name=f"Synthetic-Rehash-{uuid.uuid4()}")
    await create_membership(
        db_session, user_id=user_id, tenant_id=other_tenant.id, role="HR_USER",
    )
    await db_session.commit()
    pending = await client.post(
        "/ui/login", data={"username": username, "password": password},
    )
    assert pending.status_code == 200
    response = await client.post(
        "/ui/login/select-tenant", data={
            "token": _hidden(pending.text, "token"), "membership_id": str(membership_id),
        }, follow_redirects=False,
    )
    assert response.status_code == 303
    updated = await db_session.get(User, user_id, populate_existing=True)
    assert updated is not None and updated.security_version != old_stamp
    assert not needs_rehash(updated.password_hash)


async def test_crashed_uncommitted_submission_is_reclaimed_only_after_lease(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    _, user, password, _ = tenant_and_user
    fake = FakeLLMProvider(agent_decision=AgentDecision(
        action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT,
    ))
    app.dependency_overrides[get_llm_provider] = lambda: fake
    await _login(client, user.username, password)
    page = await client.get("/ui/agent")
    from meyar.services.agent_submission_repo import request_hash

    data = {
        "csrf_token": _hidden(page.text, "csrf_token"),
        "submission_id": _hidden(page.text, "submission_id"),
        "conversation_id": _hidden(page.text, "conversation_id"), "message": "Kömək et",
    }
    row = await db_session.get(AgentTurnSubmission, uuid.UUID(data["submission_id"]))
    conversation = await db_session.get(AgentConversation, uuid.UUID(data["conversation_id"]))
    assert row is not None and conversation is not None
    row.status = "PROCESSING"
    row.request_sha256 = request_hash(data["message"])
    row.reservation_id = uuid.uuid4()
    conversation.active_turn_id = row.reservation_id
    row.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    conversation.active_turn_expires_at = row.lease_expires_at
    await db_session.commit()
    assert (await client.post("/ui/agent", data=data, follow_redirects=False)).status_code == 409
    assert fake.agent_call_count == 0
    row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    conversation.active_turn_expires_at = row.lease_expires_at
    await db_session.commit()
    assert (await client.post("/ui/agent", data=data, follow_redirects=False)).status_code == 200
    calls = fake.agent_call_count
    assert calls == 1
    assert (await client.post("/ui/agent", data=data, follow_redirects=False)).status_code == 303
    assert fake.agent_call_count == calls


async def test_submission_retirement_keeps_live_processing_and_completed_provenance(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    _, user, password, _ = tenant_and_user
    fake = FakeLLMProvider(agent_decision=AgentDecision(
        action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT,
    ))
    app.dependency_overrides[get_llm_provider] = lambda: fake
    await _login(client, user.username, password)
    page = await client.get("/ui/agent")
    cid = uuid.UUID(_hidden(page.text, "conversation_id"))
    completed_id = uuid.UUID(_hidden(page.text, "submission_id"))
    assert (await client.post("/ui/agent", data={
        "csrf_token": _hidden(page.text, "csrf_token"), "conversation_id": str(cid),
        "submission_id": str(completed_id), "message": "Kömək et",
    })).status_code == 200
    completed = await db_session.get(AgentTurnSubmission, completed_id)
    conversation = await db_session.get(AgentConversation, cid)
    assert completed is not None and conversation is not None
    await db_session.refresh(conversation)
    turns = list(conversation.turns)
    fresh = await client.get(f"/ui/agent?conversation={cid}")
    live_id = uuid.UUID(_hidden(fresh.text, "submission_id"))
    live = await db_session.get(AgentTurnSubmission, live_id)
    assert live is not None
    completed.expires_at = datetime.now(UTC) - timedelta(days=1)
    live.expires_at = completed.expires_at
    live.status = "PROCESSING"
    live.reservation_id = uuid.uuid4()
    live.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    conversation.active_turn_id = live.reservation_id
    conversation.active_turn_expires_at = live.lease_expires_at
    await db_session.commit()
    assert (await client.get(f"/ui/agent?conversation={cid}")).status_code == 200
    assert await db_session.scalar(select(AgentTurnSubmission.id).where(
        AgentTurnSubmission.id == completed_id
    )) is None
    assert await db_session.scalar(select(AgentTurnSubmission.id).where(
        AgentTurnSubmission.id == live_id
    )) == live_id
    await db_session.refresh(conversation)
    assert conversation.turns == turns

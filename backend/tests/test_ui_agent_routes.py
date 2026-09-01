"""Slice 2 (issue #31) — HTTP-level tests for the MEYAR AI workspace
(/ui/agent). Complements tests/test_agent_service.py's direct
service-level coverage with auth/CSRF/session/template behavior."""

import re

import pytest
from fakes import FakeLLMProvider
from httpx import AsyncClient
from search_helpers import seed_candidate_with_profile
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.schemas import AgentActionType, AgentDecision
from meyar.config import Settings, get_settings
from meyar.llm.dependency import get_llm_provider
from meyar.main import app
from meyar.search.planner_schemas import PlannerDraft
from meyar.search.schemas import RequiredFilters

EMPTY_PROFILE = {
    "skills": [],
    "employment_history": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "projects": [],
}


def _profile(*skills: str) -> dict:
    return {
        **EMPTY_PROFILE,
        "skills": [
            {
                "name": skill,
                "category": "Backend",
                "evidence": [{"page": 1, "block_index": 0, "quote": "Synthetic evidence"}],
            }
            for skill in skills
        ],
    }


@pytest.fixture
def local_ui_settings() -> Settings:
    settings = Settings(ui_cookie_secure=False)
    app.dependency_overrides[get_settings] = lambda: settings
    return settings


async def _login_and_csrf(client: AsyncClient, username: str, password: str) -> str:
    response = await client.post(
        "/ui/login", data={"username": username, "password": password}, follow_redirects=False
    )
    assert response.status_code == 303
    home = await client.get("/ui")
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', home.text)
    assert match is not None
    return match.group(1)


async def test_agent_workspace_requires_authentication(
    client: AsyncClient, local_ui_settings: Settings
) -> None:
    response = await client.get("/ui/agent", follow_redirects=False)
    assert response.status_code == 303
    response = await client.post(
        "/ui/agent", data={"message": "salam", "csrf_token": "x"}, follow_redirects=False
    )
    assert response.status_code == 303


async def test_agent_nav_link_present_on_authenticated_pages(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    await _login_and_csrf(client, user.username, password)
    home = await client.get("/ui")
    assert 'href="/ui/agent"' in home.text
    assert "MEYAR AI" in home.text


async def test_search_candidates_turn_renders_grounded_results_not_model_text(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    fake = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query="Python bilən namizədləri göstər",
            ),
            AgentDecision(action=AgentActionType.FINAL_ANSWER, message="Budur nəticələr."),
        ],
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    response = await client.post(
        "/ui/agent", data={"message": "Python bilən namizədləri göstər", "csrf_token": csrf}
    )
    assert response.status_code == 200
    assert str(candidate.id) in response.text
    assert "Budur nəticələr." in response.text


async def test_multi_turn_ordinal_reference_over_http(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    fake_search = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query="Python bilən namizədləri göstər",
            ),
            AgentDecision(action=AgentActionType.FINAL_ANSWER, message="Budur nəticələr."),
        ],
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake_search
    csrf = await _login_and_csrf(client, user.username, password)
    first = await client.post(
        "/ui/agent", data={"message": "Python bilən namizədləri göstər", "csrf_token": csrf}
    )
    assert first.status_code == 200

    fake_profile = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1)
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake_profile
    second = await client.post(
        "/ui/agent", data={"message": "birincinin təcrübəsini izah et", "csrf_token": csrf}
    )
    assert second.status_code == 200
    assert str(candidate.id) in second.text


async def test_two_sessions_do_not_share_agent_conversation_state(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    fake_search = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query="Python bilən namizədləri göstər",
            ),
            AgentDecision(action=AgentActionType.FINAL_ANSWER, message="Budur nəticələr."),
        ],
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake_search
    csrf_a = await _login_and_csrf(client, user.username, password)
    await client.post(
        "/ui/agent", data={"message": "Python bilən namizədləri göstər", "csrf_token": csrf_a}
    )
    del candidate

    from httpx import ASGITransport

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as second_client:
        csrf_b = await _login_and_csrf(second_client, user.username, password)
        fake_profile = FakeLLMProvider(
            agent_decision=AgentDecision(
                action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1
            )
        )
        app.dependency_overrides[get_llm_provider] = lambda: fake_profile
        response = await second_client.post(
            "/ui/agent", data={"message": "birincini aç", "csrf_token": csrf_b}
        )
        # A fresh browser session has its own empty conversation — an
        # ordinal reference from session A's search must not resolve here.
        assert "Namizəd tapılmadı" in response.text


async def test_user_message_and_model_message_are_html_escaped_in_render(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    """Both the HR user's own typed text and the model's own framing
    message are untrusted for HTML purposes — Jinja autoescape (the same
    mechanism the rest of the UI relies on) must render them as inert
    text, never live markup."""
    _tenant, user, password, _membership = tenant_and_user
    payload = "<script>alert(1)</script>"
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.FINAL_ANSWER, message=payload)
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    response = await client.post(
        "/ui/agent", data={"message": f"salam {payload}", "csrf_token": csrf}
    )
    assert response.status_code == 200
    assert payload not in response.text
    # Escaped 3x: the user's own turn in history, the model's assistant
    # turn in history, and the outcome banner (which renders the same
    # model message) — never raw markup anywhere.
    assert response.text.count("&lt;script&gt;alert(1)&lt;/script&gt;") == 3


async def test_agent_turn_csrf_required(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    await _login_and_csrf(client, user.username, password)
    response = await client.post("/ui/agent", data={"message": "salam", "csrf_token": "wrong"})
    assert response.status_code == 403


async def test_existing_api_key_rest_search_unaffected_by_agent_slice(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Regression guard: adding the agent module must not disturb the
    unchanged machine API-key REST path."""
    from meyar.services.api_key_repo import create_api_key
    from meyar.services.tenant_repo import create_tenant

    tenant = await create_tenant(db_session, name="Regression tenant")
    _api_key, plaintext = await create_api_key(db_session, tenant_id=tenant.id, env="test")
    await db_session.commit()
    response = await client.get("/api/v1/usage", headers={"Authorization": f"Bearer {plaintext}"})
    assert response.status_code == 200


# --- D-036 regression tests: rendered-page assertions for the live-inspection
# bug report (empty assistant bubble + red "AI response could not be safely
# processed" error co-rendered with a valid, green, executed-search result
# and a misleading "Uyğunluq 0%" badge on an unscored discovery query). ---


async def test_successful_search_with_failed_framing_renders_no_contradictory_error(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    fake = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decision=AgentDecision(
            action=AgentActionType.SEARCH_CANDIDATES, search_query="Python bilən namizədləri göstər"
        ),
        agent_fail_after_n_calls=1,
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    response = await client.post(
        "/ui/agent", data={"message": "Python bilən namizədləri göstər", "csrf_token": csrf}
    )
    assert response.status_code == 200
    # The grounded result is present...
    assert str(candidate.id) in response.text
    # ...and there is no fatal "could not be safely processed" banner
    # anywhere alongside it — a real result must never co-render with a
    # scary top-level failure message.
    assert "təhlükəsiz şəkildə emal etmək mümkün olmadı" not in response.text
    assert 'data-outcome="MALFORMED_MODEL_OUTPUT"' not in response.text
    assert 'data-outcome="AGENT_PROVIDER_FAILURE"' not in response.text
    # No empty assistant bubble in the conversation history.
    assert '<p class="untrusted-text"></p>' not in response.text


async def test_unscored_structured_discovery_has_no_misleading_percentage(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    """A plain required-skill discovery query (STRUCTURED_ONLY, no
    preferred_filters) has no real relevance score to show — the card
    must present matched criteria, never a fabricated 'Uyğunluq 0%'."""
    tenant, user, password, _membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    fake = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query="Python bilən namizədləri göstər",
            ),
            AgentDecision(action=AgentActionType.FINAL_ANSWER, message="Budur nəticələr."),
        ],
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    response = await client.post(
        "/ui/agent", data={"message": "Python bilən namizədləri göstər", "csrf_token": csrf}
    )
    assert response.status_code == 200
    assert str(candidate.id) in response.text
    assert "Uyğunluq" not in response.text
    assert "Məcburi uyğunluqlar" in response.text


async def test_get_candidate_profile_success_never_renders_empty_bubble(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    fake_search = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query="Python bilən namizədləri göstər",
            ),
            AgentDecision(action=AgentActionType.FINAL_ANSWER, message="Budur nəticələr."),
        ],
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake_search
    csrf = await _login_and_csrf(client, user.username, password)
    await client.post(
        "/ui/agent", data={"message": "Python bilən namizədləri göstər", "csrf_token": csrf}
    )

    fake_profile = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1)
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake_profile
    response = await client.post("/ui/agent", data={"message": "birincini aç", "csrf_token": csrf})
    assert response.status_code == 200
    assert '<p class="untrusted-text"></p>' not in response.text
    assert str(candidate.id) in response.text

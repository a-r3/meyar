"""Slice 2 (issue #31) — HTTP-level tests for the MEYAR AI workspace
(/ui/agent). Complements tests/test_agent_service.py's direct
service-level coverage with auth/CSRF/session/template behavior."""

import asyncio
import re
import uuid

import pytest
from fakes import FakeLLMProvider
from httpx import AsyncClient
from pydantic import ValidationError
from search_helpers import seed_candidate_with_profile
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.schemas import AgentActionType, AgentDecision, AgentResponseCode
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
                "category": None,
                "evidence": [
                    {"page": 1, "block_index": 0, "quote": f"Synthetic evidence: {skill}"}
                ],
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


def _hidden_value(html: str, name: str) -> str:
    match = re.search(rf'name="{re.escape(name)}" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _draft_confirm_path(html: str) -> str:
    match = re.search(r'action="(/ui/agent/drafts/[0-9a-f-]+/confirm)"', html)
    assert match is not None
    return match.group(1)


def _python_confirmation_data(html: str, csrf: str, *, title: str = "Backend") -> dict[str, str]:
    return {
        "csrf_token": csrf,
        "title": title,
        "must_span_id_0": _hidden_value(html, "must_span_id_0"),
        "must_kind_0": "SKILL",
        "must_requirement_0": "Python",
        "must_min_years_0": "",
        "must_weight_0": "1",
    }


async def _render_python_draft(
    client: AsyncClient, *, username: str, password: str
) -> tuple[str, str, str]:
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem

    fake = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title="Backend",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind="SKILL",
                    requirement="Python",
                    source_text="Python required",
                )
            ],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, username, password)
    response = await client.post(
        "/ui/agent", data={"message": "Backend. Python required.", "csrf_token": csrf}
    )
    assert response.status_code == 200
    return csrf, _draft_confirm_path(response.text), response.text


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


async def test_primary_nav_is_agent_first_classic_tools_are_secondary(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    """Slice 4 (issue #33, D-030) + PR #42 owner corrections (D-043, D-044):
    normal HR navigation/discovery is exactly MEYAR AI | Namizədlər |
    Çıxış — no Vacancies, no classic-search secondary line. The backend
    job/ranking routes and the classic /ui search page still exist
    (D-043/D-044), just never discoverable from normal HR nav; a job is
    reached only via the agent's own JD-drafting confirmation, which
    lands directly on its ranking result."""
    _tenant, user, password, _membership = tenant_and_user
    await _login_and_csrf(client, user.username, password)
    page = await client.get("/ui/agent")
    primary_nav = re.search(r'<nav aria-label="Əsas naviqasiya">(.*?)</nav>', page.text, re.S)
    assert primary_nav is not None
    assert 'href="/ui/agent"' in primary_nav.group(1)
    assert 'href="/ui/library"' in primary_nav.group(1)
    assert 'href="/ui/jobs"' not in primary_nav.group(1)
    assert 'href="/ui"' not in primary_nav.group(1)
    # No secondary discovery nav at all any more — Vacancies and classic
    # search are both backend/supporting capability only (reachable by
    # direct URL), never surfaced as normal HR destinations.
    assert 'class="nav-secondary"' not in page.text
    assert 'href="/ui/jobs"' not in page.text
    assert 'href="/ui">' not in page.text


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
            AgentDecision(
                action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT
            ),
        ],
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    response = await client.post(
        "/ui/agent", data={"message": "Python bilən namizədləri göstər", "csrf_token": csrf}
    )
    assert response.status_code == 200
    assert str(candidate.id) in response.text
    assert "Python tələbinə uyğun 1 namizəd tapdım." in response.text


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
            AgentDecision(
                action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT
            ),
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
            AgentDecision(
                action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT
            ),
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
        assert "Göstərilən namizəd tapılmadı" in response.text


async def test_user_message_and_model_message_are_html_escaped_in_render(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    """User text stays inert, while model response prose is impossible."""
    _tenant, user, password, _membership = tenant_and_user
    payload = "<script>alert(1)</script>"
    with pytest.raises(ValidationError):
        AgentDecision.model_validate({"action": "FINAL_ANSWER", "message": payload})
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(
            action=AgentActionType.FINAL_ANSWER,
            response_code=AgentResponseCode.GREETING,
        )
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    response = await client.post(
        "/ui/agent", data={"message": f"salam {payload}", "csrf_token": csrf}
    )
    assert response.status_code == 200
    assert payload not in response.text
    assert response.text.count("&lt;script&gt;alert(1)&lt;/script&gt;") == 1


async def test_zero_tool_model_candidate_claim_and_hiring_recommendation_never_render_or_persist(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    invented = "The first candidate has 20 years of Python experience and should be hired."
    with pytest.raises(ValidationError):
        AgentDecision.model_validate({"action": "FINAL_ANSWER", "message": invented})
    fake = FakeLLMProvider(agent_fail_first_n_calls=99)
    fake._agent_decisions = [
        AgentDecision(
            action=AgentActionType.FINAL_ANSWER,
            response_code=AgentResponseCode.ACKNOWLEDGEMENT,
        )
    ]
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)

    response = await client.post("/ui/agent", data={"message": "Who is best?", "csrf_token": csrf})
    assert response.status_code == 200
    assert invented not in response.text

    history = await client.get("/ui/agent")
    assert history.status_code == 200
    assert invented not in history.text


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
            AgentDecision(
                action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT
            ),
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
            AgentDecision(
                action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT
            ),
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


# --- D-038 regression tests: rendered-page assertions for a grounded
# conversational explanation of a specific candidate's experience, where
# the server (not the model) authors every span of factual text. ---


async def test_grounded_explanation_renders_as_meaningful_answer_with_evidence(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    """UI requirement: a follow-up like 'birincinin təcrübəsini izah et'
    must show (1) a meaningful assistant explanation, (2) relevant
    grounded evidence, (3) an optional 'Tam profilə bax' link — not
    merely a generic framing sentence plus a full-profile dump."""
    from meyar.agent.schemas import GroundedSelection

    tenant, user, password, _membership = tenant_and_user
    profile_content = {
        **_profile("Python"),
        "employment_history": [
            {
                "title": "Backend Developer",
                "organization": "Synthetic Co",
                "start_date": "2020",
                "end_date": None,
                "is_current": True,
                "evidence": [
                    {
                        "page": 1,
                        "block_index": 0,
                        "quote": ("Backend Developer at Synthetic Co since 2020; current."),
                    }
                ],
            }
        ],
    }
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=profile_content
    )
    await db_session.commit()

    fake_search = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query="Python bilən namizədləri göstər",
            ),
            AgentDecision(
                action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT
            ),
        ],
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake_search
    csrf = await _login_and_csrf(client, user.username, password)
    await client.post(
        "/ui/agent", data={"message": "Python bilən namizədləri göstər", "csrf_token": csrf}
    )

    # facts: 0 = Python skill, 1 = Backend Developer employment (detail "2020 — hazırda davam edir")
    fake_profile = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1),
        grounded_selection=GroundedSelection(used_facts=[1, 0]),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake_profile
    response = await client.post(
        "/ui/agent", data={"message": "birincinin təcrübəsini izah et", "csrf_token": csrf}
    )
    assert response.status_code == 200
    assert "Backend Developer" in response.text
    assert "Bacarıqlar" in response.text  # supporting evidence cards still present
    assert f"/ui/candidates/{candidate.id}" in response.text  # optional "Tam profilə bax"


async def test_grounded_explanation_never_contains_unsupported_claim_over_http(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    """End-to-end version of the owner-reported vulnerability: even a
    GroundedSelection over a real employment fact can never render
    'managed a team' or any other predicate the fact doesn't state —
    there is no field for the model to author it in."""
    from meyar.agent.schemas import GroundedSelection

    tenant, user, password, _membership = tenant_and_user
    profile_content = {
        **_profile("Python"),
        "employment_history": [
            {
                "title": "Data Analyst",
                "organization": "Caspian Analytics",
                "start_date": "2021",
                "end_date": "2025",
                "is_current": False,
                "evidence": [
                    {
                        "page": 1,
                        "block_index": 0,
                        "quote": "Data Analyst at Caspian Analytics, 2021-2025.",
                    }
                ],
            }
        ],
    }
    candidate, _pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=profile_content
    )
    await db_session.commit()

    fake_search = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query="Python bilən namizədləri göstər",
            ),
            AgentDecision(
                action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT
            ),
        ],
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake_search
    csrf = await _login_and_csrf(client, user.username, password)
    await client.post(
        "/ui/agent", data={"message": "Python bilən namizədləri göstər", "csrf_token": csrf}
    )

    fake_profile = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1),
        grounded_selection=GroundedSelection(used_facts=[1]),  # the employment fact
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake_profile
    response = await client.post(
        "/ui/agent", data={"message": "birincinin təcrübəsini izah et", "csrf_token": csrf}
    )
    assert response.status_code == 200
    assert "Data Analyst" in response.text
    assert "2021" in response.text and "2025" in response.text
    for forbidden in ("managed", "team", "komanda", "rəhbərlik"):
        assert forbidden not in response.text.casefold()


async def test_grounded_explanation_never_leaks_identity_to_model(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    from meyar.services.candidate_identity_repo import create_identity_version

    await create_identity_version(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        candidate_document_id=pv.candidate_document_id,
        canonical_document_id=pv.canonical_document_id,
        source_sha256=pv.source_sha256,
        schema_version="candidate-identity-v1",
        prompt_version="test",
        model_provider="fake",
        model_name="fake",
        status="COMPLETED",
        identity_content={
            "full_name": {
                "value": "Tural Demo-Aliyev",
                "evidence": [{"page": 1, "block_index": 0, "quote": "Synthetic evidence"}],
            },
            "email": {
                "value": "tural@example.invalid",
                "evidence": [{"page": 1, "block_index": 0, "quote": "Synthetic evidence"}],
            },
            "phone": None,
        },
    )
    await db_session.commit()

    from meyar.agent.schemas import GroundedSelection

    fake_profile = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1),
        grounded_selection=GroundedSelection(used_facts=[0]),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake_profile
    csrf = await _login_and_csrf(client, user.username, password)
    csrf_conv_setup = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"])),
        agent_decisions=[
            AgentDecision(
                action=AgentActionType.SEARCH_CANDIDATES,
                search_query="Python bilən namizədləri göstər",
            ),
            AgentDecision(
                action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT
            ),
        ],
    )
    app.dependency_overrides[get_llm_provider] = lambda: csrf_conv_setup
    await client.post(
        "/ui/agent", data={"message": "Python bilən namizədləri göstər", "csrf_token": csrf}
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake_profile
    response = await client.post(
        "/ui/agent", data={"message": "birincinin təcrübəsini izah et", "csrf_token": csrf}
    )
    assert response.status_code == 200
    # The human-facing page may legitimately show the candidate's name
    # (an authorized HR user is viewing it) — what must never happen is
    # identity reaching the MODEL's own input. Assert the fake provider's
    # captured facts/question never carried it.
    assert fake_profile.last_grounded_facts is not None
    for fact in fake_profile.last_grounded_facts:
        assert "Tural" not in fact.title and "tural@" not in (fact.detail or "")
    assert "tural@example.invalid" not in (fake_profile.last_grounded_question or "")


# --- Slice 4 (issue #33, D-030/D-032): DRAFT_JOB_CRITERIA + "Yeni söhbət" ---


async def test_draft_job_criteria_renders_editable_prefilled_review_form(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.schemas.criteria import CriterionKind

    _tenant, user, password, _membership = tenant_and_user
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title="Baş Backend Mühəndisi",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL, requirement="Python", source_text="Python bilməlidir"
                )
            ],
            preferred=[
                JDDraftCriterionItem(
                    span_id="req-0002",
                    kind=CriterionKind.SKILL, requirement="AWS", source_text="AWS üstünlükdür"
                )
            ],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    jd_text = "Baş Backend Mühəndisi axtarırıq. Python bilməlidir. AWS üstünlükdür."
    response = await client.post("/ui/agent", data={"message": jd_text, "csrf_token": csrf})
    assert response.status_code == 200
    assert 'value="Baş Backend Mühəndisi"' in response.text
    assert 'value="Python"' in response.text
    assert 'value="AWS"' in response.text
    # HR-facing kind label, never the raw enum value, and no raw tool
    # name/internal marker anywhere in the visible page.
    assert "Bacarıq" in response.text
    assert "DRAFT_JOB_CRITERIA" not in response.text
    assert _draft_confirm_path(response.text).endswith("/confirm")
    assert 'name="from_agent_draft"' not in response.text
    assert 'name="draft_id"' not in response.text

    # Drafting alone persists no Job; only the dedicated, server-authorized
    # confirmation operation creates the vacancy and ranks it.
    confirm_path = _draft_confirm_path(response.text)
    create = await client.post(
        confirm_path,
        data={
            "csrf_token": csrf,
            "title": "Baş Backend Mühəndisi",
            "must_span_id_0": _hidden_value(response.text, "must_span_id_0"),
            "must_kind_0": "SKILL",
            "must_requirement_0": "Python",
            "must_min_years_0": "",
            "must_weight_0": "1",
            "pref_span_id_0": _hidden_value(response.text, "pref_span_id_0"),
            "pref_kind_0": "SKILL",
            "pref_requirement_0": "AWS",
            "pref_min_years_0": "",
            "pref_weight_0": "1",
        },
        follow_redirects=False,
    )
    assert create.status_code == 200
    assert "Reytinq nəticələri" in create.text


async def test_draft_job_criteria_drops_prohibited_item_and_notes_it(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.schemas.criteria import CriterionKind

    _tenant, user, password, _membership = tenant_and_user
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title="Rol",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0002",
                    kind=CriterionKind.SKILL, requirement="Python", source_text="Python bilməlidir"
                ),
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL, requirement="kişi", source_text="kişi olmalıdır"
                ),
            ],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    response = await client.post(
        "/ui/agent",
        data={
            "message": "Rol üçün namizəd kişi olmalıdır. Python bilməlidir.",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 200
    assert 'value="Python"' in response.text
    # The dropped, prohibited term never becomes a form row (it may still
    # appear verbatim in the HR user's own echoed message above, which is
    # untrusted text shown as-is — that is not a policy leak).
    assert 'value="kişi"' not in response.text
    assert "Şəxsi və ya həssas xüsusiyyətə aid 1 tələb aşkarlandı" in response.text
    assert "işlə bağlı peşəkar tələblə əvəz edin" in response.text


async def test_draft_job_criteria_discloses_unsupported_requirement_visibly(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    """D-043/D-045 (PR #42 owner correction, issue #33): a non-sensitive
    requirement CriterionIn cannot represent (here an EXPERIENCE item with
    no derivable duration) must be visibly disclosed to HR, not silently
    dropped — and must never appear as an editable, persistable CRITERION
    row (a "must_requirement_N" field, which build_job_create_request
    would validate and could turn into a real, scored criterion). The
    browser receives no hidden copy as authority; confirmation resolves the
    server-held draft by draft_id."""
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.schemas.criteria import CriterionKind

    _tenant, user, password, _membership = tenant_and_user
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title="Rol",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL, requirement="Python", source_text="Python bilməlidir"
                ),
                JDDraftCriterionItem(
                    span_id="req-0002",
                    kind="SKILL_EXPERIENCE",
                    requirement="ACAMS sertifikatı üzrə",
                    source_text="ACAMS sertifikatı üzrə təcrübə tələb olunur",
                ),
            ],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    response = await client.post(
        "/ui/agent",
        data={
            "message": "Python bilməlidir. ACAMS sertifikatı üzrə təcrübə tələb olunur.",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 200
    assert 'value="Python"' in response.text
    assert "ACAMS sertifikatı üzrə təcrübə tələb olunur" in response.text
    # Never an editable criterion row or browser-authority hidden field.
    assert 'name="must_requirement_1" value="ACAMS sertifikatı təcrübəsi"' not in response.text
    assert 'name="unsupported_must_have"' not in response.text
    assert "Məlumat üçün — qiymətləndirməyə daxil edilmir" in response.text


async def test_draft_job_criteria_explicit_intent_routes_without_magic_wording(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    """The "JD-dən meyar hazırla" affordance (intent=draft_job_criteria)
    must reach the review form deterministically for arbitrary pasted
    text with no explicit lead-in phrase and no routing-decision model
    call at all — agent_decision is deliberately left unset so a
    fallback to model routing would fail loudly."""
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.schemas.criteria import CriterionKind

    _tenant, user, password, _membership = tenant_and_user
    fake = FakeLLMProvider(
        jd_draft=JDCriteriaDraft(
            title="Rol",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL, requirement="Python", source_text="Python bilməlidir"
                )
            ],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    jd_text = "Heç bir açar söz olmadan mətn. Python bilməlidir."
    response = await client.post(
        "/ui/agent",
        data={"message": jd_text, "intent": "draft_job_criteria", "csrf_token": csrf},
    )
    assert response.status_code == 200
    assert 'value="Python"' in response.text
    assert _draft_confirm_path(response.text).endswith("/confirm")
    assert fake.agent_call_count == 0


async def test_draft_job_criteria_explicit_intent_never_becomes_a_search(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    """Even if the (unused) routing decision would have misrouted this
    text to SEARCH_CANDIDATES, the explicit intent must still deterministically
    draft criteria — never a candidate-search result."""
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.schemas.criteria import CriterionKind

    _tenant, user, password, _membership = tenant_and_user
    jd_text = "Vakansiya: Backend Mühəndisi. Python bilməlidir."
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(
            action=AgentActionType.SEARCH_CANDIDATES, search_query=jd_text
        ),
        jd_draft=JDCriteriaDraft(
            title="Backend Mühəndisi",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL, requirement="Python", source_text="Python bilməlidir"
                )
            ],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    response = await client.post(
        "/ui/agent",
        data={"message": jd_text, "intent": "draft_job_criteria", "csrf_token": csrf},
    )
    assert response.status_code == 200
    assert _draft_confirm_path(response.text).endswith("/confirm")
    assert "namizəd tapıldı" not in response.text
    assert fake.agent_call_count == 0
    assert fake.call_count == 0


async def test_confirming_agent_drafted_criteria_lands_on_ranking_not_jobs_list(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    """D-043 (PR #42 owner correction, issue #33): confirming the agent's
    JD-drafted criteria creates the Job internally (unchanged persistence
    path) but lands directly on its ranking/result context — never a bare
    redirect to the de-emphasized vacancy list. Only accepted criteria
    (Python here) were ever persisted or scored."""
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.schemas.criteria import CriterionKind

    tenant, user, password, _membership = tenant_and_user
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    fake = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title="Baş Backend Mühəndisi",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL, requirement="Python", source_text="Python bilməlidir"
                )
            ],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    jd_text = "Baş Backend Mühəndisi axtarırıq. Python bilməlidir."
    draft_response = await client.post("/ui/agent", data={"message": jd_text, "csrf_token": csrf})
    assert draft_response.status_code == 200
    confirm_path = _draft_confirm_path(draft_response.text)
    span_id = _hidden_value(draft_response.text, "must_span_id_0")

    create = await client.post(
        confirm_path,
        data={
            "csrf_token": csrf,
            "title": "Baş Backend Mühəndisi",
            "must_span_id_0": span_id,
            "must_kind_0": "SKILL",
            "must_requirement_0": "Python",
            "must_min_years_0": "",
            "must_weight_0": "1",
        },
        follow_redirects=False,
    )
    # Rendered directly (never a 303 redirect to /ui/jobs).
    assert create.status_code == 200
    assert "Reytinq nəticələri" in create.text
    assert "Baş Backend Mühəndisi" in create.text

    replay = await client.post(
        confirm_path,
        data={
            "csrf_token": csrf,
            "title": "Başqa başlıq",
            "must_span_id_0": span_id,
            "must_kind_0": "SKILL",
            "must_requirement_0": "Python",
            "must_min_years_0": "",
            "must_weight_0": "1",
        },
    )
    assert replay.status_code == 200
    assert "Reytinq nəticələri" in replay.text

    from sqlalchemy import func, select

    from meyar.models.agent_draft_confirmation import AgentDraftConfirmation
    from meyar.models.job import Job
    from meyar.models.job_criteria_version import JobCriteriaVersion

    assert await db_session.scalar(select(func.count()).select_from(Job)) == 1
    assert await db_session.scalar(select(func.count()).select_from(JobCriteriaVersion)) == 1
    assert await db_session.scalar(select(func.count()).select_from(AgentDraftConfirmation)) == 1


async def test_confirmation_survives_transcript_reset_and_new_database_session(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from meyar.models.agent_conversation import AgentConversation
    from meyar.models.agent_draft_confirmation import AgentDraftConfirmation
    from meyar.models.browser_session import BrowserSession
    from meyar.models.job import Job
    from meyar.models.job_criteria_version import JobCriteriaVersion
    from meyar.services.agent_conversation_repo import reset_conversation
    from meyar.services.agent_draft_confirmation_repo import get_draft_confirmation

    tenant, user, password, _membership = tenant_and_user
    csrf, confirm_path, html = await _render_python_draft(
        client, username=user.username, password=password
    )
    draft_id = uuid.UUID(confirm_path.split("/")[-2])
    confirmed = await client.post(confirm_path, data=_python_confirmation_data(html, csrf))
    assert confirmed.status_code == 200, confirmed.text

    conversation = await db_session.scalar(select(AgentConversation))
    session = await db_session.scalar(select(BrowserSession))
    durable = await db_session.scalar(select(AgentDraftConfirmation))
    assert conversation is not None and session is not None and durable is not None

    # Even if bounded transcript JSON disagrees, it cannot replace the
    # independent confirmation identity.
    conversation.turns = [
        {
            **turn,
            "confirmed_job_draft": {
                **turn["confirmed_job_draft"],
                "job_id": str(uuid.uuid4()),
                "criteria_version_id": str(uuid.uuid4()),
            },
        }
        if "confirmed_job_draft" in turn
        else turn
        for turn in conversation.turns
    ]
    await db_session.commit()
    disagreement_replay = await client.post(confirm_path, data={"csrf_token": csrf})
    assert disagreement_replay.status_code == 200
    assert "Reytinq nəticələri" in disagreement_replay.text

    await reset_conversation(db_session, conversation)
    await db_session.commit()

    assert db_session.bind is not None
    engine = create_async_engine(str(db_session.bind.url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as reconstructed:
            recovered = await get_draft_confirmation(
                reconstructed,
                tenant_id=tenant.id,
                browser_session_id=session.id,
                draft_id=draft_id,
            )
            assert recovered is not None
            assert (recovered.job_id, recovered.criteria_version_id) == (
                durable.job_id,
                durable.criteria_version_id,
            )
            assert (
                await get_draft_confirmation(
                    reconstructed,
                    tenant_id=uuid.uuid4(),
                    browser_session_id=session.id,
                    draft_id=draft_id,
                )
                is None
            )
            assert (
                await get_draft_confirmation(
                    reconstructed,
                    tenant_id=tenant.id,
                    browser_session_id=uuid.uuid4(),
                    draft_id=draft_id,
                )
                is None
            )
    finally:
        await engine.dispose()

    replay = await client.post(confirm_path, data={"csrf_token": csrf})
    assert replay.status_code == 200
    assert "Reytinq nəticələri" in replay.text
    assert await db_session.scalar(select(func.count()).select_from(Job)) == 1
    assert await db_session.scalar(select(func.count()).select_from(JobCriteriaVersion)) == 1
    assert await db_session.scalar(select(func.count()).select_from(AgentDraftConfirmation)) == 1


async def test_agent_confirmation_rejects_browser_weakened_row_without_persistence(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from sqlalchemy import func, select

    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.models.job import Job

    _tenant, user, password, _membership = tenant_and_user
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title="Backend",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind="SKILL", requirement="Python", source_text="Python required"
                )
            ],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    response = await client.post(
        "/ui/agent", data={"message": "Backend. Python required.", "csrf_token": csrf}
    )
    confirm_path = _draft_confirm_path(response.text)
    span_id = _hidden_value(response.text, "must_span_id_0")

    tampered = await client.post(
        confirm_path,
        data={
            "csrf_token": csrf,
            "title": "Backend",
            "must_span_id_0": span_id,
            "must_kind_0": "SKILL",
            "must_requirement_0": "JavaScript",
            "must_min_years_0": "",
            "must_weight_0": "1",
        },
    )
    assert tampered.status_code == 422
    assert "server təsdiqli forması ilə uyğun gəlmir" in tampered.text
    assert await db_session.scalar(select(func.count()).select_from(Job)) == 0


async def test_agent_confirmation_route_does_not_depend_on_browser_mode_markers(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from sqlalchemy import func, select

    from meyar.models.job import Job

    _tenant, user, password, _membership = tenant_and_user
    csrf, confirm_path, html = await _render_python_draft(
        client, username=user.username, password=password
    )
    assert 'name="from_agent_draft"' not in html
    assert 'name="draft_id"' not in html

    # Removing every optional provenance marker, or adding/changing the old
    # mode flag, cannot alter which server operation handles the request.
    data = _python_confirmation_data(html, csrf)
    data["from_agent_draft"] = "changed-by-browser"
    response = await client.post(confirm_path, data=data)
    assert response.status_code == 200
    assert await db_session.scalar(select(func.count()).select_from(Job)) == 1


async def test_manual_job_endpoint_rejects_agent_review_payload(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from sqlalchemy import func, select

    from meyar.models.job import Job

    _tenant, user, password, _membership = tenant_and_user
    csrf, _confirm_path, html = await _render_python_draft(
        client, username=user.username, password=password
    )
    response = await client.post("/ui/jobs", data=_python_confirmation_data(html, csrf))
    assert response.status_code == 422
    assert "yalnız öz təsdiq əməliyyatı" in response.text
    assert await db_session.scalar(select(func.count()).select_from(Job)) == 0


@pytest.mark.parametrize(
    "mutation",
    (
        "value",
        "kind",
        "modality",
        "years",
        "weight",
        "span_id",
        "insert",
        "duplicate",
        "delete",
        "remove_span_marker",
        "title",
    ),
)
async def test_agent_confirmation_browser_tampering_matrix_is_atomic(
    mutation: str,
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from sqlalchemy import func, select

    from meyar.models.agent_draft_confirmation import AgentDraftConfirmation
    from meyar.models.audit_event import AuditEvent
    from meyar.models.job import Job
    from meyar.models.job_criteria_version import JobCriteriaVersion

    _tenant, user, password, _membership = tenant_and_user
    csrf, confirm_path, html = await _render_python_draft(
        client, username=user.username, password=password
    )
    data = _python_confirmation_data(html, csrf)
    if mutation == "value":
        data["must_requirement_0"] = "JavaScript"
    elif mutation == "kind":
        data["must_kind_0"] = "CERTIFICATION"
    elif mutation == "modality":
        for field in ("span_id", "kind", "requirement", "min_years", "weight"):
            data[f"pref_{field}_0"] = data.pop(f"must_{field}_0")
    elif mutation == "years":
        data["must_min_years_0"] = "5"
    elif mutation == "weight":
        data["must_weight_0"] = "2"
    elif mutation == "span_id":
        data["must_span_id_0"] = "req-9999"
    elif mutation in {"insert", "duplicate"}:
        data.update(
            {
                "must_span_id_1": (
                    "req-9999" if mutation == "insert" else data["must_span_id_0"]
                ),
                "must_kind_1": "SKILL",
                "must_requirement_1": "SQL",
                "must_min_years_1": "",
                "must_weight_1": "1",
            }
        )
    elif mutation == "delete":
        for key in list(data):
            if key.startswith("must_"):
                data.pop(key)
    elif mutation == "remove_span_marker":
        data.pop("must_span_id_0")
    elif mutation == "title":
        data["title"] = "Browser-authored title"

    response = await client.post(confirm_path, data=data)
    assert response.status_code == 422
    assert await db_session.scalar(select(func.count()).select_from(Job)) == 0
    assert await db_session.scalar(select(func.count()).select_from(JobCriteriaVersion)) == 0
    assert (
        await db_session.scalar(select(func.count()).select_from(AgentDraftConfirmation))
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.event_type == "job.created")
        )
        == 0
    )


async def test_agent_confirmation_rejects_unsupported_to_scorable_insertion(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from sqlalchemy import func, select

    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.models.job import Job

    _tenant, user, password, _membership = tenant_and_user
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title="Backend",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind="SKILL",
                    requirement="Python",
                    source_text="Python required",
                ),
                JDDraftCriterionItem(
                    span_id="req-0002",
                    kind="SKILL_EXPERIENCE",
                    requirement="Kubernetes",
                    min_years=5,
                    source_text="5 years Kubernetes experience required",
                ),
            ],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    draft = await client.post(
        "/ui/agent",
        data={
            "message": "Backend. Python required. 5 years Kubernetes experience required.",
            "csrf_token": csrf,
        },
    )
    data = _python_confirmation_data(draft.text, csrf)
    data.update(
        {
            "must_span_id_1": "req-0002",
            "must_kind_1": "SKILL",
            "must_requirement_1": "Kubernetes",
            "must_min_years_1": "",
            "must_weight_1": "1",
        }
    )
    response = await client.post(_draft_confirm_path(draft.text), data=data)
    assert response.status_code == 422
    assert await db_session.scalar(select(func.count()).select_from(Job)) == 0


async def test_agent_confirmation_rejects_prohibited_to_scorable_insertion(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from sqlalchemy import func, select

    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.models.job import Job

    _tenant, user, password, _membership = tenant_and_user
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title="Backend",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind="SKILL",
                    requirement="Python",
                    source_text="Python required",
                ),
                JDDraftCriterionItem(
                    span_id="req-0002",
                    kind="SKILL",
                    requirement="Female",
                    source_text="Female required",
                ),
            ],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    draft = await client.post(
        "/ui/agent",
        data={
            "message": "Backend. Python required. Female required.",
            "csrf_token": csrf,
        },
    )
    data = _python_confirmation_data(draft.text, csrf)
    data.update(
        {
            "must_span_id_1": "req-0002",
            "must_kind_1": "SKILL",
            "must_requirement_1": "Female",
            "must_min_years_1": "",
            "must_weight_1": "1",
        }
    )
    response = await client.post(_draft_confirm_path(draft.text), data=data)
    assert response.status_code == 422
    assert await db_session.scalar(select(func.count()).select_from(Job)) == 0


async def test_draft_job_criteria_fabricated_requirement_never_rendered(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    """D-046 (PR #42 owner correction, issue #33): real-Ollama acceptance
    testing (qwen3:1.7b) found a travel-readiness-only JD surfacing an
    entirely unrelated, unstated requirement (reported as "Passing an
    exam") in the HR-facing review form as if it were a genuine JD
    requirement. The fabricated item's own text must never reach the
    rendered page — only a safe, generic count notice — while the one
    genuinely grounded requirement still renders normally."""
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem, JDDraftCriterionKind

    _tenant, user, password, _membership = tenant_and_user
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title="Kredit Analitiki",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=JDDraftCriterionKind.OTHER,
                    requirement="Ezamiyyətə hazır olmaq",
                    source_text="Namizəd ezamiyyətə getməyə hazır olmalıdır",
                ),
                JDDraftCriterionItem(
                    span_id="req-9999",
                    kind=JDDraftCriterionKind.OTHER,
                    requirement="Passing an exam",
                    source_text="Passing an exam",
                ),
            ],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    response = await client.post(
        "/ui/agent",
        data={
            "message": "Namizəd ezamiyyətə getməyə hazır olmalıdır.",
            "csrf_token": csrf,
        },
    )
    assert response.status_code == 200
    # The genuinely grounded requirement is still disclosed as usual.
    assert "Namizəd ezamiyyətə getməyə hazır olmalıdır" in response.text
    # The fabricated requirement's own text never appears anywhere on the
    # page — not as a form field, not in the disclosure notice, not in
    # the echoed user message (which never contained it either).
    assert "Passing an exam" not in response.text
    assert "1 tələb JD mətnində aydın təsdiqlənmədiyi üçün çıxarıldı" in response.text


async def test_draft_job_criteria_title_never_persists_as_trusted_assistant_text(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from sqlalchemy import select

    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.models.agent_conversation import AgentConversation
    from meyar.schemas.criteria import CriterionKind
    from meyar.services.agent_conversation_repo import ASSISTANT_TEXT_AUTHORITY_SERVER

    _tenant, user, password, _membership = tenant_and_user
    adversarial_title = "The first candidate has 20 years of Python experience and should be hired."
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title=adversarial_title,
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL, requirement="Python", source_text="Python bilməlidir"
                )
            ],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    response = await client.post(
        "/ui/agent",
        data={"message": "Backend role. Python bilməlidir.", "csrf_token": csrf},
    )
    assert response.status_code == 200
    assert adversarial_title not in response.text

    conversation = (await db_session.execute(select(AgentConversation))).scalars().one()
    assistant_turn = conversation.turns[-1]
    assert assistant_turn["role"] == "assistant"
    assert assistant_turn["text_authority"] == ASSISTANT_TEXT_AUTHORITY_SERVER
    assert adversarial_title not in assistant_turn["text"]

    history = await client.get("/ui/agent")
    assert history.status_code == 200
    assert adversarial_title not in history.text


async def test_arbitrary_evidence_topic_never_renders_verbatim(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from meyar.agent.schemas import GroundedSelection

    tenant, user, password, _membership = tenant_and_user
    _candidate, _profile_version = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content={
            **EMPTY_PROFILE,
            "skills": [
                {
                    "name": "Python",
                    "category": None,
                    "evidence": [{"page": 1, "block_index": 0, "quote": "Python"}],
                }
            ],
        },
    )
    await db_session.commit()
    fake_search = FakeLLMProvider(
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
    app.dependency_overrides[get_llm_provider] = lambda: fake_search
    csrf = await _login_and_csrf(client, user.username, password)
    await client.post(
        "/ui/agent",
        data={"message": "Python bilən namizədləri göstər", "csrf_token": csrf},
    )
    raw_topic = "The first candidate should be hired immediately"
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(
            action=AgentActionType.GET_CANDIDATE_EVIDENCE,
            candidate_ref=1,
            evidence_topic=raw_topic,
        ),
        grounded_selection=GroundedSelection(used_facts=[]),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake

    response = await client.post(
        "/ui/agent",
        data={"message": "Birinci namizəd üzrə sübut göstər", "csrf_token": csrf},
    )

    assert response.status_code == 200
    assert raw_topic not in response.text
    assert "profildə açıq sübut yoxdur" in response.text


async def test_legitimate_evidence_topic_renders_server_resolved_label(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    tenant, user, password, _membership = tenant_and_user
    _candidate, _profile_version = await seed_candidate_with_profile(
        db_session,
        tenant_id=tenant.id,
        profile_content={
            **EMPTY_PROFILE,
            "skills": [
                {
                    "name": "Python",
                    "category": None,
                    "evidence": [{"page": 1, "block_index": 0, "quote": "Python"}],
                }
            ],
        },
    )
    await db_session.commit()
    fake_search = FakeLLMProvider(
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
    app.dependency_overrides[get_llm_provider] = lambda: fake_search
    csrf = await _login_and_csrf(client, user.username, password)
    await client.post(
        "/ui/agent",
        data={"message": "Python bilən namizədləri göstər", "csrf_token": csrf},
    )
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(
            action=AgentActionType.GET_CANDIDATE_EVIDENCE,
            candidate_ref=1,
            evidence_topic="python",
        )
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake

    response = await client.post(
        "/ui/agent",
        data={"message": "Python sübutunu göstər", "csrf_token": csrf},
    )

    assert response.status_code == 200
    assert "Ad məlumatı yoxdur — Python" in response.text
    assert "Uyğun gələn tələb: Python" in response.text


async def test_unsupported_requirement_survives_confirmation_and_scores_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    """D-045 (PR #42 owner correction, issue #33, item 6): a requirement
    the model explicitly flagged as JDDraftCriterionKind.OTHER (real,
    non-sensitive, outside the deterministic evaluator's five scoring
    dimensions) must still be visible, clearly labeled informational/
    not-scored, on the ranking page reached by confirming — never
    silently dropped by the confirm action — while never becoming a
    persisted Criterion or influencing the numeric score."""
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem, JDDraftCriterionKind
    from meyar.schemas.criteria import CriterionKind

    tenant, user, password, _membership = tenant_and_user
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    fake = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title="Data Analitiki",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind=CriterionKind.SKILL, requirement="Python", source_text="Python bilməlidir"
                )
            ],
            preferred=[
                JDDraftCriterionItem(
                    span_id="req-0002",
                    kind=JDDraftCriterionKind.OTHER,
                    requirement="Ezamiyyətə hazır olmaq",
                    source_text="Namizəd ezamiyyətə hazır olması üstünlükdür",
                )
            ],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    draft_response = await client.post(
        "/ui/agent",
        data={
            "message": (
                "Data Analitiki axtarırıq. Python bilməlidir. "
                "Namizəd ezamiyyətə hazır olması üstünlükdür."
            ),
            "csrf_token": csrf,
        },
    )
    assert draft_response.status_code == 200
    unsupported = "Namizəd ezamiyyətə hazır olması üstünlükdür"
    assert unsupported in draft_response.text
    assert 'name="unsupported_preferred"' not in draft_response.text
    confirm_path = _draft_confirm_path(draft_response.text)
    span_id = _hidden_value(draft_response.text, "must_span_id_0")

    create = await client.post(
        confirm_path,
        data={
            "csrf_token": csrf,
            "title": "Data Analitiki",
            "must_span_id_0": span_id,
            "must_kind_0": "SKILL",
            "must_requirement_0": "Python",
            "must_min_years_0": "",
            "must_weight_0": "1",
        },
        follow_redirects=False,
    )
    assert create.status_code == 200
    assert "Reytinq nəticələri" in create.text
    # Survives confirmation: still visible, clearly informational/not-scored.
    assert "Məlumat üçün — qiymətləndirməyə daxil edilmir" in create.text
    assert unsupported in create.text
    # Contributes nothing to score/ranking: only the real Python MUST_HAVE
    # criterion was ever persisted or shown as a scored requirement.
    from sqlalchemy import select

    from meyar.models.job_criteria_version import JobCriteriaVersion

    version = (
        await db_session.execute(
            select(JobCriteriaVersion).where(JobCriteriaVersion.tenant_id == tenant.id)
        )
    ).scalar_one()
    labels = [c["label"] for c in version.criteria]
    assert labels == ["Python"]
    assert unsupported not in [c["label"] for c in version.criteria]
    assert version.unsupported_requirements == [unsupported]
    assert version.needs_review_requirements == []
    assert version.eligible_only is True

    rerank = await client.post(
        f"/ui/jobs/{version.id}/rank", data={"csrf_token": csrf}
    )
    assert rerank.status_code == 200
    assert unsupported in rerank.text
    assert "Məlumat üçün — qiymətləndirməyə daxil edilmir" in rerank.text

    from sqlalchemy import select

    from meyar.models.job import Job

    jobs = (await db_session.execute(select(Job).where(Job.tenant_id == tenant.id))).scalars().all()
    assert len(jobs) == 1


async def test_omitted_source_requirement_survives_confirmation_without_scoring(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    """The HTTP review/confirmation boundary persists only accepted rows;
    an omitted material source requirement stays visible before and after
    confirmation, without exposing internal validation enums."""
    from sqlalchemy import select

    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem
    from meyar.models.job_criteria_version import JobCriteriaVersion

    tenant, user, password, _membership = tenant_and_user
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()

    source = "Data Analyst. Python required. Candidate must be willing to travel."
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title="Data Analyst",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind="SKILL",
                    requirement="Python",
                    source_text="Python required",
                )
            ],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    draft_response = await client.post("/ui/agent", data={"message": source, "csrf_token": csrf})
    assert draft_response.status_code == 200
    omitted = "Candidate must be willing to travel"
    assert omitted in draft_response.text
    assert 'name="needs_review_requirement"' not in draft_response.text
    assert "İnsan baxışı tələb edir — qiymətləndirməyə daxil edilmir" in draft_response.text
    for internal_code in ("NEEDS_HUMAN_REVIEW", "UNGROUNDED", "PROHIBITED", "UNSUPPORTED"):
        assert internal_code not in draft_response.text
    confirm_path = _draft_confirm_path(draft_response.text)
    span_id = _hidden_value(draft_response.text, "must_span_id_0")

    create = await client.post(
        confirm_path,
        data={
            "csrf_token": csrf,
            "title": "Data Analyst",
            "must_span_id_0": span_id,
            "must_kind_0": "SKILL",
            "must_requirement_0": "Python",
            "must_min_years_0": "",
            "must_weight_0": "1",
        },
    )
    assert create.status_code == 200
    assert omitted in create.text
    assert "heç bir namizədin balına/sırasına təsir etmir" in create.text

    version = (
        await db_session.execute(
            select(JobCriteriaVersion).where(JobCriteriaVersion.tenant_id == tenant.id)
        )
    ).scalar_one()
    assert [criterion["label"] for criterion in version.criteria] == ["Python"]
    assert omitted not in str(version.criteria)


async def test_agent_draft_confirmation_is_tenant_and_session_bound(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from httpx import ASGITransport
    from sqlalchemy import func, select

    from meyar.core.roles import ROLE_HR_USER
    from meyar.models.job import Job
    from meyar.services.tenant_membership_repo import create_membership
    from meyar.services.tenant_repo import create_tenant
    from meyar.services.user_repo import create_user

    _tenant_a, user_a, password_a, _membership_a = tenant_and_user
    csrf_a, confirm_path, html = await _render_python_draft(
        client, username=user_a.username, password=password_a
    )
    data_a = _python_confirmation_data(html, csrf_a)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as session_b:
        csrf_b = await _login_and_csrf(session_b, user_a.username, password_a)
        cross_session = await session_b.post(
            confirm_path, data={**data_a, "csrf_token": csrf_b}
        )
        assert cross_session.status_code == 422

    tenant_b = await create_tenant(db_session, name=f"Tenant-{uuid.uuid4().hex[:8]}")
    user_b = await create_user(
        db_session,
        username=f"hr-{uuid.uuid4().hex[:8]}",
        plaintext_password="correct-horse-battery-staple-2",
    )
    await create_membership(
        db_session, user_id=user_b.id, tenant_id=tenant_b.id, role=ROLE_HR_USER
    )
    await db_session.commit()
    async with AsyncClient(transport=transport, base_url="http://test") as tenant_b_client:
        csrf_b = await _login_and_csrf(
            tenant_b_client, user_b.username, "correct-horse-battery-staple-2"
        )
        cross_tenant = await tenant_b_client.post(
            confirm_path, data={**data_a, "csrf_token": csrf_b}
        )
        assert cross_tenant.status_code == 422

    assert await db_session.scalar(select(func.count()).select_from(Job)) == 0


async def test_unknown_or_unreachable_agent_draft_fails_closed(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from sqlalchemy import func, select

    from meyar.models.job import Job

    _tenant, user, password, _membership = tenant_and_user
    csrf = await _login_and_csrf(client, user.username, password)
    response = await client.post(
        f"/ui/agent/drafts/{uuid.uuid4()}/confirm",
        data={"csrf_token": csrf, "title": "Backend"},
    )
    assert response.status_code == 422
    assert await db_session.scalar(select(func.count()).select_from(Job)) == 0


async def test_concurrent_confirmation_creates_exactly_one_canonical_object(
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import AsyncGenerator

    from httpx import ASGITransport
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession as IndependentSession
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from meyar.db import get_db
    from meyar.models.agent_conversation import AgentConversation
    from meyar.models.agent_draft_confirmation import AgentDraftConfirmation
    from meyar.models.audit_event import AuditEvent
    from meyar.models.job import Job
    from meyar.models.job_criteria_version import JobCriteriaVersion
    from meyar.services import agent_conversation_repo

    _tenant, user, password, _membership = tenant_and_user
    assert db_session.bind is not None
    engine = create_async_engine(str(db_session.bind.url))
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def independent_db() -> AsyncGenerator[IndependentSession, None]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = independent_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as login_client:
            csrf, confirm_path, html = await _render_python_draft(
                login_client, username=user.username, password=password
            )
            cookies = login_client.cookies

        original_lock = agent_conversation_repo.get_conversation_for_update_by_session
        both_entered = asyncio.Event()
        arrival_count = 0

        async def synchronized_lock(*args, **kwargs):
            nonlocal arrival_count
            arrival_count += 1
            if arrival_count == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), timeout=5)
            return await original_lock(*args, **kwargs)

        monkeypatch.setattr(
            agent_conversation_repo,
            "get_conversation_for_update_by_session",
            synchronized_lock,
        )
        data = _python_confirmation_data(html, csrf)
        async with (
            AsyncClient(transport=transport, base_url="http://test", cookies=cookies) as first,
            AsyncClient(transport=transport, base_url="http://test", cookies=cookies) as second,
        ):
            responses = await asyncio.gather(
                first.post(confirm_path, data=data),
                second.post(confirm_path, data=data),
            )
        assert [response.status_code for response in responses] == [200, 200]
        assert arrival_count == 2
        assert await db_session.scalar(select(func.count()).select_from(Job)) == 1
        assert await db_session.scalar(select(func.count()).select_from(JobCriteriaVersion)) == 1
        assert (
            await db_session.scalar(
                select(func.count()).select_from(AgentDraftConfirmation)
            )
            == 1
        )
        assert (
            await db_session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.event_type == "job.created")
            )
            == 1
        )
        conversation = (await db_session.execute(select(AgentConversation))).scalar_one()
        assert sum("confirmed_job_draft" in turn for turn in conversation.turns) == 1
        assert all("pending_job_draft" not in turn for turn in conversation.turns)
    finally:
        await engine.dispose()


async def test_confirmation_ranking_failure_is_truthful_and_retryable(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import func, select

    from meyar.models.agent_draft_confirmation import AgentDraftConfirmation
    from meyar.models.job import Job
    from meyar.models.job_criteria_version import JobCriteriaVersion
    from meyar.scoring.policy import ScoringPolicyError
    from meyar.ui import router as ui_router

    _tenant, user, password, _membership = tenant_and_user
    csrf, confirm_path, html = await _render_python_draft(
        client, username=user.username, password=password
    )
    original_rank = ui_router.rank_candidates_for_job
    calls = 0

    async def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ScoringPolicyError("FORCED", "forced ranking failure")
        return await original_rank(*args, **kwargs)

    monkeypatch.setattr(ui_router, "rank_candidates_for_job", fail_once)
    failed_rank = await client.post(
        confirm_path, data=_python_confirmation_data(html, csrf)
    )
    assert failed_rank.status_code == 503
    assert "Tələblər təsdiqləndi, reytinq isə tamamlanmadı" in failed_rank.text
    assert "yeni vakansiya yaratmayacaq" in failed_rank.text
    assert await db_session.scalar(select(func.count()).select_from(Job)) == 1
    assert await db_session.scalar(select(func.count()).select_from(AgentDraftConfirmation)) == 1
    version = (await db_session.execute(select(JobCriteriaVersion))).scalar_one()

    retry = await client.post(
        f"/ui/jobs/{version.id}/rank", data={"csrf_token": csrf}
    )
    assert retry.status_code == 200
    assert "Reytinq nəticələri" in retry.text

    replay = await client.post(confirm_path, data={"csrf_token": csrf})
    assert replay.status_code == 200
    assert await db_session.scalar(select(func.count()).select_from(Job)) == 1
    assert await db_session.scalar(select(func.count()).select_from(JobCriteriaVersion)) == 1
    assert await db_session.scalar(select(func.count()).select_from(AgentDraftConfirmation)) == 1


async def test_skill_domain_and_language_draft_renders_complete_review_form(
    client: AsyncClient,
    db_session: AsyncSession,
    tenant_and_user,
    local_ui_settings: Settings,
) -> None:
    from meyar.agent.schemas import JDCriteriaDraft, JDDraftCriterionItem

    _tenant, user, password, _membership = tenant_and_user
    source = (
        "Role. 5 years Python experience required. English B2 required. "
        "Banking experience preferred. Show top 10 candidates."
    )
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft=JDCriteriaDraft(
            title="Role",
            must_have=[
                JDDraftCriterionItem(
                    span_id="req-0001",
                    kind="SKILL_EXPERIENCE",
                    requirement="Python",
                    min_years=5,
                    source_text="5 years Python experience required",
                ),
                JDDraftCriterionItem(
                    span_id="req-0002",
                    kind="LANGUAGE",
                    requirement="English",
                    required_level="B2",
                    source_text="English B2 required",
                ),
            ],
            preferred=[
                JDDraftCriterionItem(
                    span_id="req-0003",
                    kind="DOMAIN_EXPERIENCE",
                    requirement="Banking",
                    source_text="Banking experience preferred",
                )
            ],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    response = await client.post(
        "/ui/agent", data={"message": source, "csrf_token": csrf}
    )
    assert response.status_code == 200
    for requirement in (
        "5 years Python experience required",
        "English B2 required",
        "Banking experience preferred",
    ):
        assert requirement in response.text
    assert "Avtomatik sıralama üçün hazır meyar yoxdur" not in response.text
    assert "Tələbləri təsdiqlə və namizədləri sırala" in response.text
    assert "Bacarıq üzrə təcrübə" in response.text
    assert "Sahə təcrübəsi" in response.text
    assert 'value="B2"' in response.text
    assert "/confirm" in response.text
    title_match = re.search(r'name="title"[^>]*value="([^"]+)"', response.text)
    assert title_match is not None

    confirm_path = _draft_confirm_path(response.text)
    confirmed = await client.post(
        confirm_path,
        data={
            "csrf_token": csrf,
            "title": title_match.group(1),
            "must_span_id_0": _hidden_value(response.text, "must_span_id_0"),
            "must_kind_0": "SKILL_EXPERIENCE",
            "must_requirement_0": "Python",
            "must_min_years_0": "5",
            "must_weight_0": "1",
            "must_span_id_1": _hidden_value(response.text, "must_span_id_1"),
            "must_kind_1": "LANGUAGE",
            "must_requirement_1": "English",
            "must_min_years_1": "",
            "must_required_level_1": "B2",
            "must_weight_1": "1",
            "pref_span_id_0": _hidden_value(response.text, "pref_span_id_0"),
            "pref_kind_0": "DOMAIN_EXPERIENCE",
            "pref_requirement_0": "Banking",
            "pref_min_years_0": "",
            "pref_weight_0": "1",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert "Reytinq nəticələri" in confirmed.text
    assert "ən çox 10 uyğun namizəd" in confirmed.text
    assert "Qiymətləndirmə tarixi:" in confirmed.text

    from sqlalchemy import select

    from meyar.models.job_criteria_version import JobCriteriaVersion

    version = (
        await db_session.execute(
            select(JobCriteriaVersion).where(JobCriteriaVersion.tenant_id == _tenant.id)
        )
    ).scalar_one()
    assert version.result_limit == 10
    assert version.eligible_only is True
    assert [
        (row["kind"], row["value"], row["min_years"], row["required_level"], row["weight"])
        for row in version.criteria
    ] == [
        ("SKILL_EXPERIENCE", "Python", 5.0, None, 1.0),
        ("LANGUAGE", "English", None, "B2", 1.0),
        ("DOMAIN_EXPERIENCE", "Banking", None, None, 1.0),
    ]


async def test_draft_job_criteria_provider_failure_renders_safe_message(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    from meyar.llm.provider import ModelUnavailableError

    _tenant, user, password, _membership = tenant_and_user
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.DRAFT_JOB_CRITERIA),
        jd_draft_error=ModelUnavailableError("simulated outage"),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    response = await client.post("/ui/agent", data={"message": "JD mətni", "csrf_token": csrf})
    assert response.status_code == 200
    assert "kriteriya qaralaması hazırlana bilmədi" in response.text


async def test_agent_reset_clears_this_sessions_conversation_state(
    client: AsyncClient, db_session: AsyncSession, tenant_and_user, local_ui_settings: Settings
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
            AgentDecision(
                action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.ACKNOWLEDGEMENT
            ),
        ],
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake_search
    csrf = await _login_and_csrf(client, user.username, password)
    query_text = "Python bilən mühəndisləri tap zəhmət olmasa"
    await client.post("/ui/agent", data={"message": query_text, "csrf_token": csrf})
    workspace = await client.get("/ui/agent")
    assert query_text in workspace.text

    reset_response = await client.post(
        "/ui/agent/reset", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert reset_response.status_code == 303
    workspace_after = await client.get("/ui/agent")
    assert query_text not in workspace_after.text
    del candidate

    # The cleared last_search_candidate_ids table means a stale ordinal
    # reference from before the reset can no longer resolve.
    fake_profile = FakeLLMProvider(
        agent_decision=AgentDecision(action=AgentActionType.GET_CANDIDATE_PROFILE, candidate_ref=1)
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake_profile
    response = await client.post("/ui/agent", data={"message": "birincini aç", "csrf_token": csrf})
    assert "Göstərilən namizəd tapılmadı" in response.text


async def test_agent_reset_requires_valid_csrf(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    _tenant, user, password, _membership = tenant_and_user
    await _login_and_csrf(client, user.username, password)
    response = await client.post(
        "/ui/agent/reset", data={"csrf_token": "wrong-token"}, follow_redirects=False
    )
    assert response.status_code == 403


async def test_agent_reset_does_not_affect_another_sessions_conversation(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    from httpx import ASGITransport

    tenant, user, password, _membership = tenant_and_user
    fake = FakeLLMProvider(
        agent_decision=AgentDecision(
            action=AgentActionType.FINAL_ANSWER, response_code=AgentResponseCode.GREETING
        )
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf_a = await _login_and_csrf(client, user.username, password)
    await client.post("/ui/agent", data={"message": "salam", "csrf_token": csrf_a})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as second_client:
        csrf_b = await _login_and_csrf(second_client, user.username, password)
        await second_client.post("/ui/agent/reset", data={"csrf_token": csrf_b})

    workspace_a = await client.get("/ui/agent")
    assert "salam" in workspace_a.text

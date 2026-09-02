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


async def test_primary_nav_is_agent_first_classic_tools_are_secondary(
    client: AsyncClient, tenant_and_user, local_ui_settings: Settings
) -> None:
    """Slice 4 (issue #33, D-030): MEYAR AI + Namizədlər are the primary
    HR navigation; classic search and Vacancies remain fully reachable but
    de-emphasized (per D-032, not removed)."""
    _tenant, user, password, _membership = tenant_and_user
    await _login_and_csrf(client, user.username, password)
    page = await client.get("/ui/agent")
    primary_nav = re.search(r'<nav aria-label="Əsas naviqasiya">(.*?)</nav>', page.text, re.S)
    assert primary_nav is not None
    assert 'href="/ui/agent"' in primary_nav.group(1)
    assert 'href="/ui/library"' in primary_nav.group(1)
    assert 'href="/ui/jobs"' not in primary_nav.group(1)
    assert 'href="/ui"' not in primary_nav.group(1)
    # Still reachable, just not primary.
    assert 'href="/ui/jobs"' in page.text
    assert 'href="/ui">Klassik axtarış' in page.text


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
        assert "Göstərilən namizəd tapılmadı" in response.text


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
                "evidence": [{"page": 1, "block_index": 0, "quote": "Synthetic evidence"}],
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
            AgentDecision(action=AgentActionType.FINAL_ANSWER, message="Budur nəticələr."),
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
                "evidence": [{"page": 1, "block_index": 0, "quote": "Synthetic evidence"}],
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
            AgentDecision(action=AgentActionType.FINAL_ANSWER, message="Budur nəticələr."),
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
            AgentDecision(action=AgentActionType.FINAL_ANSWER, message="Budur nəticələr."),
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
            must_have=[JDDraftCriterionItem(kind=CriterionKind.SKILL, requirement="Python")],
            preferred=[JDDraftCriterionItem(kind=CriterionKind.SKILL, requirement="AWS")],
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
    assert '<form method="post" action="/ui/jobs"' in response.text

    # The review form is fully editable and posts through the EXISTING,
    # unchanged /ui/jobs creation path — nothing was persisted by drafting
    # alone (D-032): submitting it is what actually creates the vacancy.
    create = await client.post(
        "/ui/jobs",
        data={
            "csrf_token": csrf,
            "title": "Baş Backend Mühəndisi",
            "must_kind_0": "SKILL",
            "must_requirement_0": "Python",
            "must_min_years_0": "",
            "must_weight_0": "1",
            "pref_kind_0": "SKILL",
            "pref_requirement_0": "AWS",
            "pref_min_years_0": "",
            "pref_weight_0": "1",
        },
        follow_redirects=False,
    )
    assert create.status_code == 303
    jobs_page = await client.get("/ui/jobs")
    assert "Baş Backend Mühəndisi" in jobs_page.text


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
                JDDraftCriterionItem(kind=CriterionKind.SKILL, requirement="Python"),
                JDDraftCriterionItem(kind=CriterionKind.SKILL, requirement="kişi"),
            ],
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    csrf = await _login_and_csrf(client, user.username, password)
    response = await client.post(
        "/ui/agent", data={"message": "Rol üçün namizəd kişi olmalıdır.", "csrf_token": csrf}
    )
    assert response.status_code == 200
    assert 'value="Python"' in response.text
    # The dropped, prohibited term never becomes a form row (it may still
    # appear verbatim in the HR user's own echoed message above, which is
    # untrusted text shown as-is — that is not a policy leak).
    assert 'value="kişi"' not in response.text
    assert "1 tələb siyasətə görə çıxarıldı" in response.text


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
    response = await client.post(
        "/ui/agent", data={"message": "JD mətni", "csrf_token": csrf}
    )
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
            AgentDecision(action=AgentActionType.FINAL_ANSWER, message="Budur nəticələr."),
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
    response = await client.post(
        "/ui/agent", data={"message": "birincini aç", "csrf_token": csrf}
    )
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
        agent_decision=AgentDecision(action=AgentActionType.FINAL_ANSWER, message="Salam!")
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

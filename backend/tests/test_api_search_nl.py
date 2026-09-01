"""Slice 12 — REST tests for POST /api/v1/search/natural-language.
All 7 typed PlannerOutcome values must be reachable, distinguishable, and
never collapsed to a generic error. No live Ollama — FakeLLMProvider only.
See .claude/rules/testing.md."""

import pytest
from fakes import FakeLLMProvider
from httpx import AsyncClient
from search_helpers import seed_candidate_with_profile
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.embedding.dependency import get_embedding_provider
from meyar.embedding.provider import EmbeddingUnavailableError
from meyar.llm.dependency import get_llm_provider
from meyar.llm.provider import LLMResultProvenance, ModelUnavailableError
from meyar.main import app
from meyar.search.planner_schemas import PlannerDraft, PlannerOutcome
from meyar.search.schemas import RequiredFilters

EVIDENCE = [{"page": 1, "block_index": 0, "quote": "Synthetic evidence"}]
EMPTY_PROFILE = {
    "skills": [],
    "employment_history": [],
    "education": [],
    "certifications": [],
    "languages": [],
    "projects": [],
}


def _auth(plaintext: str) -> dict:
    return {"Authorization": f"Bearer {plaintext}"}


def _profile(*skills: str) -> dict:
    return {
        **EMPTY_PROFILE,
        "skills": [
            {"name": skill, "category": "Backend", "evidence": EVIDENCE} for skill in skills
        ],
    }


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


async def _post(client: AsyncClient, plaintext: str, query: str) -> dict:
    resp = await client.post(
        "/api/v1/search/natural-language",
        json={"query": query, "as_of_date": "2026-01-01"},
        headers=_auth(plaintext),
    )
    assert resp.status_code == 200
    return resp.json()


async def test_executable_outcome_returns_search_results(
    db_session: AsyncSession, client: AsyncClient, tenant_and_key
) -> None:
    tenant, _key, plaintext = tenant_and_key
    candidate, _p = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile("Python")
    )
    await db_session.commit()
    fake = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Python"]))
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake

    body = await _post(client, plaintext, "Python bilən namizədləri göstər.")
    assert body["outcome"] == PlannerOutcome.EXECUTABLE.value
    assert body["executable"] is True
    assert body["search"] is not None
    assert body["search"]["result_count"] == 1
    assert body["search"]["results"][0]["candidate_id"] == str(candidate.id)


async def test_prohibited_request_outcome_never_calls_llm(
    client: AsyncClient, tenant_and_key
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    fake = FakeLLMProvider(planner_draft=PlannerDraft())
    app.dependency_overrides[get_llm_provider] = lambda: fake

    body = await _post(client, plaintext, "30 yaşdan aşağı namizədləri göstər.")
    assert body["outcome"] == PlannerOutcome.PROHIBITED_REQUEST.value
    assert body["executable"] is False
    assert body["search"] is None
    assert fake.call_count == 0


async def test_unsupported_semantics_outcome_never_searches(
    client: AsyncClient, tenant_and_key
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    fake = FakeLLMProvider(
        planner_draft=PlannerDraft(semantic_query="banking AML project experience")
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake

    body = await _post(client, plaintext, "Banking AML project experience is required.")
    assert body["outcome"] == PlannerOutcome.UNSUPPORTED_SEMANTICS.value
    assert body["executable"] is False
    assert body["search"] is None


async def test_ambiguous_request_outcome_never_searches(
    client: AsyncClient, tenant_and_key
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    fake = FakeLLMProvider(planner_draft=PlannerDraft())
    app.dependency_overrides[get_llm_provider] = lambda: fake

    body = await _post(client, plaintext, "Uyğun namizəd tap.")
    assert body["outcome"] == PlannerOutcome.AMBIGUOUS_REQUEST.value
    assert body["executable"] is False
    assert body["search"] is None
    assert fake.call_count == 1


async def test_malformed_model_output_outcome_never_searches(
    client: AsyncClient, tenant_and_key
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    fake = FakeLLMProvider(fail_first_n_calls=2)
    app.dependency_overrides[get_llm_provider] = lambda: fake

    # A semantic/free-text request — not one of the deterministic fast
    # path's bounded structured intents (D-026) — so this genuinely
    # exercises the LLM planner call being tested here.
    body = await _post(
        client, plaintext, "Find candidates experienced in modernizing legacy backend systems."
    )
    assert body["outcome"] == PlannerOutcome.MALFORMED_MODEL_OUTPUT.value
    assert body["executable"] is False
    assert body["search"] is None
    assert fake.call_count == 2


async def test_planner_provider_failure_outcome_is_typed_200_not_503(
    client: AsyncClient, tenant_and_key
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    fake = FakeLLMProvider(error=ModelUnavailableError("private provider detail"))
    app.dependency_overrides[get_llm_provider] = lambda: fake

    resp = await client.post(
        "/api/v1/search/natural-language",
        json={"query": "Java candidates", "as_of_date": "2026-01-01"},
        headers=_auth(plaintext),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == PlannerOutcome.PLANNER_PROVIDER_FAILURE.value
    assert body["executable"] is False
    assert body["search"] is None
    assert "private provider detail" not in resp.text


async def test_validation_failure_outcome_never_searches(
    client: AsyncClient, tenant_and_key
) -> None:
    _tenant, _key, plaintext = tenant_and_key
    fake = FakeLLMProvider(
        planner_draft=PlannerDraft(required_filters=RequiredFilters(skills=["Java"])),
        planner_provenance=LLMResultProvenance(
            provider="fake", model_name="other-model", model_revision=""
        ),
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake

    body = await _post(client, plaintext, "Java candidates")
    assert body["outcome"] == PlannerOutcome.VALIDATION_FAILURE.value
    assert body["executable"] is False
    assert body["search"] is None


async def test_infrastructure_failure_is_503_not_a_typed_outcome(
    client: AsyncClient, tenant_and_key
) -> None:
    """Genuine embedding/DB infra failure (distinct from the typed
    PLANNER_PROVIDER_FAILURE outcome) maps to 503 — mirrors
    ui/router.py's except clause."""
    _tenant, _key, plaintext = tenant_and_key
    fake = FakeLLMProvider(
        planner_draft=PlannerDraft(semantic_query="Python backend engineer")
    )
    app.dependency_overrides[get_llm_provider] = lambda: fake
    app.dependency_overrides[get_embedding_provider] = lambda: _FailingEmbedding()

    resp = await client.post(
        "/api/v1/search/natural-language",
        json={"query": "Python backend engineer", "as_of_date": "2026-01-01"},
        headers=_auth(plaintext),
    )
    assert resp.status_code == 503


class _FailingEmbedding:
    provider_name = "fake-embedding"
    model_name = "fake-embedding-model-v1"
    model_revision = ""

    async def embed(self, text: str):
        raise EmbeddingUnavailableError("simulated outage")


async def test_search_nl_requires_auth(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/search/natural-language",
        json={"query": "Python candidates", "as_of_date": "2026-01-01"},
    )
    assert resp.status_code == 401


async def test_search_nl_missing_as_of_date_rejected(client: AsyncClient, tenant_and_key) -> None:
    _tenant, _key, plaintext = tenant_and_key
    resp = await client.post(
        "/api/v1/search/natural-language",
        json={"query": "Python candidates"},
        headers=_auth(plaintext),
    )
    assert resp.status_code == 422

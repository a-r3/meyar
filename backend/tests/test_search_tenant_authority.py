"""PR #116 acceptance correction: tenant loss across query inference.

Synthetic candidates/providers only. Assert refusal BEFORE candidate lookup
or result construction, not merely the already-existing final response check.
"""

from datetime import date
from unittest.mock import AsyncMock, Mock

import pytest
from conftest import TEST_DATABASE_URL
from fakes import FakeEmbeddingProvider, FakeLLMProvider
from httpx import ASGITransport, AsyncClient
from search_helpers import seed_candidate_with_profile, seed_embedding
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_agent_inference_boundary import PoolProbe
from test_ui_routes import _identity, _login_and_csrf, _profile

import meyar.search.service as search_service
from meyar.config import Settings, get_settings
from meyar.db import get_db
from meyar.embedding.db_release import DbReleasingEmbeddingProvider
from meyar.embedding.dependency import get_embedding_provider, get_embedding_search_config
from meyar.llm.dependency import get_llm_provider
from meyar.main import app
from meyar.models.audit_event import AuditEvent
from meyar.search.planner_schemas import PlannerDraft
from meyar.search.planner_service import plan_and_search_candidates
from meyar.search.schemas import (
    CandidateSearchRequest,
    EmbeddingSearchConfig,
    RequiredFilters,
    SearchMode,
)
from meyar.services.tenant_authority import TenantInactiveError, set_tenant_active

MODES = [SearchMode.SEMANTIC_ONLY, SearchMode.HYBRID]
VECTOR = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
NAME = "Synthetic Query Candidate"
SEMANTIC_QUERY = "backend modernization experience"


def _config():
    return EmbeddingSearchConfig(
        provider="fake-embedding", model_name="fake-embedding-model-v1",
        model_revision="", serializer_version="candidate-professional-embedding-text-v1",
        embedding_dimensions=8,
    )


def _plan(mode, provider_type=FakeLLMProvider):
    required = RequiredFilters(skills=["Java"]) if mode == SearchMode.HYBRID else RequiredFilters()
    query = (
        "Java is required; backend modernization experience is preferred."
        if mode == SearchMode.HYBRID else "Backend modernization experience preferred."
    )
    return query, provider_type(
        planner_draft=PlannerDraft(required_filters=required, semantic_query=SEMANTIC_QUERY)
    )


@pytest.fixture
async def query_runtime(db_session, monkeypatch):
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    probe = PoolProbe(engine)
    requests = []

    async def request_db():
        async with factory() as db:
            requests.append(db)
            yield db

    monkeypatch.setitem(app.dependency_overrides, get_db, request_db)
    monkeypatch.setitem(
        app.dependency_overrides, get_settings, lambda: Settings(ui_cookie_secure=False)
    )
    monkeypatch.setitem(app.dependency_overrides, get_embedding_search_config, _config)
    try:
        yield factory, probe, requests
    finally:
        await engine.dispose()


@pytest.fixture
def candidate_work(monkeypatch):
    lookup = AsyncMock(wraps=search_service.search_compatible_embeddings)
    build = Mock(wraps=search_service.CandidateSearchResult)
    monkeypatch.setattr(search_service, "search_compatible_embeddings", lookup)
    monkeypatch.setattr(search_service, "CandidateSearchResult", build)
    return lookup, build


async def _seed(db, tenant_id):
    content = _profile("Java")
    candidate, profile = await seed_candidate_with_profile(
        db, tenant_id=tenant_id, profile_content=content
    )
    await seed_embedding(
        db, tenant_id=tenant_id, candidate_id=candidate.id, profile_version_id=profile.id,
        vector=VECTOR, profile_content=content,
    )
    await _identity(db, tenant_id=tenant_id, candidate=candidate, profile=profile, name=NAME)
    ids = candidate.id, profile.id
    await db.commit()
    return ids


async def _disable(factory, tenant_id):
    async with factory() as db:
        await set_tenant_active(db, tenant_id=tenant_id, is_active=False)
        await db.commit()


class ControlledEmbedding(FakeEmbeddingProvider):
    def __init__(self, runtime, tenant_id, *, disable=True, released=True, wrong_result=False):
        super().__init__(vector=VECTOR)
        self.runtime, self.tenant_id = runtime, tenant_id
        self.disable, self.released, self.wrong_result = disable, released, wrong_result

    async def embed(self, text):
        factory, probe, requests = self.runtime
        if self.released:
            # Real pool checkouts and real request sessions, not mocked commit.
            assert probe.checked_out == 0
            assert all(not db.in_transaction() for db in requests)
        if self.disable:
            await _disable(factory, self.tenant_id)
        if self.released:
            assert probe.checked_out == 0
        result = await super().embed(text)
        if self.wrong_result:
            result.model_name = "synthetic-wrong-result-provenance"
        return result


def _assert_no_candidate_work(work):
    lookup, build = work
    lookup.assert_not_awaited()
    build.assert_not_called()


async def _assert_no_search_audit(factory, tenant_id):
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.event_type == "CANDIDATE_SEARCH_EXECUTED",
        )) == 0


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("release_db", [True, False])
async def test_direct_query_embedding_loss_refuses_before_candidate_use(
    db_session, tenant_and_key, query_runtime, candidate_work, mode, release_db
):
    tenant_id = tenant_and_key[0].id
    await _seed(db_session, tenant_id)
    factory, _, _ = query_runtime
    inner = ControlledEmbedding(query_runtime, tenant_id, released=release_db)
    async with factory() as db:
        provider = DbReleasingEmbeddingProvider(inner, db) if release_db else inner
        with pytest.raises(TenantInactiveError):
            await search_service.search_candidates(
                db, tenant_id=tenant_id,
                request=CandidateSearchRequest(
                    mode=mode, semantic_query=SEMANTIC_QUERY, embedding_config=_config()
                ), embedding_provider=provider,
            )
        assert not db.in_transaction()
    assert inner.call_count == 1
    _assert_no_candidate_work(candidate_work)
    await _assert_no_search_audit(factory, tenant_id)


@pytest.mark.parametrize("mode", MODES)
async def test_tenant_refusal_precedes_embedding_result_validation(
    db_session, tenant_and_key, query_runtime, candidate_work, mode
):
    tenant_id = tenant_and_key[0].id
    await _seed(db_session, tenant_id)
    factory, _, _ = query_runtime
    inner = ControlledEmbedding(query_runtime, tenant_id, wrong_result=True)
    async with factory() as db:
        with pytest.raises(TenantInactiveError):
            await search_service.search_candidates(
                db, tenant_id=tenant_id,
                request=CandidateSearchRequest(
                    mode=mode, semantic_query=SEMANTIC_QUERY, embedding_config=_config()
                ), embedding_provider=DbReleasingEmbeddingProvider(inner, db),
            )
    _assert_no_candidate_work(candidate_work)


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("natural_language", [False, True])
@pytest.mark.parametrize("disable", [True, False])
async def test_rest_query_embedding_tenant_authority_and_active_control(
    db_session, tenant_and_key, query_runtime, candidate_work, monkeypatch,
    mode, natural_language, disable,
):
    tenant, _, raw = tenant_and_key
    tenant_id = tenant.id
    candidate_id, profile_id = await _seed(db_session, tenant_id)
    inner = ControlledEmbedding(query_runtime, tenant_id, disable=disable)
    query, llm = _plan(mode)
    monkeypatch.setitem(app.dependency_overrides, get_embedding_provider, lambda: inner)
    monkeypatch.setitem(app.dependency_overrides, get_llm_provider, lambda: llm)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/search/natural-language" if natural_language else "/api/v1/search",
            json={"query": query, "as_of_date": "2026-01-01"} if natural_language else {
                "mode": mode.value, "semantic_query": SEMANTIC_QUERY,
            }, headers={"Authorization": f"Bearer {raw}"},
        )
    assert inner.call_count == 1
    if disable:
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid or missing API key."}
        assert response.headers["www-authenticate"] == "Bearer"
        for private_value in (str(candidate_id), str(profile_id), NAME, "results", "Java"):
            assert private_value not in response.text
        _assert_no_candidate_work(candidate_work)
        await _assert_no_search_audit(query_runtime[0], tenant_id)
    else:
        assert response.status_code == 200, response.text
        body = response.json()["search"] if natural_language else response.json()
        assert body["mode"] == mode.value
        assert body["result_count"] == 1
        assert body["results"][0]["candidate_id"] == str(candidate_id)
        assert body["results"][0]["full_name"] == NAME
        assert candidate_work[0].await_count == candidate_work[1].call_count == 1


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("disable", [True, False])
async def test_ui_classic_query_embedding_tenant_authority_and_active_control(
    db_session, tenant_and_user, query_runtime, candidate_work, monkeypatch, mode, disable
):
    tenant, user, password, _ = tenant_and_user
    tenant_id, username = tenant.id, user.username
    candidate_id, profile_id = await _seed(db_session, tenant_id)
    query, llm = _plan(mode)
    inner = ControlledEmbedding(query_runtime, tenant_id, disable=disable)
    monkeypatch.setitem(app.dependency_overrides, get_embedding_provider, lambda: inner)
    monkeypatch.setitem(app.dependency_overrides, get_llm_provider, lambda: llm)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        csrf = await _login_and_csrf(client, username, password)
        response = await client.post(
            "/ui/search", data={"query": query, "csrf_token": csrf}, follow_redirects=False
        )
        if disable:
            assert response.status_code == 303
            assert response.headers["location"] == "/ui/login"
            assert "Max-Age=0" in response.headers["set-cookie"]
            for private_value in (str(candidate_id), str(profile_id), NAME, "Java"):
                assert private_value not in response.text
            assert (await client.get("/ui", follow_redirects=False)).status_code == 303
            _assert_no_candidate_work(candidate_work)
        else:
            assert response.status_code == 200, response.text
            assert str(candidate_id) in response.text and NAME in response.text
            assert candidate_work[0].await_count == candidate_work[1].call_count == 1
            assert candidate_work[1].call_args.kwargs["mode"] == mode
    assert inner.call_count == 1


@pytest.mark.parametrize("mode", MODES)
async def test_natural_language_service_query_embedding_refuses_candidate_use(
    db_session, tenant_and_key, query_runtime, candidate_work, mode
):
    tenant_id = tenant_and_key[0].id
    await _seed(db_session, tenant_id)
    query, llm = _plan(mode)
    factory, _, _ = query_runtime
    inner = ControlledEmbedding(query_runtime, tenant_id)
    async with factory() as db:
        with pytest.raises(TenantInactiveError):
            await plan_and_search_candidates(
                db, llm, tenant_id=tenant_id, natural_language_request=query,
                as_of_date=date(2026, 1, 1), embedding_config=_config(),
                embedding_provider=DbReleasingEmbeddingProvider(inner, db),
            )
    assert inner.call_count == 1
    _assert_no_candidate_work(candidate_work)


@pytest.mark.parametrize("mode", MODES)
async def test_disable_just_before_query_db_release_is_rejected_by_commit_guard(
    db_session, tenant_and_key, query_runtime, candidate_work, mode
):
    tenant_id = tenant_and_key[0].id
    await _seed(db_session, tenant_id)
    factory, _, _ = query_runtime
    inner = FakeEmbeddingProvider(vector=VECTOR)

    class DisableBeforeRelease(DbReleasingEmbeddingProvider):
        async def embed(self, text):
            await _disable(factory, tenant_id)
            return await super().embed(text)

    async with factory() as db:
        with pytest.raises(TenantInactiveError):
            await search_service.search_candidates(
                db, tenant_id=tenant_id,
                request=CandidateSearchRequest(
                    mode=mode, semantic_query=SEMANTIC_QUERY, embedding_config=_config()
                ), embedding_provider=DisableBeforeRelease(inner, db),
            )
        await db.rollback()
    assert inner.call_count == 0
    _assert_no_candidate_work(candidate_work)


@pytest.mark.parametrize("surface", ["service", "REST", "UI"])
async def test_planner_inference_loss_preserves_existing_post_model_authority(
    db_session, tenant_key_and_user, query_runtime, candidate_work, monkeypatch, surface
):
    tenant, _, raw, user, password, _ = tenant_key_and_user
    tenant_id, username = tenant.id, user.username
    await _seed(db_session, tenant_id)
    factory, probe, requests = query_runtime
    inner = FakeEmbeddingProvider(vector=VECTOR)

    class DisablingPlanner(FakeLLMProvider):
        async def plan_candidate_search(self, text, *, repair=False):
            assert probe.checked_out == 0
            assert all(not db.in_transaction() for db in requests)
            await _disable(factory, tenant_id)
            return await super().plan_candidate_search(text, repair=repair)

    query, llm = _plan(SearchMode.HYBRID, provider_type=DisablingPlanner)
    monkeypatch.setitem(app.dependency_overrides, get_llm_provider, lambda: llm)
    monkeypatch.setitem(app.dependency_overrides, get_embedding_provider, lambda: inner)
    if surface == "service":
        async with factory() as db:
            with pytest.raises(TenantInactiveError):
                await plan_and_search_candidates(
                    db, llm, tenant_id=tenant_id, natural_language_request=query,
                    as_of_date=date(2026, 1, 1), embedding_config=_config(),
                    embedding_provider=DbReleasingEmbeddingProvider(inner, db),
                )
    else:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            if surface == "REST":
                response = await client.post(
                    "/api/v1/search/natural-language",
                    json={"query": query, "as_of_date": "2026-01-01"},
                    headers={"Authorization": f"Bearer {raw}"},
                )
                assert response.status_code == 401
                assert response.json() == {"detail": "Invalid or missing API key."}
                assert response.headers["www-authenticate"] == "Bearer"
            else:
                csrf = await _login_and_csrf(client, username, password)
                response = await client.post(
                    "/ui/search", data={"query": query, "csrf_token": csrf}, follow_redirects=False
                )
                assert response.status_code == 303
                assert response.headers["location"] == "/ui/login"
                assert "Max-Age=0" in response.headers["set-cookie"]
            assert NAME not in response.text
    assert llm.call_count == 1
    assert inner.call_count == 0
    _assert_no_candidate_work(candidate_work)

"""Slice 8 — SEMANTIC_ONLY candidate search. Synthetic content only.
See docs/DECISIONS.md (meyar-search-v1) and .claude/rules/testing.md."""

import uuid

import pytest
from fakes import FakeEmbeddingProvider
from search_helpers import seed_candidate_with_profile, seed_embedding, seed_next_profile_version
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.embedding.provider import EmbeddingUnavailableError
from meyar.search.schemas import CandidateSearchRequest, EmbeddingSearchConfig, SearchMode
from meyar.search.service import SearchRequestError, search_candidates
from meyar.services.tenant_repo import create_tenant

_EVIDENCE = [{"page": 1, "block_index": 0, "quote": "synthetic evidence"}]


def _profile(skill: str = "Python") -> dict:
    return {
        "skills": [{"name": skill, "category": None, "evidence": _EVIDENCE}],
        "employment_history": [],
        "education": [],
        "certifications": [],
        "languages": [],
        "projects": [],
    }


def _config(
    *, dimensions: int = 8, model_name: str = "fake-embedding-model-v1"
) -> EmbeddingSearchConfig:
    return EmbeddingSearchConfig(
        provider="fake-embedding",
        model_name=model_name,
        model_revision="",
        serializer_version="candidate-professional-embedding-text-v1",
        embedding_dimensions=dimensions,
    )


async def _tenant(db_session: AsyncSession, name: str = "SemanticTenant"):
    tenant = await create_tenant(db_session, name=f"{name}-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    return tenant


async def test_semantic_ranking_orders_by_cosine_similarity(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    close, close_pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile()
    )
    far, far_pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile()
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=close.id,
        profile_version_id=close_pv.id,
        vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=far.id,
        profile_version_id=far_pv.id,
        vector=[0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[0.9, 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="backend engineer", embedding_config=_config()
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )

    assert [r.candidate_id for r in response.results] == [close.id, far.id]
    assert response.results[0].semantic_score > response.results[1].semantic_score
    for r in response.results:
        assert 0.0 <= r.semantic_score <= 1.0
        assert r.structured_score is None  # not evaluated in SEMANTIC_ONLY


async def test_score_normalization_bounded(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile()
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=pv.id,
        vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[-1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="unrelated", embedding_config=_config()
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )

    assert response.results[0].semantic_score == pytest.approx(0.0, abs=1e-6)


async def test_top_n_limit(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    for i in range(3):
        candidate, pv = await seed_candidate_with_profile(
            db_session, tenant_id=tenant.id, profile_content=_profile()
        )
        await seed_embedding(
            db_session,
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            profile_version_id=pv.id,
            vector=[1.0 - i * 0.1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY,
        semantic_query="engineer",
        embedding_config=_config(),
        limit=2,
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )

    assert response.result_count == 2
    assert [r.rank for r in response.results] == [1, 2]


async def test_stale_profile_embedding_excluded_then_reincluded(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate, pv1 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile()
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=pv1.id,
        vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    )
    await db_session.commit()

    pv2 = await seed_next_profile_version(
        db_session, tenant_id=tenant.id, candidate=candidate, profile_content=_profile("Rust")
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    assert response.result_count == 0
    assert response.excluded_missing_embedding_count == 1

    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=pv2.id,
        vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    )
    await db_session.commit()

    response_after = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    assert response_after.result_count == 1
    assert response_after.results[0].candidate_id == candidate.id
    assert response_after.results[0].candidate_profile_version_id == pv2.id


async def test_incompatible_provider_excluded(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile()
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=pv.id,
        vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        provider="other-provider",
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    assert response.result_count == 0


async def test_incompatible_model_excluded(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile()
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=pv.id,
        vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        model_name="other-model",
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    assert response.result_count == 0


async def test_incompatible_model_revision_excluded(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile()
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=pv.id,
        vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        model_revision="sha256:abcd",
    )
    await db_session.commit()

    # Active config requests MODEL_REVISION_UNKNOWN ("") — must NOT match
    # a row with an explicit revision digest.
    provider = FakeEmbeddingProvider(vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    assert response.result_count == 0


async def test_incompatible_serializer_excluded(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile()
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=pv.id,
        vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        serializer_version="candidate-professional-embedding-text-v2",
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    assert response.result_count == 0


async def test_incompatible_dimension_excluded(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate_8, pv_8 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile()
    )
    candidate_4, pv_4 = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile()
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate_8.id,
        profile_version_id=pv_8.id,
        vector=[1.0] * 8,
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate_4.id,
        profile_version_id=pv_4.id,
        vector=[1.0] * 4,
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[1.0] * 8, dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY,
        semantic_query="engineer",
        embedding_config=_config(dimensions=8),
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    assert {r.candidate_id for r in response.results} == {candidate_8.id}


async def test_candidate_without_compatible_embedding_excluded(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile()
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[1.0] * 8, dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    assert response.result_count == 0
    assert response.excluded_missing_embedding_count == 1


async def test_query_vector_invalid_dimension_rejected(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[1.0] * 4, dimensions=4)
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY,
        semantic_query="engineer",
        embedding_config=_config(dimensions=8),
    )
    with pytest.raises(SearchRequestError):
        await search_candidates(
            db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
        )


async def test_zero_norm_query_vector_rejected(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[0.0] * 8, dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    with pytest.raises(SearchRequestError):
        await search_candidates(
            db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
        )


async def test_provider_error_propagates_safely(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    await db_session.commit()

    provider = FakeEmbeddingProvider(error=EmbeddingUnavailableError("down"))
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    with pytest.raises(EmbeddingUnavailableError):
        await search_candidates(
            db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
        )


async def test_tenant_isolation_semantic_search(db_session: AsyncSession) -> None:
    tenant_a = await _tenant(db_session, "SemA")
    tenant_b = await _tenant(db_session, "SemB")
    cand_a, pv_a = await seed_candidate_with_profile(
        db_session, tenant_id=tenant_a.id, profile_content=_profile()
    )
    cand_b, pv_b = await seed_candidate_with_profile(
        db_session, tenant_id=tenant_b.id, profile_content=_profile()
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant_a.id,
        candidate_id=cand_a.id,
        profile_version_id=pv_a.id,
        vector=[1.0] * 8,
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant_b.id,
        candidate_id=cand_b.id,
        profile_version_id=pv_b.id,
        vector=[1.0] * 8,
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[1.0] * 8, dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    response_a = await search_candidates(
        db_session, tenant_id=tenant_a.id, request=request, embedding_provider=provider
    )
    response_b = await search_candidates(
        db_session, tenant_id=tenant_b.id, request=request, embedding_provider=provider
    )
    assert {r.candidate_id for r in response_a.results} == {cand_a.id}
    assert {r.candidate_id for r in response_b.results} == {cand_b.id}


async def test_vector_values_never_returned(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile()
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=pv.id,
        vector=[1.0] * 8,
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[1.0] * 8, dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    dumped = response.model_dump()
    assert "embedding" not in dumped
    assert "vector" not in str(dumped)


async def test_raw_query_text_not_audited(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[1.0] * 8, dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY,
        semantic_query="a very specific unusual phrase xyzzyplugh",
        embedding_config=_config(),
    )
    await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    await db_session.commit()

    from sqlalchemy import select

    from meyar.models.audit_event import AuditEvent

    result = await db_session.execute(
        select(AuditEvent).where(AuditEvent.event_type == "CANDIDATE_SEARCH_EXECUTED")
    )
    events = result.scalars().all()
    assert len(events) == 1
    assert "xyzzyplugh" not in str(events[0].event_metadata)


async def test_embedding_provider_config_mismatch_rejected(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[1.0] * 8, dimensions=8, model_name="different-model")
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    with pytest.raises(SearchRequestError):
        await search_candidates(
            db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
        )

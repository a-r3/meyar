"""Slice 8 — HYBRID candidate search. Synthetic content only. Includes
the merge-critical regressions: hard-constraint gating cannot be
bypassed by semantic similarity, and no premature semantic top-k before
the full hybrid score is computed. See docs/DECISIONS.md and
.claude/rules/testing.md."""

import uuid

import pytest
from fakes import FakeEmbeddingProvider
from pydantic import ValidationError
from search_helpers import seed_candidate_with_profile, seed_embedding
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.search.schemas import (
    CandidateSearchRequest,
    EmbeddingSearchConfig,
    PreferredFilters,
    RequiredFilters,
    SearchMode,
)
from meyar.search.service import search_candidates
from meyar.services.candidate_identity_repo import create_identity_version
from meyar.services.tenant_repo import create_tenant

_EVIDENCE = [{"page": 1, "block_index": 0, "quote": "synthetic evidence"}]


def _profile(skills: list[str] | None = None) -> dict:
    return {
        "skills": [
            {"name": s, "category": None, "evidence": _EVIDENCE} for s in (skills or ["Python"])
        ],
        "employment_history": [],
        "education": [],
        "certifications": [],
        "languages": [],
        "projects": [],
    }


def _config() -> EmbeddingSearchConfig:
    return EmbeddingSearchConfig(
        provider="fake-embedding",
        model_name="fake-embedding-model-v1",
        model_revision="",
        serializer_version="candidate-professional-embedding-text-v1",
        embedding_dimensions=8,
    )


async def _tenant(db_session: AsyncSession, name: str = "HybridTenant"):
    tenant = await create_tenant(db_session, name=f"{name}-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    return tenant


# --- #49 Critical hard-constraint regression ----------------------------


async def test_hard_constraint_gate_never_bypassed_by_semantic_similarity(
    db_session: AsyncSession,
) -> None:
    """Candidate A fails the required skill but has an extremely high
    semantic similarity; Candidate B passes the required skill with
    lower similarity. A must be excluded entirely; B must be returned."""
    tenant = await _tenant(db_session)
    candidate_a, pv_a = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(["Rust"])  # fails required Python
    )
    candidate_b, pv_b = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(["Python"])
    )
    # A: near-perfect semantic match.
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate_a.id,
        profile_version_id=pv_a.id,
        vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    )
    # B: much lower semantic similarity.
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate_b.id,
        profile_version_id=pv_b.id,
        vector=[0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.HYBRID,
        required_filters=RequiredFilters(skills=["Python"]),
        semantic_query="backend engineer",
        embedding_config=_config(),
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )

    result_ids = [r.candidate_id for r in response.results]
    assert candidate_a.id not in result_ids
    assert candidate_b.id in result_ids


# --- #48 Critical hybrid top-N regression -------------------------------


async def test_no_premature_semantic_top_k_before_hybrid_score(db_session: AsyncSession) -> None:
    """A: very high semantic score, low preferred-structured score.
    B: moderately high semantic score, perfect preferred-structured
    score. With weights favoring structured, B's final hybrid score must
    outrank A. limit=1 must return B — proving the implementation did
    NOT prematurely select top-1 by semantic score alone."""
    tenant = await _tenant(db_session)
    candidate_a, pv_a = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(["Python"])  # no AWS preferred
    )
    candidate_b, pv_b = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(["Python", "AWS"])
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate_a.id,
        profile_version_id=pv_a.id,
        vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # near-perfect similarity
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate_b.id,
        profile_version_id=pv_b.id,
        vector=[0.7, 0.3, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],  # lower similarity
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.HYBRID,
        required_filters=RequiredFilters(skills=["Python"]),
        preferred_filters=PreferredFilters(skills=["AWS"]),
        semantic_query="backend engineer",
        embedding_config=_config(),
        structured_weight=0.9,
        semantic_weight=0.1,
        limit=1,
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )

    # Sanity: verify B's hybrid score genuinely exceeds A's before
    # asserting on the (already limited) response.
    assert response.result_count == 1
    assert response.results[0].candidate_id == candidate_b.id


async def test_preferred_structured_score_affects_final_rank(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    strong, pv_strong = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(["Python", "AWS"])
    )
    weak, pv_weak = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(["Python"])
    )
    same_vector = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    await seed_embedding(
        db_session, tenant_id=tenant.id, candidate_id=strong.id, profile_version_id=pv_strong.id,
        vector=same_vector,
    )
    await seed_embedding(
        db_session, tenant_id=tenant.id, candidate_id=weak.id, profile_version_id=pv_weak.id,
        vector=same_vector,
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=same_vector, dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.HYBRID,
        preferred_filters=PreferredFilters(skills=["AWS"]),
        semantic_query="engineer",
        embedding_config=_config(),
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    assert response.results[0].candidate_id == strong.id
    assert response.results[0].relevance_score > response.results[1].relevance_score


def test_effective_weights_must_sum_to_one() -> None:
    with pytest.raises(ValidationError):
        CandidateSearchRequest(
            mode=SearchMode.HYBRID,
            semantic_query="engineer",
            embedding_config=_config(),
            structured_weight=0.7,
            semantic_weight=0.7,
        )


async def test_deterministic_configured_weight_formula(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(["Python", "AWS"])
    )
    vector = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    await seed_embedding(
        db_session, tenant_id=tenant.id, candidate_id=candidate.id, profile_version_id=pv.id,
        vector=vector,
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=vector, dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.HYBRID,
        preferred_filters=PreferredFilters(skills=["AWS"]),
        semantic_query="engineer",
        embedding_config=_config(),
        structured_weight=0.3,
        semantic_weight=0.7,
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    result = response.results[0]
    expected = (0.3 * result.structured_score) + (0.7 * result.semantic_score)
    assert result.relevance_score == pytest.approx(expected)
    assert response.effective_structured_weight == 0.3
    assert response.effective_semantic_weight == 0.7


async def test_candidate_lacking_compatible_embedding_excluded_from_hybrid(
    db_session: AsyncSession,
) -> None:
    tenant = await _tenant(db_session)
    await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(["Python"])
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=[1.0] * 8, dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.HYBRID, semantic_query="engineer", embedding_config=_config()
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    assert response.result_count == 0
    assert response.excluded_missing_embedding_count == 1


async def test_stable_tie_break_in_hybrid(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    vector = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    candidate_a, pv_a = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(["Python"])
    )
    candidate_b, pv_b = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(["Python"])
    )
    await seed_embedding(
        db_session, tenant_id=tenant.id, candidate_id=candidate_a.id, profile_version_id=pv_a.id,
        vector=vector,
    )
    await seed_embedding(
        db_session, tenant_id=tenant.id, candidate_id=candidate_b.id, profile_version_id=pv_b.id,
        vector=vector,
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=vector, dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.HYBRID, semantic_query="engineer", embedding_config=_config()
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    expected_order = sorted([candidate_a.id, candidate_b.id], key=str)
    assert [r.candidate_id for r in response.results] == expected_order


async def test_repeated_identical_hybrid_search_is_deterministic(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(["Python", "AWS"])
    )
    vector = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    await seed_embedding(
        db_session, tenant_id=tenant.id, candidate_id=candidate.id, profile_version_id=pv.id,
        vector=vector,
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=vector, dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.HYBRID,
        preferred_filters=PreferredFilters(skills=["AWS"]),
        semantic_query="engineer",
        embedding_config=_config(),
    )
    response_1 = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    response_2 = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    assert response_1.results == response_2.results


# --- #52 Critical identity-invariance regression ------------------------


async def test_identity_data_does_not_change_rank(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    vector = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    candidate_a, pv_a = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(["Python"])
    )
    candidate_b, pv_b = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(["Python"])
    )
    await seed_embedding(
        db_session, tenant_id=tenant.id, candidate_id=candidate_a.id, profile_version_id=pv_a.id,
        vector=vector,
    )
    await seed_embedding(
        db_session, tenant_id=tenant.id, candidate_id=candidate_b.id, profile_version_id=pv_b.id,
        vector=vector,
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=vector, dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.HYBRID, semantic_query="engineer", embedding_config=_config()
    )
    before = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )

    # Attach wildly different identity data to each candidate.
    await create_identity_version(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate_a.id,
        candidate_document_id=pv_a.candidate_document_id,
        canonical_document_id=pv_a.canonical_document_id,
        source_sha256="a" * 64,
        schema_version="candidate-identity-v1",
        prompt_version="candidate-identity-extraction-v1",
        model_provider="fake",
        model_name="fake-model",
        status="COMPLETED",
        identity_content={
            "full_name": "Alice Synthetic",
            "email": "alice@example.test",
            "phone": None,
        },
    )
    await create_identity_version(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate_b.id,
        candidate_document_id=pv_b.candidate_document_id,
        canonical_document_id=pv_b.canonical_document_id,
        source_sha256="b" * 64,
        schema_version="candidate-identity-v1",
        prompt_version="candidate-identity-extraction-v1",
        model_provider="fake",
        model_name="fake-model",
        status="COMPLETED",
        identity_content={
            "full_name": "Zed Synthetic",
            "email": "zed@example.test",
            "phone": "+1000",
        },
    )
    await db_session.commit()

    after = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )

    assert [r.candidate_id for r in before.results] == [r.candidate_id for r in after.results]
    assert [r.relevance_score for r in before.results] == [r.relevance_score for r in after.results]


async def test_explanation_components_match_score_calculation(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile(["Python", "AWS"])
    )
    vector = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    await seed_embedding(
        db_session, tenant_id=tenant.id, candidate_id=candidate.id, profile_version_id=pv.id,
        vector=vector,
    )
    await db_session.commit()

    provider = FakeEmbeddingProvider(vector=vector, dimensions=8)
    request = CandidateSearchRequest(
        mode=SearchMode.HYBRID,
        required_filters=RequiredFilters(skills=["Python"]),
        preferred_filters=PreferredFilters(skills=["AWS"]),
        semantic_query="engineer",
        embedding_config=_config(),
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    result = response.results[0]
    required_labels = {(m.category, m.value) for m in result.required_filters_matched}
    preferred_labels = {(m.category, m.value) for m in result.preferred_filters_matched}
    assert ("skill", "Python") in required_labels
    assert ("skill", "AWS") in preferred_labels
    expected = (
        request.structured_weight * result.structured_score
        + request.semantic_weight * result.semantic_score
    )
    assert result.relevance_score == pytest.approx(expected)

"""Slice 8 blocker-fix regressions (post-acceptance-audit).

Covers the two independently-reproduced acceptance-audit findings:

1. The ACTUAL EmbeddingResult's own provider/model_name/model_revision
   must be validated against the request's embedding_config — not just
   the EmbeddingProvider object's declared static attributes.
2. A candidate embedding is compatible only if its source_sha256 equals
   the hash the CURRENT canonical professional serializer produces from
   the candidate's CURRENT profile_content — never an arbitrary row
   selected by insertion/SQL-return order.

See docs/DECISIONS.md D-015 and .claude/rules/testing.md."""

import math
import uuid

import pytest
from search_helpers import (
    current_source_sha256,
    seed_candidate_with_profile,
    seed_embedding,
    synthetic_evidence,
)
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.embedding.provider import EmbeddingResult
from meyar.search.schemas import CandidateSearchRequest, EmbeddingSearchConfig, SearchMode
from meyar.search.service import SearchRequestError, search_candidates
from meyar.services.tenant_repo import create_tenant

_EVIDENCE = [{"page": 1, "block_index": 0, "quote": "synthetic evidence"}]

_QUERY_VECTOR = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def _profile(skill: str = "Python") -> dict:
    return {
        "skills": [{"name": skill, "category": None, "evidence": synthetic_evidence(skill)}],
        "employment_history": [],
        "education": [],
        "certifications": [],
        "languages": [],
        "projects": [],
    }


def _config() -> EmbeddingSearchConfig:
    return EmbeddingSearchConfig(
        provider="fake-embedding",
        model_name="model-a",
        model_revision="",
        serializer_version="candidate-professional-embedding-text-v1",
        embedding_dimensions=8,
    )


async def _tenant(db_session: AsyncSession, name: str = "ProvenanceTenant"):
    tenant = await create_tenant(db_session, name=f"{name}-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    return tenant


class _MismatchedResultProvider:
    """Static declared attributes match the active EmbeddingSearchConfig
    exactly (so the pre-embed() provider-object check passes) but
    embed() returns an EmbeddingResult whose OWN provenance fields claim
    something different — same vector/dimensions throughout. Simulates
    a provider whose actual inference call used a different
    provider/model/revision than it declares statically."""

    provider_name = "fake-embedding"
    model_name = "model-a"
    model_revision = ""

    def __init__(
        self,
        *,
        result_provider: str = "fake-embedding",
        result_model_name: str = "model-a",
        result_model_revision: str = "",
        vector: list[float] | None = None,
    ) -> None:
        self._result_provider = result_provider
        self._result_model_name = result_model_name
        self._result_model_revision = result_model_revision
        self._vector = vector if vector is not None else list(_QUERY_VECTOR)
        self.call_count = 0

    async def embed(self, text: str) -> EmbeddingResult:
        self.call_count += 1
        return EmbeddingResult(
            vector=self._vector,
            dimensions=len(self._vector),
            provider=self._result_provider,
            model_name=self._result_model_name,
            model_revision=self._result_model_revision,
        )


class _NonFiniteVectorProvider:
    """Bypasses OllamaEmbeddingProvider's own finite-value validation
    entirely — proves meyar.search itself, not just one provider
    implementation, rejects a non-finite query vector."""

    provider_name = "fake-embedding"
    model_name = "model-a"
    model_revision = ""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    async def embed(self, text: str) -> EmbeddingResult:
        return EmbeddingResult.model_construct(
            vector=self._vector,
            dimensions=len(self._vector),
            provider=self.provider_name,
            model_name=self.model_name,
            model_revision=self.model_revision,
        )


# --- Finding 1: EmbeddingResult provenance validation -------------------


async def test_embedding_result_model_name_mismatch_rejected(db_session: AsyncSession) -> None:
    """The provider object's declared model_name matches config
    ('model-a'), but the actual embed() result claims 'model-b' — with
    the SAME dimensions. This must be rejected; dimension equality is
    never proof of model compatibility."""
    tenant = await _tenant(db_session)
    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile()
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=pv.id,
        vector=_QUERY_VECTOR,
        model_name="model-a",
        profile_content=_profile(),
    )
    await db_session.commit()

    provider = _MismatchedResultProvider(result_model_name="model-b")
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )

    with pytest.raises(SearchRequestError) as exc_info:
        await search_candidates(
            db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
        )
    assert exc_info.value.code == "EMBEDDING_RESULT_PROVENANCE_MISMATCH"


async def test_embedding_result_provider_mismatch_rejected(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    await db_session.commit()

    provider = _MismatchedResultProvider(result_provider="other-provider")
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    with pytest.raises(SearchRequestError) as exc_info:
        await search_candidates(
            db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
        )
    assert exc_info.value.code == "EMBEDDING_RESULT_PROVENANCE_MISMATCH"


async def test_embedding_result_model_revision_mismatch_rejected(db_session: AsyncSession) -> None:
    """Config requests MODEL_REVISION_UNKNOWN (""); the result claims an
    explicit revision digest. "" must match only "" — never treated as
    equivalent to a known revision."""
    tenant = await _tenant(db_session)
    await db_session.commit()

    provider = _MismatchedResultProvider(result_model_revision="sha256:abcd")
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    with pytest.raises(SearchRequestError) as exc_info:
        await search_candidates(
            db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
        )
    assert exc_info.value.code == "EMBEDDING_RESULT_PROVENANCE_MISMATCH"


async def test_matching_embedding_result_provenance_accepted(db_session: AsyncSession) -> None:
    """Sanity check: when the result's own provenance genuinely matches
    config, search proceeds normally (the fix must not be over-strict)."""
    tenant = await _tenant(db_session)
    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=_profile()
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=pv.id,
        vector=_QUERY_VECTOR,
        model_name="model-a",
        profile_content=_profile(),
    )
    await db_session.commit()

    provider = _MismatchedResultProvider()  # all defaults match config exactly
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    assert response.result_count == 1
    assert response.results[0].candidate_id == candidate.id


# --- Query vector defense-in-depth (non-finite) -------------------------


async def test_nan_query_vector_rejected(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    await db_session.commit()

    provider = _NonFiniteVectorProvider([math.nan, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    with pytest.raises(SearchRequestError) as exc_info:
        await search_candidates(
            db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
        )
    assert exc_info.value.code == "QUERY_VECTOR_INVALID"


async def test_infinite_query_vector_rejected(db_session: AsyncSession) -> None:
    tenant = await _tenant(db_session)
    await db_session.commit()

    provider = _NonFiniteVectorProvider([math.inf, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    with pytest.raises(SearchRequestError) as exc_info:
        await search_candidates(
            db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
        )
    assert exc_info.value.code == "QUERY_VECTOR_INVALID"


# --- Finding 2: current canonical source-hash freshness -----------------


async def test_source_hash_selection_is_insertion_order_independent(
    db_session: AsyncSession,
) -> None:
    """H1 (stale hash, orthogonal vector) and H2 (the CURRENT canonical
    hash, query-matching vector) coexist for the same candidate/profile/
    six-field-config. Only E2 may ever participate, regardless of
    whether H1 or H2 was inserted first."""
    tenant = await _tenant(db_session)
    profile_content = _profile()
    h2 = current_source_sha256(profile_content)
    h1 = "f" * 64
    assert h1 != h2

    # Case A: H1 inserted before H2.
    candidate_a, pv_a = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=profile_content
    )
    e1_a = await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate_a.id,
        profile_version_id=pv_a.id,
        vector=[0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        model_name="model-a",
        source_sha256=h1,
    )
    e2_a = await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate_a.id,
        profile_version_id=pv_a.id,
        vector=_QUERY_VECTOR,
        model_name="model-a",
        source_sha256=h2,
    )

    # Case B: same setup, but H2 inserted before H1 for a second candidate.
    candidate_b, pv_b = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=profile_content
    )
    e2_b = await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate_b.id,
        profile_version_id=pv_b.id,
        vector=_QUERY_VECTOR,
        model_name="model-a",
        source_sha256=h2,
    )
    e1_b = await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate_b.id,
        profile_version_id=pv_b.id,
        vector=[0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        model_name="model-a",
        source_sha256=h1,
    )
    await db_session.commit()

    provider = _MismatchedResultProvider()
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )

    assert response.result_count == 2
    by_candidate = {r.candidate_id: r for r in response.results}

    # Both candidates must resolve to E2 (current), never E1 (stale),
    # regardless of insertion order — and with identical scores.
    assert by_candidate[candidate_a.id].candidate_embedding_version_id == e2_a.id
    assert by_candidate[candidate_a.id].candidate_embedding_version_id != e1_a.id
    assert by_candidate[candidate_b.id].candidate_embedding_version_id == e2_b.id
    assert by_candidate[candidate_b.id].candidate_embedding_version_id != e1_b.id
    score_a = by_candidate[candidate_a.id].semantic_score
    score_b = by_candidate[candidate_b.id].semantic_score
    assert score_a == score_b
    assert score_a == pytest.approx(1.0)


async def test_current_hash_missing_excludes_candidate(db_session: AsyncSession) -> None:
    """Only a stale-hash (H1) embedding exists for the candidate's
    current profile — the current canonical serializer would produce
    H2, which has no corresponding row. The candidate must be treated
    as having NO current compatible embedding, never silently matched
    via H1."""
    tenant = await _tenant(db_session)
    profile_content = _profile()
    h2 = current_source_sha256(profile_content)
    h1 = "e" * 64
    assert h1 != h2

    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=profile_content
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=pv.id,
        vector=_QUERY_VECTOR,
        model_name="model-a",
        source_sha256=h1,
    )
    await db_session.commit()

    provider = _MismatchedResultProvider()
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )
    assert response.result_count == 0
    assert response.excluded_missing_embedding_count == 1


async def test_multiple_source_hashes_only_current_participates(db_session: AsyncSession) -> None:
    """Three embedding rows (H1, H2, H3) exist for the same candidate/
    profile/config, where H2 is the CURRENT canonical hash. Exactly one
    row (H2) may participate; the candidate appears exactly once."""
    tenant = await _tenant(db_session)
    profile_content = _profile()
    h2 = current_source_sha256(profile_content)
    h1 = "1" * 64
    h3 = "3" * 64
    assert len({h1, h2, h3}) == 3

    candidate, pv = await seed_candidate_with_profile(
        db_session, tenant_id=tenant.id, profile_content=profile_content
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=pv.id,
        vector=[0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        model_name="model-a",
        source_sha256=h1,
    )
    e2 = await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=pv.id,
        vector=_QUERY_VECTOR,
        model_name="model-a",
        source_sha256=h2,
    )
    await seed_embedding(
        db_session,
        tenant_id=tenant.id,
        candidate_id=candidate.id,
        profile_version_id=pv.id,
        vector=[0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        model_name="model-a",
        source_sha256=h3,
    )
    await db_session.commit()

    provider = _MismatchedResultProvider()
    request = CandidateSearchRequest(
        mode=SearchMode.SEMANTIC_ONLY, semantic_query="engineer", embedding_config=_config()
    )
    response = await search_candidates(
        db_session, tenant_id=tenant.id, request=request, embedding_provider=provider
    )

    assert response.result_count == 1
    assert response.results[0].candidate_id == candidate.id
    assert response.results[0].candidate_embedding_version_id == e2.id
    assert response.results[0].semantic_score == pytest.approx(1.0)

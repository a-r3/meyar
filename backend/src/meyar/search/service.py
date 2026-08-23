"""Slice 8 — internal candidate search service (meyar-search-v1).

search_candidates() is the single entry point. It never queries
CandidateIdentity. It never truncates the semantic candidate set before
computing the final (structured+semantic) score for HYBRID mode — the
full eligible+compatible set is scored, then sorted, then limited (see
docs/DECISIONS.md, "no premature semantic top-k"). No REST endpoint is
added in this slice — Slice 12 owns API productization."""

import hashlib
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.embedding.provider import EmbeddingProvider
from meyar.embedding.serializer import build_professional_embedding_text, compute_source_sha256
from meyar.models.candidate_profile_version import PROFILE_STATUS_COMPLETED
from meyar.schemas.candidate_profile import CandidateProfileExtraction
from meyar.search.policy import (
    SEARCH_POLICY_VERSION,
    compute_hybrid_score,
    cosine_distance_to_similarity,
    is_valid_query_vector,
    normalize_semantic_score,
)
from meyar.search.schemas import (
    CandidateSearchRequest,
    CandidateSearchResponse,
    CandidateSearchResult,
    SearchMode,
)
from meyar.search.structured import evaluate_preferred_filters, evaluate_required_filters
from meyar.services.audit_repo import record_event
from meyar.services.candidate_embedding_repo import search_compatible_embeddings
from meyar.services.candidate_profile_repo import list_current_profile_versions_for_tenant


class SearchRequestError(Exception):
    """Raised for a request/config problem the schema itself cannot
    catch (e.g. the supplied embedding_provider not matching the
    request's embedding_config) — never a partial/undefined search."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _is_zero_vector(vector: list[float]) -> bool:
    return all(v == 0.0 for v in vector)


async def search_candidates(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    request: CandidateSearchRequest,
    embedding_provider: EmbeddingProvider | None = None,
) -> CandidateSearchResponse:
    """Deterministic, tenant-scoped candidate search. `embedding_provider`
    is READ (call .embed()) only for SEMANTIC_ONLY/HYBRID — STRUCTURED_ONLY
    never invokes it, even if one is supplied (hard regression requirement,
    see docs/DECISIONS.md and test_search_structured.py)."""
    needs_semantic = request.mode in (SearchMode.SEMANTIC_ONLY, SearchMode.HYBRID)
    as_of_year = request.as_of_date.year if request.as_of_date else None

    profile_versions = await list_current_profile_versions_for_tenant(db, tenant_id=tenant_id)

    # (candidate_id, profile_version_id, profile, profile_content, required_matches)
    eligible: list[tuple[uuid.UUID, uuid.UUID, CandidateProfileExtraction, dict, list]] = []
    for version in profile_versions:
        if version.status != PROFILE_STATUS_COMPLETED or version.profile_content is None:
            continue
        try:
            profile = CandidateProfileExtraction.model_validate(version.profile_content)
        except Exception:  # noqa: BLE001 - malformed stored content is simply not searchable
            continue
        required_result = evaluate_required_filters(
            profile, request.required_filters, as_of_year=as_of_year
        )
        if not required_result.satisfied:
            continue
        eligible.append(
            (
                version.candidate_id,
                version.id,
                profile,
                version.profile_content,
                required_result.matches,
            )
        )

    eligible_profile_count = len(eligible)

    # structured_score is only meaningful for STRUCTURED_ONLY/HYBRID —
    # SEMANTIC_ONLY leaves it as None ("not evaluated in this mode").
    structured_scores: dict[uuid.UUID, tuple[float | None, list]] = {}
    if request.mode in (SearchMode.STRUCTURED_ONLY, SearchMode.HYBRID):
        for candidate_id, _pv_id, profile, _content, _req_matches in eligible:
            preferred_result = evaluate_preferred_filters(
                profile, request.preferred_filters, as_of_year=as_of_year
            )
            structured_scores[candidate_id] = (preferred_result.score, preferred_result.matches)
    else:
        for candidate_id, _pv_id, _profile, _content, _req_matches in eligible:
            structured_scores[candidate_id] = (None, [])

    semantic_scores: dict[uuid.UUID, tuple[float, uuid.UUID]] = {}
    compatible_embedding_count = 0
    excluded_missing_embedding_count = 0

    if needs_semantic:
        config = request.embedding_config
        assert config is not None  # enforced by CandidateSearchRequest validation
        if embedding_provider is None:
            raise SearchRequestError(
                "EMBEDDING_PROVIDER_REQUIRED",
                "An embedding_provider is required for SEMANTIC_ONLY/HYBRID search.",
            )
        if (
            embedding_provider.provider_name != config.provider
            or embedding_provider.model_name != config.model_name
            or embedding_provider.model_revision != config.model_revision
        ):
            raise SearchRequestError(
                "EMBEDDING_PROVIDER_CONFIG_MISMATCH",
                "The supplied embedding_provider does not match the request's "
                "embedding_config (provider/model_name/model_revision).",
            )

        assert request.semantic_query is not None  # enforced by request validation
        query_text = request.semantic_query.strip()
        embed_result = await embedding_provider.embed(query_text)

        # Defect fix (post-acceptance-audit): validate the ACTUAL
        # EmbeddingResult's own provenance fields, not just the provider
        # OBJECT's declared static attributes checked above. A provider
        # whose static attributes match config but whose embed() call
        # itself used/reports a different provider/model/revision must
        # be rejected — dimension equality is never proof of model
        # compatibility. "" (MODEL_REVISION_UNKNOWN) matches only "".
        if (
            embed_result.provider != config.provider
            or embed_result.model_name != config.model_name
            or embed_result.model_revision != config.model_revision
        ):
            raise SearchRequestError(
                "EMBEDDING_RESULT_PROVENANCE_MISMATCH",
                "The embedding result's own provider/model_name/model_revision does "
                "not match the request's embedding_config — the embedding call itself "
                "may have used a different model than declared.",
            )
        if not is_valid_query_vector(embed_result.vector):
            raise SearchRequestError(
                "QUERY_VECTOR_INVALID",
                "Query embedding vector must be non-empty and contain only finite "
                "numeric values.",
            )
        if (
            embed_result.dimensions != config.embedding_dimensions
            or len(embed_result.vector) != config.embedding_dimensions
        ):
            raise SearchRequestError(
                "QUERY_VECTOR_DIMENSION_MISMATCH",
                f"Query embedding produced {len(embed_result.vector)} dimensions, "
                f"expected {config.embedding_dimensions}.",
            )
        if _is_zero_vector(embed_result.vector):
            raise SearchRequestError(
                "QUERY_VECTOR_ZERO_NORM",
                "Query embedding is a zero-norm vector; cosine similarity is undefined.",
            )

        # Defect fix (post-acceptance-audit): a candidate embedding is
        # compatible only if its source_sha256 equals the hash the
        # CURRENT canonical professional serializer produces from this
        # candidate's CURRENT profile_content — never merely a
        # profile-version + six-field-config match. This is provenance-
        # based freshness, not chronology-based (no ORDER BY / MAX(id)
        # substitute) — see docs/DECISIONS.md D-015.
        profile_version_source_hashes = {
            pv_id: compute_source_sha256(build_professional_embedding_text(profile_content))
            for _cid, pv_id, _profile, profile_content, _matches in eligible
        }
        rows = await search_compatible_embeddings(
            db,
            tenant_id=tenant_id,
            profile_version_source_hashes=profile_version_source_hashes,
            provider=config.provider,
            model_name=config.model_name,
            model_revision=config.model_revision,
            serializer_version=config.serializer_version,
            embedding_dimensions=config.embedding_dimensions,
            query_vector=embed_result.vector,
        )
        for candidate_id, _pv_id, matched_embedding_version_id, distance in rows:
            similarity = cosine_distance_to_similarity(distance)
            semantic_scores[candidate_id] = (
                normalize_semantic_score(similarity),
                matched_embedding_version_id,
            )

        compatible_embedding_count = len(semantic_scores)
        excluded_missing_embedding_count = eligible_profile_count - compatible_embedding_count

    ranked: list[CandidateSearchResult] = []
    for candidate_id, pv_id, _profile, _content, required_matches in eligible:
        structured_score, preferred_matches = structured_scores.get(candidate_id, (None, []))
        semantic_entry = semantic_scores.get(candidate_id)

        if needs_semantic and semantic_entry is None:
            continue  # excluded: no current compatible embedding (see docs/DECISIONS.md)

        semantic_score = semantic_entry[0] if semantic_entry is not None else None
        embedding_version_id = semantic_entry[1] if semantic_entry is not None else None

        if request.mode == SearchMode.STRUCTURED_ONLY:
            relevance = structured_score if structured_score is not None else 0.0
        elif request.mode == SearchMode.SEMANTIC_ONLY:
            relevance = semantic_score if semantic_score is not None else 0.0
        else:  # HYBRID
            relevance = compute_hybrid_score(
                structured_score or 0.0,
                semantic_score or 0.0,
                request.structured_weight,
                request.semantic_weight,
            )

        ranked.append(
            CandidateSearchResult(
                candidate_id=candidate_id,
                rank=0,  # assigned below, after the full set is sorted
                mode=request.mode,
                relevance_score=relevance,
                structured_score=structured_score,
                semantic_score=semantic_score,
                required_filters_matched=required_matches,
                preferred_filters_matched=preferred_matches,
                candidate_profile_version_id=pv_id,
                candidate_embedding_version_id=embedding_version_id,
                search_policy_version=SEARCH_POLICY_VERSION,
            )
        )

    # Deterministic sort: relevance descending, then candidate_id
    # ascending as a stable, non-PII tie-break (never name/email/phone,
    # never insertion order).
    ranked.sort(key=lambda r: (-r.relevance_score, str(r.candidate_id)))
    limited = ranked[: request.limit]
    for i, search_result in enumerate(limited, start=1):
        search_result.rank = i

    audit_metadata: dict = {
        "mode": request.mode.value,
        "result_count": len(limited),
        "eligible_count": eligible_profile_count,
        "excluded_missing_embedding_count": excluded_missing_embedding_count,
        "limit": request.limit,
        "search_policy_version": SEARCH_POLICY_VERSION,
    }
    if needs_semantic:
        assert request.embedding_config is not None
        assert request.semantic_query is not None
        audit_metadata.update(
            {
                "provider": request.embedding_config.provider,
                "model_name": request.embedding_config.model_name,
                "model_revision": request.embedding_config.model_revision,
                "serializer_version": request.embedding_config.serializer_version,
                "embedding_dimensions": request.embedding_config.embedding_dimensions,
                "compatible_embedding_count": compatible_embedding_count,
                "query_sha256": hashlib.sha256(
                    request.semantic_query.strip().encode("utf-8")
                ).hexdigest(),
            }
        )
    await record_event(
        db, tenant_id=tenant_id, event_type="CANDIDATE_SEARCH_EXECUTED", metadata=audit_metadata
    )

    return CandidateSearchResponse(
        mode=request.mode,
        policy_version=SEARCH_POLICY_VERSION,
        results=limited,
        result_count=len(limited),
        eligible_profile_count=eligible_profile_count,
        compatible_embedding_count=compatible_embedding_count,
        excluded_missing_embedding_count=excluded_missing_embedding_count,
        limit=request.limit,
        effective_structured_weight=(
            request.structured_weight if request.mode == SearchMode.HYBRID else None
        ),
        effective_semantic_weight=(
            request.semantic_weight if request.mode == SearchMode.HYBRID else None
        ),
        embedding_config=request.embedding_config,
    )

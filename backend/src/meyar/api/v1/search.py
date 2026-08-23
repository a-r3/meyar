"""Slice 12 — thin REST wrappers over the accepted Slice 8/9 search
services. HTTP validates and presents; `meyar.search.service` and
`meyar.search.planner_service` decide. No ranking/matching logic is
duplicated here."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.core.auth import TenantContext, require_scope
from meyar.db import get_db
from meyar.embedding.dependency import get_embedding_provider, get_embedding_search_config
from meyar.embedding.provider import EmbeddingProvider, EmbeddingProviderError
from meyar.llm.dependency import get_llm_provider
from meyar.llm.provider import LLMProvider
from meyar.schemas.api_search import (
    ApiCandidateSearchRequest,
    ApiCandidateSearchResponse,
    ApiCandidateSearchResultItem,
    ApiNaturalLanguageSearchRequest,
    ApiNaturalLanguageSearchResponse,
)
from meyar.schemas.criteria import ProhibitedCriterionError
from meyar.search.planner_service import plan_and_search_candidates
from meyar.search.policy import DEFAULT_SEMANTIC_WEIGHT, DEFAULT_STRUCTURED_WEIGHT
from meyar.search.schemas import (
    CandidateSearchRequest,
    CandidateSearchResponse,
    EmbeddingSearchConfig,
    SearchMode,
)
from meyar.search.service import SearchRequestError, search_candidates
from meyar.ui.service import build_search_result_views
from meyar.ui.view_models import CandidateSearchResultView

router = APIRouter(tags=["search"])


def _to_api_search_response(
    response: CandidateSearchResponse, views: list[CandidateSearchResultView]
) -> ApiCandidateSearchResponse:
    """Adapt the Slice 8 result authority/order plus Slice 11 identity
    enrichment into the external DTO — never re-ranks/re-sorts."""
    items = [
        ApiCandidateSearchResultItem(
            candidate_id=result.candidate_id,
            full_name=view.full_name,
            rank=result.rank,
            mode=result.mode,
            relevance_score=result.relevance_score,
            structured_score=result.structured_score,
            semantic_score=result.semantic_score,
            required_filters_matched=result.required_filters_matched,
            preferred_filters_matched=result.preferred_filters_matched,
            candidate_profile_version_id=result.candidate_profile_version_id,
            search_policy_version=result.search_policy_version,
        )
        for result, view in zip(response.results, views, strict=True)
    ]
    return ApiCandidateSearchResponse(
        mode=response.mode,
        policy_version=response.policy_version,
        results=items,
        result_count=response.result_count,
        limit=response.limit,
    )


@router.post("/search", response_model=ApiCandidateSearchResponse)
async def post_search(
    body: ApiCandidateSearchRequest,
    ctx: TenantContext = Depends(require_scope("candidates:read")),
    db: AsyncSession = Depends(get_db),
    trusted_embedding_config: EmbeddingSearchConfig = Depends(get_embedding_search_config),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider),
) -> ApiCandidateSearchResponse:
    needs_semantic = body.mode in (SearchMode.SEMANTIC_ONLY, SearchMode.HYBRID)
    # STRUCTURED_ONLY must never set embedding_config on the internal
    # request (CandidateSearchRequest's own validator forbids it) and
    # meyar.search.service never calls embedding_provider.embed() unless
    # needs_semantic — the FastAPI dependency object may be constructed,
    # but STRUCTURED_ONLY never invokes it (hard regression requirement).
    embedding_config = trusted_embedding_config if needs_semantic else None

    try:
        internal_request = CandidateSearchRequest(
            mode=body.mode,
            required_filters=body.required_filters,
            preferred_filters=body.preferred_filters,
            semantic_query=body.semantic_query,
            embedding_config=embedding_config,
            as_of_date=body.as_of_date,
            limit=body.limit,
            structured_weight=DEFAULT_STRUCTURED_WEIGHT,
            semantic_weight=DEFAULT_SEMANTIC_WEIGHT,
        )
    except (ValidationError, ProhibitedCriterionError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The search request is invalid.",
        ) from exc

    try:
        response = await search_candidates(
            db,
            tenant_id=ctx.tenant_id,
            request=internal_request,
            embedding_provider=embedding_provider,
        )
        views = await build_search_result_views(db, tenant_id=ctx.tenant_id, response=response)
        await db.commit()
    except (SearchRequestError, EmbeddingProviderError) as exc:
        await db.rollback()
        code = exc.code if isinstance(exc, SearchRequestError) else "EMBEDDING_PROVIDER_ERROR"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=code
        ) from exc

    return _to_api_search_response(response, views)


@router.post("/search/natural-language", response_model=ApiNaturalLanguageSearchResponse)
async def post_natural_language_search(
    body: ApiNaturalLanguageSearchRequest,
    ctx: TenantContext = Depends(require_scope("candidates:read")),
    db: AsyncSession = Depends(get_db),
    llm: LLMProvider = Depends(get_llm_provider),
    embedding_provider: EmbeddingProvider = Depends(get_embedding_provider),
    embedding_config: EmbeddingSearchConfig = Depends(get_embedding_search_config),
) -> ApiNaturalLanguageSearchResponse:
    try:
        planned = await plan_and_search_candidates(
            db,
            llm,
            tenant_id=ctx.tenant_id,
            natural_language_request=body.query,
            as_of_date=body.as_of_date,
            embedding_config=embedding_config,
            embedding_provider=embedding_provider,
        )
        search_api_response = None
        if planned.search_response is not None:
            views = await build_search_result_views(
                db, tenant_id=ctx.tenant_id, response=planned.search_response
            )
            search_api_response = _to_api_search_response(planned.search_response, views)
        await db.commit()
    except (EmbeddingProviderError, SearchRequestError, SQLAlchemyError) as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Search infrastructure is currently unavailable.",
        ) from exc

    return ApiNaturalLanguageSearchResponse(
        outcome=planned.plan.outcome,
        executable=planned.plan.executable,
        reason_codes=planned.plan.reason_codes,
        search=search_api_response,
    )

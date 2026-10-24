"""Candidate / job / evaluation contract routes shared by BOTH API surfaces.

One route factory, two authentication policies (issue #45, D-116):

- machine: ``/api/v1`` — API-key Bearer principal (``require_scope``);
- human:   ``/ui/api/v1`` — BrowserSession cookie principal, plus the
  ``X-CSRF-Token`` header on every state-changing request.

The handlers are identical and call only ``meyar.application`` operations, so
the two surfaces cannot drift. A policy is just "how to get a principal for a
scope"; nothing else differs."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.api.responses import error_responses
from meyar.application import candidates as candidate_ops
from meyar.application import evaluations as evaluation_ops
from meyar.application import jobs as job_ops
from meyar.application.errors import (
    ApplicationError,
    ErrorCode,
    not_found,
    validation_failed,
)
from meyar.application.principal import Principal
from meyar.config import Settings, get_settings
from meyar.db import get_db
from meyar.embedding.dependency import get_embedding_provider, get_embedding_search_config
from meyar.embedding.provider import EmbeddingProvider
from meyar.llm.dependency import get_llm_provider
from meyar.llm.provider import LLMProvider
from meyar.models.job import JOB_STATUS_ACTIVE, JOB_STATUS_ARCHIVED
from meyar.schemas.api_candidates import (
    ApiCandidateDetailResponse,
    ApiCandidateLibraryPage,
    RetryRequest,
    RetryResponse,
)
from meyar.schemas.api_evaluation import (
    ApiBatchRankingResponse,
    ApiBatchRankRequest,
    ApiEvaluationOut,
    ApiScoreCandidateRequest,
    ApiScoreCandidateResponse,
)
from meyar.schemas.api_jobs import ApiJobPage, CriteriaVersionProvenanceOut, JobArchiveOut
from meyar.search.schemas import EmbeddingSearchConfig
from meyar.storage.base import DocumentStorage
from meyar.storage.dependency import get_document_storage

ScopeDependency = Callable[[str], Callable[..., Any]]


@dataclass(frozen=True)
class AuthPolicy:
    """How a surface turns a required scope into a trusted principal."""

    read: ScopeDependency
    write: ScopeDependency
    channel: str  # audit label for shared operations: "API" | "UI_JSON"


def build_contract_router(policy: AuthPolicy, *, tags: dict[str, str]) -> APIRouter:
    """``tags`` maps the groups candidates / jobs / evaluations to OpenAPI tags."""
    router = APIRouter()

    # ---- candidates --------------------------------------------------------

    @router.get(
        "/candidates",
        tags=[tags["candidates"]],
        response_model=ApiCandidateLibraryPage,
        responses=error_responses(401, 403, 422),
        summary="List the candidate library (bounded, newest first)",
    )
    async def list_candidates(
        page: Annotated[int, Query(ge=1, le=100000)] = 1,
        page_size: Annotated[int, Query(ge=1, le=50)] = 25,
        parser_status: Annotated[str | None, Query(max_length=32)] = None,
        profile_status: Annotated[str | None, Query(max_length=32)] = None,
        folder_status: Annotated[str | None, Query(max_length=32)] = None,
        principal: Principal = Depends(policy.read("candidates:read")),
        db: AsyncSession = Depends(get_db),
    ) -> ApiCandidateLibraryPage:
        return await candidate_ops.list_library(
            db, principal, page=page, page_size=page_size,
            parser_status=parser_status, profile_status=profile_status,
            folder_status=folder_status,
        )

    @router.get(
        "/candidates/{candidate_id}/detail",
        tags=[tags["candidates"]],
        response_model=ApiCandidateDetailResponse,
        responses=error_responses(401, 403, 404),
        summary="Candidate facts with evidence, readiness and exact provenance",
    )
    async def get_candidate_detail_enriched(
        candidate_id: uuid.UUID,
        principal: Principal = Depends(policy.read("candidates:read")),
        db: AsyncSession = Depends(get_db),
        embedding_config: EmbeddingSearchConfig = Depends(get_embedding_search_config),
    ) -> ApiCandidateDetailResponse:
        detail = await candidate_ops.get_candidate_detail(
            db, principal, candidate_id=candidate_id, embedding_config=embedding_config
        )
        if detail is None:
            raise ApplicationError(
                ErrorCode.NOT_FOUND, status_code=404, message="Candidate not found.",
                legacy_detail="Candidate not found.",
            )
        return detail

    @router.get(
        "/candidates/{candidate_id}/documents/{document_id}/original",
        tags=[tags["candidates"]],
        responses={
            200: {
                "description": "The original uploaded bytes as an attachment.",
                "content": {
                    "application/pdf": {},
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {},
                },
            },
            **error_responses(401, 403, 404, 503),
        },
        response_class=Response,
        summary="Download the original CV (authorized and audited)",
    )
    async def download_original_cv(
        candidate_id: uuid.UUID,
        document_id: uuid.UUID,
        principal: Principal = Depends(policy.read("candidates:read")),
        db: AsyncSession = Depends(get_db),
        storage: DocumentStorage = Depends(get_document_storage),
    ) -> Response:
        try:
            original = await candidate_ops.retrieve_original_cv(
                db, principal, storage,
                candidate_id=candidate_id, document_id=document_id, channel=policy.channel,
            )
        except candidate_ops.OriginalCvUnavailableError:
            raise ApplicationError(
                ErrorCode.SERVICE_UNAVAILABLE, status_code=503, reason="ORIGINAL_UNAVAILABLE",
                retryable=False,
            ) from None
        if original is None:
            raise not_found("Document not found.")
        return Response(
            content=original.content,
            media_type=original.media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{original.filename}"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post(
        "/candidates/{candidate_id}/processing/retry",
        tags=[tags["candidates"]],
        response_model=RetryResponse,
        responses=error_responses(401, 403, 404, 409, 422, 503),
        summary="Re-run one supported processing stage",
    )
    async def retry_candidate_processing(
        candidate_id: uuid.UUID,
        body: RetryRequest,
        principal: Principal = Depends(policy.write("candidates:write")),
        db: AsyncSession = Depends(get_db),
        settings: Settings = Depends(get_settings),
        llm: LLMProvider = Depends(get_llm_provider),
        embedding_provider: EmbeddingProvider = Depends(get_embedding_provider),
        embedding_config: EmbeddingSearchConfig = Depends(get_embedding_search_config),
    ) -> RetryResponse:
        result = await candidate_ops.retry_processing(
            db, principal, settings=settings, llm=llm, embedding_provider=embedding_provider,
            embedding_config=embedding_config, candidate_id=candidate_id,
            stage=body.stage, document_id=body.document_id,
        )
        if result is None:
            raise not_found("Candidate or document not found.")
        return result

    # ---- jobs --------------------------------------------------------------

    @router.get(
        "/jobs",
        tags=[tags["jobs"]],
        response_model=ApiJobPage,
        responses=error_responses(401, 403, 422),
        summary="List jobs by lifecycle status (bounded, newest first)",
    )
    async def list_jobs(
        job_status: Annotated[str, Query(alias="status", max_length=16)] = JOB_STATUS_ACTIVE,
        page: Annotated[int, Query(ge=1, le=100000)] = 1,
        page_size: Annotated[int, Query(ge=1, le=50)] = 25,
        principal: Principal = Depends(policy.read("jobs:read")),
        db: AsyncSession = Depends(get_db),
    ) -> ApiJobPage:
        normalized = job_status.strip().upper()
        if normalized not in (JOB_STATUS_ACTIVE, JOB_STATUS_ARCHIVED):
            raise validation_failed("UNSUPPORTED_FILTER", "The job status filter is not supported.")
        return await job_ops.list_jobs(
            db, principal, status=normalized, page=page, page_size=page_size
        )

    @router.post(
        "/jobs/{job_id}/archive",
        tags=[tags["jobs"]],
        response_model=JobArchiveOut,
        responses=error_responses(401, 403, 404),
        summary="Archive a job (soft, idempotent; criteria and evaluations are kept)",
    )
    async def archive_job_endpoint(
        job_id: uuid.UUID,
        principal: Principal = Depends(policy.write("jobs:write")),
        db: AsyncSession = Depends(get_db),
    ) -> JobArchiveOut:
        result = await job_ops.archive_job_for(db, principal, job_id=job_id)
        if result is None:
            raise ApplicationError(
                ErrorCode.NOT_FOUND, status_code=404, message="Job not found.",
                legacy_detail="Job not found.",
            )
        return result

    @router.get(
        "/jobs/{job_id}/criteria/{version_number}/provenance",
        tags=[tags["jobs"]],
        response_model=CriteriaVersionProvenanceOut,
        responses=error_responses(401, 403, 404),
        summary="Authoring and source provenance of one criteria version",
    )
    async def get_criteria_version_provenance(
        job_id: uuid.UUID,
        version_number: int,
        principal: Principal = Depends(policy.read("jobs:read")),
        db: AsyncSession = Depends(get_db),
    ) -> CriteriaVersionProvenanceOut:
        result = await job_ops.get_criteria_provenance(
            db, principal, job_id=job_id, version_number=version_number
        )
        if result is None:
            raise not_found("Criteria version not found.")
        return result

    # ---- evaluations -------------------------------------------------------

    @router.post(
        "/jobs/{job_id}/criteria/{version_number}/score",
        tags=[tags["evaluations"]],
        response_model=ApiScoreCandidateResponse,
        responses=error_responses(401, 403, 404, 422),
        summary="Deterministically score one candidate",
    )
    async def post_score_candidate(
        job_id: uuid.UUID,
        version_number: int,
        body: ApiScoreCandidateRequest,
        principal: Principal = Depends(policy.write("evaluations:write")),
        db: AsyncSession = Depends(get_db),
        settings: Settings = Depends(get_settings),
    ) -> ApiScoreCandidateResponse:
        return await evaluation_ops.score_candidate(
            db, principal, settings=settings, job_id=job_id, version_number=version_number,
            candidate_id=body.candidate_id,
            evaluation_as_of_date=body.evaluation_as_of_date,
            candidate_profile_version_id=body.candidate_profile_version_id,
        )

    @router.post(
        "/jobs/{job_id}/criteria/{version_number}/rank",
        tags=[tags["evaluations"]],
        response_model=ApiBatchRankingResponse,
        responses=error_responses(401, 403, 404, 409, 422),
        summary="Deterministically rank the library for a criteria version",
    )
    async def post_rank_job(
        job_id: uuid.UUID,
        version_number: int,
        body: ApiBatchRankRequest,
        principal: Principal = Depends(policy.write("evaluations:write")),
        db: AsyncSession = Depends(get_db),
        settings: Settings = Depends(get_settings),
    ) -> ApiBatchRankingResponse:
        return await evaluation_ops.rank_candidates(
            db, principal, settings=settings, job_id=job_id, version_number=version_number,
            evaluation_as_of_date=body.evaluation_as_of_date,
        )

    @router.get(
        "/evaluations/{evaluation_id}",
        tags=[tags["evaluations"]],
        response_model=ApiEvaluationOut,
        responses=error_responses(401, 403, 404),
        summary="One persisted, immutable evaluation with its full explanation",
    )
    async def get_evaluation_endpoint(
        evaluation_id: uuid.UUID,
        principal: Principal = Depends(policy.read("evaluations:read")),
        db: AsyncSession = Depends(get_db),
    ) -> ApiEvaluationOut:
        result = await evaluation_ops.get_persisted_evaluation(
            db, principal, evaluation_id=evaluation_id
        )
        if result is None:
            raise not_found("Evaluation not found.")
        return result

    return router

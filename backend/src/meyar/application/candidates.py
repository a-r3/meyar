"""Candidate use cases shared by the machine REST, human JSON and Jinja
adapters (issue #45, D-116).

Every function takes an already-trusted ``Principal`` and tenant-scopes every
read; a foreign or missing id yields the same "not found" result, so existence
is never revealed across tenants."""

import uuid
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.application.errors import ApplicationError, ErrorCode, conflict, validation_failed
from meyar.application.principal import Principal
from meyar.config import Settings
from meyar.embedding.provider import EmbeddingBusyError, EmbeddingProvider, EmbeddingProviderError
from meyar.embedding.serializer import embedding_source
from meyar.extraction.deferral import ExtractionDeferredError
from meyar.extraction.service import ExtractionPreconditionError, extract_candidate_profile
from meyar.ingestion.validation import PDF_MIME
from meyar.llm.provider import LLMProvider
from meyar.models.candidate_document import PARSER_STATUS_PARSED
from meyar.models.candidate_profile_version import PROFILE_STATUS_COMPLETED
from meyar.schemas.api_candidates import (
    ApiCandidateDetailResponse,
    ApiCandidateLibraryItem,
    ApiCandidateLibraryPage,
    CandidateProvenanceOut,
    CandidateReadinessOut,
    ReadinessState,
    RetryResponse,
    RetryStage,
)
from meyar.search.schemas import EmbeddingSearchConfig
from meyar.services.audit_repo import record_event
from meyar.services.candidate_document_repo import (
    get_candidate_document,
    list_candidate_documents,
)
from meyar.services.candidate_embedding_repo import (
    EmbeddingCompatibility,
    find_compatible_embedding,
)
from meyar.services.candidate_embedding_service import (
    EmbeddingPreconditionError,
    embed_candidate_profile,
)
from meyar.services.candidate_profile_repo import (
    get_effective_profile_version,
    get_latest_profile_attempt,
    get_latest_profile_version_for_document,
)
from meyar.services.profile_authority import ProfileAuthorityError, authorize_profile_version
from meyar.storage.base import DocumentStorage
from meyar.ui.service import (
    UIServiceInputError,
    get_candidate_detail_view,
    list_candidate_library,
)


def readiness_state_for(
    profile_status: str | None, parser_status: str | None
) -> ReadinessState:
    """Deterministic coarse state from existing, already-truthful statuses."""
    if profile_status == PROFILE_STATUS_COMPLETED:
        return ReadinessState.READY
    if profile_status is None:
        # No extraction attempt yet. A terminal parse failure will never be
        # picked up, so it needs attention rather than "processing".
        return (
            ReadinessState.NEEDS_ATTENTION
            if parser_status == "PARSE_FAILED"
            else ReadinessState.PROCESSING
        )
    return ReadinessState.NEEDS_ATTENTION  # FAILED / MANUAL_REVIEW_REQUIRED / UNAVAILABLE


async def list_library(
    db: AsyncSession,
    principal: Principal,
    *,
    page: int,
    page_size: int,
    parser_status: str | None,
    profile_status: str | None,
    folder_status: str | None,
) -> ApiCandidateLibraryPage:
    """Bounded, deterministically ordered (newest first, then id) tenant library.

    Supported filters are the closed status sets of ``list_candidate_library``;
    anything else is a validation error, never silently ignored."""
    try:
        result = await list_candidate_library(
            db,
            tenant_id=principal.tenant_id,
            page=page,
            page_size=page_size,
            parser_status=parser_status,
            profile_status=profile_status,
            folder_status=folder_status,
        )
    except UIServiceInputError:
        raise validation_failed(
            "UNSUPPORTED_FILTER", "The library filter is not supported."
        ) from None
    return ApiCandidateLibraryPage(
        items=[
            ApiCandidateLibraryItem(
                **item.model_dump(),
                readiness=CandidateReadinessOut(
                    state=readiness_state_for(
                        item.current_profile_status, item.latest_parser_status
                    ),
                    latest_parser_status=item.latest_parser_status,
                    profile_status=item.current_profile_status,
                    current_profile_available=item.current_profile_version is not None,
                ),
            )
            for item in result.items
        ],
        page=result.page,
        page_size=result.page_size,
        total=result.total,
        has_previous=result.has_previous,
        has_next=result.has_next,
    )


async def _retryable_stages(
    db: AsyncSession,
    principal: Principal,
    *,
    candidate_id: uuid.UUID,
    embedding_config: EmbeddingSearchConfig,
) -> list[RetryStage]:
    stages: list[RetryStage] = []
    documents = await list_candidate_documents(
        db, tenant_id=principal.tenant_id, candidate_id=candidate_id
    )
    for document in documents:
        if document.parser_status != PARSER_STATUS_PARSED:
            continue
        latest = await get_latest_profile_version_for_document(
            db, tenant_id=principal.tenant_id, candidate_document_id=document.id
        )
        if latest is None or latest.status != PROFILE_STATUS_COMPLETED:
            stages.append(RetryStage.PROFILE_EXTRACTION)
            break
    effective = await get_effective_profile_version(
        db, tenant_id=principal.tenant_id, candidate_id=candidate_id
    )
    if effective is not None:
        try:
            authorized = await authorize_profile_version(db, version=effective)
        except ProfileAuthorityError:
            authorized = None
        if authorized is not None:
            _text, source_sha256 = embedding_source(authorized.model_dump(mode="json"))
            compatible = await find_compatible_embedding(
                db,
                tenant_id=principal.tenant_id,
                candidate_profile_version_id=effective.id,
                source_sha256=source_sha256,
                compatibility=EmbeddingCompatibility(**embedding_config.model_dump()),
            )
            if compatible is None:
                stages.append(RetryStage.EMBEDDING)
    return stages


async def get_candidate_detail(
    db: AsyncSession,
    principal: Principal,
    *,
    candidate_id: uuid.UUID,
    embedding_config: EmbeddingSearchConfig,
) -> ApiCandidateDetailResponse | None:
    view = await get_candidate_detail_view(
        db, tenant_id=principal.tenant_id, candidate_id=candidate_id
    )
    if view is None:
        return None
    effective = await get_effective_profile_version(
        db, tenant_id=principal.tenant_id, candidate_id=candidate_id
    )
    latest = await get_latest_profile_attempt(
        db, tenant_id=principal.tenant_id, candidate_id=candidate_id
    )
    provenance = None
    if effective is not None and view.profile_version is not None:
        provenance = CandidateProvenanceOut(
            profile_version_id=effective.id,
            profile_version=effective.version_number,
            profile_candidate_document_id=effective.candidate_document_id,
            profile_schema_version=effective.schema_version,
            extraction_model_provider=effective.model_provider,
            extraction_model_name=effective.model_name,
            extraction_prompt_version=effective.prompt_version,
            profile_created_at=effective.created_at,
            latest_attempt_profile_version_id=(
                latest.id if latest is not None and latest.id != effective.id else None
            ),
        )
    readiness = CandidateReadinessOut(
        state=readiness_state_for(view.profile_status, view.latest_parser_status),
        latest_parser_status=view.latest_parser_status,
        profile_status=view.profile_status,
        current_profile_available=view.profile_version is not None,
        retryable_stages=await _retryable_stages(
            db, principal, candidate_id=candidate_id, embedding_config=embedding_config
        ),
    )
    return ApiCandidateDetailResponse(
        **view.model_dump(), readiness=readiness, provenance=provenance
    )


# --- original CV ------------------------------------------------------------


@dataclass(frozen=True)
class OriginalCv:
    content: bytes
    media_type: str
    filename: str


class OriginalCvUnavailableError(Exception):
    """The document row is authorized but its bytes could not be served."""


def original_filename(document_id: uuid.UUID, mime_type: str) -> str:
    """Synthetic, path-free download name; the uploaded filename is never used."""
    extension = "pdf" if mime_type == PDF_MIME else "docx"
    return f"cv-{document_id.hex}.{extension}"


async def retrieve_original_cv(
    db: AsyncSession,
    principal: Principal,
    storage: DocumentStorage,
    *,
    candidate_id: uuid.UUID,
    document_id: uuid.UUID,
    channel: str,
) -> OriginalCv | None:
    """The ONE authorized, audited path to original CV bytes.

    The storage key is resolved here from the tenant-scoped document row — a
    caller only ever supplies the candidate/document ids. Success is audited
    here, AFTER the bytes were read and BEFORE they are returned, so no adapter
    can serve an original without an audit event; a failed audit means the
    bytes are not served. ``None`` = not found in the principal's tenant.
    Audit metadata is ids + the access channel only (no filename, path, key or
    content)."""
    document = await get_candidate_document(
        db, tenant_id=principal.tenant_id, candidate_id=candidate_id, document_id=document_id
    )
    if document is None:
        return None
    mime_type = document.mime_type
    resolved_document_id = document.id
    try:
        content = await storage.read(storage_key=document.storage_key)
    except (OSError, ValueError, KeyError):
        raise OriginalCvUnavailableError from None
    try:
        await record_event(
            db,
            tenant_id=principal.tenant_id,
            event_type="candidate.original_cv.accessed",
            metadata={
                "candidate_id": str(candidate_id),
                "document_id": str(resolved_document_id),
                "channel": channel,
            },
            actor_type=principal.actor_type,
            actor_id=principal.actor_id,
        )
        await db.commit()
    except SQLAlchemyError:
        await db.rollback()
        raise OriginalCvUnavailableError from None
    return OriginalCv(
        content=content,
        media_type=mime_type,
        filename=original_filename(resolved_document_id, mime_type),
    )


# --- supported retries ------------------------------------------------------


async def retry_processing(
    db: AsyncSession,
    principal: Principal,
    *,
    settings: Settings,
    llm: LLMProvider,
    embedding_provider: EmbeddingProvider,
    embedding_config: EmbeddingSearchConfig,
    candidate_id: uuid.UUID,
    stage: RetryStage,
    document_id: uuid.UUID | None,
) -> RetryResponse | None:
    """Re-run ONE supported processing stage synchronously, bounded by the same
    local-inference admission as every other local-model call. Returns ``None``
    when the candidate/document is not in the principal's tenant."""
    tenant_id = principal.tenant_id
    if stage == RetryStage.PROFILE_EXTRACTION:
        if document_id is None:
            raise validation_failed("DOCUMENT_REQUIRED", "A document id is required.")
        document = await get_candidate_document(
            db, tenant_id=tenant_id, candidate_id=candidate_id, document_id=document_id
        )
        if document is None:
            return None
    else:
        document = None
        from meyar.services.candidate_repo import get_candidate

        if await get_candidate(db, tenant_id=tenant_id, candidate_id=candidate_id) is None:
            return None
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="candidate.processing.retry",
        metadata={"candidate_id": str(candidate_id), "stage": stage.value},
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
    )
    await db.commit()
    try:
        if stage == RetryStage.PROFILE_EXTRACTION:
            assert document is not None
            version = await extract_candidate_profile(
                db,
                llm,
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                candidate_document=document,
                model_provider_name=settings.llm_provider,
                max_input_chars=settings.llm_max_input_chars,
            )
            await db.commit()
            return RetryResponse(
                stage=stage,
                outcome=(
                    "COMPLETED" if version.status == PROFILE_STATUS_COMPLETED else "NOT_COMPLETED"
                ),
                profile_version_id=version.id,
                profile_status=version.status,
                error_code=version.error_code,
            )
        embedding, reused = await embed_candidate_profile(
            db,
            embedding_provider,
            tenant_id=tenant_id,
            candidate_id=candidate_id,
            max_input_chars=settings.embedding_max_input_chars,
            compatibility=EmbeddingCompatibility(**embedding_config.model_dump()),
        )
        await db.commit()
        return RetryResponse(
            stage=stage,
            outcome="REUSED" if reused else "COMPLETED",
            embedding_version_id=embedding.id,
        )
    except (ExtractionPreconditionError, EmbeddingPreconditionError) as exc:
        await db.rollback()
        raise conflict(exc.code) from None
    except (ExtractionDeferredError, EmbeddingBusyError):
        await db.rollback()
        raise ApplicationError(
            ErrorCode.RETRYABLE_BUSY, status_code=503, reason="INFERENCE_BUSY", retryable=True
        ) from None
    except EmbeddingProviderError:
        await db.commit()  # keep the closed failure audit
        raise ApplicationError(
            ErrorCode.SERVICE_UNAVAILABLE, status_code=503,
            reason="EMBEDDING_PROVIDER_ERROR", retryable=True,
        ) from None

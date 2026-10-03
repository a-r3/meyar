import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.config import Settings, get_settings
from meyar.core.auth import TenantContext, api_key_is_active, require_scope
from meyar.db import get_db
from meyar.ingestion.bounded_read import read_bounded
from meyar.ingestion.dependency import get_document_parser
from meyar.ingestion.parser import DocumentParser, ParseError
from meyar.ingestion.validation import DocumentTooLargeError, UnsupportedDocumentError
from meyar.models.api_key import ApiKey
from meyar.models.candidate import Candidate
from meyar.models.candidate_document import PARSER_STATUS_PARSED, CandidateDocument
from meyar.models.canonical_document import CanonicalDocument
from meyar.schemas.api_candidate import ApiCandidateDetailResponse
from meyar.schemas.candidate import (
    CandidateDocumentOut,
    CandidateOut,
    CanonicalDocumentOut,
)
from meyar.services.audit_repo import ACTOR_API_KEY, record_event
from meyar.services.candidate_document_repo import (
    get_candidate_document,
    get_latest_canonical_document,
    list_candidate_documents,
)
from meyar.services.candidate_document_service import (
    persist_candidate_document,
    prepare_candidate_document,
)
from meyar.services.candidate_photo_service import process_photo_for_document
from meyar.services.candidate_repo import create_candidate, get_candidate
from meyar.services.candidate_service import delete_candidate_cascade
from meyar.services.storage_recovery import recover_on_failure
from meyar.services.tenant_authority import require_active_tenant
from meyar.storage.base import DocumentStorage
from meyar.storage.dependency import get_document_storage, get_photo_storage
from meyar.storage.photo import LocalPhotoStorage
from meyar.ui.service import get_candidate_detail_view

router = APIRouter(tags=["candidates"])
logger = logging.getLogger(__name__)


def _candidate_out(candidate) -> CandidateOut:
    return CandidateOut.model_validate(candidate, from_attributes=True)


def _canonical_out(canonical: CanonicalDocument) -> CanonicalDocumentOut:
    return CanonicalDocumentOut(
        id=canonical.id,
        parser_name=canonical.parser_name,
        parser_version=canonical.parser_version,
        language=canonical.language,
        pages=canonical.content["pages"],
        partial_extraction=bool(canonical.content.get("warnings")),
        created_at=canonical.created_at,
    )


async def _document_out(
    db: AsyncSession, document: CandidateDocument, *, tenant_id: uuid.UUID
) -> CandidateDocumentOut:
    canonical = None
    if document.parser_status == PARSER_STATUS_PARSED:
        latest = await get_latest_canonical_document(
            db, tenant_id=tenant_id, candidate_document_id=document.id
        )
        if latest is not None:
            canonical = _canonical_out(latest)
    return CandidateDocumentOut(
        id=document.id,
        candidate_id=document.candidate_id,
        original_filename=document.original_filename,
        mime_type=document.mime_type,
        byte_size=document.byte_size,
        sha256_hash=document.sha256_hash,
        document_status=document.document_status,
        parser_status=document.parser_status,
        parser_name=document.parser_name,
        parser_version=document.parser_version,
        parse_error_code=document.parse_error_code,
        parse_error_message=document.parse_error_message,
        created_at=document.created_at,
        parsed_at=document.parsed_at,
        canonical=canonical,
    )


async def _get_candidate_or_404(db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID):
    candidate = await get_candidate(db, tenant_id=tenant_id, candidate_id=candidate_id)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found.")
    return candidate


async def _revalidate_upload_authority(
    db: AsyncSession, ctx: TenantContext, candidate_id: uuid.UUID
) -> None:
    """Fresh persistence-phase authority; locks last only until upload commit.

    Shared locks prevent credential/ownership changes or candidate deletion
    between revalidation and persistence without serializing unrelated uploads.
    Tenant is SHARE-locked before key and candidate through the final commit.
    """
    await require_active_tenant(db, ctx.tenant_id, lock=True)
    key = await db.scalar(
        select(ApiKey)
        .where(ApiKey.id == ctx.api_key_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if key is None or key.tenant_id != ctx.tenant_id or not api_key_is_active(key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if "candidates:write" not in key.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key is missing required scope: candidates:write",
        )
    candidate = await db.scalar(
        select(Candidate)
        .where(Candidate.id == candidate_id, Candidate.tenant_id == ctx.tenant_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found.")


@router.post("/candidates", status_code=status.HTTP_201_CREATED, response_model=CandidateOut)
async def post_candidate(
    ctx: TenantContext = Depends(require_scope("candidates:write")),
    db: AsyncSession = Depends(get_db),
) -> CandidateOut:
    candidate = await create_candidate(db, tenant_id=ctx.tenant_id)
    await record_event(
        db,
        tenant_id=ctx.tenant_id,
        event_type="CANDIDATE_CREATED",
        metadata={"candidate_id": str(candidate.id)},
        actor_type=ACTOR_API_KEY,
        actor_id=ctx.api_key_id,
    )
    await db.commit()
    return _candidate_out(candidate)


@router.get("/candidates/{candidate_id}", response_model=CandidateOut)
async def get_candidate_detail(
    candidate_id: uuid.UUID,
    ctx: TenantContext = Depends(require_scope("candidates:read")),
    db: AsyncSession = Depends(get_db),
) -> CandidateOut:
    candidate = await _get_candidate_or_404(db, ctx.tenant_id, candidate_id)
    return _candidate_out(candidate)


@router.get("/candidates/{candidate_id}/detail", response_model=ApiCandidateDetailResponse)
async def get_candidate_detail_enriched(
    candidate_id: uuid.UUID,
    ctx: TenantContext = Depends(require_scope("candidates:read")),
    db: AsyncSession = Depends(get_db),
) -> ApiCandidateDetailResponse:
    detail = await get_candidate_detail_view(
        db, tenant_id=ctx.tenant_id, candidate_id=candidate_id
    )
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found.")
    return detail


@router.delete("/candidates/{candidate_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_candidate(
    candidate_id: uuid.UUID,
    ctx: TenantContext = Depends(require_scope("candidates:write")),
    db: AsyncSession = Depends(get_db),
    storage: DocumentStorage = Depends(get_document_storage),
    photo_storage: LocalPhotoStorage = Depends(get_photo_storage),
) -> None:
    deleted_count = await delete_candidate_cascade(
        db, storage, tenant_id=ctx.tenant_id, candidate_id=candidate_id,
        photo_storage=photo_storage, actor_id=ctx.api_key_id,
    )
    if deleted_count is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found.")


@router.post(
    "/candidates/{candidate_id}/documents",
    status_code=status.HTTP_201_CREATED,
    response_model=CandidateDocumentOut,
)
async def post_candidate_document(
    candidate_id: uuid.UUID,
    file: UploadFile = File(...),
    ctx: TenantContext = Depends(require_scope("candidates:write")),
    db: AsyncSession = Depends(get_db),
    storage: DocumentStorage = Depends(get_document_storage),
    photo_storage: LocalPhotoStorage = Depends(get_photo_storage),
    parser: DocumentParser = Depends(get_document_parser),
    settings: Settings = Depends(get_settings),
) -> CandidateDocumentOut:
    await _get_candidate_or_404(db, ctx.tenant_id, candidate_id)
    # Complete the short, read-only ownership phase. Auth last-used was already
    # committed; no document/storage mutation exists yet. Every document write
    # follows preparation and fresh, locked persistence-phase revalidation.
    await db.commit()

    data = await read_bounded(file, max_bytes=settings.max_upload_bytes)
    try:
        prepared = await prepare_candidate_document(
            parser,
            filename=file.filename or "",
            content_type=file.content_type or "",
            data=data,
            max_bytes=settings.max_upload_bytes,
        )
    except DocumentTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=str(exc)
        ) from exc
    except UnsupportedDocumentError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    except ParseError as exc:
        # Operational validation/parser failures precede every storage/document write.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=exc.public_message
        ) from None

    # Original bytes and DB authority are not one transaction: any failure up to
    # and including the commit rolls back and removes the just-saved original.
    async with recover_on_failure(db):
        await _revalidate_upload_authority(db, ctx, candidate_id)
        document = await persist_candidate_document(
            db, storage, prepared, tenant_id=ctx.tenant_id,
            candidate_id=candidate_id, filename=file.filename or "",
        )
        await db.commit()
    document_id = document.id
    try:
        await process_photo_for_document(
            db, storage, photo_storage, tenant_id=ctx.tenant_id,
            candidate_id=candidate_id, document_id=document_id,
        )
    except Exception:
        await db.rollback()
        logger.warning("Unexpected photo-only failure after durable document upload")
    await require_active_tenant(db, ctx.tenant_id)
    durable_document = await get_candidate_document(
        db, tenant_id=ctx.tenant_id, candidate_id=candidate_id, document_id=document_id
    )
    if durable_document is None:
        # A legitimate concurrent hard-delete committed after the upload did.
        # Nothing remains to describe; report the truthful current state.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found.")
    return await _document_out(db, durable_document, tenant_id=ctx.tenant_id)


@router.get("/candidates/{candidate_id}/documents", response_model=list[CandidateDocumentOut])
async def list_candidate_documents_endpoint(
    candidate_id: uuid.UUID,
    ctx: TenantContext = Depends(require_scope("candidates:read")),
    db: AsyncSession = Depends(get_db),
) -> list[CandidateDocumentOut]:
    await _get_candidate_or_404(db, ctx.tenant_id, candidate_id)
    documents = await list_candidate_documents(
        db, tenant_id=ctx.tenant_id, candidate_id=candidate_id
    )
    return [await _document_out(db, d, tenant_id=ctx.tenant_id) for d in documents]


@router.get(
    "/candidates/{candidate_id}/documents/{document_id}", response_model=CandidateDocumentOut
)
async def get_candidate_document_detail(
    candidate_id: uuid.UUID,
    document_id: uuid.UUID,
    ctx: TenantContext = Depends(require_scope("candidates:read")),
    db: AsyncSession = Depends(get_db),
) -> CandidateDocumentOut:
    await _get_candidate_or_404(db, ctx.tenant_id, candidate_id)
    document = await get_candidate_document(
        db, tenant_id=ctx.tenant_id, candidate_id=candidate_id, document_id=document_id
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    return await _document_out(db, document, tenant_id=ctx.tenant_id)

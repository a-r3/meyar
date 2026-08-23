import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.config import Settings, get_settings
from meyar.core.auth import TenantContext, require_scope
from meyar.db import get_db
from meyar.ingestion.dependency import get_document_parser
from meyar.ingestion.parser import DocumentParser
from meyar.ingestion.validation import DocumentTooLargeError, UnsupportedDocumentError
from meyar.models.candidate_document import PARSER_STATUS_PARSED, CandidateDocument
from meyar.models.canonical_document import CanonicalDocument
from meyar.schemas.candidate import (
    CandidateDocumentOut,
    CandidateOut,
    CanonicalDocumentOut,
)
from meyar.services.audit_repo import record_event
from meyar.services.candidate_document_repo import (
    get_candidate_document,
    get_latest_canonical_document,
    list_candidate_documents,
)
from meyar.services.candidate_document_service import ingest_candidate_document
from meyar.services.candidate_repo import create_candidate, get_candidate
from meyar.services.candidate_service import delete_candidate_cascade
from meyar.storage.base import DocumentStorage
from meyar.storage.dependency import get_document_storage

router = APIRouter(tags=["candidates"])


def _candidate_out(candidate) -> CandidateOut:
    return CandidateOut.model_validate(candidate, from_attributes=True)


def _canonical_out(canonical: CanonicalDocument) -> CanonicalDocumentOut:
    return CanonicalDocumentOut(
        id=canonical.id,
        parser_name=canonical.parser_name,
        parser_version=canonical.parser_version,
        language=canonical.language,
        pages=canonical.content["pages"],
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


@router.delete("/candidates/{candidate_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_candidate(
    candidate_id: uuid.UUID,
    ctx: TenantContext = Depends(require_scope("candidates:write")),
    db: AsyncSession = Depends(get_db),
    storage: DocumentStorage = Depends(get_document_storage),
) -> None:
    deleted_count = await delete_candidate_cascade(
        db, storage, tenant_id=ctx.tenant_id, candidate_id=candidate_id
    )
    if deleted_count is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found.")
    await record_event(
        db,
        tenant_id=ctx.tenant_id,
        event_type="CANDIDATE_DELETED",
        metadata={"candidate_id": str(candidate_id), "document_count": deleted_count},
    )
    await db.commit()


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
    parser: DocumentParser = Depends(get_document_parser),
    settings: Settings = Depends(get_settings),
) -> CandidateDocumentOut:
    await _get_candidate_or_404(db, ctx.tenant_id, candidate_id)

    data = await file.read()
    try:
        document = await ingest_candidate_document(
            db,
            storage,
            parser,
            tenant_id=ctx.tenant_id,
            candidate_id=candidate_id,
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

    await db.commit()
    return await _document_out(db, document, tenant_id=ctx.tenant_id)


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

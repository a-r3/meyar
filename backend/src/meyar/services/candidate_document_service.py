import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.ingestion.parser import DocumentParser, ParseError
from meyar.ingestion.validation import validate_upload
from meyar.models.candidate_document import (
    PARSER_STATUS_PARSE_FAILED,
    PARSER_STATUS_PARSED,
    CandidateDocument,
)
from meyar.services.audit_repo import record_event
from meyar.services.candidate_document_repo import (
    create_candidate_document,
    create_canonical_document,
)
from meyar.storage.base import DocumentStorage


async def ingest_candidate_document(
    db: AsyncSession,
    storage: DocumentStorage,
    parser: DocumentParser,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    filename: str,
    content_type: str,
    data: bytes,
    max_bytes: int,
) -> CandidateDocument:
    """The single secure document-ingestion pipeline: validate (MIME
    sniffing + size cap) -> hash -> store (opaque storage key) -> persist
    CandidateDocument -> parse -> persist CanonicalDocument, with an audit
    event at each stage. Shared by the direct-upload API route and the
    local-folder indexer so there is exactly one ingestion code path.
    Raises DocumentTooLargeError/UnsupportedDocumentError from validation
    — the caller decides how to surface that (HTTP 4xx, or a per-file
    failure record for folder indexing). Never commits; the caller
    controls the transaction boundary."""
    detected = validate_upload(
        filename=filename, content_type=content_type, data=data, max_bytes=max_bytes
    )

    sha256_hash = hashlib.sha256(data).hexdigest()
    storage_key = await storage.save(tenant_id=tenant_id, content=data)

    # original_filename is retained for display only — truncated, never
    # used to build a path or influence storage/parsing behavior.
    safe_original_filename = (filename or "upload")[:255]

    document = await create_candidate_document(
        db,
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        original_filename=safe_original_filename,
        mime_type=detected.mime_type,
        byte_size=len(data),
        sha256_hash=sha256_hash,
        storage_key=storage_key,
    )
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="CANDIDATE_DOCUMENT_UPLOADED",
        metadata={
            "candidate_id": str(candidate_id),
            "document_id": str(document.id),
            "byte_size": len(data),
            "mime_type": detected.mime_type,
        },
    )

    try:
        result = await parser.parse(data=data, document_type=detected.document_type)
    except ParseError as exc:
        document.parser_status = PARSER_STATUS_PARSE_FAILED
        document.parse_error_code = "PARSE_FAILED"
        document.parse_error_message = str(exc)[:500]
        await record_event(
            db,
            tenant_id=tenant_id,
            event_type="CANDIDATE_DOCUMENT_PARSE_FAILED",
            metadata={
                "candidate_id": str(candidate_id),
                "document_id": str(document.id),
                "error_code": "PARSE_FAILED",
            },
        )
    else:
        document.parser_status = PARSER_STATUS_PARSED
        document.parser_name = result.parser_name
        document.parser_version = result.parser_version
        document.parsed_at = datetime.now(UTC)
        await create_canonical_document(
            db,
            tenant_id=tenant_id,
            candidate_document_id=document.id,
            parser_name=result.parser_name,
            parser_version=result.parser_version,
            language=result.content.language,
            content=result.content.model_dump(mode="json"),
        )
        await record_event(
            db,
            tenant_id=tenant_id,
            event_type="CANDIDATE_DOCUMENT_PARSED",
            metadata={
                "candidate_id": str(candidate_id),
                "document_id": str(document.id),
                "parser_name": result.parser_name,
                "parser_version": result.parser_version,
                "page_count": len(result.content.pages),
            },
        )

    return document

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.ingestion.parser import DocumentParser, ParseError, ParseResult
from meyar.ingestion.validation import DetectedDocument, validate_upload_async
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
from meyar.services.storage_recovery import abandon_created, settle_leftovers, track_created
from meyar.services.tenant_authority import require_active_tenant
from meyar.storage.base import DocumentStorage


@dataclass(frozen=True)
class PreparedDocument:
    data: bytes
    detected: DetectedDocument
    sha256_hash: str
    outcome: ParseResult | ParseError


async def prepare_candidate_document(
    parser: DocumentParser, *, filename: str, content_type: str, data: bytes, max_bytes: int
) -> PreparedDocument:
    """No DB or storage access: validation/admission/parsing before persistence.

    Operational failures propagate without creating document authority. Only
    terminal content/output-policy failures are retained for durable ingestion.
    """
    detected = await validate_upload_async(
        filename=filename, content_type=content_type, data=data, max_bytes=max_bytes
    )
    outcome: ParseResult | ParseError
    try:
        outcome = await parser.parse(data=data, document_type=detected.document_type)
    except ParseError as exc:
        if not exc.is_terminal:
            raise
        outcome = exc
    return PreparedDocument(data, detected, hashlib.sha256(data).hexdigest(), outcome)


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
    """Shared folder/service entry point; caller owns its transaction.

    Direct uploads call prepare/persist separately to release the request DB
    connection during validation and parsing. Folder transaction architecture
    remains unchanged; operational failures follow its existing FAILED retry path.
    """
    await require_active_tenant(db, tenant_id)
    prepared = await prepare_candidate_document(
        parser, filename=filename, content_type=content_type, data=data, max_bytes=max_bytes
    )
    return await persist_candidate_document(
        db, storage, prepared, tenant_id=tenant_id, candidate_id=candidate_id, filename=filename
    )


async def persist_candidate_document(
    db: AsyncSession,
    storage: DocumentStorage,
    prepared: PreparedDocument,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    filename: str,
) -> CandidateDocument:
    """Persist a prepared success/terminal outcome; never commits or parses.

    The saved original is tracked in the session's storage-recovery ledger: a
    failure here compensates immediately; a later caller rollback or failed
    commit is compensated by storage_recovery.commit_with_recovery /
    rollback_with_recovery / recover_on_failure.

    Request callers revalidate and lock live authority first. Operational
    failures never reach storage.save or CandidateDocument creation.
    """
    await require_active_tenant(db, tenant_id)
    detected, data, outcome = prepared.detected, prepared.data, prepared.outcome
    if isinstance(outcome, ParseError) and not outcome.is_terminal:
        raise outcome
    await settle_leftovers(db)  # compensate any earlier direct db.rollback()
    storage_key = await storage.save(tenant_id=tenant_id, content=data)
    created = track_created(db, storage, tenant_id=tenant_id, storage_key=storage_key)
    try:
        # Savepoint: if any DB step fails, this call's rows are rolled back here,
        # so deleting the just-saved original can never strand a surviving row.
        async with db.begin_nested():
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
                sha256_hash=prepared.sha256_hash,
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

            if isinstance(outcome, ParseError):
                exc = outcome
                document.parser_status = PARSER_STATUS_PARSE_FAILED
                document.parse_error_code = exc.code.value
                document.parse_error_message = exc.public_message
                await record_event(
                    db,
                    tenant_id=tenant_id,
                    event_type="CANDIDATE_DOCUMENT_PARSE_FAILED",
                    metadata={
                        "candidate_id": str(candidate_id),
                        "document_id": str(document.id),
                        "error_code": exc.code.value,
                    },
                )
            else:
                result = outcome
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

    except BaseException as exc:
        await abandon_created(db, created, exc)
        raise

    return document

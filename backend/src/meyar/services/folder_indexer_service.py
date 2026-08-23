import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.ingestion.folder_scanner import DiscoveredFile, resolve_source_root, scan_source_root
from meyar.ingestion.parser import DocumentParser
from meyar.ingestion.validation import DocumentTooLargeError, UnsupportedDocumentError
from meyar.models.folder_indexed_file import (
    INDEX_STATUS_FAILED,
    INDEX_STATUS_INDEXED,
    INDEX_STATUS_MISSING,
    FolderIndexedFile,
)
from meyar.services.audit_repo import record_event
from meyar.services.candidate_document_service import ingest_candidate_document
from meyar.services.candidate_repo import create_candidate
from meyar.services.folder_indexed_file_repo import (
    create_folder_indexed_file,
    list_folder_indexed_files,
    update_folder_indexed_file,
)
from meyar.services.folder_source_repo import get_or_create_folder_source
from meyar.storage.base import DocumentStorage


@dataclass(frozen=True)
class FolderScanSummary:
    folder_source_id: uuid.UUID
    discovered: int
    new: int
    changed: int
    retried: int
    unchanged: int
    successful: int
    failed: int
    missing: int


async def index_folder(
    db: AsyncSession,
    storage: DocumentStorage,
    parser: DocumentParser,
    *,
    tenant_id: uuid.UUID,
    root_path: str,
    max_bytes: int,
) -> FolderScanSummary:
    """Scans a local folder for supported CV files, ingests new/changed
    ones through the existing secure ingestion pipeline
    (meyar.services.candidate_document_service.ingest_candidate_document
    — no bypass), and persists idempotent per-file indexing state. Safe
    to re-run: an unchanged file is a no-op, a changed file creates a new
    CandidateDocument version without destroying prior evidence, a file
    that disappears is tombstoned (MISSING), never hard-deleted. One
    malformed file never aborts the rest of the scan. Never commits —
    the caller controls the transaction boundary. Triggers no LLM/
    embedding extraction — discovery, validation, and parsing only."""
    # Eager validation before any DB write — scan_source_root is a
    # generator and would otherwise only raise once first iterated,
    # after the FolderSource row below had already been created.
    resolve_source_root(root_path)

    source = await get_or_create_folder_source(db, tenant_id=tenant_id, root_path=root_path)

    existing_rows = await list_folder_indexed_files(
        db, tenant_id=tenant_id, folder_source_id=source.id
    )
    remaining: dict[str, FolderIndexedFile] = {row.relative_path: row for row in existing_rows}

    discovered = new_count = changed_count = retried_count = unchanged_count = 0
    successful_count = failed_count = 0

    for entry in scan_source_root(root_path):
        discovered += 1
        existing = remaining.pop(entry.relative_path, None)

        if existing is None:
            new_count += 1
            outcome = await _handle_new_file(
                db,
                storage,
                parser,
                tenant_id=tenant_id,
                source_id=source.id,
                entry=entry,
                max_bytes=max_bytes,
            )
        elif existing.sha256_hash != entry.sha256_hash:
            changed_count += 1
            await record_event(
                db,
                tenant_id=tenant_id,
                event_type="FOLDER_FILE_CHANGED",
                metadata={"folder_indexed_file_id": str(existing.id)},
            )
            outcome = await _handle_changed_or_retry(
                db,
                storage,
                parser,
                tenant_id=tenant_id,
                row=existing,
                entry=entry,
                max_bytes=max_bytes,
            )
        elif existing.index_status == INDEX_STATUS_FAILED:
            retried_count += 1
            outcome = await _handle_changed_or_retry(
                db,
                storage,
                parser,
                tenant_id=tenant_id,
                row=existing,
                entry=entry,
                max_bytes=max_bytes,
            )
        else:
            unchanged_count += 1
            await update_folder_indexed_file(
                db,
                existing,
                index_status=INDEX_STATUS_INDEXED,
                last_seen_at=datetime.now(UTC),
            )
            outcome = None  # not an ingestion attempt — no successful/failed tally

        if outcome == INDEX_STATUS_INDEXED:
            successful_count += 1
        elif outcome == INDEX_STATUS_FAILED:
            failed_count += 1

    # Rows never observed this scan are tombstoned, never hard-deleted —
    # prior candidate/document evidence is left untouched.
    for missing_row in remaining.values():
        if missing_row.index_status != INDEX_STATUS_MISSING:
            await update_folder_indexed_file(db, missing_row, index_status=INDEX_STATUS_MISSING)
            await record_event(
                db,
                tenant_id=tenant_id,
                event_type="FOLDER_FILE_MISSING",
                metadata={"folder_indexed_file_id": str(missing_row.id)},
            )
    missing_count = len(remaining)

    summary = FolderScanSummary(
        folder_source_id=source.id,
        discovered=discovered,
        new=new_count,
        changed=changed_count,
        retried=retried_count,
        unchanged=unchanged_count,
        successful=successful_count,
        failed=failed_count,
        missing=missing_count,
    )
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="FOLDER_INDEXING_COMPLETED",
        metadata={
            "folder_source_id": str(source.id),
            "discovered": discovered,
            "new": new_count,
            "changed": changed_count,
            "retried": retried_count,
            "unchanged": unchanged_count,
            "successful": successful_count,
            "failed": failed_count,
            "missing": missing_count,
        },
    )
    return summary


async def _handle_new_file(
    db: AsyncSession,
    storage: DocumentStorage,
    parser: DocumentParser,
    *,
    tenant_id: uuid.UUID,
    source_id: uuid.UUID,
    entry: DiscoveredFile,
    max_bytes: int,
) -> str:
    """Creates a new Candidate for a never-before-seen path, attempts
    ingestion, and persists the resulting index row. Returns the
    resulting index_status."""
    candidate = await create_candidate(db, tenant_id=tenant_id)
    filename = _synthetic_filename(entry.relative_path)
    try:
        document = await ingest_candidate_document(
            db,
            storage,
            parser,
            tenant_id=tenant_id,
            candidate_id=candidate.id,
            filename=filename,
            content_type="",
            data=entry.data,
            max_bytes=max_bytes,
        )
    except (UnsupportedDocumentError, DocumentTooLargeError) as exc:
        await create_folder_indexed_file(
            db,
            tenant_id=tenant_id,
            folder_source_id=source_id,
            relative_path=entry.relative_path,
            document_type=_extension_document_type(entry.relative_path),
            byte_size=entry.byte_size,
            sha256_hash=entry.sha256_hash,
            index_status=INDEX_STATUS_FAILED,
            candidate_id=candidate.id,
            candidate_document_id=None,
            failure_code=type(exc).__name__,
            failure_message=str(exc)[:500],
        )
        await record_event(
            db,
            tenant_id=tenant_id,
            event_type="FOLDER_FILE_IMPORT_FAILED",
            metadata={"folder_source_id": str(source_id), "failure_code": type(exc).__name__},
        )
        return INDEX_STATUS_FAILED

    await create_folder_indexed_file(
        db,
        tenant_id=tenant_id,
        folder_source_id=source_id,
        relative_path=entry.relative_path,
        document_type=_extension_document_type(entry.relative_path),
        byte_size=entry.byte_size,
        sha256_hash=entry.sha256_hash,
        index_status=INDEX_STATUS_INDEXED,
        candidate_id=candidate.id,
        candidate_document_id=document.id,
        last_seen_at=datetime.now(UTC),
    )
    return INDEX_STATUS_INDEXED


async def _handle_changed_or_retry(
    db: AsyncSession,
    storage: DocumentStorage,
    parser: DocumentParser,
    *,
    tenant_id: uuid.UUID,
    row: FolderIndexedFile,
    entry: DiscoveredFile,
    max_bytes: int,
) -> str:
    """Re-ingests a path whose content hash changed, or whose last
    attempt previously FAILED. A failed re-import never clears a
    previously successful candidate_document_id — only the observed
    hash/status change. Returns the resulting index_status."""
    assert row.candidate_id is not None  # every existing row was created with one
    filename = _synthetic_filename(entry.relative_path)
    try:
        document = await ingest_candidate_document(
            db,
            storage,
            parser,
            tenant_id=tenant_id,
            candidate_id=row.candidate_id,
            filename=filename,
            content_type="",
            data=entry.data,
            max_bytes=max_bytes,
        )
    except (UnsupportedDocumentError, DocumentTooLargeError) as exc:
        await update_folder_indexed_file(
            db,
            row,
            byte_size=entry.byte_size,
            sha256_hash=entry.sha256_hash,
            index_status=INDEX_STATUS_FAILED,
            failure_code=type(exc).__name__,
            failure_message=str(exc)[:500],
        )
        await record_event(
            db,
            tenant_id=tenant_id,
            event_type="FOLDER_FILE_IMPORT_FAILED",
            metadata={
                "folder_source_id": str(row.folder_source_id),
                "failure_code": type(exc).__name__,
            },
        )
        return INDEX_STATUS_FAILED

    await update_folder_indexed_file(
        db,
        row,
        byte_size=entry.byte_size,
        sha256_hash=entry.sha256_hash,
        index_status=INDEX_STATUS_INDEXED,
        candidate_document_id=document.id,
        failure_code=None,
        failure_message=None,
        last_seen_at=datetime.now(UTC),
    )
    return INDEX_STATUS_INDEXED


def _synthetic_filename(relative_path: str) -> str:
    """A safe display name derived from the relative path's final
    segment only — never the full local path (avoids leaking directory
    structure that may contain names) — passed through the same
    255-char truncation as a direct upload's original_filename inside
    ingest_candidate_document."""
    return PurePosixPath(relative_path).name


def _extension_document_type(relative_path: str) -> str:
    suffix = PurePosixPath(relative_path).suffix.lower()
    return "PDF" if suffix == ".pdf" else "DOCX"

"""Slice 6 — Local CV Library & Folder Indexer. All CV bytes come from
`fixtures/synthetic_cvs/` — never real candidate data. See
docs/MVP_PLAN.md Slice 6 and .claude/rules/testing.md."""

import hashlib
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.ingestion.folder_scanner import InvalidSourceRootError
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.models.audit_event import AuditEvent
from meyar.models.candidate_document import CandidateDocument
from meyar.models.folder_indexed_file import (
    INDEX_STATUS_FAILED,
    INDEX_STATUS_INDEXED,
    INDEX_STATUS_MISSING,
)
from meyar.services.folder_indexed_file_repo import list_folder_indexed_files
from meyar.services.folder_indexer_service import index_folder
from meyar.services.tenant_repo import create_tenant
from meyar.storage.local import LocalFilesystemStorage

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "synthetic_cvs"
MAX_BYTES = 10 * 1024 * 1024


def _parser() -> LocalTextParser:
    return LocalTextParser()


def _storage(tmp_path: Path) -> LocalFilesystemStorage:
    return LocalFilesystemStorage(root=str(tmp_path / "storage"))


def _copy_fixture(fixture_name: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes((FIXTURES_DIR / fixture_name).read_bytes())


async def _rows(db_session: AsyncSession, tenant_id: uuid.UUID, folder_source_id: uuid.UUID):
    return await list_folder_indexed_files(
        db_session, tenant_id=tenant_id, folder_source_id=folder_source_id
    )


async def test_empty_folder_scan(db_session: AsyncSession, tmp_path: Path) -> None:
    tenant = await create_tenant(db_session, name="T-empty")
    await db_session.commit()
    root = tmp_path / "cvs"
    root.mkdir()

    summary = await index_folder(
        db_session,
        _storage(tmp_path),
        _parser(),
        tenant_id=tenant.id,
        root_path=str(root),
        max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    assert summary.discovered == 0
    assert summary.new == summary.changed == summary.retried == 0
    assert summary.unchanged == summary.successful == summary.failed == summary.missing == 0


async def test_discovers_and_imports_valid_pdf(db_session: AsyncSession, tmp_path: Path) -> None:
    tenant = await create_tenant(db_session, name="T-pdf")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "candidate.pdf")

    summary = await index_folder(
        db_session,
        _storage(tmp_path),
        _parser(),
        tenant_id=tenant.id,
        root_path=str(root),
        max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    assert summary.discovered == 1
    assert summary.new == 1
    assert summary.successful == 1
    assert summary.failed == 0

    rows = await _rows(db_session, tenant.id, summary.folder_source_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.relative_path == "candidate.pdf"
    assert row.index_status == INDEX_STATUS_INDEXED
    assert row.document_type == "PDF"
    assert row.candidate_id is not None
    assert row.candidate_document_id is not None

    doc = await db_session.get(CandidateDocument, row.candidate_document_id)
    assert doc is not None
    assert doc.parser_status == "PARSED"


async def test_discovers_and_imports_valid_docx(db_session: AsyncSession, tmp_path: Path) -> None:
    tenant = await create_tenant(db_session, name="T-docx")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.docx", root / "candidate.docx")

    summary = await index_folder(
        db_session,
        _storage(tmp_path),
        _parser(),
        tenant_id=tenant.id,
        root_path=str(root),
        max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    assert summary.discovered == 1
    assert summary.successful == 1
    rows = await _rows(db_session, tenant.id, summary.folder_source_id)
    assert rows[0].document_type == "DOCX"
    assert rows[0].index_status == INDEX_STATUS_INDEXED


async def test_nested_folder_discovery_and_relative_path(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant = await create_tenant(db_session, name="T-nested")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "2026" / "batch-1" / "cv.pdf")

    summary = await index_folder(
        db_session,
        _storage(tmp_path),
        _parser(),
        tenant_id=tenant.id,
        root_path=str(root),
        max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    assert summary.discovered == 1
    rows = await _rows(db_session, tenant.id, summary.folder_source_id)
    assert rows[0].relative_path == "2026/batch-1/cv.pdf"


async def test_unsupported_extension_ignored(db_session: AsyncSession, tmp_path: Path) -> None:
    tenant = await create_tenant(db_session, name="T-unsupported")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("unsupported.txt", root / "notes.txt")
    _copy_fixture("unsupported.png", root / "photo.png")

    summary = await index_folder(
        db_session,
        _storage(tmp_path),
        _parser(),
        tenant_id=tenant.id,
        root_path=str(root),
        max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    assert summary.discovered == 0
    assert summary.failed == 0
    rows = await _rows(db_session, tenant.id, summary.folder_source_id)
    assert rows == []


async def test_case_insensitive_extension(db_session: AsyncSession, tmp_path: Path) -> None:
    tenant = await create_tenant(db_session, name="T-case")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "RESUME.PDF")

    summary = await index_folder(
        db_session,
        _storage(tmp_path),
        _parser(),
        tenant_id=tenant.id,
        root_path=str(root),
        max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    assert summary.discovered == 1
    assert summary.successful == 1


async def test_unchanged_rescan_is_idempotent_no_duplicates(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    """The critical regression test: re-scanning an unchanged folder must
    create zero duplicate Candidate/CandidateDocument/index rows."""
    tenant = await create_tenant(db_session, name="T-idempotent")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "candidate.pdf")
    storage = _storage(tmp_path)
    parser = _parser()

    first = await index_folder(
        db_session, storage, parser, tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES
    )
    await db_session.commit()
    assert first.new == 1
    assert first.successful == 1

    second = await index_folder(
        db_session, storage, parser, tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES
    )
    await db_session.commit()

    assert second.discovered == 1
    assert second.new == 0
    assert second.changed == 0
    assert second.unchanged == 1
    assert second.successful == 0  # no ingestion attempt — nothing to succeed/fail
    assert second.failed == 0

    rows = await _rows(db_session, tenant.id, first.folder_source_id)
    assert len(rows) == 1  # no duplicate active index row

    doc_count = await db_session.execute(
        select(CandidateDocument).where(CandidateDocument.tenant_id == tenant.id)
    )
    assert len(doc_count.scalars().all()) == 1  # no duplicate CandidateDocument


async def test_changed_file_creates_new_document_preserves_history(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant = await create_tenant(db_session, name="T-changed")
    await db_session.commit()
    root = tmp_path / "cvs"
    path = root / "candidate.pdf"
    _copy_fixture("valid_cv.pdf", path)
    storage = _storage(tmp_path)
    parser = _parser()

    first = await index_folder(
        db_session, storage, parser, tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES
    )
    await db_session.commit()
    first_rows = await _rows(db_session, tenant.id, first.folder_source_id)
    first_candidate_id = first_rows[0].candidate_id
    first_document_id = first_rows[0].candidate_document_id

    # Replace with a different, still-valid PDF fixture at the same path.
    _copy_fixture("prompt_injection_cv.pdf", path)

    second = await index_folder(
        db_session, storage, parser, tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES
    )
    await db_session.commit()

    assert second.changed == 1
    assert second.successful == 1
    second_rows = await _rows(db_session, tenant.id, first.folder_source_id)
    assert len(second_rows) == 1  # still one index row — same path identity
    row = second_rows[0]
    assert row.candidate_id == first_candidate_id  # same candidate identity preserved
    assert row.candidate_document_id != first_document_id  # points at the new version
    assert row.index_status == INDEX_STATUS_INDEXED

    # Prior evidence was never destroyed.
    old_doc = await db_session.get(CandidateDocument, first_document_id)
    assert old_doc is not None
    doc_count = await db_session.execute(
        select(CandidateDocument).where(CandidateDocument.candidate_id == first_candidate_id)
    )
    assert len(doc_count.scalars().all()) == 2


async def test_removed_file_marked_missing_then_reappears(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant = await create_tenant(db_session, name="T-missing")
    await db_session.commit()
    root = tmp_path / "cvs"
    path = root / "candidate.pdf"
    _copy_fixture("valid_cv.pdf", path)
    storage = _storage(tmp_path)
    parser = _parser()

    first = await index_folder(
        db_session, storage, parser, tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES
    )
    await db_session.commit()
    first_rows = await _rows(db_session, tenant.id, first.folder_source_id)
    original_candidate_id = first_rows[0].candidate_id
    original_document_id = first_rows[0].candidate_document_id

    path.unlink()
    second = await index_folder(
        db_session, storage, parser, tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES
    )
    await db_session.commit()

    assert second.discovered == 0
    assert second.missing == 1
    rows = await _rows(db_session, tenant.id, first.folder_source_id)
    assert rows[0].index_status == INDEX_STATUS_MISSING
    assert rows[0].candidate_id == original_candidate_id  # evidence preserved
    assert rows[0].candidate_document_id == original_document_id

    # Candidate/document evidence was never hard-deleted.
    assert await db_session.get(CandidateDocument, original_document_id) is not None

    # File reappears unchanged: reactivated without re-ingesting.
    _copy_fixture("valid_cv.pdf", path)
    third = await index_folder(
        db_session, storage, parser, tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES
    )
    await db_session.commit()

    assert third.unchanged == 1
    assert third.missing == 0
    rows = await _rows(db_session, tenant.id, first.folder_source_id)
    assert rows[0].index_status == INDEX_STATUS_INDEXED
    assert rows[0].candidate_document_id == original_document_id  # no duplicate ingestion


async def test_malformed_pdf_isolated_scan_continues(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant = await create_tenant(db_session, name="T-malformed-pdf")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "a-valid.pdf")
    _copy_fixture("malformed.pdf", root / "b-broken.pdf")
    _copy_fixture("valid_cv.docx", root / "c-valid.docx")

    summary = await index_folder(
        db_session,
        _storage(tmp_path),
        _parser(),
        tenant_id=tenant.id,
        root_path=str(root),
        max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    assert summary.discovered == 3
    # malformed.pdf passes upload validation (real PDF signature) but
    # fails at parse time — same as the direct-upload endpoint, the
    # document is still created (parser_status=PARSE_FAILED), so the
    # index row is INDEXED, not FAILED. The scan still processes both
    # other valid files.
    assert summary.successful == 3
    assert summary.failed == 0

    all_rows = await _rows(db_session, tenant.id, summary.folder_source_id)
    rows = {r.relative_path: r for r in all_rows}
    broken_doc = await db_session.get(CandidateDocument, rows["b-broken.pdf"].candidate_document_id)
    assert broken_doc is not None
    assert broken_doc.parser_status == "PARSE_FAILED"
    assert rows["a-valid.pdf"].index_status == INDEX_STATUS_INDEXED
    assert rows["c-valid.docx"].index_status == INDEX_STATUS_INDEXED


async def test_malformed_docx_isolated_scan_continues(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant = await create_tenant(db_session, name="T-malformed-docx")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "a-valid.pdf")
    _copy_fixture("malformed.docx", root / "b-broken.docx")

    summary = await index_folder(
        db_session,
        _storage(tmp_path),
        _parser(),
        tenant_id=tenant.id,
        root_path=str(root),
        max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    assert summary.discovered == 2
    assert summary.successful == 1
    assert summary.failed == 1

    all_rows = await _rows(db_session, tenant.id, summary.folder_source_id)
    rows = {r.relative_path: r for r in all_rows}
    broken = rows["b-broken.docx"]
    assert broken.index_status == INDEX_STATUS_FAILED
    assert broken.candidate_document_id is None
    assert broken.candidate_id is not None  # identity still reserved for retry
    assert broken.failure_code == "UnsupportedDocumentError"
    assert rows["a-valid.pdf"].index_status == INDEX_STATUS_INDEXED


async def test_retry_previously_failed_file_then_fixed(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant = await create_tenant(db_session, name="T-retry")
    await db_session.commit()
    root = tmp_path / "cvs"
    path = root / "candidate.docx"
    _copy_fixture("malformed.docx", path)
    storage = _storage(tmp_path)
    parser = _parser()

    first = await index_folder(
        db_session, storage, parser, tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES
    )
    await db_session.commit()
    assert first.new == 1
    assert first.failed == 1
    first_rows = await _rows(db_session, tenant.id, first.folder_source_id)
    reserved_candidate_id = first_rows[0].candidate_id

    # Unchanged content, still broken: must be retried, not skipped.
    second = await index_folder(
        db_session, storage, parser, tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES
    )
    await db_session.commit()
    assert second.retried == 1
    assert second.unchanged == 0
    assert second.failed == 1

    # Now "fix" the file — different (valid) content at the same path.
    _copy_fixture("valid_cv.docx", path)
    third = await index_folder(
        db_session, storage, parser, tenant_id=tenant.id, root_path=str(root), max_bytes=MAX_BYTES
    )
    await db_session.commit()
    assert third.changed == 1
    assert third.successful == 1
    rows = await _rows(db_session, tenant.id, first.folder_source_id)
    assert rows[0].index_status == INDEX_STATUS_INDEXED
    assert rows[0].candidate_id == reserved_candidate_id
    assert rows[0].candidate_document_id is not None


async def test_content_hash_correctness(db_session: AsyncSession, tmp_path: Path) -> None:
    tenant = await create_tenant(db_session, name="T-hash")
    await db_session.commit()
    root = tmp_path / "cvs"
    fixture_bytes = (FIXTURES_DIR / "valid_cv.pdf").read_bytes()
    _copy_fixture("valid_cv.pdf", root / "candidate.pdf")

    summary = await index_folder(
        db_session,
        _storage(tmp_path),
        _parser(),
        tenant_id=tenant.id,
        root_path=str(root),
        max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    rows = await _rows(db_session, tenant.id, summary.folder_source_id)
    assert rows[0].sha256_hash == hashlib.sha256(fixture_bytes).hexdigest()
    assert rows[0].byte_size == len(fixture_bytes)


async def test_source_root_escape_and_symlink_prevention(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant = await create_tenant(db_session, name="T-symlink")
    await db_session.commit()
    root = tmp_path / "cvs"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _copy_fixture("valid_cv.pdf", outside / "secret.pdf")

    # A symlink inside root pointing to a file outside root must never
    # be discovered/ingested.
    os.symlink(outside / "secret.pdf", root / "escape.pdf")

    summary = await index_folder(
        db_session,
        _storage(tmp_path),
        _parser(),
        tenant_id=tenant.id,
        root_path=str(root),
        max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    assert summary.discovered == 0
    rows = await _rows(db_session, tenant.id, summary.folder_source_id)
    assert rows == []


async def test_symlinked_directory_not_followed(db_session: AsyncSession, tmp_path: Path) -> None:
    tenant = await create_tenant(db_session, name="T-symlink-dir")
    await db_session.commit()
    root = tmp_path / "cvs"
    root.mkdir()
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    _copy_fixture("valid_cv.pdf", outside / "secret.pdf")
    os.symlink(outside, root / "linked", target_is_directory=True)

    summary = await index_folder(
        db_session,
        _storage(tmp_path),
        _parser(),
        tenant_id=tenant.id,
        root_path=str(root),
        max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    assert summary.discovered == 0


async def test_invalid_source_root_raises(db_session: AsyncSession, tmp_path: Path) -> None:
    tenant = await create_tenant(db_session, name="T-invalid-root")
    await db_session.commit()
    missing_root = tmp_path / "does-not-exist"

    with pytest.raises(InvalidSourceRootError):
        await index_folder(
            db_session,
            _storage(tmp_path),
            _parser(),
            tenant_id=tenant.id,
            root_path=str(missing_root),
            max_bytes=MAX_BYTES,
        )

    # No FolderSource ghost row created on validation failure.
    from meyar.models.folder_source import FolderSource

    result = await db_session.execute(
        select(FolderSource).where(FolderSource.tenant_id == tenant.id)
    )
    assert result.scalar_one_or_none() is None


async def test_source_root_that_is_a_file_raises(db_session: AsyncSession, tmp_path: Path) -> None:
    tenant = await create_tenant(db_session, name="T-file-root")
    await db_session.commit()
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("not a directory")

    with pytest.raises(InvalidSourceRootError):
        await index_folder(
            db_session,
            _storage(tmp_path),
            _parser(),
            tenant_id=tenant.id,
            root_path=str(not_a_dir),
            max_bytes=MAX_BYTES,
        )


async def test_tenant_isolation_indexed_files_never_leak(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    tenant_a = await create_tenant(db_session, name="T-iso-A")
    tenant_b = await create_tenant(db_session, name="T-iso-B")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "candidate.pdf")
    storage = _storage(tmp_path)
    parser = _parser()

    summary = await index_folder(
        db_session,
        storage,
        parser,
        tenant_id=tenant_a.id,
        root_path=str(root),
        max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    # Tenant B scanning the exact same folder path gets its own
    # FolderSource and its own index/candidate/document rows.
    summary_b = await index_folder(
        db_session,
        storage,
        parser,
        tenant_id=tenant_b.id,
        root_path=str(root),
        max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    assert summary.folder_source_id != summary_b.folder_source_id

    rows_a = await _rows(db_session, tenant_a.id, summary.folder_source_id)
    rows_b_as_a = await _rows(db_session, tenant_a.id, summary_b.folder_source_id)
    assert len(rows_a) == 1
    assert rows_b_as_a == []  # tenant A cannot see tenant B's index rows by id

    rows_b = await _rows(db_session, tenant_b.id, summary_b.folder_source_id)
    assert len(rows_b) == 1
    assert rows_a[0].candidate_id != rows_b[0].candidate_id
    assert rows_a[0].candidate_document_id != rows_b[0].candidate_document_id


async def test_audit_events_recorded_without_pii(db_session: AsyncSession, tmp_path: Path) -> None:
    tenant = await create_tenant(db_session, name="T-audit")
    await db_session.commit()
    root = tmp_path / "cvs"
    _copy_fixture("valid_cv.pdf", root / "candidate.pdf")

    summary = await index_folder(
        db_session,
        _storage(tmp_path),
        _parser(),
        tenant_id=tenant.id,
        root_path=str(root),
        max_bytes=MAX_BYTES,
    )
    await db_session.commit()

    result = await db_session.execute(
        select(AuditEvent).where(AuditEvent.tenant_id == tenant.id)
    )
    events = result.scalars().all()
    event_types = {e.event_type for e in events}
    assert "FOLDER_INDEXING_COMPLETED" in event_types
    completed = next(e for e in events if e.event_type == "FOLDER_INDEXING_COMPLETED")
    assert completed.event_metadata["folder_source_id"] == str(summary.folder_source_id)
    assert completed.event_metadata["successful"] == 1

    # No audit metadata value contains the local filesystem path or
    # any PII-shaped string.
    for event in events:
        for value in event.event_metadata.values():
            assert "candidate.pdf" not in str(value)
            assert str(root) not in str(value)

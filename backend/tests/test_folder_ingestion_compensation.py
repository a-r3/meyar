"""Issue #46 M-5: synthetic rejected intake vs retained terminal originals."""

import hashlib
import re
from pathlib import Path

import pytest
from sqlalchemy import select

from meyar.config import Settings, get_settings
from meyar.ingestion.parser import ParseError, ParseFailureCode
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.main import app
from meyar.models.candidate_document import CandidateDocument
from meyar.models.candidate_identity_version import CandidateIdentityVersion
from meyar.models.candidate_profile_version import CandidateProfileVersion
from meyar.models.canonical_document import CanonicalDocument
from meyar.services.candidate_repo import count_candidates_for_tenant
from meyar.services.folder_indexed_file_repo import (
    create_folder_indexed_file,
    list_folder_indexed_files,
)
from meyar.services.folder_indexer_service import index_folder
from meyar.services.folder_source_repo import get_or_create_folder_source
from meyar.storage.dependency import get_document_storage
from meyar.storage.local import LocalFilesystemStorage

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/synthetic_cvs"
VALID = (FIXTURES / "valid_cv.pdf").read_bytes()
MAX_BYTES = 10 * 1024 * 1024


class BusyParser:
    async def parse(self, **kwargs):
        raise ParseError(ParseFailureCode.PARSER_BUSY)


async def scan(db, storage, tenant_id, root, parser=None):
    result = await index_folder(
        db, storage, parser or LocalTextParser(), tenant_id=tenant_id,
        root_path=str(root), max_bytes=MAX_BYTES,
    )
    await db.commit()
    rows = await list_folder_indexed_files(
        db, tenant_id=tenant_id, folder_source_id=result.folder_source_id
    )
    return result, rows


@pytest.mark.parametrize("filename,data", [
    ("fake.docx", b"synthetic unsupported content"),
    ("fake.pdf", b"synthetic not a PDF"),
    ("renamed.pdf", (FIXTURES / "valid_cv.docx").read_bytes()),
], ids=["fake-docx", "fake-pdf", "docx-renamed-pdf"])
async def test_rejected_new_file_has_no_candidate_and_retry_reuses_row(
    db_session, tenant_and_key, tmp_path, filename, data
):
    tenant, _, _ = tenant_and_key
    root = tmp_path / "synthetic"
    root.mkdir()
    path = root / filename
    path.write_bytes(data)
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))
    first, rows = await scan(db_session, storage, tenant.id, root)
    row = rows[0]
    row_id = row.id
    assert first.failed == 1 and row.index_status == "FAILED"
    assert row.candidate_document_id is None and row.candidate_id is None
    assert await count_candidates_for_tenant(db_session, tenant_id=tenant.id) == 0
    assert await db_session.scalar(select(CandidateDocument)) is None
    assert not [p for p in (tmp_path / "storage").rglob("*") if p.is_file()]
    second, rows = await scan(db_session, storage, tenant.id, root)
    assert second.retried == 1 and second.failed == 1 and rows[0].id == row_id
    path.write_bytes(
        (FIXTURES / "valid_cv.docx").read_bytes() if filename.endswith("docx") else VALID
    )
    fixed, rows = await scan(db_session, storage, tenant.id, root)
    assert fixed.successful == 1 and rows[0].id == row_id
    assert row.candidate_id is not None and row.candidate_document_id is not None
    assert row.failure_code is None and row.failure_message is None
    again, rows = await scan(db_session, storage, tenant.id, root)
    assert again.unchanged == 1 and rows[0].id == row_id
    assert await count_candidates_for_tenant(db_session, tenant_id=tenant.id) == 1


async def test_operational_new_failure_creates_no_candidate_and_retries_same_bytes(
    db_session, tenant_and_key, tmp_path
):
    tenant, _, _ = tenant_and_key
    root = tmp_path / "synthetic"
    root.mkdir()
    (root / "synthetic.pdf").write_bytes(VALID)
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))
    failed, rows = await scan(db_session, storage, tenant.id, root, BusyParser())
    row_id = rows[0].id
    assert failed.failed == 1 and rows[0].failure_code == "PARSER_BUSY"
    assert rows[0].candidate_id is None
    assert await count_candidates_for_tenant(db_session, tenant_id=tenant.id) == 0
    retried, rows = await scan(db_session, storage, tenant.id, root)
    assert retried.retried == 1 and retried.successful == 1 and rows[0].id == row_id
    assert rows[0].candidate_id is not None and rows[0].candidate_document_id is not None
    assert rows[0].failure_code is None and rows[0].failure_message is None
    assert await count_candidates_for_tenant(db_session, tenant_id=tenant.id) == 1


async def test_preexisting_failed_row_without_candidate_recovers(
    db_session, tenant_and_key, tmp_path
):
    tenant, _, _ = tenant_and_key
    root = tmp_path / "synthetic"
    root.mkdir()
    (root / "synthetic.pdf").write_bytes(VALID)
    source = await get_or_create_folder_source(db_session, tenant_id=tenant.id, root_path=str(root))
    row = await create_folder_indexed_file(
        db_session, tenant_id=tenant.id, folder_source_id=source.id,
        relative_path="synthetic.pdf", document_type="PDF", byte_size=len(VALID),
        sha256_hash=hashlib.sha256(VALID).hexdigest(), index_status="FAILED",
        candidate_id=None, candidate_document_id=None, failure_code="PARSER_BUSY",
        failure_message="Document parsing is busy. Please try again later.",
    )
    row_id = row.id
    await db_session.commit()
    result, rows = await scan(
        db_session, LocalFilesystemStorage(root=str(tmp_path / "storage")), tenant.id, root
    )
    assert result.retried == 1 and result.successful == 1
    assert rows[0].id == row_id and rows[0].candidate_id is not None
    assert rows[0].failure_code is None and rows[0].failure_message is None


async def test_terminal_original_remains_authorized_with_truthful_hr_state(
    client, db_session, tenant_key_and_user, tmp_path
):
    tenant, _, _, user, password, _ = tenant_key_and_user
    app.dependency_overrides[get_settings] = lambda: Settings(ui_cookie_secure=False)
    root = tmp_path / "synthetic"
    root.mkdir()
    data = (FIXTURES / "malformed.pdf").read_bytes()
    (root / "synthetic.pdf").write_bytes(data)
    storage = app.dependency_overrides[get_document_storage]()
    result, rows = await scan(db_session, storage, tenant.id, root)
    row = rows[0]
    assert result.successful == 1 and row.index_status == "INDEXED"
    assert row.candidate_id is not None and row.candidate_document_id is not None
    document = await db_session.get(CandidateDocument, row.candidate_document_id)
    assert document.parser_status == "PARSE_FAILED"
    assert await db_session.scalar(select(CanonicalDocument)) is None
    assert await db_session.scalar(select(CandidateProfileVersion)) is None
    assert await db_session.scalar(select(CandidateIdentityVersion)) is None
    login = await client.post("/ui/login", data={"username": user.username, "password": password})
    assert login.status_code == 303
    original = await client.get(
        f"/ui/candidates/{row.candidate_id}/documents/{document.id}/original"
    )
    assert original.status_code == 200 and original.content == data
    for url in ("/ui/library", f"/ui/candidates/{row.candidate_id}"):
        page = await client.get(url)
        assert page.status_code == 200
        assert "Diqqət tələb edir" in page.text
        assert "Emal olunur" not in page.text
        visible = re.sub(r"<[^>]+>", " ", page.text)
        assert "PARSE_FAILED" not in visible and document.parse_error_code not in visible
        assert str(row.candidate_id) not in visible


async def test_candidate_less_retry_dedups_but_never_bypasses_extension_validation(
    db_session, tenant_and_key, tmp_path
):
    tenant, _, _ = tenant_and_key
    root = tmp_path / "synthetic"
    root.mkdir()
    path = root / "b-retry.pdf"
    path.write_bytes(b"synthetic rejected")
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))
    _, rows = await scan(db_session, storage, tenant.id, root)
    retry_id = rows[0].id
    (root / "a-valid.pdf").write_bytes(VALID)
    path.write_bytes(VALID)
    # Same content at an invalid extension must still fail validation.
    (root / "c-invalid.docx").write_bytes(VALID)
    result, rows = await scan(db_session, storage, tenant.id, root)
    valid, retry, invalid = rows
    assert result.successful == 2 and result.failed == 1
    assert retry.id == retry_id and retry.candidate_id == valid.candidate_id
    assert retry.candidate_document_id == valid.candidate_document_id
    assert retry.failure_code is None and retry.failure_message is None
    assert invalid.candidate_id is None and invalid.index_status == "FAILED"
    # Exercise the candidate-less duplicate retry, including validation refusal.
    result, rows = await scan(db_session, storage, tenant.id, root)
    assert result.retried == 1 and result.failed == 1
    assert rows[2].candidate_id is None
    assert await count_candidates_for_tenant(db_session, tenant_id=tenant.id) == 1
    assert len((await db_session.scalars(select(CandidateDocument))).all()) == 1


async def test_changed_failure_keeps_history_and_cannot_dedup_wrong_bytes(
    db_session, tenant_and_key, tmp_path
):
    tenant, _, _ = tenant_and_key
    root = tmp_path / "synthetic"
    root.mkdir()
    path = root / "a-valid.pdf"
    path.write_bytes(VALID)
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))
    _, rows = await scan(db_session, storage, tenant.id, root)
    candidate_id, document_id = rows[0].candidate_id, rows[0].candidate_document_id
    canonical = await db_session.scalar(select(CanonicalDocument))
    canonical_content = canonical.content.copy()
    changed = (FIXTURES / "prompt_injection_cv.pdf").read_bytes()
    path.write_bytes(changed)
    for parser in (BusyParser(), BusyParser()):
        result, rows = await scan(db_session, storage, tenant.id, root, parser)
        assert result.failed == 1
        assert rows[0].candidate_id == candidate_id
        assert rows[0].candidate_document_id == document_id
        assert canonical.content == canonical_content
        assert await count_candidates_for_tenant(db_session, tenant_id=tenant.id) == 1
    # New matching observed bytes must not link to the prior retained document.
    (root / "b-new.pdf").write_bytes(changed)

    class FailFirst:
        calls = 0

        async def parse(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ParseError(ParseFailureCode.PARSER_BUSY)
            return await LocalTextParser().parse(**kwargs)

    result, rows = await scan(db_session, storage, tenant.id, root, FailFirst())
    assert result.failed == 1 and result.successful == 1
    assert rows[0].candidate_document_id == document_id
    assert rows[1].candidate_document_id != document_id
    assert rows[1].candidate_id != candidate_id
    _, rows = await scan(db_session, storage, tenant.id, root)
    assert rows[0].candidate_id == candidate_id  # changed path keeps its identity
    assert rows[0].candidate_document_id != document_id
    assert await db_session.get(CandidateDocument, document_id) is not None


async def test_missing_failed_path_reappears_and_retries_instead_of_false_indexed(
    db_session, tenant_and_key, tmp_path
):
    tenant, _, _ = tenant_and_key
    root = tmp_path / "synthetic"
    root.mkdir()
    path = root / "synthetic.pdf"
    path.write_bytes(VALID)
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))
    _, rows = await scan(db_session, storage, tenant.id, root, BusyParser())
    row_id = rows[0].id
    path.unlink()
    _, rows = await scan(db_session, storage, tenant.id, root)
    assert rows[0].index_status == "MISSING"
    path.write_bytes(VALID)
    result, rows = await scan(db_session, storage, tenant.id, root)
    assert result.retried == 1 and result.successful == 1
    assert rows[0].id == row_id and rows[0].candidate_document_id is not None


@pytest.mark.parametrize("shared", [False, True])
async def test_legacy_empty_candidate_retained_when_creation_ownership_is_unprovable(
    db_session, tenant_and_key, tmp_path, shared
):
    from meyar.models.candidate import Candidate
    from meyar.services.candidate_repo import create_candidate

    tenant, _, _ = tenant_and_key
    manual = await create_candidate(db_session, tenant_id=tenant.id)
    legacy = await create_candidate(db_session, tenant_id=tenant.id)
    manual_id, legacy_id = manual.id, legacy.id
    root = tmp_path / "synthetic"
    root.mkdir()
    data = b"synthetic rejected"
    (root / "legacy.pdf").write_bytes(data)
    source = await get_or_create_folder_source(db_session, tenant_id=tenant.id, root_path=str(root))
    row = await create_folder_indexed_file(
        db_session, tenant_id=tenant.id, folder_source_id=source.id,
        relative_path="legacy.pdf", document_type="PDF", byte_size=len(data),
        sha256_hash=hashlib.sha256(data).hexdigest(), index_status="FAILED",
        candidate_id=legacy_id, candidate_document_id=None, failure_code="UnsupportedDocumentError",
    )
    if shared:
        await create_folder_indexed_file(
            db_session, tenant_id=tenant.id, folder_source_id=source.id,
            relative_path="shared.pdf", document_type="PDF", byte_size=len(data),
            sha256_hash=hashlib.sha256(data).hexdigest(), index_status="MISSING",
            candidate_id=legacy_id, candidate_document_id=None,
        )
    await db_session.commit()
    _, rows = await scan(
        db_session, LocalFilesystemStorage(root=str(tmp_path / "storage")), tenant.id, root
    )
    assert row.candidate_id == legacy_id and row.candidate_document_id is None
    assert await db_session.get(Candidate, manual_id) is not None
    assert await db_session.get(Candidate, legacy_id) is not None
    assert await count_candidates_for_tenant(db_session, tenant_id=tenant.id) == 2
    (root / "legacy.pdf").write_bytes(VALID)
    result, _ = await scan(
        db_session, LocalFilesystemStorage(root=str(tmp_path / "storage")), tenant.id, root
    )
    assert result.successful == 1 and row.candidate_id == legacy_id
    assert await db_session.get(Candidate, manual_id) is not None


async def test_foreign_candidate_reference_fails_closed_and_dedup_cannot_link_it(
    db_session, tenant_and_key, tmp_path
):
    from meyar.models.candidate import Candidate
    from meyar.services.candidate_repo import create_candidate
    from meyar.services.tenant_repo import create_tenant

    tenant, _, _ = tenant_and_key
    other = await create_tenant(db_session, name="Synthetic other tenant")
    foreign = await create_candidate(db_session, tenant_id=other.id)
    foreign_id = foreign.id
    root = tmp_path / "synthetic"
    root.mkdir()
    (root / "a-foreign.pdf").write_bytes(VALID)
    (root / "b-own.pdf").write_bytes(VALID)
    source = await get_or_create_folder_source(db_session, tenant_id=tenant.id, root_path=str(root))
    await create_folder_indexed_file(
        db_session, tenant_id=tenant.id, folder_source_id=source.id,
        relative_path="a-foreign.pdf", document_type="PDF", byte_size=len(VALID),
        sha256_hash=hashlib.sha256(VALID).hexdigest(), index_status="FAILED",
        candidate_id=foreign_id, candidate_document_id=None,
    )
    await db_session.commit()
    result, rows = await scan(
        db_session, LocalFilesystemStorage(root=str(tmp_path / "storage")), tenant.id, root
    )
    assert result.failed == 1 and result.successful == 1
    assert rows[0].failure_code == "INDEX_AUTHORITY_INVALID"
    assert rows[0].candidate_document_id is None
    assert rows[1].candidate_id != foreign_id
    assert await db_session.get(Candidate, foreign_id) is not None
    assert await count_candidates_for_tenant(db_session, tenant_id=other.id) == 1
    assert await db_session.scalar(select(CandidateDocument).where(
        CandidateDocument.tenant_id == other.id
    )) is None


async def test_preparation_and_candidate_creation_failure_never_save_original(
    db_session, tenant_and_key, tmp_path, monkeypatch
):
    from meyar.services import folder_indexer_service

    tenant, _, _ = tenant_and_key
    tenant_id = tenant.id
    root = tmp_path / "synthetic"
    root.mkdir()
    (root / "synthetic.pdf").write_bytes(VALID)
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))

    async def refuse_candidate(*args, **kwargs):
        raise RuntimeError("synthetic candidate creation refusal")

    monkeypatch.setattr(folder_indexer_service, "create_candidate", refuse_candidate)
    with pytest.raises(RuntimeError, match="synthetic candidate creation"):
        await index_folder(db_session, storage, LocalTextParser(), tenant_id=tenant_id,
                           root_path=str(root), max_bytes=MAX_BYTES)
    await db_session.rollback()
    assert await count_candidates_for_tenant(db_session, tenant_id=tenant_id) == 0
    assert not [p for p in (tmp_path / "storage").rglob("*") if p.is_file()]


async def test_persistence_failure_rolls_back_candidate_but_storage_recovery_is_deferred(
    db_session, tenant_and_key, tmp_path, monkeypatch
):
    from meyar.services import candidate_document_service

    tenant, _, _ = tenant_and_key
    tenant_id = tenant.id
    root = tmp_path / "synthetic"
    root.mkdir()
    (root / "synthetic.pdf").write_bytes(VALID)
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))

    async def refuse_document(*args, **kwargs):
        raise RuntimeError("synthetic DB persistence refusal")

    monkeypatch.setattr(candidate_document_service, "create_candidate_document", refuse_document)
    with pytest.raises(RuntimeError, match="synthetic DB persistence"):
        await index_folder(db_session, storage, LocalTextParser(), tenant_id=tenant_id,
                           root_path=str(root), max_bytes=MAX_BYTES)
    await db_session.rollback()
    assert await count_candidates_for_tenant(db_session, tenant_id=tenant_id) == 0
    assert await db_session.scalar(select(CandidateDocument)) is None
    # Existing save-before-DB ordering, explicitly NOT solved by this M-5 slice.
    saved = [p for p in (tmp_path / "storage").rglob("*") if p.is_file()]
    assert len(saved) == 1 and saved[0].read_bytes() == VALID


async def test_hr_readiness_uses_latest_document_not_historical_failure(
    db_session, tenant_and_key, tmp_path
):
    from meyar.ui.presentation import readiness_label
    from meyar.ui.service import get_candidate_detail_view, list_candidate_library

    tenant, _, _ = tenant_and_key
    root = tmp_path / "synthetic"
    root.mkdir()
    path = root / "synthetic.pdf"
    path.write_bytes((FIXTURES / "malformed.pdf").read_bytes())
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))
    _, rows = await scan(db_session, storage, tenant.id, root)
    candidate_id = rows[0].candidate_id
    path.write_bytes(VALID)
    await scan(db_session, storage, tenant.id, root)
    library = await list_candidate_library(db_session, tenant_id=tenant.id)
    item = library.items[0]
    assert item.current_profile_status is None
    assert item.parser_statuses == ["PARSED", "PARSE_FAILED"]
    assert item.latest_parser_status == "PARSED"
    assert readiness_label(item.current_profile_status, item.latest_parser_status) == "Emal olunur"
    detail = await get_candidate_detail_view(
        db_session, tenant_id=tenant.id, candidate_id=candidate_id
    )
    assert detail.profile_status is None and detail.latest_parser_status == "PARSED"
    # PostgreSQL now() gives all documents in one transaction the same
    # timestamp. A UUID ordering cannot hide a failed original in that tie.
    documents = list((await db_session.scalars(select(CandidateDocument))).all())
    documents[0].created_at = documents[1].created_at
    await db_session.flush()
    tied = await list_candidate_library(db_session, tenant_id=tenant.id)
    assert tied.items[0].latest_parser_status == "PARSE_FAILED"
    tied_detail = await get_candidate_detail_view(
        db_session, tenant_id=tenant.id, candidate_id=candidate_id
    )
    assert tied_detail.latest_parser_status == "PARSE_FAILED"
    # Existing profile status always retains its accepted precedence (M-9 untouched).
    assert readiness_label("COMPLETED", "PARSE_FAILED") == "Hazır"
    assert readiness_label("FAILED", "PARSED") == "Diqqət tələb edir"
    assert readiness_label(None, None) == "Emal olunur"


async def test_failure_metadata_is_closed_and_scan_continues(
    db_session, tenant_and_key, tmp_path, monkeypatch
):
    from meyar.ingestion.validation import UnsupportedDocumentError
    from meyar.services import folder_indexer_service

    tenant, _, _ = tenant_and_key
    root = tmp_path / "synthetic"
    root.mkdir()
    (root / "a-invalid.pdf").write_bytes(b"synthetic rejected")
    (root / "b-valid.pdf").write_bytes(VALID)
    original = folder_indexer_service.prepare_candidate_document

    async def reject_with_unsafe_detail(parser, **kwargs):
        if kwargs["filename"] == "a-invalid.pdf":
            raise UnsupportedDocumentError("/synthetic/private/path: synthetic CV body detail")
        return await original(parser, **kwargs)

    monkeypatch.setattr(
        folder_indexer_service, "prepare_candidate_document", reject_with_unsafe_detail
    )
    result, rows = await scan(
        db_session, LocalFilesystemStorage(root=str(tmp_path / "storage")), tenant.id, root
    )
    assert result.failed == 1 and result.successful == 1
    assert rows[0].failure_message == "The document is not a supported PDF or DOCX."
    assert rows[0].candidate_id is None and rows[1].candidate_document_id is not None


async def test_dedup_rejects_corrupt_cross_tenant_document_relationship(
    db_session, tenant_and_key, tmp_path
):
    from meyar.services.tenant_repo import create_tenant

    tenant, _, _ = tenant_and_key
    other = await create_tenant(db_session, name="Synthetic document owner")
    other_id = other.id
    await db_session.commit()
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    (foreign_root / "synthetic.pdf").write_bytes(VALID)
    _, foreign_rows = await scan(db_session, storage, other_id, foreign_root)
    foreign_candidate_id = foreign_rows[0].candidate_id
    foreign_document_id = foreign_rows[0].candidate_document_id
    root = tmp_path / "own"
    root.mkdir()
    (root / "own.pdf").write_bytes(VALID)
    source = await get_or_create_folder_source(db_session, tenant_id=tenant.id, root_path=str(root))
    await create_folder_indexed_file(
        db_session, tenant_id=tenant.id, folder_source_id=source.id,
        relative_path="corrupt.pdf", document_type="PDF", byte_size=len(VALID),
        sha256_hash=hashlib.sha256(VALID).hexdigest(), index_status="INDEXED",
        candidate_id=foreign_candidate_id, candidate_document_id=foreign_document_id,
    )
    result, rows = await scan(db_session, storage, tenant.id, root)
    own = next(row for row in rows if row.relative_path == "own.pdf")
    assert result.successful == 1 and own.candidate_id != foreign_candidate_id
    assert own.candidate_document_id != foreign_document_id
    assert await db_session.get(CandidateDocument, foreign_document_id) is not None
    assert await count_candidates_for_tenant(db_session, tenant_id=other_id) == 1

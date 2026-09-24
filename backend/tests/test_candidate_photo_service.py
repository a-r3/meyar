"""Synthetic photo provenance and presentation authority checks."""

import hashlib
import io
import uuid
from datetime import date
from pathlib import Path

import pytest
from docx import Document
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.config import Settings, get_settings
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.main import app
from meyar.models.candidate_document import CandidateDocument
from meyar.models.candidate_photo_version import CandidatePhotoVersion
from meyar.models.folder_indexed_file import FolderIndexedFile
from meyar.models.folder_source import FolderSource
from meyar.services.candidate_document_repo import (
    create_candidate_document,
    get_latest_canonical_document,
)
from meyar.services.candidate_document_service import ingest_candidate_document
from meyar.services.candidate_identity_repo import (
    create_identity_version,
    get_current_identity_version,
)
from meyar.services.candidate_photo_service import (
    PLACEHOLDER_JPEG,
    current_presentable_photo,
    process_photo_for_document,
)
from meyar.services.candidate_profile_repo import (
    create_profile_version,
    get_current_profile_version,
)
from meyar.services.candidate_repo import create_candidate
from meyar.services.demo_seed_service import (
    _build_docx,
    _demo_candidates,
    _synthetic_portrait,
    seed_demo,
)
from meyar.services.tenant_repo import create_tenant
from meyar.storage.local import LocalFilesystemStorage
from meyar.storage.photo import LocalPhotoStorage

MAX_BYTES = 10 * 1024 * 1024


async def _seed(db: AsyncSession, tmp_path: Path):
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))
    summary = await seed_demo(
        db,
        storage,
        LocalTextParser(),
        max_bytes=MAX_BYTES,
        max_profile_input_chars=20000,
        max_identity_input_chars=20000,
        max_embedding_input_chars=20000,
        evaluation_as_of_date=date(2026, 1, 1),
    )
    await db.commit()
    document = await db.scalar(
        select(CandidateDocument).where(
            CandidateDocument.tenant_id == summary.tenant_id,
            CandidateDocument.original_filename == _demo_candidates()[0].doc_filename,
        )
    )
    assert document is not None
    return summary, storage, document


async def _changed(
    db: AsyncSession,
    storage: LocalFilesystemStorage,
    old: CandidateDocument,
    portrait_index: int | None,
    data: bytes | None = None,
) -> CandidateDocument:
    document = await ingest_candidate_document(
        db,
        storage,
        LocalTextParser(),
        tenant_id=old.tenant_id,
        candidate_id=old.candidate_id,
        filename="synthetic-changed.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        data=data if data is not None else _build_docx(_demo_candidates()[0].lines, portrait_index),
        max_bytes=MAX_BYTES,
    )
    await db.commit()
    prior_identity = await get_current_identity_version(
        db, tenant_id=old.tenant_id, candidate_id=old.candidate_id
    )
    canonical = await get_latest_canonical_document(
        db, tenant_id=old.tenant_id, candidate_document_id=document.id
    )
    assert prior_identity is not None and canonical is not None
    await create_identity_version(
        db,
        tenant_id=old.tenant_id,
        candidate_id=old.candidate_id,
        candidate_document_id=document.id,
        canonical_document_id=canonical.id,
        source_sha256=document.sha256_hash,
        schema_version=prior_identity.schema_version,
        prompt_version=prior_identity.prompt_version,
        model_provider=prior_identity.model_provider,
        model_name=prior_identity.model_name,
        status="COMPLETED",
        identity_content=prior_identity.identity_content,
    )
    await db.commit()
    return document


async def test_freshness_reuse_and_no_photo(db_session: AsyncSession, tmp_path: Path) -> None:
    _summary, storage, old = await _seed(db_session, tmp_path)
    photos = LocalPhotoStorage(str(tmp_path / "storage"))

    async def process(document: CandidateDocument):
        return await process_photo_for_document(
            db_session,
            storage,
            photos,
            tenant_id=document.tenant_id,
            candidate_id=document.candidate_id,
            document_id=document.id,
        )

    first = await process(old)
    assert first is not None and first.status == "AVAILABLE"
    assert first.source_page is None and first.source_locator.startswith("word/media/")
    assert (await process(old)).id == first.id
    assert (
        await current_presentable_photo(
            db_session, tenant_id=old.tenant_id, candidate_id=old.candidate_id
        )
    ).id == first.id

    changed = await _changed(db_session, storage, old, portrait_index=1)
    assert (
        await current_presentable_photo(
            db_session, tenant_id=old.tenant_id, candidate_id=old.candidate_id
        )
        is None
    )
    second = await process(changed)
    assert second is not None and second.status == "AVAILABLE"
    assert second.version_number == first.version_number + 1
    assert second.derived_sha256 != first.derived_sha256
    assert (
        await current_presentable_photo(
            db_session, tenant_id=old.tenant_id, candidate_id=old.candidate_id
        )
    ).id == second.id

    no_photo = await _changed(db_session, storage, old, portrait_index=None)
    third = await process(no_photo)
    assert third is not None and third.status == "NO_PHOTO"
    assert (
        await current_presentable_photo(
            db_session, tenant_id=old.tenant_id, candidate_id=old.candidate_id
        )
        is None
    )


async def test_photo_constraints_reject_mismatches(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    _summary, _storage, document = await _seed(db_session, tmp_path)
    tenant_id, candidate_id, document_id = (document.tenant_id, document.candidate_id, document.id)
    bad = CandidatePhotoVersion(
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_document_id=document_id,
        version_number=1,
        status="NO_PHOTO",
        extractor_version="photo-v1",
        derived_storage_key="photo/invalid",
    )
    db_session.add(bad)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
    other = await create_candidate(db_session, tenant_id=tenant_id)
    await db_session.commit()
    bad = CandidatePhotoVersion(
        tenant_id=tenant_id,
        candidate_id=other.id,
        candidate_document_id=document_id,
        version_number=1,
        status="NO_PHOTO",
        extractor_version="photo-v1",
    )
    db_session.add(bad)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_photo_route_authorization_and_integrity(
    client: AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    app.dependency_overrides[get_settings] = lambda: Settings(ui_cookie_secure=False)
    summary, storage, document = await _seed(db_session, tmp_path)
    photos = LocalPhotoStorage(str(tmp_path / "storage"))
    row = await process_photo_for_document(
        db_session,
        storage,
        photos,
        tenant_id=document.tenant_id,
        candidate_id=document.candidate_id,
        document_id=document.id,
    )
    assert row is not None and row.status == "AVAILABLE"
    url = f"/ui/candidates/{document.candidate_id}/photo"
    assert (await client.get(url, follow_redirects=False)).status_code in (302, 303)
    login = await client.post(
        "/ui/login",
        data={"username": summary.human_username, "password": summary.human_temp_password},
        follow_redirects=False,
    )
    assert login.status_code == 303
    response = await client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert hashlib.sha256(response.content).hexdigest() == row.derived_sha256
    assert row.derived_storage_key is not None
    assert row.derived_storage_key not in str(response.headers)
    assert row.derived_storage_key not in (await client.get("/ui/library")).text
    foreign = await create_tenant(db_session, name="Synthetic foreign")
    foreign_candidate = await create_candidate(db_session, tenant_id=foreign.id)
    await db_session.commit()
    assert (await client.get(f"/ui/candidates/{foreign_candidate.id}/photo")).status_code == 404
    await photos.delete(tenant_id=document.tenant_id, storage_key=row.derived_storage_key)
    missing = await client.get(url)
    assert missing.status_code == 200 and missing.content == PLACEHOLDER_JPEG


async def test_failed_photo_upload_keeps_durable_document(
    client: AsyncClient, db_session: AsyncSession, tmp_path: Path, monkeypatch
) -> None:
    from meyar.services import candidate_photo_service

    _summary, _storage, original = await _seed(db_session, tmp_path)

    async def fail_worker(_data: bytes, _kind: str) -> dict:
        raise RuntimeError("synthetic worker failure")

    monkeypatch.setattr(candidate_photo_service, "_extract_isolated", fail_worker)
    auth = {"Authorization": f"Bearer {_summary.api_key_plaintext}"}
    uploaded = await client.post(
        f"/api/v1/candidates/{original.candidate_id}/documents",
        headers=auth,
        files={
            "file": (
                "synthetic-changed.docx",
                _build_docx(_demo_candidates()[0].lines, 1),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert uploaded.status_code == 201
    document_id = uuid.UUID(uploaded.json()["id"])
    assert uploaded.json()["parser_status"] == "PARSED"
    row = await db_session.scalar(
        select(CandidatePhotoVersion).where(
            CandidatePhotoVersion.candidate_document_id == document_id
        )
    )
    assert row is not None and row.status == "EXTRACTION_FAILED"
    document = await db_session.get(CandidateDocument, document_id)
    assert document is not None
    assert await LocalFilesystemStorage(str(tmp_path / "storage")).read(
        storage_key=document.storage_key
    )


async def test_candidate_hard_delete_removes_derived_asset(
    client: AsyncClient, db_session: AsyncSession, tmp_path: Path
) -> None:
    summary, storage, document = await _seed(db_session, tmp_path)
    photos = LocalPhotoStorage(str(tmp_path / "storage"))
    row = await process_photo_for_document(
        db_session,
        storage,
        photos,
        tenant_id=document.tenant_id,
        candidate_id=document.candidate_id,
        document_id=document.id,
    )
    assert row is not None and row.derived_storage_key is not None
    photo_key, original_key = row.derived_storage_key, document.storage_key
    assert await photos.read(tenant_id=document.tenant_id, storage_key=photo_key)
    response = await client.delete(
        f"/api/v1/candidates/{document.candidate_id}",
        headers={"Authorization": f"Bearer {summary.api_key_plaintext}"},
    )
    assert response.status_code == 204
    with pytest.raises(FileNotFoundError):
        await photos.read(tenant_id=document.tenant_id, storage_key=photo_key)
    with pytest.raises(FileNotFoundError):
        await storage.read(storage_key=original_key)


async def test_dedup_linked_folder_paths_diverge_without_stale_photo(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    _summary, storage, old = await _seed(db_session, tmp_path)
    photos = LocalPhotoStorage(str(tmp_path / "storage"))
    old_photo = await process_photo_for_document(
        db_session,
        storage,
        photos,
        tenant_id=old.tenant_id,
        candidate_id=old.candidate_id,
        document_id=old.id,
    )
    assert old_photo is not None and old_photo.status == "AVAILABLE"
    source = FolderSource(tenant_id=old.tenant_id, root_path=str(tmp_path / "synthetic-source"))
    db_session.add(source)
    await db_session.flush()
    links = [
        FolderIndexedFile(
            tenant_id=old.tenant_id,
            folder_source_id=source.id,
            relative_path=name,
            document_type="DOCX",
            byte_size=old.byte_size,
            sha256_hash=old.sha256_hash,
            index_status="INDEXED",
            candidate_id=old.candidate_id,
            candidate_document_id=old.id,
        )
        for name in ("a.docx", "b.docx")
    ]
    db_session.add_all(links)
    await db_session.commit()
    assert links[0].candidate_document_id == links[1].candidate_document_id
    assert (
        await process_photo_for_document(
            db_session,
            storage,
            photos,
            tenant_id=old.tenant_id,
            candidate_id=old.candidate_id,
            document_id=links[1].candidate_document_id,
        )
    ).id == old_photo.id

    changed = await _changed(db_session, storage, old, portrait_index=None)
    links[0].candidate_document_id = changed.id
    links[0].sha256_hash = changed.sha256_hash
    await db_session.commit()
    assert links[1].candidate_document_id == old.id
    assert (
        await current_presentable_photo(
            db_session, tenant_id=old.tenant_id, candidate_id=old.candidate_id
        )
        is None
    )
    changed_photo = await process_photo_for_document(
        db_session,
        storage,
        photos,
        tenant_id=changed.tenant_id,
        candidate_id=changed.candidate_id,
        document_id=changed.id,
    )
    assert changed_photo is not None and changed_photo.status == "NO_PHOTO"
    assert (
        await current_presentable_photo(
            db_session, tenant_id=old.tenant_id, candidate_id=old.candidate_id
        )
        is None
    )


async def test_new_ambiguous_and_failed_photo_never_reuse_older_asset(
    db_session: AsyncSession, tmp_path: Path, monkeypatch
) -> None:
    from meyar.services import candidate_photo_service

    _summary, storage, old = await _seed(db_session, tmp_path)
    photos = LocalPhotoStorage(str(tmp_path / "storage"))
    original_photo = await process_photo_for_document(
        db_session,
        storage,
        photos,
        tenant_id=old.tenant_id,
        candidate_id=old.candidate_id,
        document_id=old.id,
    )
    assert original_photo is not None and original_photo.status == "AVAILABLE"
    document = Document()
    for line in _demo_candidates()[0].lines:
        document.add_paragraph(line)
    for index in (1, 2):
        document.add_picture(io.BytesIO(_synthetic_portrait(index)))
    output = io.BytesIO()
    document.save(output)
    ambiguous = await _changed(db_session, storage, old, None, data=output.getvalue())
    ambiguous_photo = await process_photo_for_document(
        db_session,
        storage,
        photos,
        tenant_id=ambiguous.tenant_id,
        candidate_id=ambiguous.candidate_id,
        document_id=ambiguous.id,
    )
    assert ambiguous_photo is not None and ambiguous_photo.status == "AMBIGUOUS"
    assert (
        await current_presentable_photo(
            db_session, tenant_id=old.tenant_id, candidate_id=old.candidate_id
        )
        is None
    )

    failed = await _changed(db_session, storage, old, portrait_index=3)

    async def fail_worker(_data: bytes, _kind: str) -> dict:
        return {"status": "EXTRACTION_FAILED", "reason_code": "SYNTHETIC_FAILURE"}

    monkeypatch.setattr(candidate_photo_service, "_extract_isolated", fail_worker)
    failed_photo = await process_photo_for_document(
        db_session,
        storage,
        photos,
        tenant_id=failed.tenant_id,
        candidate_id=failed.candidate_id,
        document_id=failed.id,
    )
    assert failed_photo is not None and failed_photo.status == "EXTRACTION_FAILED"
    assert (
        await current_presentable_photo(
            db_session, tenant_id=old.tenant_id, candidate_id=old.candidate_id
        )
        is None
    )


async def test_failed_profile_does_not_block_valid_identity_photo_and_parse_failure_hides_old(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    _summary, storage, old = await _seed(db_session, tmp_path)
    photos = LocalPhotoStorage(str(tmp_path / "storage"))
    photo = await process_photo_for_document(
        db_session,
        storage,
        photos,
        tenant_id=old.tenant_id,
        candidate_id=old.candidate_id,
        document_id=old.id,
    )
    assert photo is not None and photo.status == "AVAILABLE"
    profile = await get_current_profile_version(
        db_session, tenant_id=old.tenant_id, candidate_id=old.candidate_id
    )
    assert profile is not None
    await create_profile_version(
        db_session,
        tenant_id=old.tenant_id,
        candidate_id=old.candidate_id,
        candidate_document_id=old.id,
        canonical_document_id=profile.canonical_document_id,
        source_sha256=old.sha256_hash,
        schema_version=profile.schema_version,
        prompt_version=profile.prompt_version,
        model_provider=profile.model_provider,
        model_name=profile.model_name,
        model_metadata={},
        status="FAILED",
        error_code="SYNTHETIC_PROFILE_FAILURE",
    )
    await db_session.commit()
    assert (
        await current_presentable_photo(
            db_session, tenant_id=old.tenant_id, candidate_id=old.candidate_id
        )
    ).id == photo.id
    data = b"synthetic parse-failed document"
    key = await storage.save(tenant_id=old.tenant_id, content=data)
    failed_document = await create_candidate_document(
        db_session,
        tenant_id=old.tenant_id,
        candidate_id=old.candidate_id,
        original_filename="synthetic-failed.pdf",
        mime_type="application/pdf",
        byte_size=len(data),
        sha256_hash=hashlib.sha256(data).hexdigest(),
        storage_key=key,
    )
    failed_document.parser_status = "PARSE_FAILED"
    await db_session.commit()
    assert (
        await current_presentable_photo(
            db_session, tenant_id=old.tenant_id, candidate_id=old.candidate_id
        )
        is None
    )

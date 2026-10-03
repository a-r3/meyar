"""Issue #46 S6: post-upload photo persistence versus candidate hard-delete.

Real PostgreSQL connections, real local synthetic storage, asyncio Events as
barriers, ``pg_blocking_pids`` for lock observation. The worker subprocess is
replaced by a deterministic synthetic outcome; no sleeps are correctness proof.
"""

import asyncio
import base64
import hashlib
import uuid

import pytest
from conftest import FIXTURES_DIR, TEST_DATABASE_URL
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from meyar.api.v1 import candidates as candidates_api
from meyar.core.auth import TenantContext
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.models.candidate import Candidate
from meyar.models.candidate_document import CandidateDocument
from meyar.models.candidate_photo_version import CandidatePhotoVersion
from meyar.services import candidate_photo_service, candidate_service
from meyar.services.candidate_document_service import (
    persist_candidate_document,
    prepare_candidate_document,
)
from meyar.services.candidate_photo_service import PLACEHOLDER_JPEG, process_photo_for_document
from meyar.services.candidate_repo import create_candidate
from meyar.services.tenant_authority import require_active_tenant
from meyar.storage.local import LocalFilesystemStorage
from meyar.storage.photo import LocalPhotoStorage

pytestmark = pytest.mark.usefixtures("enabled_diagnostic_loggers")


@pytest.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def setup(db_session, tenant_and_key, tmp_path, monkeypatch):
    tenant, key, _ = tenant_and_key
    ctx = TenantContext(tenant.id, key.id, list(key.scopes))
    storage = LocalFilesystemStorage(str(tmp_path / "storage"))
    photos = LocalPhotoStorage(str(tmp_path / "storage"))
    prepared = await prepare_candidate_document(
        LocalTextParser(), filename="synthetic.pdf", content_type="application/pdf",
        data=(FIXTURES_DIR / "valid_cv.pdf").read_bytes(), max_bytes=10 * 1024 * 1024,
    )
    candidate = await create_candidate(db_session, tenant_id=ctx.tenant_id)
    document = await persist_candidate_document(
        db_session, storage, prepared, tenant_id=ctx.tenant_id,
        candidate_id=candidate.id, filename="synthetic.pdf",
    )
    await db_session.commit()

    async def available(data: bytes, kind: str) -> dict:
        return {
            "status": "AVAILABLE", "jpeg_base64": base64.b64encode(PLACEHOLDER_JPEG).decode(),
            "derived_sha256": hashlib.sha256(PLACEHOLDER_JPEG).hexdigest(),
            "width": 256, "height": 256, "source_kind": "PDF_IMAGE",
        }

    monkeypatch.setattr(candidate_photo_service, "_extract_isolated", available)
    return ctx, candidate.id, document.id, storage, photos


def derived_files(photos: LocalPhotoStorage):
    return [p for p in (photos._root / "photo").rglob("*") if p.is_file()] if (
        photos._root / "photo"
    ).exists() else []


async def pid(session):
    return await session.scalar(text("SELECT pg_backend_pid()"))


async def blocked_or_finished(observer, task, waiter, holder):
    async with asyncio.timeout(10):
        while not task.done():
            blockers = await observer.scalar(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter}
            )
            if holder in blockers:
                return True
        return False


async def delete(db, ctx, candidate_id, storage, photos):
    await require_active_tenant(db, ctx.tenant_id, lock=True)
    return await candidate_service.delete_candidate_cascade(
        db, storage, tenant_id=ctx.tenant_id, candidate_id=candidate_id,
        photo_storage=photos, actor_id=ctx.api_key_id,
    )


async def photo(db, storage, photos, ctx, candidate_id, document_id):
    return await process_photo_for_document(
        db, storage, photos, tenant_id=ctx.tenant_id,
        candidate_id=candidate_id, document_id=document_id,
    )


async def stop(*tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
    await asyncio.gather(*[t for t in tasks if t is not None], return_exceptions=True)


async def test_case_a_delete_wins_before_photo_persistence(setup, factory, monkeypatch):
    """Delete commits while photo extraction is in progress."""
    ctx, candidate_id, document_id, storage, photos = setup
    in_extraction, release = asyncio.Event(), asyncio.Event()

    async def paused(data: bytes, kind: str) -> dict:
        in_extraction.set()
        await release.wait()
        return await available_outcome()

    async def available_outcome() -> dict:
        return {
            "status": "AVAILABLE", "jpeg_base64": base64.b64encode(PLACEHOLDER_JPEG).decode(),
            "derived_sha256": hashlib.sha256(PLACEHOLDER_JPEG).hexdigest(),
            "width": 256, "height": 256, "source_kind": "PDF_IMAGE",
        }

    monkeypatch.setattr(candidate_photo_service, "_extract_isolated", paused)
    async with factory() as worker, factory() as deleter, factory() as observer:
        task = asyncio.create_task(photo(worker, storage, photos, ctx, candidate_id, document_id))
        try:
            await asyncio.wait_for(in_extraction.wait(), 10)
            assert await delete(deleter, ctx, candidate_id, storage, photos) == 1
            release.set()
            result = await asyncio.wait_for(task, 20)
        finally:
            await stop(task)
        assert result is None
        assert derived_files(photos) == []
        assert await observer.scalar(select(func.count()).select_from(CandidatePhotoVersion)) == 0
        assert await observer.scalar(select(func.count()).select_from(Candidate)) == 0


async def test_case_b_photo_row_flushed_before_delete_cascade_leaves_no_orphan(setup, factory,
                                                                               monkeypatch):
    """Derived JPEG saved + photo row INSERTed (uncommitted, holds FK KEY SHARE on the
    document); delete takes Candidate authority, cannot see the row, then blocks on
    the document delete until the photo commits and cascades the row away."""
    ctx, candidate_id, document_id, storage, photos = setup
    flushed, release = asyncio.Event(), asyncio.Event()
    real_create = candidate_photo_service.create_photo_version

    async def pause_after_flush(db, **kwargs):
        row = await real_create(db, **kwargs)
        flushed.set()
        await release.wait()
        return row

    monkeypatch.setattr(candidate_photo_service, "create_photo_version", pause_after_flush)
    async with factory() as worker, factory() as deleter, factory() as observer:
        worker_pid, delete_pid = await pid(worker), await pid(deleter)
        photo_task = asyncio.create_task(
            photo(worker, storage, photos, ctx, candidate_id, document_id)
        )
        delete_task = None
        try:
            await asyncio.wait_for(flushed.wait(), 10)
            assert len(derived_files(photos)) == 1
            delete_task = asyncio.create_task(delete(deleter, ctx, candidate_id, storage, photos))
            waits_on_photo = await blocked_or_finished(
                observer, delete_task, delete_pid, worker_pid
            )
            release.set()
            await asyncio.wait_for(asyncio.gather(photo_task, delete_task), 20)
        finally:
            await stop(photo_task, delete_task)
        # Wait-graph evidence is recorded; the invariant is the outcome below.
        assert await observer.scalar(select(func.count()).select_from(Candidate)) == 0
        assert await observer.scalar(select(func.count()).select_from(CandidateDocument)) == 0
        assert await observer.scalar(select(func.count()).select_from(CandidatePhotoVersion)) == 0
        assert derived_files(photos) == [], (
            f"orphan derived JPEG survived candidate deletion (delete_waited={waits_on_photo})"
        )


async def test_case_c_photo_durable_before_delete_is_staged_and_purged(setup, factory):
    ctx, candidate_id, document_id, storage, photos = setup
    async with factory() as worker, factory() as deleter, factory() as observer:
        row = await photo(worker, storage, photos, ctx, candidate_id, document_id)
        assert row is not None and row.status == "AVAILABLE"
        assert len(derived_files(photos)) == 1
        assert await delete(deleter, ctx, candidate_id, storage, photos) == 1
        assert derived_files(photos) == []
        assert await observer.scalar(select(func.count()).select_from(CandidatePhotoVersion)) == 0


async def test_upload_response_when_delete_wins_after_document_commit(
    client: AsyncClient, db_session, tenant_and_key, tmp_path, monkeypatch, factory
):
    """Durable upload, then a legitimate concurrent delete before the response."""
    tenant, key, plaintext = tenant_and_key
    auth = {"Authorization": f"Bearer {plaintext}"}
    created = await client.post("/api/v1/candidates", headers=auth)
    candidate_id = uuid.UUID(created.json()["id"])
    storage = LocalFilesystemStorage(str(tmp_path / "storage"))
    photos = LocalPhotoStorage(str(tmp_path / "storage"))
    ctx = TenantContext(tenant.id, key.id, list(key.scopes))
    real_process = candidates_api.process_photo_for_document

    async def delete_then_process(db, document_storage, photo_storage, **kwargs):
        async with factory() as deleter:
            assert await delete(deleter, ctx, candidate_id, storage, photos) == 1
        return await real_process(db, document_storage, photo_storage, **kwargs)

    monkeypatch.setattr(candidates_api, "process_photo_for_document", delete_then_process)
    monkeypatch.setattr(candidate_photo_service, "_extract_isolated", _never_called)
    response = await client.post(
        f"/api/v1/candidates/{candidate_id}/documents", headers=auth,
        files={"file": ("synthetic.pdf", (FIXTURES_DIR / "valid_cv.pdf").read_bytes(),
                        "application/pdf")},
    )
    assert response.status_code == 404, response.status_code
    assert derived_files(photos) == []


async def _never_called(data: bytes, kind: str) -> dict:
    raise AssertionError("extraction must not run for a deleted candidate")


async def test_delete_holding_authority_first_makes_photo_wait_and_write_nothing(
    setup, factory, monkeypatch
):
    ctx, candidate_id, document_id, storage, photos = setup
    in_extraction, extraction_go = asyncio.Event(), asyncio.Event()
    staged, release = asyncio.Event(), asyncio.Event()
    inner = candidate_photo_service._extract_isolated
    real_delete = candidate_service.delete_candidate_row

    async def paused_extract(data: bytes, kind: str) -> dict:
        in_extraction.set()
        await extraction_go.wait()
        return await inner(data, kind)

    async def pause_after_staging(db, **kwargs):
        staged.set()
        await release.wait()
        return await real_delete(db, **kwargs)

    monkeypatch.setattr(candidate_photo_service, "_extract_isolated", paused_extract)
    monkeypatch.setattr(candidate_service, "delete_candidate_row", pause_after_staging)
    async with factory() as worker, factory() as deleter, factory() as observer:
        worker_pid, delete_pid = await pid(worker), await pid(deleter)
        photo_task = asyncio.create_task(
            photo(worker, storage, photos, ctx, candidate_id, document_id)
        )
        delete_task = None
        try:
            await asyncio.wait_for(in_extraction.wait(), 10)  # original already read
            delete_task = asyncio.create_task(delete(deleter, ctx, candidate_id, storage, photos))
            await asyncio.wait_for(staged.wait(), 10)
            extraction_go.set()
            assert await blocked_or_finished(observer, photo_task, worker_pid, delete_pid)
            assert derived_files(photos) == []  # nothing saved while waiting
            release.set()
            assert await asyncio.wait_for(photo_task, 20) is None
            assert await asyncio.wait_for(delete_task, 20) == 1
        finally:
            await stop(photo_task, delete_task)
        assert derived_files(photos) == []
        assert await observer.scalar(select(func.count()).select_from(CandidatePhotoVersion)) == 0


async def test_photo_persistence_is_idempotent_for_document_and_extractor(setup, factory):
    ctx, candidate_id, document_id, storage, photos = setup
    async with factory() as first, factory() as second:
        row = await photo(first, storage, photos, ctx, candidate_id, document_id)
        again = await photo(second, storage, photos, ctx, candidate_id, document_id)
    assert row is not None and again is not None and row.id == again.id
    assert len(derived_files(photos)) == 1


async def test_photo_commit_failure_removes_unreferenced_derived_asset(
    setup, factory, monkeypatch
):
    ctx, candidate_id, document_id, storage, photos = setup
    async with factory() as worker, factory() as observer:
        real_commit = worker.commit
        failed = False

        async def fail_first_commit():
            nonlocal failed
            if not failed:
                failed = True
                raise OSError("synthetic photo commit failure")
            await real_commit()

        monkeypatch.setattr(worker, "commit", fail_first_commit)
        row = await photo(worker, storage, photos, ctx, candidate_id, document_id)
        assert row is not None and row.status == "EXTRACTION_FAILED"
        assert derived_files(photos) == []
        assert await observer.scalar(
            select(func.count()).select_from(CandidatePhotoVersion).where(
                CandidatePhotoVersion.status == "AVAILABLE"
            )
        ) == 0


async def test_ambiguous_photo_commit_keeps_referenced_asset(setup, factory, monkeypatch):
    """COMMIT became durable but the client saw an error: DB is the arbiter."""
    ctx, candidate_id, document_id, storage, photos = setup
    async with factory() as worker, factory() as observer:
        real_commit = worker.commit

        async def commit_then_fail():
            await real_commit()
            raise OSError("synthetic ambiguous photo commit")

        monkeypatch.setattr(worker, "commit", commit_then_fail)
        await photo(worker, storage, photos, ctx, candidate_id, document_id)
        keys = (await observer.scalars(
            select(CandidatePhotoVersion.derived_storage_key).where(
                CandidatePhotoVersion.status == "AVAILABLE"
            )
        )).all()
        assert len(keys) == 1 and len(derived_files(photos)) == 1
        assert keys[0].endswith(derived_files(photos)[0].name)


async def test_save_succeeds_but_row_insert_fails_removes_asset(setup, factory, monkeypatch):
    ctx, candidate_id, document_id, storage, photos = setup
    real_create = candidate_photo_service.create_photo_version
    calls = 0

    async def fail_available_insert(db, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic row persistence failure")
        return await real_create(db, **kwargs)

    monkeypatch.setattr(candidate_photo_service, "create_photo_version", fail_available_insert)
    async with factory() as worker:
        row = await photo(worker, storage, photos, ctx, candidate_id, document_id)
    assert row is not None and row.status == "EXTRACTION_FAILED"
    assert derived_files(photos) == []


async def test_derived_cleanup_failure_is_bounded_and_observable(
    setup, factory, monkeypatch, caplog
):
    ctx, candidate_id, document_id, storage, photos = setup
    real_create = candidate_photo_service.create_photo_version

    async def fail_insert(db, **kwargs):
        raise OSError("synthetic row persistence failure")

    async def fail_cleanup(*, tenant_id, storage_key):
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(candidate_photo_service, "create_photo_version", fail_insert)
    monkeypatch.setattr(photos, "delete_owned", fail_cleanup)
    assert real_create is not None
    async with factory() as worker:
        assert await photo(worker, storage, photos, ctx, candidate_id, document_id) is None
    assert "component=storage_recovery" in caplog.text
    assert str(ctx.tenant_id) not in caplog.text
    assert len(derived_files(photos)) == 1  # unresolved state is visible, not hidden


async def test_tenant_suspended_during_persistence_phase_writes_nothing(
    setup, factory, monkeypatch
):
    from meyar.services.tenant_authority import TenantInactiveError, set_tenant_active

    ctx, candidate_id, document_id, storage, photos = setup
    in_extraction, release = asyncio.Event(), asyncio.Event()
    inner = candidate_photo_service._extract_isolated

    async def paused(data: bytes, kind: str) -> dict:
        in_extraction.set()
        await release.wait()
        return await inner(data, kind)

    monkeypatch.setattr(candidate_photo_service, "_extract_isolated", paused)
    async with factory() as worker, factory() as operator, factory() as observer:
        task = asyncio.create_task(photo(worker, storage, photos, ctx, candidate_id, document_id))
        try:
            await asyncio.wait_for(in_extraction.wait(), 10)
            await set_tenant_active(operator, tenant_id=ctx.tenant_id, is_active=False)
            await operator.commit()
            release.set()
            with pytest.raises(TenantInactiveError):
                await asyncio.wait_for(task, 20)
        finally:
            await stop(task)
        assert derived_files(photos) == []
        assert await observer.scalar(select(func.count()).select_from(CandidatePhotoVersion)) == 0


async def test_unrelated_candidate_photo_is_not_serialized_behind_delete(
    setup, factory, db_session, monkeypatch
):
    ctx, candidate_id, document_id, storage, photos = setup
    prepared = await prepare_candidate_document(
        LocalTextParser(), filename="other.pdf", content_type="application/pdf",
        data=(FIXTURES_DIR / "valid_cv.pdf").read_bytes(), max_bytes=10 * 1024 * 1024,
    )
    other = await create_candidate(db_session, tenant_id=ctx.tenant_id)
    other_doc = await persist_candidate_document(
        db_session, storage, prepared, tenant_id=ctx.tenant_id,
        candidate_id=other.id, filename="other.pdf",
    )
    await db_session.commit()
    staged, release = asyncio.Event(), asyncio.Event()
    real_delete = candidate_service.delete_candidate_row

    async def pause_after_staging(db, **kwargs):
        staged.set()
        await release.wait()
        return await real_delete(db, **kwargs)

    monkeypatch.setattr(candidate_service, "delete_candidate_row", pause_after_staging)
    async with factory() as worker, factory() as deleter:
        delete_task = asyncio.create_task(delete(deleter, ctx, candidate_id, storage, photos))
        try:
            await asyncio.wait_for(staged.wait(), 10)
            row = await asyncio.wait_for(
                photo(worker, storage, photos, ctx, other.id, other_doc.id), 10
            )
            assert row is not None and row.status == "AVAILABLE"
        finally:
            release.set()
            await asyncio.wait_for(delete_task, 20)
    assert len(derived_files(photos)) == 1  # only the unrelated candidate's asset remains

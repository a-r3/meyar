"""Issue #46: real PostgreSQL and local synthetic originals, no timing sleeps."""

import asyncio
from datetime import UTC, datetime

import pytest
from conftest import FIXTURES_DIR, TEST_DATABASE_URL
from fastapi import HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from meyar.api.v1.candidates import _revalidate_upload_authority
from meyar.core.auth import TenantContext
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.models.api_key import ApiKey
from meyar.models.candidate import Candidate
from meyar.models.candidate_document import CandidateDocument
from meyar.services import candidate_service
from meyar.services.api_key_repo import create_api_key
from meyar.services.candidate_document_service import (
    persist_candidate_document,
    prepare_candidate_document,
)
from meyar.services.candidate_repo import create_candidate
from meyar.services.storage_recovery import recover_on_failure
from meyar.services.tenant_authority import (
    TenantInactiveError,
    require_active_tenant,
    set_tenant_active,
)
from meyar.services.tenant_repo import create_tenant
from meyar.storage.local import LocalFilesystemStorage
from meyar.storage.photo import LocalPhotoStorage


@pytest.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SHOW transaction_isolation")) == "read committed"
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def setup(db_session, tenant_and_key, tmp_path):
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
    return ctx, candidate.id, document.storage_key, prepared, storage, photos


def files(storage):
    return [p for p in storage._root.rglob("*") if p.is_file() and p.suffix != ".json"]


async def blocked_or_finished(observer, task, waiter, holder):
    """Observe the server wait graph, or actual task completion, within a bound."""
    async with asyncio.timeout(10):
        while not task.done():
            blockers = await observer.scalar(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter}
            )
            if holder in blockers:
                return True
        return False


async def delete(db, ctx, candidate_id, storage, photos):
    # Same Tenant authority phase as the authenticated DELETE request.
    await require_active_tenant(db, ctx.tenant_id, lock=True)
    return await candidate_service.delete_candidate_cascade(
        db, storage, tenant_id=ctx.tenant_id, candidate_id=candidate_id,
        photo_storage=photos, actor_id=ctx.api_key_id,
    )


async def upload(db, ctx, candidate_id, prepared, storage):
    # Exact direct-upload persistence phase, after DB-free preparation.
    async with recover_on_failure(db):
        await _revalidate_upload_authority(db, ctx, candidate_id)
        document = await persist_candidate_document(
            db, storage, prepared, tenant_id=ctx.tenant_id,
            candidate_id=candidate_id, filename="synthetic-new.pdf",
        )
        await db.commit()
        return document


async def stop(*tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
    await asyncio.gather(*[t for t in tasks if t is not None], return_exceptions=True)


async def test_delete_first_does_not_leave_concurrent_original(
    setup, factory, monkeypatch,
):
    ctx, candidate_id, old_key, prepared, storage, photos = setup
    staged, release = asyncio.Event(), asyncio.Event()
    real_delete = candidate_service.delete_candidate_row

    async def pause_after_staging(db, **kwargs):
        assert not (storage._root / old_key).exists()
        assert len(files(storage)) == 1  # old original really moved to trash
        staged.set()
        await release.wait()
        return await real_delete(db, **kwargs)

    monkeypatch.setattr(candidate_service, "delete_candidate_row", pause_after_staging)
    async with factory() as deleter, factory() as uploader, factory() as observer:
        delete_pid = await deleter.scalar(text("SELECT pg_backend_pid()"))
        upload_pid = await uploader.scalar(text("SELECT pg_backend_pid()"))
        assert delete_pid != upload_pid
        deleting = asyncio.create_task(delete(deleter, ctx, candidate_id, storage, photos))
        uploading = None
        try:
            await asyncio.wait_for(staged.wait(), 10)
            uploading = asyncio.create_task(upload(uploader, ctx, candidate_id, prepared, storage))
            blocked = await blocked_or_finished(observer, uploading, upload_pid, delete_pid)
            if not blocked:
                # Rejected baseline: upload is durably visible to an independent
                # connection AFTER delete enumerated/staged only the old set.
                document = await uploading
                assert await observer.scalar(
                    select(CandidateDocument.id).where(CandidateDocument.id == document.id)
                ) == document.id
                assert (storage._root / document.storage_key).is_file()
            release.set()
            assert await asyncio.wait_for(deleting, 10) == 1
            if blocked:
                with pytest.raises(HTTPException) as exc:
                    await asyncio.wait_for(uploading, 10)
                assert exc.value.status_code == 404
            assert await observer.scalar(select(func.count()).select_from(Candidate)) == 0
            assert await observer.scalar(select(func.count()).select_from(CandidateDocument)) == 0
            remaining = files(storage)
            assert len(remaining) == 0, (
                f"durable candidates=0 documents=0 filesystem originals={len(remaining)}; "
                "upload committed after the delete staged its old asset set"
            )
            assert blocked, "DELETE did not serialize upload at candidate authority"
        finally:
            release.set()
            tasks = [t for t in (deleting, uploading) if t is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("outcome", ["commit", "rollback", "ambiguous"])
@pytest.mark.parametrize("repetition", range(3))
async def test_upload_first_delete_waits_then_stages_durable_set(
    setup, factory, monkeypatch, outcome, repetition,
):
    ctx, candidate_id, old_key, prepared, storage, photos = setup
    persisted, release = asyncio.Event(), asyncio.Event()
    real_commit = None
    enumerated = []
    real_list = candidate_service.list_candidate_documents

    async def list_after_upload(db, **kwargs):
        assert release.is_set(), "delete enumerated before upload released authority"
        documents = await real_list(db, **kwargs)
        enumerated.extend(d.storage_key for d in documents)
        return documents

    monkeypatch.setattr(candidate_service, "list_candidate_documents", list_after_upload)
    async with factory() as uploader, factory() as deleter, factory() as observer:
        real_commit = uploader.commit

        async def controlled_commit():
            # SAVEPOINT release uses a transaction method, not Session.commit.
            persisted.set()
            await release.wait()
            if outcome == "rollback":
                raise RuntimeError("Synthetic upload commit refused")
            await real_commit()
            if outcome == "ambiguous":
                raise RuntimeError("Synthetic upload commit acknowledgement lost")

        monkeypatch.setattr(uploader, "commit", controlled_commit)
        up_pid = await uploader.scalar(text("SELECT pg_backend_pid()"))
        del_pid = await deleter.scalar(text("SELECT pg_backend_pid()"))
        uploading = asyncio.create_task(upload(uploader, ctx, candidate_id, prepared, storage))
        deleting = None
        try:
            await asyncio.wait_for(persisted.wait(), 10)
            # New original exists, but independent DB authority still sees old only.
            assert len(files(storage)) == 2
            assert await observer.scalar(select(func.count()).select_from(CandidateDocument)) == 1
            deleting = asyncio.create_task(delete(deleter, ctx, candidate_id, storage, photos))
            assert await blocked_or_finished(observer, deleting, del_pid, up_pid)
            assert enumerated == []
            release.set()
            if outcome == "commit":
                await asyncio.wait_for(uploading, 10)
            else:
                with pytest.raises(RuntimeError, match="Synthetic upload commit"):
                    await asyncio.wait_for(uploading, 10)
            expected = 1 if outcome == "rollback" else 2
            assert await asyncio.wait_for(deleting, 10) == expected
            assert len(enumerated) == expected and old_key in enumerated
            assert await observer.scalar(select(func.count()).select_from(Candidate)) == 0
            assert await observer.scalar(select(func.count()).select_from(CandidateDocument)) == 0
            assert files(storage) == []
        finally:
            release.set()
            await stop(uploading, deleting)


@pytest.mark.parametrize("durable", [False, True])
async def test_delete_commit_failure_releases_waiting_upload_with_recovery(
    setup, factory, monkeypatch, durable,
):
    ctx, candidate_id, old_key, prepared, storage, photos = setup
    staged, release = asyncio.Event(), asyncio.Event()
    async with factory() as deleter, factory() as uploader, factory() as observer:
        real_commit = deleter.commit

        async def fail_commit():
            assert not (storage._root / old_key).exists()
            staged.set()
            await release.wait()
            if durable:
                await real_commit()
            raise RuntimeError("Synthetic delete commit failure")

        monkeypatch.setattr(deleter, "commit", fail_commit)
        del_pid = await deleter.scalar(text("SELECT pg_backend_pid()"))
        up_pid = await uploader.scalar(text("SELECT pg_backend_pid()"))
        deleting = asyncio.create_task(delete(deleter, ctx, candidate_id, storage, photos))
        uploading = None
        try:
            await asyncio.wait_for(staged.wait(), 10)
            uploading = asyncio.create_task(upload(uploader, ctx, candidate_id, prepared, storage))
            assert await blocked_or_finished(observer, uploading, up_pid, del_pid)
            release.set()
            with pytest.raises(RuntimeError, match="Synthetic delete commit failure"):
                await asyncio.wait_for(deleting, 10)
            if durable:
                with pytest.raises(HTTPException) as exc:
                    await asyncio.wait_for(uploading, 10)
                assert exc.value.status_code == 404
                assert files(storage) == []
                assert await observer.scalar(select(func.count()).select_from(Candidate)) == 0
                assert await observer.scalar(
                    select(func.count()).select_from(CandidateDocument)
                ) == 0
            else:
                document = await asyncio.wait_for(uploading, 10)
                keys = set(await observer.scalars(select(CandidateDocument.storage_key)))
                assert keys == {old_key, document.storage_key}
                assert len(files(storage)) == 2
                for key in keys:
                    assert await storage.read(storage_key=key) == prepared.data
                assert files(LocalFilesystemStorage(str(storage._root / ".trash"))) == []
        finally:
            release.set()
            await stop(deleting, uploading)


@pytest.mark.parametrize("other_tenant", [False, True])
async def test_unrelated_candidate_upload_does_not_wait_for_delete(
    setup, factory, db_session, monkeypatch, other_tenant,
):
    ctx, candidate_id, old_key, prepared, storage, photos = setup
    if other_tenant:
        tenant = await create_tenant(db_session, name="Synthetic independent tenant")
        key, _ = await create_api_key(db_session, tenant_id=tenant.id, env="test")
        other_ctx = TenantContext(tenant.id, key.id, list(key.scopes))
    else:
        other_ctx = ctx
    other = await create_candidate(db_session, tenant_id=other_ctx.tenant_id)
    await db_session.commit()
    staged, release = asyncio.Event(), asyncio.Event()
    real_delete = candidate_service.delete_candidate_row

    async def pause(db, **kwargs):
        staged.set()
        await release.wait()
        await real_delete(db, **kwargs)

    monkeypatch.setattr(candidate_service, "delete_candidate_row", pause)
    async with factory() as deleter, factory() as uploader, factory() as observer:
        deleting = asyncio.create_task(delete(deleter, ctx, candidate_id, storage, photos))
        try:
            await asyncio.wait_for(staged.wait(), 10)
            document = await asyncio.wait_for(
                upload(uploader, other_ctx, other.id, prepared, storage), 10
            )
            # Commit completed while delete is still held at the barrier.
            assert not deleting.done() and not release.is_set()
            assert await observer.scalar(
                select(CandidateDocument.id).where(CandidateDocument.id == document.id)
            ) == document.id
            if other_tenant:
                # A forged cross-tenant ID is absent and does not lock that row.
                assert await asyncio.wait_for(
                    delete(observer, other_ctx, candidate_id, storage, photos), 10
                ) is None
                await observer.rollback()
            release.set()
            assert await asyncio.wait_for(deleting, 10) == 1
            assert not (storage._root / old_key).exists()
            assert files(storage) == [storage._root / document.storage_key]
        finally:
            release.set()
            await stop(deleting)


async def deactivate(db, ctx, authority):
    if authority == "tenant":
        await set_tenant_active(db, tenant_id=ctx.tenant_id, is_active=False)
    else:
        await db.execute(
            update(ApiKey).where(ApiKey.id == ctx.api_key_id)
            .values(revoked_at=datetime.now(UTC))
        )


@pytest.mark.parametrize("authority", ["tenant", "key"])
async def test_upload_delete_and_deactivation_follow_lock_order(
    setup, factory, monkeypatch, authority,
):
    ctx, candidate_id, _, prepared, storage, photos = setup
    persisted, release = asyncio.Event(), asyncio.Event()
    async with factory() as uploader, factory() as deleter, factory() as admin, factory() as obs:
        real_commit = uploader.commit

        async def pause_commit():
            persisted.set()
            await release.wait()
            await real_commit()

        monkeypatch.setattr(uploader, "commit", pause_commit)
        up_pid = await uploader.scalar(text("SELECT pg_backend_pid()"))
        del_pid = await deleter.scalar(text("SELECT pg_backend_pid()"))
        admin_pid = await admin.scalar(text("SELECT pg_backend_pid()"))
        uploading = asyncio.create_task(upload(uploader, ctx, candidate_id, prepared, storage))
        deleting = disabling = None
        try:
            await asyncio.wait_for(persisted.wait(), 10)
            deleting = asyncio.create_task(delete(deleter, ctx, candidate_id, storage, photos))
            assert await blocked_or_finished(obs, deleting, del_pid, up_pid)

            async def disable_and_commit():
                await deactivate(admin, ctx, authority)
                await admin.commit()

            disabling = asyncio.create_task(disable_and_commit())
            assert await blocked_or_finished(obs, disabling, admin_pid, up_pid)
            release.set()
            await asyncio.wait_for(asyncio.gather(uploading, deleting, disabling), 10)
            assert deleting.result() == 2
            assert files(storage) == []
            assert await obs.scalar(select(func.count()).select_from(CandidateDocument)) == 0
        finally:
            release.set()
            await stop(uploading, deleting, disabling)


@pytest.mark.parametrize("authority", ["tenant", "key"])
async def test_deactivation_first_upload_rechecks_before_saving(
    setup, factory, authority,
):
    ctx, candidate_id, old_key, prepared, storage, _ = setup
    async with factory() as admin, factory() as uploader, factory() as obs:
        admin_pid = await admin.scalar(text("SELECT pg_backend_pid()"))
        up_pid = await uploader.scalar(text("SELECT pg_backend_pid()"))
        await deactivate(admin, ctx, authority)
        uploading = asyncio.create_task(upload(uploader, ctx, candidate_id, prepared, storage))
        try:
            assert await blocked_or_finished(obs, uploading, up_pid, admin_pid)
            await admin.commit()
            expected = TenantInactiveError if authority == "tenant" else HTTPException
            with pytest.raises(expected) as exc:
                await asyncio.wait_for(uploading, 10)
            if authority == "key":
                assert exc.value.status_code == 401
            assert files(storage) == [storage._root / old_key]
            assert await obs.scalar(select(func.count()).select_from(CandidateDocument)) == 1
        finally:
            await stop(uploading)


async def test_suspension_first_delete_rechecks_before_staging(setup, factory):
    ctx, candidate_id, old_key, _, storage, photos = setup
    async with factory() as admin, factory() as deleter, factory() as obs:
        admin_pid = await admin.scalar(text("SELECT pg_backend_pid()"))
        del_pid = await deleter.scalar(text("SELECT pg_backend_pid()"))
        await deactivate(admin, ctx, "tenant")
        # Direct service caller exercises the new entry Tenant lock itself.
        deleting = asyncio.create_task(candidate_service.delete_candidate_cascade(
            deleter, storage, tenant_id=ctx.tenant_id, candidate_id=candidate_id,
            photo_storage=photos, actor_id=ctx.api_key_id,
        ))
        try:
            assert await blocked_or_finished(obs, deleting, del_pid, admin_pid)
            await admin.commit()
            with pytest.raises(TenantInactiveError):
                await asyncio.wait_for(deleting, 10)
            assert files(storage) == [storage._root / old_key]
            assert await obs.scalar(select(func.count()).select_from(Candidate)) == 1
        finally:
            await stop(deleting)

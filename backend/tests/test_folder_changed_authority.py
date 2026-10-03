"""S8: immutable scanner snapshots and changed-file/delete authority.

Real PostgreSQL and synthetic local storage. Gates use Events; server lock
observations, never elapsed sleeps, establish which operation owns authority.
The synchronous scanner runs in a worker for the acquisition gates; its exact
immutable outcomes then feed the real indexer validation/persistence path.
"""

import asyncio
import hashlib
import mmap
import os

import pytest
from test_folder_reconcile_concurrency import (
    MAX_BYTES,
    GateParser,
    X,
    commit_scan,
    contention,
    make_commit,
    make_root,
    originals,
    pid,
    state,
    stop,
)
from test_folder_reconcile_concurrency import env as env
from test_folder_reconcile_concurrency import factory as factory

from meyar.ingestion import folder_scanner
from meyar.ingestion.folder_scanner import DiscoveredFile, UnstableFile
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.services import candidate_document_service, candidate_service, folder_indexer_service
from meyar.services.tenant_authority import TenantInactiveError, set_tenant_active
from meyar.services.tenant_repo import create_tenant
from meyar.storage.photo import LocalPhotoStorage

CHANGED = X + b"\n% synthetic changed revision\n"


@pytest.mark.parametrize("pass_number", [1, 2])
async def test_mmap_mutation_with_unchanged_metadata_cannot_persist_mixed_bytes(
    env, factory, monkeypatch, pass_number,
):
    tenant, _, storage, base = env
    old = X + b"\n%" + b"A" * 150 + b"\n"
    replacement = old.replace(b"PDF-1.3", b"PDF-1.4", 1).replace(b"A" * 150, b"B" * 150)
    root = make_root(base, "mapped", {"cv.pdf": old})
    path = root / "cv.pdf"
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), asyncio.Event()
    real_read = os.read
    inode = path.stat().st_ino
    gated = False
    reading_pass = 1

    async def gate():
        entered.set()
        await release.wait()

    def read(fd, size):
        nonlocal gated, reading_pass
        chunk = real_read(fd, size)
        if os.fstat(fd).st_ino == inode:
            if not gated and reading_pass == pass_number:
                gated = True
                asyncio.run_coroutine_threadsafe(gate(), loop).result(timeout=15)
            if not chunk:
                reading_pass += 1
        return chunk

    monkeypatch.setattr(folder_scanner.os, "read", read)
    monkeypatch.setattr(folder_scanner, "_READ_CHUNK_BYTES", 64)
    with path.open("r+b") as handle, mmap.mmap(handle.fileno(), 0) as mapped:
        # Establish the writable page BEFORE acquisition. Later writes to this
        # same mapped page need no new write fault / timestamp update on Linux.
        mapped[0] = mapped[0]
        before = path.stat()
        acquiring = asyncio.create_task(asyncio.to_thread(
            lambda: list(folder_scanner.scan_source_root(str(root), max_bytes=MAX_BYTES))
        ))
        try:
            await asyncio.wait_for(entered.wait(), 15)
            mapped[:] = replacement
            after = path.stat()
            assert folder_scanner._identity(before) == folder_scanner._identity(after)
            release.set()
            entries = await asyncio.wait_for(acquiring, 15)
        finally:
            release.set()
            await stop(acquiring)
    monkeypatch.setattr(folder_indexer_service, "scan_source_root", lambda *a, **k: iter(entries))
    async with factory() as scanner:
        summary = await commit_scan(scanner, storage, LocalTextParser(), tenant.id, root)
    async with factory() as observer:
        final = await state(observer, storage, tenant.id)
    # Exact S7 persisted old header + new tail: a valid PDF matching NEITHER
    # complete source generation, despite unchanged pre/post metadata.
    for document in final["documents"]:
        data = await storage.read(storage_key=document.storage_key)
        assert data in (old, replacement), "scanner persisted a mixed source generation"
    assert entries == [UnstableFile("cv.pdf")]
    assert summary.skipped_unstable == 1 and summary.successful == 0
    assert final["candidates"] == [] and final["rows"] == []
    await assert_bytes(final, storage, [])


async def seed_changed(factory, env):
    tenant, _, storage, base = env
    root = make_root(base, "source", {"cv.pdf": X})
    async with factory() as seed:
        await commit_scan(seed, storage, LocalTextParser(), tenant.id, root)
    async with factory() as observer:
        before = await state(observer, storage, tenant.id)
    (root / "cv.pdf").write_bytes(CHANGED)
    return root, before["candidates"][0], before["documents"][0].storage_key


async def delete(db, env, candidate_id):
    tenant, key, storage, base = env
    return await candidate_service.delete_candidate_cascade(
        db, storage, tenant_id=tenant.id, candidate_id=candidate_id,
        photo_storage=LocalPhotoStorage(str(base / "storage")), actor_id=key.id,
    )


async def assert_bytes(final, storage, expected):
    actual = []
    for document in final["documents"]:
        data = await storage.read(storage_key=document.storage_key)
        assert document.sha256_hash == hashlib.sha256(data).hexdigest()
        assert document.byte_size == len(data)
        actual.append(data)
    assert sorted(actual) == sorted(expected)
    for row in final["rows"]:
        if row.index_status == "INDEXED" and row.candidate_document_id is not None:
            doc = next(d for d in final["documents"] if d.id == row.candidate_document_id)
            assert (row.sha256_hash, row.byte_size) == (doc.sha256_hash, doc.byte_size)


@pytest.mark.parametrize("phase", [
    "discovery", "read", "grow", "truncate", "replace", "stable", "after-acquisition",
])
@pytest.mark.parametrize("tracked", [False, True])
async def test_snapshot_acquisition_and_persisted_authority(
    env, factory, monkeypatch, phase, tracked,
):
    tenant, _, storage, base = env
    root = make_root(base, "acquire", {"cv.pdf": X})
    path = root / "cv.pdf"
    if tracked:
        async with factory() as seed:
            await commit_scan(seed, storage, LocalTextParser(), tenant.id, root)
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), asyncio.Event()
    real_open, real_read = os.open, os.read
    inode = path.stat().st_ino
    gated = False

    async def gate():
        entered.set()
        await release.wait()

    def wait():
        nonlocal gated
        if not gated:
            gated = True
            asyncio.run_coroutine_threadsafe(gate(), loop).result(timeout=15)

    def open_file(target, flags, *args, **kwargs):
        if phase == "discovery" and str(target) == str(path):
            wait()  # walk/lstat complete; descriptor/stat/read have not started
        return real_open(target, flags, *args, **kwargs)

    def read_file(fd, size):
        data = real_read(fd, size)
        if phase in ("read", "grow", "truncate", "replace") and os.fstat(fd).st_ino == inode:
            wait()  # first chunk acquired; writer now changes the same inode
        return data

    monkeypatch.setattr(folder_scanner.os, "open", open_file)
    monkeypatch.setattr(folder_scanner.os, "read", read_file)
    monkeypatch.setattr(folder_scanner, "_READ_CHUNK_BYTES", 64)
    acquiring = asyncio.create_task(asyncio.to_thread(
        lambda: list(folder_scanner.scan_source_root(str(root), max_bytes=MAX_BYTES))
    ))
    try:
        if phase in ("discovery", "read", "grow", "truncate", "replace"):
            await asyncio.wait_for(entered.wait(), 15)
            previous = path.stat()
            if phase == "replace":
                other = root / "replacement"
                other.write_bytes(CHANGED)
                os.replace(other, path)
            else:
                replacement = {
                    "discovery": CHANGED, "read": b"x" * len(X),
                    "grow": CHANGED, "truncate": X[:70],
                }[phase]
                path.write_bytes(replacement)
            # Same mtime and, during read, SAME SIZE: mtime alone cannot decide.
            os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            release.set()
        entries = await asyncio.wait_for(acquiring, 15)
    finally:
        release.set()
        await stop(acquiring)
    if phase == "after-acquisition":
        path.write_bytes(CHANGED)
    # Atomic replacement may leave the opened old inode unchanged on some
    # filesystems. Its complete old bytes are a valid immutable snapshot;
    # an observed metadata change instead requires a safe skip.
    unstable = isinstance(entries[0], UnstableFile)
    if phase in ("read", "grow", "truncate"):
        assert unstable
    if unstable:
        assert entries == [UnstableFile("cv.pdf")]
    else:
        assert isinstance(entries[0], DiscoveredFile)
        expected = CHANGED if phase == "discovery" else X
        assert entries[0].data == expected
    monkeypatch.setattr(folder_indexer_service, "scan_source_root", lambda *a, **k: iter(entries))
    async with factory() as scanner:
        summary = await commit_scan(scanner, storage, LocalTextParser(), tenant.id, root)
    async with factory() as observer:
        final = await state(observer, storage, tenant.id)
    if unstable:
        assert summary.skipped_unstable == 1 and summary.successful == 0
        assert summary.missing == 0 and summary.failed == 0
        if tracked:
            assert len(final["candidates"]) == 1 and final["rows"][0].index_status == "INDEXED"
        else:
            assert final["candidates"] == [] and final["rows"] == []
        await assert_bytes(final, storage, [X] if tracked else [])
        # The skipped source is safely retried using a later stable acquisition.
        path.write_bytes(CHANGED)
        monkeypatch.setattr(
            folder_indexer_service, "scan_source_root", folder_scanner.scan_source_root,
        )
        async with factory() as scanner:
            retried = await commit_scan(scanner, storage, LocalTextParser(), tenant.id, root)
        async with factory() as observer:
            final = await state(observer, storage, tenant.id)
        assert (retried.successful, retried.failed) == (1, 0)
        await assert_bytes(final, storage, [X, CHANGED] if tracked else [CHANGED])
    elif tracked:
        if phase == "discovery":
            assert (summary.changed, summary.successful, summary.failed) == (1, 1, 0)
            await assert_bytes(final, storage, [X, CHANGED])
        else:
            assert summary.unchanged == 1 and summary.successful == 0
            await assert_bytes(final, storage, [X])
    else:
        assert (summary.new, summary.successful, summary.failed) == (1, 1, 0)
        await assert_bytes(final, storage, [expected])


@pytest.mark.parametrize("outcome", ["commit", "rollback", "ambiguous"])
@pytest.mark.parametrize("repetition", range(2))
async def test_changed_file_delete_wins(env, factory, monkeypatch, outcome, repetition):
    tenant, _, storage, _ = env
    root, owner, old_key = await seed_changed(factory, env)
    parser = GateParser()
    staged, release_delete = asyncio.Event(), asyncio.Event()
    real_delete = candidate_service.delete_candidate_row

    async def pause(db, **kwargs):
        assert any(f.startswith(f".trash/{tenant.id.hex}/") for f in originals(storage))
        staged.set()
        await release_delete.wait()
        return await real_delete(db, **kwargs)

    monkeypatch.setattr(candidate_service, "delete_candidate_row", pause)
    async with factory() as scanner, factory() as deleter, factory() as observer:
        scan_pid, del_pid = await pid(scanner), await pid(deleter)
        make_commit(deleter, outcome)
        scanning = asyncio.create_task(commit_scan(scanner, storage, parser, tenant.id, root))
        deleting = None
        try:
            await asyncio.wait_for(parser.first_entered.wait(), 15)
            deleting = asyncio.create_task(delete(deleter, env, owner))
            await asyncio.wait_for(staged.wait(), 15)
            parser.release.set()
            seen = await contention(observer, scanning, scan_pid, del_pid)
            # No newly saved original while delete owns Candidate UPDATE.
            normal = [f for f in originals(storage) if f.startswith(f"{tenant.id}/")]
            assert seen == "blocked"
            release_delete.set()
            if outcome == "commit":
                assert await asyncio.wait_for(deleting, 15) == 1
            else:
                with pytest.raises(RuntimeError, match="Synthetic commit"):
                    await asyncio.wait_for(deleting, 15)
            try:
                summary = await asyncio.wait_for(scanning, 15)
            except Exception:
                # Baseline defect proof: even the unhandled FK failure must
                # leave S4 filesystem authority matching durable DB truth.
                final = await state(observer, storage, tenant.id)
                await assert_bytes(final, storage, [X] if outcome == "rollback" else [])
                raise
            assert normal == [], "re-ingest saved bytes before Candidate authority"
            final = await state(observer, storage, tenant.id)
            row = final["rows"][0]
            assert summary.changed == 1 and summary.new == 0
            if outcome == "rollback":
                assert final["candidates"] == [owner]
                assert (summary.successful, summary.failed) == (1, 0)
                assert row.index_status == "INDEXED" and row.candidate_id == owner
                assert old_key in final["files"]
                await assert_bytes(final, storage, [X, CHANGED])
            else:
                assert final["candidates"] == []
                assert (summary.successful, summary.failed) == (0, 1)
                assert row.index_status == "FAILED"
                assert row.failure_code == "INDEX_AUTHORITY_INVALID"
                assert row.candidate_id is None and row.candidate_document_id is None
                assert row.sha256_hash == hashlib.sha256(CHANGED).hexdigest()
                await assert_bytes(final, storage, [])
                row_id = row.id
                async with factory() as retry_db:
                    retried = await commit_scan(
                        retry_db, storage, LocalTextParser(), tenant.id, root,
                    )
                async with factory() as retry_observer:
                    retried_state = await state(retry_observer, storage, tenant.id)
                assert (retried.retried, retried.successful, retried.failed) == (1, 1, 0)
                assert retried_state["rows"][0].id == row_id
                assert retried_state["candidates"][0] != owner
                await assert_bytes(retried_state, storage, [CHANGED])
        finally:
            parser.release.set()
            release_delete.set()
            await stop(scanning, deleting)


@pytest.mark.parametrize("outcome", ["commit", "rollback", "ambiguous"])
@pytest.mark.parametrize("repetition", range(2))
async def test_changed_file_persistence_wins(env, factory, monkeypatch, outcome, repetition):
    tenant, _, storage, _ = env
    root, owner, _ = await seed_changed(factory, env)
    saved, release = asyncio.Event(), asyncio.Event()
    real_save = storage.save

    async def pause_save(**kwargs):
        key = await real_save(**kwargs)
        saved.set()  # bytes really exist, CandidateDocument not yet inserted
        await release.wait()
        return key

    monkeypatch.setattr(storage, "save", pause_save)
    async with factory() as scanner, factory() as deleter, factory() as observer:
        scan_pid, del_pid = await pid(scanner), await pid(deleter)
        make_commit(scanner, outcome)
        scanning = asyncio.create_task(
            commit_scan(scanner, storage, LocalTextParser(), tenant.id, root)
        )
        deleting = None
        try:
            await asyncio.wait_for(saved.wait(), 15)
            assert len(originals(storage)) == 2
            deleting = asyncio.create_task(delete(deleter, env, owner))
            seen = await contention(observer, deleting, del_pid, scan_pid)
            release.set()
            if outcome == "commit":
                summary = await asyncio.wait_for(scanning, 15)
                assert (summary.changed, summary.successful, summary.failed) == (1, 1, 0)
            else:
                with pytest.raises(RuntimeError, match="Synthetic commit"):
                    await asyncio.wait_for(scanning, 15)
            count = await asyncio.wait_for(deleting, 15)
            assert count == (1 if outcome == "rollback" else 2)
            final = await state(observer, storage, tenant.id)
            assert final["candidates"] == [] and final["documents"] == []
            assert final["rows"][0].candidate_id is None
            assert final["rows"][0].candidate_document_id is None
            await assert_bytes(final, storage, [])
            assert seen == "blocked", "delete enumerated assets before re-ingest ended"
        finally:
            release.set()
            await stop(scanning, deleting)


async def test_changed_file_save_then_database_failure_compensates(env, factory, monkeypatch):
    tenant, _, storage, _ = env
    root, owner, old_key = await seed_changed(factory, env)

    async def fail(*args, **kwargs):
        assert len(originals(storage)) == 2  # real save completed and was tracked
        raise RuntimeError("Synthetic database failure after save")

    monkeypatch.setattr(candidate_document_service, "create_candidate_document", fail)
    async with factory() as scanner:
        with pytest.raises(RuntimeError, match="Synthetic database failure"):
            await commit_scan(scanner, storage, LocalTextParser(), tenant.id, root)
    async with factory() as observer:
        final = await state(observer, storage, tenant.id)
        assert final["candidates"] == [owner] and final["files"] == [old_key]
        assert final["rows"][0].index_status == "INDEXED"
        await assert_bytes(final, storage, [X])


@pytest.mark.parametrize("phase", ["parser", "saved"])
async def test_changed_file_tenant_suspension_compensates(env, factory, monkeypatch, phase):
    tenant, _, storage, _ = env
    root, owner, old_key = await seed_changed(factory, env)
    entered, release = asyncio.Event(), asyncio.Event()
    parser = GateParser()
    real_save = storage.save

    async def save(**kwargs):
        key = await real_save(**kwargs)
        entered.set()
        await release.wait()
        return key

    if phase == "saved":
        monkeypatch.setattr(storage, "save", save)
        parser.release.set()
    else:
        entered, release = parser.first_entered, parser.release
    async with factory() as scanner, factory() as admin, factory() as observer:
        task = asyncio.create_task(commit_scan(scanner, storage, parser, tenant.id, root))
        try:
            await asyncio.wait_for(entered.wait(), 15)
            # S7's non-locking tenant check remains intentional: suspension
            # can commit promptly even while the scan holds Candidate SHARE.
            await asyncio.wait_for(
                set_tenant_active(admin, tenant_id=tenant.id, is_active=False), 15,
            )
            await admin.commit()
            release.set()
            with pytest.raises(TenantInactiveError):
                await asyncio.wait_for(task, 15)
            final = await state(observer, storage, tenant.id)
            assert final["candidates"] == [owner] and final["files"] == [old_key]
            await assert_bytes(final, storage, [X])
        finally:
            release.set()
            await stop(task)


@pytest.mark.parametrize("kind", ["candidate-delete", "source-scan", "other-tenant"])
async def test_changed_candidate_authority_does_not_block_unrelated_work(
    env, factory, db_session, monkeypatch, kind,
):
    tenant, key, storage, base = env
    root, _, _ = await seed_changed(factory, env)
    other_tenant = tenant
    if kind == "other-tenant":
        other_tenant = await create_tenant(db_session, name="Synthetic other tenant S8")
        await db_session.commit()
    free_root = make_root(base, "free", {"other.pdf": X + b"\n% synthetic other\n"})
    async with factory() as seed:
        await commit_scan(seed, storage, LocalTextParser(), other_tenant.id, free_root)
    async with factory() as observer:
        free = await state(observer, storage, other_tenant.id)
        free_owner = next(r.candidate_id for r in free["rows"] if r.relative_path == "other.pdf")
    (free_root / "other.pdf").write_bytes(X + b"\n% synthetic unrelated changed\n")
    held, release = asyncio.Event(), asyncio.Event()
    real_save = storage.save

    async def save(**kwargs):
        result = await real_save(**kwargs)
        if kwargs["content"] == CHANGED:
            held.set()
            await release.wait()
        return result

    monkeypatch.setattr(storage, "save", save)
    async with factory() as scanner, factory() as unrelated, factory() as observer:
        task = asyncio.create_task(
            commit_scan(scanner, storage, LocalTextParser(), tenant.id, root)
        )
        try:
            await asyncio.wait_for(held.wait(), 15)
            if kind == "candidate-delete":
                assert await asyncio.wait_for(delete(unrelated, env, free_owner), 15) == 1
            else:
                result = await asyncio.wait_for(commit_scan(
                    unrelated, storage, LocalTextParser(), other_tenant.id, free_root,
                ), 15)
                assert (result.changed, result.successful, result.failed) == (1, 1, 0)
            release.set()
            result = await asyncio.wait_for(task, 15)
            assert (result.changed, result.successful, result.failed) == (1, 1, 0)
            await state(observer, storage, tenant.id)
            await state(observer, storage, other_tenant.id)
        finally:
            release.set()
            await stop(task)


async def test_changed_recent_file_keeps_previous_authority_and_stability_window(
    env, factory, monkeypatch,
):
    tenant, _, storage, _ = env
    root, owner, old_key = await seed_changed(factory, env)
    now = (root / "cv.pdf").stat().st_mtime
    monkeypatch.setattr(folder_indexer_service.time, "time", lambda: now)
    async with factory() as scanner:
        summary = await folder_indexer_service.index_folder_and_commit(
            scanner, storage, LocalTextParser(), tenant_id=tenant.id,
            root_path=str(root), max_bytes=MAX_BYTES, stability_window_seconds=60,
        )
    async with factory() as observer:
        final = await state(observer, storage, tenant.id)
    assert (summary.skipped_unstable, summary.changed, summary.missing) == (1, 0, 0)
    assert final["candidates"] == [owner] and final["files"] == [old_key]
    await assert_bytes(final, storage, [X])

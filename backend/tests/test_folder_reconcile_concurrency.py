"""Issue #46 S7: concurrent folder reconciliation and same-content dedup.

Real PostgreSQL connections (READ COMMITTED), real synthetic local storage,
Events and ``pg_blocking_pids`` as the synchronization/observation authority.
Nothing here relies on a sleep for correctness: the first run is held at an
explicit parser gate while it owns whatever authority the production code
gives it, a second independent session is started, and the server wait graph
(or actual overlap) says what that second session did.
"""

import asyncio
import hashlib
import uuid
from pathlib import Path

import pytest
from conftest import TEST_DATABASE_URL
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.models.candidate import Candidate
from meyar.models.candidate_document import CandidateDocument
from meyar.models.folder_indexed_file import FolderIndexedFile
from meyar.services import candidate_service
from meyar.services.folder_indexer_service import index_folder_and_commit
from meyar.services.tenant_authority import TenantInactiveError, set_tenant_active
from meyar.services.tenant_repo import create_tenant
from meyar.storage.local import LocalFilesystemStorage
from meyar.storage.photo import LocalPhotoStorage

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/synthetic_cvs"
X = (FIXTURES / "valid_cv.pdf").read_bytes()
Y = (FIXTURES / "valid_cv.docx").read_bytes()
MAX_BYTES = 10 * 1024 * 1024


class GateParser:
    """The real parser; the FIRST parse call pauses until released.

    ``calls`` counts parse entries, so a second session that overlapped the
    first (nothing serialized it) is directly observable.
    """

    def __init__(self) -> None:
        self.inner = LocalTextParser()
        self.calls = 0
        self.first_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def parse(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.first_entered.set()
            await self.release.wait()
        return await self.inner.parse(**kwargs)


async def commit_scan(db, storage, parser, tenant_id, root):
    """Exactly the CLI ``index-folder`` persistence phase."""
    return await index_folder_and_commit(
        db, storage, parser, tenant_id=tenant_id, root_path=str(root), max_bytes=MAX_BYTES
    )


@pytest.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=12, max_overflow=12)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(text("SHOW transaction_isolation")) == "read committed"
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def env(db_session, tenant_and_key, tmp_path):
    tenant, key, _ = tenant_and_key
    return tenant, key, LocalFilesystemStorage(str(tmp_path / "storage")), tmp_path


def make_root(base: Path, name: str, files: dict[str, bytes]) -> Path:
    root = base / name
    root.mkdir()
    for filename, data in files.items():
        (root / filename).write_bytes(data)
    return root


def originals(storage) -> list[str]:
    return sorted(
        str(p.relative_to(storage._root)) for p in storage._root.rglob("*") if p.is_file()
    )


async def state(observer, storage, tenant_id):
    """Durable truth seen from an independent connection, plus the filesystem."""
    candidates = (
        await observer.scalars(select(Candidate.id).where(Candidate.tenant_id == tenant_id))
    ).all()
    documents = (
        await observer.scalars(
            select(CandidateDocument).where(CandidateDocument.tenant_id == tenant_id)
        )
    ).all()
    rows = (
        await observer.scalars(
            select(FolderIndexedFile)
            .where(FolderIndexedFile.tenant_id == tenant_id)
            .order_by(FolderIndexedFile.relative_path)
        )
    ).all()
    files = [f for f in originals(storage) if f.startswith(f"{tenant_id}/")]
    # No unreferenced original and no staged-but-unpurged trash for a handled conflict.
    assert sorted(d.storage_key for d in documents) == files, (
        "stored originals differ from durable CandidateDocument authority"
    )
    assert not [f for f in files if ".trash" in f or f.endswith(".tmp")]
    for row in rows:  # ownership: every INDEXED row points at a real candidate/document pair
        # A row whose candidate was deleted keeps its path with NULL links (FK SET
        # NULL) and is retried by the next scan; that is existing delete behavior.
        if row.index_status == "INDEXED" and row.candidate_id is not None:
            assert row.candidate_id in candidates
            doc = next(d for d in documents if d.id == row.candidate_document_id)
            assert doc.candidate_id == row.candidate_id
            assert doc.sha256_hash == row.sha256_hash
    return {"candidates": candidates, "documents": documents, "rows": rows, "files": files}


async def pid(db) -> int:
    return await db.scalar(text("SELECT pg_backend_pid()"))


async def contention(observer, task, waiter_pid, holder_pid, parser=None) -> str:
    """Observe what the second session did while the first holds authority.

    "blocked": the server wait graph shows waiter blocked by holder.
    "overlapped": the second session reached the parser too (nothing stopped it).
    "finished": the second session completed without ever waiting on the holder.
    """
    async with asyncio.timeout(15):
        while True:
            if task.done():
                return "finished"
            blockers = await observer.scalar(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid}
            )
            if holder_pid in blockers:
                return "blocked"
            if parser is not None and parser.calls >= 2:
                return "overlapped"
            await asyncio.sleep(0.005)  # poll cadence only; never a correctness wait


def make_commit(db, outcome):
    """commit | rollback (refused) | ambiguous (durable, acknowledgement lost)."""
    real = db.commit

    async def commit():
        if outcome == "rollback":
            raise RuntimeError("Synthetic commit refused")
        await real()
        if outcome == "ambiguous":
            raise RuntimeError("Synthetic commit acknowledgement lost")

    db.commit = commit


async def finish(task, outcome):
    if outcome in ("rollback", "ambiguous"):
        with pytest.raises(RuntimeError, match="Synthetic commit"):
            await asyncio.wait_for(task, 15)
        return None
    return await asyncio.wait_for(task, 15)


async def stop(*tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
    await asyncio.gather(*[t for t in tasks if t is not None], return_exceptions=True)


async def warm_source(db_factory, storage, tenant_id, root):
    """Create the FolderSource (empty folder state) durably, as an earlier run would."""
    async with db_factory() as db:
        await commit_scan(db, storage, LocalTextParser(), tenant_id, root)


# --------------------------------------------------------------------------
# Scenario A: two concurrent runs, same folder source, same new file
# --------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["commit", "rollback", "ambiguous"])
@pytest.mark.parametrize("source_exists", [True, False], ids=["source-exists", "source-new"])
async def test_same_source_same_file_converges(env, factory, outcome, source_exists):
    tenant, _, storage, base = env
    if source_exists:
        root = make_root(base, "warm", {})
        await warm_source(factory, storage, tenant.id, root)
        (root / "cv.pdf").write_bytes(X)
    else:
        root = make_root(base, "cold", {"cv.pdf": X})
    gate = GateParser()
    async with factory() as first, factory() as second, factory() as observer:
        make_commit(first, outcome)
        first_pid, second_pid = await pid(first), await pid(second)
        winner = asyncio.create_task(commit_scan(first, storage, gate, tenant.id, root))
        loser = None
        try:
            await asyncio.wait_for(gate.first_entered.wait(), 15)
            loser = asyncio.create_task(commit_scan(second, storage, gate, tenant.id, root))
            seen = await contention(observer, loser, second_pid, first_pid, gate)
            gate.release.set()
            first_summary = await finish(winner, outcome)
            second_summary = await asyncio.wait_for(loser, 15)

            final = await state(observer, storage, tenant.id)
            assert len(final["candidates"]) == 1
            assert len(final["documents"]) == 1
            assert len(final["rows"]) == 1
            assert len(final["files"]) == 1
            row = final["rows"][0]
            assert row.index_status == "INDEXED"
            assert row.candidate_id == final["candidates"][0]
            assert row.candidate_document_id == final["documents"][0].id
            if outcome == "rollback":  # winner never became durable: loser ingested it
                assert (second_summary.new, second_summary.successful) == (1, 1)
                assert second_summary.unchanged == 0
            else:  # winner is durable truth even when its acknowledgement was lost
                assert (second_summary.new, second_summary.unchanged) == (0, 1)
                assert (second_summary.successful, second_summary.failed) == (0, 0)
                assert second_summary.discovered == 1 and second_summary.missing == 0
            if outcome == "commit":
                assert (first_summary.new, first_summary.successful) == (1, 1)
            assert seen == "blocked", f"second run was not serialized by the source ({seen})"
        finally:
            gate.release.set()
            await stop(winner, loser)


# --------------------------------------------------------------------------
# Scenario B: different source paths, identical bytes, same tenant
# --------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["commit", "rollback", "ambiguous"])
async def test_same_content_different_sources_dedup(env, factory, outcome):
    tenant, _, storage, base = env
    root_a = make_root(base, "src-a", {"alpha.pdf": X})
    root_b = make_root(base, "src-b", {"beta.pdf": X})
    gate = GateParser()
    async with factory() as first, factory() as second, factory() as observer:
        make_commit(first, outcome)
        first_pid, second_pid = await pid(first), await pid(second)
        winner = asyncio.create_task(commit_scan(first, storage, gate, tenant.id, root_a))
        loser = None
        try:
            await asyncio.wait_for(gate.first_entered.wait(), 15)
            loser = asyncio.create_task(commit_scan(second, storage, gate, tenant.id, root_b))
            seen = await contention(observer, loser, second_pid, first_pid, gate)
            gate.release.set()
            await finish(winner, outcome)
            second_summary = await asyncio.wait_for(loser, 15)

            final = await state(observer, storage, tenant.id)
            assert len(final["candidates"]) == 1, "concurrent identical content minted candidates"
            assert len(final["documents"]) == 1
            assert len(final["files"]) == 1
            assert (second_summary.new, second_summary.successful) == (1, 1)
            assert second_summary.failed == 0
            rows = final["rows"]
            assert {r.relative_path for r in rows} == (
                {"beta.pdf"} if outcome == "rollback" else {"alpha.pdf", "beta.pdf"}
            )
            assert {r.candidate_id for r in rows} == {final["candidates"][0]}
            assert {r.candidate_document_id for r in rows} == {final["documents"][0].id}
            assert seen == "blocked", f"identical content was not serialized ({seen})"
        finally:
            gate.release.set()
            await stop(winner, loser)


# --------------------------------------------------------------------------
# Scenario C: unrelated work is not globally serialized
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["other-tenant-same-bytes", "same-tenant-other-source"])
async def test_unrelated_work_proceeds_while_first_is_held(env, factory, db_session, kind):
    tenant, _, storage, base = env
    other_tenant = await create_tenant(db_session, name=f"T-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    root_a = make_root(base, "held", {"alpha.pdf": X})
    free_files = {"gamma.pdf": X} if kind.startswith("other") else {"gamma.docx": Y}
    root_b = make_root(base, "free", free_files)
    free_tenant = other_tenant.id if kind.startswith("other") else tenant.id
    gate = GateParser()
    async with factory() as first, factory() as second, factory() as observer:
        winner = asyncio.create_task(commit_scan(first, storage, gate, tenant.id, root_a))
        try:
            await asyncio.wait_for(gate.first_entered.wait(), 15)
            # The held run owns its authority right now; unrelated work must not wait on it.
            summary = await asyncio.wait_for(
                commit_scan(second, storage, LocalTextParser(), free_tenant, root_b), 15
            )
            assert (summary.new, summary.successful, summary.failed) == (1, 1, 0)
            gate.release.set()
            await asyncio.wait_for(winner, 15)
            first_state = await state(observer, storage, tenant.id)
            free_state = await state(observer, storage, free_tenant)
            assert len(originals(storage)) == 2
            if free_tenant == tenant.id:  # same tenant: both candidates, distinct content
                assert len(first_state["candidates"]) == 2 == len(first_state["files"])
            else:  # tenant-scoped dedup: identical bytes in another tenant stay separate
                assert len(first_state["candidates"]) == 1 == len(free_state["candidates"])
                assert set(first_state["candidates"]).isdisjoint(free_state["candidates"])
        finally:
            gate.release.set()
            await stop(winner)


# --------------------------------------------------------------------------
# Tenant suspension during contested persistence
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", ["same-source", "same-content"])
async def test_tenant_suspension_during_contested_persistence(env, factory, scenario):
    tenant, _, storage, base = env
    if scenario == "same-source":
        root_a = root_b = make_root(base, "one", {"cv.pdf": X})
    else:
        root_a = make_root(base, "a", {"alpha.pdf": X})
        root_b = make_root(base, "b", {"beta.pdf": X})
    gate = GateParser()
    async with factory() as first, factory() as second, factory() as admin, factory() as observer:
        first_pid, second_pid = await pid(first), await pid(second)
        winner = asyncio.create_task(commit_scan(first, storage, gate, tenant.id, root_a))
        loser = None
        try:
            await asyncio.wait_for(gate.first_entered.wait(), 15)
            loser = asyncio.create_task(commit_scan(second, storage, gate, tenant.id, root_b))
            seen = await contention(observer, loser, second_pid, first_pid, gate)
            await set_tenant_active(admin, tenant_id=tenant.id, is_active=False)
            await admin.commit()
            gate.release.set()
            with pytest.raises(TenantInactiveError):
                await asyncio.wait_for(winner, 15)
            with pytest.raises(TenantInactiveError):
                await asyncio.wait_for(loser, 15)
            final = await state(observer, storage, tenant.id)
            assert final["candidates"] == [] and final["documents"] == []
            assert final["rows"] == [] and final["files"] == []
            assert seen == "blocked"
        finally:
            gate.release.set()
            await stop(winner, loser)


# --------------------------------------------------------------------------
# Duplicate link vs candidate delete (S5/S6 candidate authority)
# --------------------------------------------------------------------------


async def test_duplicate_link_waits_for_candidate_delete_then_ingests_fresh(
    env, factory, monkeypatch
):
    tenant, key, storage, base = env
    root_a = make_root(base, "a", {"alpha.pdf": X})
    root_b = make_root(base, "b", {"beta.pdf": X})
    photos = LocalPhotoStorage(str(base / "storage"))
    async with factory() as seed:
        await commit_scan(seed, storage, LocalTextParser(), tenant.id, root_a)
        owner = await seed.scalar(select(Candidate.id).where(Candidate.tenant_id == tenant.id))
    staged, release = asyncio.Event(), asyncio.Event()
    real_delete = candidate_service.delete_candidate_row

    async def pause_after_staging(db, **kwargs):
        staged.set()
        await release.wait()
        return await real_delete(db, **kwargs)

    monkeypatch.setattr(candidate_service, "delete_candidate_row", pause_after_staging)

    async def delete(db):
        from meyar.services.tenant_authority import require_active_tenant

        await require_active_tenant(db, tenant.id, lock=True)
        return await candidate_service.delete_candidate_cascade(
            db, storage, tenant_id=tenant.id, candidate_id=owner,
            photo_storage=photos, actor_id=key.id,
        )

    async with factory() as deleter, factory() as scanner, factory() as observer:
        del_pid, scan_pid = await pid(deleter), await pid(scanner)
        deleting = asyncio.create_task(delete(deleter))
        scanning = None
        try:
            await asyncio.wait_for(staged.wait(), 15)
            scanning = asyncio.create_task(
                commit_scan(scanner, storage, LocalTextParser(), tenant.id, root_b)
            )
            seen = await contention(observer, scanning, scan_pid, del_pid)
            release.set()
            assert await asyncio.wait_for(deleting, 15) == 1
            summary = await asyncio.wait_for(scanning, 15)  # pre-fix: FK IntegrityError
            assert (summary.new, summary.successful, summary.failed) == (1, 1, 0)
            final = await state(observer, storage, tenant.id)
            assert len(final["candidates"]) == 1 and final["candidates"][0] != owner
            assert len(final["documents"]) == 1 and len(final["files"]) == 1
            beta = next(r for r in final["rows"] if r.relative_path == "beta.pdf")
            assert beta.candidate_id == final["candidates"][0]
            alpha = next(r for r in final["rows"] if r.relative_path == "alpha.pdf")
            assert alpha.candidate_id is None and alpha.candidate_document_id is None
            assert seen == "blocked"
        finally:
            release.set()
            await stop(deleting, scanning)


# --------------------------------------------------------------------------
# Bounded repetitions without gates: real scheduling, no deadlock / duplicates
# --------------------------------------------------------------------------


@pytest.mark.parametrize("repetition", range(4))
async def test_unsynchronized_runs_same_source_converge(env, factory, repetition):
    tenant, _, storage, base = env
    root = make_root(base, "shared", {"a.pdf": X, "b.docx": Y})
    async def one():
        async with factory() as db:
            return await commit_scan(db, storage, LocalTextParser(), tenant.id, root)

    summaries = await asyncio.wait_for(asyncio.gather(*[one() for _ in range(4)]), 60)
    async with factory() as observer:
        final = await state(observer, storage, tenant.id)
    assert len(final["candidates"]) == 2 and len(final["documents"]) == 2
    assert len(final["rows"]) == 2 and len(final["files"]) == 2
    assert sum(s.new for s in summaries) == 2  # exactly one run ingested each path
    assert sum(s.successful for s in summaries) == 2
    assert all(s.failed == 0 for s in summaries)


@pytest.mark.parametrize("repetition", range(4))
async def test_unsynchronized_runs_same_content_many_sources_converge(env, factory, repetition):
    tenant, _, storage, base = env
    roots = [make_root(base, f"s{i}", {f"p{i}.pdf": X}) for i in range(5)]

    async def one(root):
        async with factory() as db:
            return await commit_scan(db, storage, LocalTextParser(), tenant.id, root)

    summaries = await asyncio.wait_for(asyncio.gather(*[one(r) for r in roots]), 60)
    async with factory() as observer:
        final = await state(observer, storage, tenant.id)
    assert len(final["candidates"]) == 1, "identical content minted several candidates"
    assert len(final["documents"]) == 1 and len(final["files"]) == 1
    assert len(final["rows"]) == 5
    assert all(s.new == 1 and s.successful == 1 and s.failed == 0 for s in summaries)
    assert hashlib.sha256(X).hexdigest() == final["documents"][0].sha256_hash


# --------------------------------------------------------------------------
# Opposite-order contention: deadlock victim saved originals, then retries
# --------------------------------------------------------------------------


class RendezvousParser:
    """Both runs' first parse waits until BOTH hold their first content lock."""

    def __init__(self) -> None:
        self.inner = LocalTextParser()
        self.calls = 0
        self.both_in = asyncio.Event()

    async def parse(self, **kwargs):
        self.calls += 1
        if self.calls == 2:
            self.both_in.set()
        if self.calls <= 2:
            await asyncio.wait_for(self.both_in.wait(), 15)
        return await self.inner.parse(**kwargs)


@pytest.mark.parametrize("repetition", range(2))
async def test_opposite_content_order_deadlock_victim_compensates_and_retries(
    env, factory, monkeypatch, repetition
):
    from meyar.services import folder_indexer_service as svc

    tenant, _, storage, base = env
    root_a = make_root(base, "a", {"1.pdf": X, "2.docx": Y})  # X then Y
    root_b = make_root(base, "b", {"1.docx": Y, "2.pdf": X})  # Y then X
    attempts = []
    real_index = svc.index_folder

    async def counted(*args, **kwargs):
        attempts.append(kwargs["root_path"])
        return await real_index(*args, **kwargs)

    monkeypatch.setattr(svc, "index_folder", counted)
    parser = RendezvousParser()
    async with factory() as one, factory() as two, factory() as observer:
        first = asyncio.create_task(commit_scan(one, storage, parser, tenant.id, root_a))
        second = asyncio.create_task(commit_scan(two, storage, parser, tenant.id, root_b))
        try:
            summaries = await asyncio.wait_for(asyncio.gather(first, second), 60)
        finally:
            await stop(first, second)
        # Each run saved its own original for the first file before the cycle;
        # the aborted attempt was compensated, so only durable authority remains.
        assert len(attempts) == 3, f"expected exactly one deadlock retry, got {attempts}"
        final = await state(observer, storage, tenant.id)
        assert len(final["candidates"]) == 2 and len(final["documents"]) == 2
        assert len(final["rows"]) == 4 and len(final["files"]) == 2
        assert {s.new for s in summaries} == {2} and {s.successful for s in summaries} == {2}
        assert all(s.failed == 0 for s in summaries)

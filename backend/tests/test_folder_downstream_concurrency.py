"""Issue #46 S9: downstream folder reconciliation concurrency authority.

Real PostgreSQL (READ COMMITTED) sessions, real synthetic local storage and
deterministic gated local providers. Asyncio Events and ``pg_stat_activity`` /
``pg_blocking_pids`` are the synchronization and observation authority; no
elapsed sleep is used as proof of correctness.

The provider gates hold a run *inside local inference* so the test can start a
second run, delete the candidate, change the current document, or suspend the
tenant at exactly the point where the production code has computed a model
result but not yet persisted it.
"""

import asyncio
import base64
import hashlib
import uuid
from pathlib import Path

import pytest
from conftest import TEST_DATABASE_URL
from fakes import FakeEmbeddingProvider
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from meyar.embedding.provider import EmbeddingProviderError
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.llm.provider import ModelUnavailableError
from meyar.models.audit_event import AuditEvent
from meyar.models.candidate import Candidate
from meyar.models.candidate_embedding_version import CandidateEmbeddingVersion
from meyar.models.candidate_identity_version import CandidateIdentityVersion
from meyar.models.candidate_photo_version import CandidatePhotoVersion
from meyar.models.candidate_profile_version import CandidateProfileVersion
from meyar.schemas.candidate_identity import CandidateIdentityExtraction
from meyar.schemas.candidate_profile import CandidateProfileExtraction, EvidenceRef, SkillItem
from meyar.search.schemas import CandidateSearchRequest, RequiredFilters, SearchMode
from meyar.search.service import search_candidates
from meyar.services import (
    candidate_photo_service,
    candidate_service,
)
from meyar.services import folder_reconciliation_service as reconciliation
from meyar.services.candidate_photo_service import PLACEHOLDER_JPEG, process_photo_for_document
from meyar.services.candidate_profile_repo import get_effective_profile_version
from meyar.services.folder_indexed_file_repo import list_folder_indexed_files
from meyar.services.folder_indexer_service import index_folder_and_commit
from meyar.services.folder_reconciliation_service import process_pending_candidates
from meyar.services.tenant_authority import (
    TenantInactiveError,
    require_active_tenant,
    set_tenant_active,
)
from meyar.services.tenant_repo import create_tenant
from meyar.storage.local import LocalFilesystemStorage
from meyar.storage.photo import LocalPhotoStorage

MAX_BYTES = 10 * 1024 * 1024
CHARS = 20000


def write_docx(dest: Path, text_value: str) -> None:
    from docx import Document

    dest.parent.mkdir(parents=True, exist_ok=True)
    document = Document()
    document.add_paragraph(text_value)
    document.save(str(dest))


class GateLLM:
    """Deterministic content-driven local model with optional stage gates.

    The profile skill is read from the real parsed block ("Skills: <name>"),
    so the persisted profile identifies the exact document it was computed
    from. ``first`` is set when a gated stage is entered for the first time;
    ``second`` when a second concurrent caller reached inference too (nothing
    stopped it). The first caller then waits for ``release``.
    """

    provider_name = "fake"
    model_name = "fake-model-v1"
    model_revision = ""

    def __init__(self, *, gate: str | None = None, rendezvous: "Rendezvous | None" = None,
                 identity_error: bool = False) -> None:
        self.gate = gate
        self.rendezvous = rendezvous
        self.identity_error = identity_error
        self.calls = {"profile": 0, "identity": 0}
        self.first = asyncio.Event()
        self.second = asyncio.Event()
        self.release = asyncio.Event()

    async def _stage(self, name: str) -> None:
        self.calls[name] += 1
        if self.rendezvous is not None and self.calls[name] == 1 and name == "profile":
            await self.rendezvous.arrive()
        if self.gate != name:
            return
        if self.calls[name] == 1:
            self.first.set()
            await self.release.wait()
        elif self.calls[name] == 2:
            self.second.set()

    async def extract_candidate_profile(self, view):
        await self._stage("profile")
        block = view.blocks[0]
        skill = block.text.split(":", 1)[1].strip()
        evidence = EvidenceRef(page=block.page, block_index=block.block_index, quote=block.text)
        return CandidateProfileExtraction(skills=[SkillItem(name=skill, evidence=[evidence])]), (
            self.model_name
        )

    async def extract_candidate_identity(self, view):
        await self._stage("identity")
        if self.identity_error:
            raise ModelUnavailableError("simulated identity outage")
        return CandidateIdentityExtraction(full_name=None, email=None, phone=None), self.model_name


class GateEmbedding(FakeEmbeddingProvider):
    def __init__(self, *, gate: bool = False, fail: bool = False) -> None:
        super().__init__(vector=[1.0, 0.0])
        self.gate = gate
        self.fail = fail
        self.first = asyncio.Event()
        self.second = asyncio.Event()
        self.release = asyncio.Event()

    async def embed(self, text_value: str):
        self.call_count += 1
        if self.fail:
            raise EmbeddingProviderError("EMBEDDING_UNAVAILABLE", "simulated outage")
        if self.gate and self.call_count == 1:
            self.first.set()
            await self.release.wait()
        elif self.gate and self.call_count == 2:
            self.second.set()
        return await super().embed(text_value)


class Rendezvous:
    """Every participant must be inside inference at once, or the test times out."""

    def __init__(self, parties: int) -> None:
        self.parties = parties
        self.count = 0
        self.all_in = asyncio.Event()

    async def arrive(self) -> None:
        self.count += 1
        if self.count >= self.parties:
            self.all_in.set()
        await asyncio.wait_for(self.all_in.wait(), 15)


@pytest.fixture
async def factory():
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=12, max_overflow=12)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
async def env(db_session, tenant_and_key, tmp_path):
    tenant, key, _ = tenant_and_key
    return tenant, key, LocalFilesystemStorage(str(tmp_path / "storage")), tmp_path


async def seed(factory, storage, tenant_id, root: Path, filename: str, skill: str):
    """Durable ingestion only (no downstream): one Candidate + one current document."""
    write_docx(root / filename, f"Skills: {skill}")
    async with factory() as db:
        scan = await index_folder_and_commit(
            db, storage, LocalTextParser(), tenant_id=tenant_id, root_path=str(root),
            max_bytes=MAX_BYTES,
        )
        rows = await list_folder_indexed_files(
            db, tenant_id=tenant_id, folder_source_id=scan.folder_source_id
        )
    row = next(r for r in rows if r.relative_path == filename)
    return scan.folder_source_id, row.candidate_id, row.candidate_document_id


async def rescan(factory, storage, tenant_id, root: Path):
    async with factory() as db:
        return await index_folder_and_commit(
            db, storage, LocalTextParser(), tenant_id=tenant_id, root_path=str(root),
            max_bytes=MAX_BYTES,
        )


async def run_pending(db, llm, embedder, tenant_id, source_id):
    return await process_pending_candidates(
        db, llm, embedder, tenant_id=tenant_id, folder_source_id=source_id,
        model_provider_name="fake", max_profile_input_chars=CHARS,
        max_identity_input_chars=CHARS, max_embedding_input_chars=CHARS, limit=None,
    )


async def stop(*tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
    await asyncio.gather(*[t for t in tasks if t is not None], return_exceptions=True)


async def truth(observer, tenant_id):
    """Durable downstream truth from an independent connection."""
    await observer.rollback()  # fresh snapshot of committed state

    async def count(model):
        return await observer.scalar(
            select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
        )

    failed_events = await observer.scalar(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.tenant_id == tenant_id,
            AuditEvent.event_type == "FOLDER_RECONCILE_CANDIDATE_FAILED",
        )
    )
    return {
        "candidates": await count(Candidate),
        "profiles": await count(CandidateProfileVersion),
        "identities": await count(CandidateIdentityVersion),
        "embeddings": await count(CandidateEmbeddingVersion),
        "failed_events": failed_events,
    }


async def completed(observer, model, tenant_id):
    await observer.rollback()
    return await observer.scalar(
        select(func.count()).select_from(model).where(
            model.tenant_id == tenant_id, model.status == "COMPLETED"
        )
    )


async def settled(observer, task, overlapped: asyncio.Event | None = None) -> str:
    """Wait (event/wait-graph driven) until `task` finished, reached the same stage as
    the holder (`overlapped`), or some backend is blocked by another backend."""
    async with asyncio.timeout(20):
        while True:
            if task.done():
                return "finished"
            if overlapped is not None and overlapped.is_set():
                return "overlapped"
            await observer.rollback()
            blocked = await observer.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND cardinality(pg_blocking_pids(pid)) > 0"
                )
            )
            if blocked:
                return "blocked"
            await asyncio.sleep(0.005)  # poll cadence only


async def idle_in_transaction(observer) -> int:
    await observer.rollback()
    return await observer.scalar(
        text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND pid <> pg_backend_pid() AND state = 'idle in transaction'"
        )
    )


def whole_tree(storage):
    return sorted(
        str(p.relative_to(storage._root)) for p in storage._root.rglob("*") if p.is_file()
    )


def assert_clean_storage(storage, documents_expected: int) -> None:
    tree = whole_tree(storage)
    assert [f for f in tree if f.startswith(".trash/")] == [], "staged .trash leftovers"
    assert [f for f in tree if f.endswith(".tmp")] == [], "partial temp files"
    assert len([f for f in tree if not f.startswith(".") and not f.startswith("photo/")]) == (
        documents_expected
    )


async def assert_embedding_provenance(observer, tenant_id):
    """Every embedding points at a profile of the same tenant AND candidate."""
    await observer.rollback()
    rows = (
        await observer.execute(
            select(CandidateEmbeddingVersion, CandidateProfileVersion).join(
                CandidateProfileVersion,
                CandidateProfileVersion.id
                == CandidateEmbeddingVersion.candidate_profile_version_id,
            ).where(CandidateEmbeddingVersion.tenant_id == tenant_id)
        )
    ).all()
    for embedding, profile in rows:
        assert profile.tenant_id == tenant_id and profile.candidate_id == embedding.candidate_id
        assert profile.status == "COMPLETED"


# ---------------------------------------------------------------------------
# Same Candidate + same current document, concurrent runs, every stage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["profile", "identity", "embedding"])
async def test_same_document_concurrent_runs_converge(env, factory, stage):
    tenant, _, storage, base = env
    source_id, candidate_id, _ = await seed(
        factory, storage, tenant.id, base / "cvs", "a.docx", "Java"
    )

    # Build the partial state that leaves exactly `stage` missing.
    pre_llm = GateLLM(identity_error=(stage == "identity"))
    pre_emb = GateEmbedding(fail=(stage == "embedding"))
    if stage != "profile":
        async with factory() as db:
            await run_pending(db, pre_llm, pre_emb, tenant.id, source_id)

    llm_a = GateLLM(gate=stage if stage != "embedding" else None)
    llm_b = GateLLM()
    emb_a = GateEmbedding(gate=(stage == "embedding"))
    emb_b = GateEmbedding()
    async with factory() as first, factory() as second, factory() as observer:
        winner = asyncio.create_task(run_pending(first, llm_a, emb_a, tenant.id, source_id))
        loser = None
        gate_first = emb_a.first if stage == "embedding" else llm_a.first
        gate_release = emb_a.release if stage == "embedding" else llm_a.release
        try:
            await asyncio.wait_for(gate_first.wait(), 15)
            loser = asyncio.create_task(run_pending(second, llm_b, emb_b, tenant.id, source_id))
            gate_second = emb_a.second if stage == "embedding" else llm_a.second
            await settled(observer, loser, gate_second)
            gate_release.set()
            results = await asyncio.wait_for(asyncio.gather(winner, loser), 30)
            final = await truth(observer, tenant.id)
            assert final["failed_events"] == 0, f"a lost race was reported as failed: {final}"
            assert all(r.failed == 0 for r in results), results
            assert all(r.ready_after == 1 for r in results), results
            assert final["candidates"] == 1
            assert await completed(observer, CandidateProfileVersion, tenant.id) == 1
            assert await completed(observer, CandidateIdentityVersion, tenant.id) == 1
            assert final["embeddings"] == 1
            await assert_embedding_provenance(observer, tenant.id)
            effective = await get_effective_profile_version(
                observer, tenant_id=tenant.id, candidate_id=candidate_id
            )
            assert effective is not None and effective.status == "COMPLETED"
        finally:
            gate_release.set()
            await stop(winner, loser)


# ---------------------------------------------------------------------------
# Candidate delete racing downstream persistence (both directions)
# ---------------------------------------------------------------------------


async def delete_candidate(db, tenant_id, candidate_id, storage, photos, actor_id):
    await require_active_tenant(db, tenant_id, lock=True)
    return await candidate_service.delete_candidate_cascade(
        db, storage, tenant_id=tenant_id, candidate_id=candidate_id,
        photo_storage=photos, actor_id=actor_id,
    )


async def test_delete_wins_before_downstream_persistence(env, factory):
    tenant, key, storage, base = env
    photos = LocalPhotoStorage(str(base / "storage"))
    source_id, candidate_id, _ = await seed(
        factory, storage, tenant.id, base / "cvs", "a.docx", "Java"
    )
    llm = GateLLM(gate="profile")
    async with factory() as run_db, factory() as deleter, factory() as observer:
        running = asyncio.create_task(
            run_pending(run_db, llm, GateEmbedding(), tenant.id, source_id)
        )
        try:
            await asyncio.wait_for(llm.first.wait(), 15)
            deleted = await delete_candidate(
                deleter, tenant.id, candidate_id, storage, photos, key.id
            )
            assert deleted == 1
            llm.release.set()
            summary = await asyncio.wait_for(running, 30)
            final = await truth(observer, tenant.id)
            assert final == {**final, "candidates": 0, "profiles": 0, "identities": 0,
                             "embeddings": 0}
            assert summary.failed == 0, "a deleted candidate is superseded, not a failure"
            assert summary.superseded == 1
            assert final["failed_events"] == 0
            assert_clean_storage(storage, 0)
        finally:
            llm.release.set()
            await stop(running)


async def test_downstream_persistence_wins_before_delete(env, factory, monkeypatch):
    tenant, key, storage, base = env
    photos = LocalPhotoStorage(str(base / "storage"))
    source_id, candidate_id, _ = await seed(
        factory, storage, tenant.id, base / "cvs", "a.docx", "Java"
    )
    persisted, release = asyncio.Event(), asyncio.Event()
    real_persist = reconciliation.persist_profile_outcome

    async def held_persist(*args, **kwargs):
        # Phase B owns the Candidate row and has flushed the version; commit is held.
        version = await real_persist(*args, **kwargs)
        persisted.set()
        await release.wait()
        return version

    monkeypatch.setattr(reconciliation, "persist_profile_outcome", held_persist)
    async with factory() as run_db, factory() as deleter, factory() as observer:
        running = asyncio.create_task(
            run_pending(run_db, GateLLM(), GateEmbedding(), tenant.id, source_id)
        )
        deleting = None
        try:
            await asyncio.wait_for(persisted.wait(), 15)
            deleting = asyncio.create_task(
                delete_candidate(deleter, tenant.id, candidate_id, storage, photos, key.id)
            )
            assert await settled(observer, deleting) == "blocked", (
                "delete overtook uncommitted downstream authority"
            )
            release.set()
            summary = await asyncio.wait_for(running, 30)
            assert await asyncio.wait_for(deleting, 30) == 1
            final = await truth(observer, tenant.id)
            assert (final["candidates"], final["profiles"], final["identities"],
                    final["embeddings"]) == (0, 0, 0, 0)
            # Delete may land before or after the run's next stage; never a failure.
            assert summary.failed == 0 and summary.ready_after + summary.superseded == 1
            assert final["failed_events"] == 0
            assert_clean_storage(storage, 0)
        finally:
            release.set()
            await stop(running, deleting)


# ---------------------------------------------------------------------------
# Changed / current document: old-document work must not become authority
# ---------------------------------------------------------------------------


async def skills_found(observer, tenant_id, skill: str) -> set[uuid.UUID]:
    await observer.rollback()
    found = await search_candidates(
        observer, tenant_id=tenant_id,
        request=CandidateSearchRequest(
            mode=SearchMode.STRUCTURED_ONLY, required_filters=RequiredFilters(skills=[skill])
        ),
    )
    await observer.rollback()
    return {r.candidate_id for r in found.results}


async def test_old_document_work_never_becomes_current_authority(env, factory):
    tenant, _, storage, base = env
    root = base / "cvs"
    source_id, candidate_id, doc_a = await seed(factory, storage, tenant.id, root, "c.docx", "Java")
    stale_llm = GateLLM(gate="profile")
    async with factory() as stale_db, factory() as fresh_db, factory() as observer:
        stale = asyncio.create_task(
            run_pending(stale_db, stale_llm, GateEmbedding(), tenant.id, source_id)
        )
        try:
            await asyncio.wait_for(stale_llm.first.wait(), 15)  # inference on document A
            write_docx(root / "c.docx", "Skills: Python")  # source advances to document B
            scan = await rescan(factory, storage, tenant.id, root)
            assert scan.changed == 1 and scan.successful == 1
            fresh = await run_pending(fresh_db, GateLLM(), GateEmbedding(), tenant.id, source_id)
            assert fresh.ready_after == 1
            stale_llm.release.set()
            stale_summary = await asyncio.wait_for(stale, 30)

            await observer.rollback()
            effective = await get_effective_profile_version(
                observer, tenant_id=tenant.id, candidate_id=candidate_id
            )
            rows = await list_folder_indexed_files(
                observer, tenant_id=tenant.id, folder_source_id=source_id
            )
            current_document = rows[0].candidate_document_id
            assert current_document != doc_a
            assert effective is not None and effective.candidate_document_id == current_document
            assert candidate_id not in await skills_found(observer, tenant.id, "Java")
            assert candidate_id in await skills_found(observer, tenant.id, "Python")
            await assert_embedding_provenance(observer, tenant.id)
            old = await observer.scalar(
                select(func.count()).select_from(CandidateProfileVersion).where(
                    CandidateProfileVersion.candidate_document_id == doc_a
                )
            )
            assert old == 0, "old-document result was persisted as downstream authority"
            assert stale_summary.failed == 0
            assert stale_summary.superseded == 1, stale_summary
        finally:
            stale_llm.release.set()
            await stop(stale)


# ---------------------------------------------------------------------------
# Tenant suspension while inference is in flight
# ---------------------------------------------------------------------------


async def test_tenant_suspension_during_inference(env, factory):
    tenant, _, storage, base = env
    source_id, _, _ = await seed(factory, storage, tenant.id, base / "cvs", "a.docx", "Java")
    llm = GateLLM(gate="profile")
    async with factory() as run_db, factory() as admin, factory() as observer:
        running = asyncio.create_task(
            run_pending(run_db, llm, GateEmbedding(), tenant.id, source_id)
        )
        try:
            await asyncio.wait_for(llm.first.wait(), 15)
            await set_tenant_active(admin, tenant_id=tenant.id, is_active=False)
            await admin.commit()
            llm.release.set()
            with pytest.raises(TenantInactiveError):
                await asyncio.wait_for(running, 30)
            final = await truth(observer, tenant.id)
            assert (final["profiles"], final["identities"], final["embeddings"]) == (0, 0, 0)
            assert final["failed_events"] == 0
            await run_db.rollback()  # the session stays reusable
            assert await run_db.scalar(text("SELECT 1")) == 1
        finally:
            llm.release.set()
            await stop(running)


# ---------------------------------------------------------------------------
# Unrelated candidates / sources / tenants are not globally serialized
# ---------------------------------------------------------------------------


async def test_unrelated_work_is_not_serialized(env, factory, db_session):
    tenant, _, storage, base = env
    other = await create_tenant(db_session, name=f"T-{uuid.uuid4().hex[:8]}")
    await db_session.commit()
    s1, c1, _ = await seed(factory, storage, tenant.id, base / "one", "a.docx", "Java")
    s2, c2, _ = await seed(factory, storage, tenant.id, base / "two", "b.docx", "Go")
    s3, c3, _ = await seed(factory, storage, other.id, base / "three", "c.docx", "Java")
    gate = Rendezvous(3)  # all three must be inside local inference at the same time
    async with factory() as a, factory() as b, factory() as c, factory() as observer:
        results = await asyncio.wait_for(
            asyncio.gather(
                run_pending(a, GateLLM(rendezvous=gate), GateEmbedding(), tenant.id, s1),
                run_pending(b, GateLLM(rendezvous=gate), GateEmbedding(), tenant.id, s2),
                run_pending(c, GateLLM(rendezvous=gate), GateEmbedding(), other.id, s3),
            ),
            40,
        )
        assert all(r.ready_after == 1 and r.failed == 0 for r in results)
        assert (await truth(observer, tenant.id))["profiles"] == 2
        assert (await truth(observer, other.id))["profiles"] == 1


# ---------------------------------------------------------------------------
# Observation: is a connection/transaction held across local inference?
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["profile", "identity", "embedding"])
async def test_no_connection_or_transaction_is_held_during_local_inference(env, factory, stage):
    tenant, _, storage, base = env
    source_id, _, _ = await seed(factory, storage, tenant.id, base / "cvs", "a.docx", "Java")
    if stage != "profile":  # leave exactly the gated stage missing
        async with factory() as db:
            await run_pending(
                db, GateLLM(identity_error=(stage == "identity")),
                GateEmbedding(fail=(stage == "embedding")), tenant.id, source_id,
            )
    llm = GateLLM(gate=stage if stage != "embedding" else None)
    embedder = GateEmbedding(gate=(stage == "embedding"))
    gate = embedder if stage == "embedding" else llm
    async with factory() as run_db, factory() as observer:
        running = asyncio.create_task(run_pending(run_db, llm, embedder, tenant.id, source_id))
        try:
            await asyncio.wait_for(gate.first.wait(), 15)
            held = await idle_in_transaction(observer)
            blocking = await observer.scalar(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'relation' AND granted "
                    "AND pid <> pg_backend_pid() AND database = "
                    "(SELECT oid FROM pg_database WHERE datname = current_database())"
                )
            )
            gate.release.set()
            await asyncio.wait_for(running, 30)
            assert held == 0, f"{held} backend(s) idle in transaction during {stage} inference"
            assert blocking == 0, f"{blocking} relation lock(s) held during {stage} inference"
        finally:
            gate.release.set()
            await stop(running)


# ---------------------------------------------------------------------------
# Photo audit (separate path): concurrent photo passes for one document
# ---------------------------------------------------------------------------


async def test_photo_concurrent_passes_converge(env, factory, monkeypatch):
    tenant, _, storage, base = env
    photos = LocalPhotoStorage(str(base / "storage"))
    _, candidate_id, document_id = await seed(
        factory, storage, tenant.id, base / "cvs", "a.docx", "Java"
    )
    entered = []
    both = asyncio.Event()

    async def available(data: bytes, kind: str) -> dict:
        entered.append(1)
        if len(entered) == 2:
            both.set()
        if len(entered) == 1:
            await asyncio.wait_for(both.wait(), 15)
        return {
            "status": "AVAILABLE", "jpeg_base64": base64.b64encode(PLACEHOLDER_JPEG).decode(),
            "derived_sha256": hashlib.sha256(PLACEHOLDER_JPEG).hexdigest(),
            "width": 256, "height": 256, "source_kind": "PDF_IMAGE",
        }

    monkeypatch.setattr(candidate_photo_service, "_extract_isolated", available)
    async with factory() as one, factory() as two, factory() as observer:
        results = await asyncio.wait_for(
            asyncio.gather(
                process_photo_for_document(
                    one, storage, photos, tenant_id=tenant.id, candidate_id=candidate_id,
                    document_id=document_id,
                ),
                process_photo_for_document(
                    two, storage, photos, tenant_id=tenant.id, candidate_id=candidate_id,
                    document_id=document_id,
                ),
            ),
            30,
        )
        await observer.rollback()
        rows = (
            await observer.scalars(
                select(CandidatePhotoVersion).where(CandidatePhotoVersion.tenant_id == tenant.id)
            )
        ).all()
        assert len(rows) == 1 and rows[0].status == "AVAILABLE"
        assert any(r is not None for r in results)
        tree = whole_tree(storage)
        derived = [f for f in tree if f.startswith("photo/")]
        assert len(derived) == 1
        assert [f for f in tree if f.startswith(".trash/") or f.endswith(".tmp")] == []


# ---------------------------------------------------------------------------
# Persistence-phase commit failure: rollback / ambiguous, then a clean re-run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", ["rollback", "ambiguous"])
async def test_persistence_commit_failure_then_rerun_converges(env, factory, monkeypatch, outcome):
    tenant, _, storage, base = env
    source_id, candidate_id, _ = await seed(
        factory, storage, tenant.id, base / "cvs", "a.docx", "Java"
    )
    real_persist = reconciliation.persist_profile_outcome
    armed = []

    async def arming_persist(*args, **kwargs):
        version = await real_persist(*args, **kwargs)
        armed.append(1)
        return version

    monkeypatch.setattr(reconciliation, "persist_profile_outcome", arming_persist)
    async with factory() as run_db, factory() as observer:
        real_commit = run_db.commit

        async def failing_commit():
            if armed:
                armed.clear()
                if outcome == "rollback":
                    raise RuntimeError("Synthetic commit refused")
                await real_commit()
                raise RuntimeError("Synthetic commit acknowledgement lost")
            await real_commit()

        monkeypatch.setattr(run_db, "commit", failing_commit)
        first = await run_pending(run_db, GateLLM(), GateEmbedding(), tenant.id, source_id)
        # A handled outcome, truthfully a failure, never a crash; the session is reusable.
        assert (first.failed, first.ready_after) == (1, 0)
        assert (await truth(observer, tenant.id))["failed_events"] == 1
        monkeypatch.setattr(run_db, "commit", real_commit)
        again = await run_pending(run_db, GateLLM(), GateEmbedding(), tenant.id, source_id)
        assert (again.failed, again.ready_after) == (0, 1)
        final = await truth(observer, tenant.id)
        assert await completed(observer, CandidateProfileVersion, tenant.id) == 1
        assert await completed(observer, CandidateIdentityVersion, tenant.id) == 1
        assert final["embeddings"] == 1 and final["candidates"] == 1
        await assert_embedding_provenance(observer, tenant.id)
        effective = await get_effective_profile_version(
            observer, tenant_id=tenant.id, candidate_id=candidate_id
        )
        assert effective is not None and effective.status == "COMPLETED"


# ---------------------------------------------------------------------------
# Changed-document ingestion waits for an in-flight persistence phase (S8 + S9)
# ---------------------------------------------------------------------------


async def test_changed_document_scan_waits_for_persistence_then_new_document_wins(
    env, factory, monkeypatch
):
    tenant, _, storage, base = env
    root = base / "cvs"
    source_id, candidate_id, doc_a = await seed(factory, storage, tenant.id, root, "c.docx", "Java")
    persisted, release = asyncio.Event(), asyncio.Event()
    real_persist = reconciliation.persist_profile_outcome

    async def held_persist(*args, **kwargs):
        version = await real_persist(*args, **kwargs)
        persisted.set()
        await release.wait()
        return version

    monkeypatch.setattr(reconciliation, "persist_profile_outcome", held_persist)
    async with factory() as run_db, factory() as scan_db, factory() as observer:
        running = asyncio.create_task(
            run_pending(run_db, GateLLM(), GateEmbedding(), tenant.id, source_id)
        )
        scanning = None
        try:
            await asyncio.wait_for(persisted.wait(), 15)
            write_docx(root / "c.docx", "Skills: Python")
            scanning = asyncio.create_task(
                index_folder_and_commit(
                    scan_db, storage, LocalTextParser(), tenant_id=tenant.id,
                    root_path=str(root), max_bytes=MAX_BYTES,
                )
            )
            assert await settled(observer, scanning) == "blocked"
            release.set()
            await asyncio.wait_for(running, 30)
            scan = await asyncio.wait_for(scanning, 30)
            assert scan.changed == 1 and scan.successful == 1
            async with factory() as db:
                fresh = await run_pending(db, GateLLM(), GateEmbedding(), tenant.id, source_id)
            assert fresh.ready_after == 1 and fresh.failed == 0
            await observer.rollback()
            effective = await get_effective_profile_version(
                observer, tenant_id=tenant.id, candidate_id=candidate_id
            )
            rows = await list_folder_indexed_files(
                observer, tenant_id=tenant.id, folder_source_id=source_id
            )
            assert rows[0].candidate_document_id != doc_a
            assert effective is not None
            assert effective.candidate_document_id == rows[0].candidate_document_id
            assert candidate_id not in await skills_found(observer, tenant.id, "Java")
            await assert_embedding_provenance(observer, tenant.id)
        finally:
            release.set()
            await stop(running, scanning)


# ---------------------------------------------------------------------------
# Bounded unsynchronized repetitions (real scheduling; no deadlock, no duplicates)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("repetition", range(3))
async def test_unsynchronized_runs_same_candidate_converge(env, factory, repetition):
    tenant, _, storage, base = env
    source_id, candidate_id, _ = await seed(
        factory, storage, tenant.id, base / "cvs", "a.docx", "Java"
    )

    async def one():
        async with factory() as db:
            return await run_pending(db, GateLLM(), GateEmbedding(), tenant.id, source_id)

    results = await asyncio.wait_for(asyncio.gather(*[one() for _ in range(4)]), 60)
    async with factory() as observer:
        final = await truth(observer, tenant.id)
        assert final["failed_events"] == 0 and all(r.failed == 0 for r in results)
        assert all(r.ready_after == 1 for r in results)
        assert await completed(observer, CandidateProfileVersion, tenant.id) == 1
        assert await completed(observer, CandidateIdentityVersion, tenant.id) == 1
        assert final["embeddings"] == 1 and final["candidates"] == 1
        await assert_embedding_provenance(observer, tenant.id)


# ---------------------------------------------------------------------------
# S9 corrective: FOLDER document authority is per FolderIndexedFile, never
# "newest CandidateDocument of the Candidate" (D-013 / D-021)
# ---------------------------------------------------------------------------


async def folder_rows(observer, tenant_id, source_id):
    await observer.rollback()
    return await list_folder_indexed_files(
        observer, tenant_id=tenant_id, folder_source_id=source_id
    )


@pytest.mark.parametrize("scans", ["same-scan", "separate-scans"])
async def test_dedup_linked_paths_that_diverge_are_both_authoritative(env, factory, scans):
    tenant, _, storage, base = env
    root = base / "cvs"
    source_id, candidate_id, shared_doc = await seed(
        factory, storage, tenant.id, root, "a.docx", "Java"
    )
    (root / "b.docx").write_bytes((root / "a.docx").read_bytes())  # identical bytes: dedup link
    await rescan(factory, storage, tenant.id, root)
    async with factory() as observer:
        rows = await folder_rows(observer, tenant.id, source_id)
        assert {r.relative_path for r in rows} == {"a.docx", "b.docx"}
        assert {r.candidate_id for r in rows} == {candidate_id}
        assert {r.candidate_document_id for r in rows} == {shared_doc}

    write_docx(root / "a.docx", "Skills: Python")
    if scans == "separate-scans":
        await rescan(factory, storage, tenant.id, root)
    write_docx(root / "b.docx", "Skills: Go")
    scan = await rescan(factory, storage, tenant.id, root)
    assert scan.successful >= 1
    async with factory() as observer, factory() as db:
        rows = await folder_rows(observer, tenant.id, source_id)
        docs = {r.relative_path: r.candidate_document_id for r in rows}
        assert len(set(docs.values())) == 2 and shared_doc not in docs.values()
        assert {r.candidate_id for r in rows} == {candidate_id}  # retained identity

        summary = await run_pending(db, GateLLM(), GateEmbedding(), tenant.id, source_id)
        # Both tracked documents are authoritative for their own path: neither is
        # superseded merely because the Candidate has another, newer CandidateDocument.
        assert summary.superseded == 0, summary
        assert summary.candidates_considered == 2
        final = await truth(observer, tenant.id)
        assert final["failed_events"] == 0
        for document_id in docs.values():
            for model in (CandidateProfileVersion, CandidateIdentityVersion):
                done = await observer.scalar(
                    select(func.count()).select_from(model).where(
                        model.tenant_id == tenant.id,
                        model.candidate_document_id == document_id,
                        model.status == "COMPLETED",
                    )
                )
                assert done == 1, (model.__name__, document_id)
        await assert_embedding_provenance(observer, tenant.id)
        effective = await get_effective_profile_version(
            observer, tenant_id=tenant.id, candidate_id=candidate_id
        )
        assert effective is not None and effective.candidate_document_id in docs.values()


async def test_direct_upload_document_does_not_supersede_folder_document(env, factory):
    from meyar.services.candidate_document_service import (
        persist_candidate_document,
        prepare_candidate_document,
    )

    tenant, _, storage, base = env
    source_id, candidate_id, folder_doc = await seed(
        factory, storage, tenant.id, base / "cvs", "a.docx", "Java"
    )
    upload = base / "upload.docx"
    write_docx(upload, "Skills: Rust")
    prepared = await prepare_candidate_document(
        LocalTextParser(), filename="upload.docx", content_type="", data=upload.read_bytes(),
        max_bytes=MAX_BYTES,
    )
    async with factory() as db:  # a separate direct-upload document, newer by created_at
        await persist_candidate_document(
            db, storage, prepared, tenant_id=tenant.id, candidate_id=candidate_id,
            filename="upload.docx",
        )
        await db.commit()
    async with factory() as observer, factory() as db:
        rows = await folder_rows(observer, tenant.id, source_id)
        assert [r.candidate_document_id for r in rows] == [folder_doc]  # path authority intact
        summary = await run_pending(db, GateLLM(), GateEmbedding(), tenant.id, source_id)
        assert summary.superseded == 0 and summary.failed == 0 and summary.ready_after == 1
        effective = await get_effective_profile_version(
            observer, tenant_id=tenant.id, candidate_id=candidate_id
        )
        assert effective is not None and effective.candidate_document_id == folder_doc
        await assert_embedding_provenance(observer, tenant.id)


async def test_shared_document_stays_authoritative_while_another_path_advances(env, factory):
    tenant, _, storage, base = env
    root = base / "cvs"
    source_id, candidate_id, shared_doc = await seed(
        factory, storage, tenant.id, root, "a.docx", "Java"
    )
    (root / "b.docx").write_bytes((root / "a.docx").read_bytes())  # byte-identical
    await rescan(factory, storage, tenant.id, root)
    llm = GateLLM(gate="profile")
    async with factory() as run_db, factory() as observer:
        running = asyncio.create_task(
            run_pending(run_db, llm, GateEmbedding(), tenant.id, source_id)
        )
        try:
            await asyncio.wait_for(llm.first.wait(), 15)  # inference for the shared document
            write_docx(root / "a.docx", "Skills: Python")  # path a advances; path b keeps it
            await rescan(factory, storage, tenant.id, root)
            llm.release.set()
            summary = await asyncio.wait_for(running, 30)
            rows = {r.relative_path: r for r in await folder_rows(observer, tenant.id, source_id)}
            assert rows["b.docx"].candidate_document_id == shared_doc
            assert rows["a.docx"].candidate_document_id != shared_doc
            assert summary.superseded == 0, summary
            done = await observer.scalar(
                select(func.count()).select_from(CandidateProfileVersion).where(
                    CandidateProfileVersion.candidate_document_id == shared_doc,
                    CandidateProfileVersion.status == "COMPLETED",
                )
            )
            assert done == 1
        finally:
            llm.release.set()
            await stop(running)


async def test_documents_created_in_one_transaction_share_created_at(env, factory):
    """Characterization: PostgreSQL now() is the transaction start time, so created_at
    ties for documents created together and the random UUID id has no path meaning."""
    from meyar.services.candidate_document_service import (
        persist_candidate_document,
        prepare_candidate_document,
    )
    from meyar.services.candidate_repo import create_candidate

    tenant, _, storage, base = env
    async with factory() as db:
        candidate = await create_candidate(db, tenant_id=tenant.id)
        ids = []
        for name in ("one", "two"):
            path = base / f"{name}.docx"
            write_docx(path, f"Skills: {name}")
            prepared = await prepare_candidate_document(
                LocalTextParser(), filename=path.name, content_type="", data=path.read_bytes(),
                max_bytes=MAX_BYTES,
            )
            document = await persist_candidate_document(
                db, storage, prepared, tenant_id=tenant.id, candidate_id=candidate.id,
                filename=path.name,
            )
            ids.append(document.id)
        await db.commit()
    async with factory() as observer:
        from meyar.models.candidate_document import CandidateDocument

        stamps = (
            await observer.scalars(
                select(CandidateDocument.created_at).where(CandidateDocument.id.in_(ids))
            )
        ).all()
        assert len(stamps) == 2 and stamps[0] == stamps[1]


async def test_persistence_waits_for_inflight_path_change_then_discards_stale_work(
    env, factory, monkeypatch
):
    """Opposite lock direction (S7/S8 FolderSource -> Candidate SHARE held by a scan,
    S9 Candidate UPDATE wanted by the persister): the persister waits for the scan's
    commit, then revalidates the path row and discards the old document's result. No
    cycle: the persister never requests a FolderSource lock."""
    from meyar.services import folder_indexer_service as indexer

    tenant, _, storage, base = env
    root = base / "cvs"
    source_id, candidate_id, doc_a = await seed(factory, storage, tenant.id, root, "c.docx", "Java")
    held, release = asyncio.Event(), asyncio.Event()
    real_persist = indexer.persist_candidate_document

    async def held_persist(*args, **kwargs):
        held.set()  # Candidate SHARE (S8) is already held by this scan transaction
        await release.wait()
        return await real_persist(*args, **kwargs)

    monkeypatch.setattr(indexer, "persist_candidate_document", held_persist)
    write_docx(root / "c.docx", "Skills: Python")
    llm = GateLLM()
    async with factory() as scan_db, factory() as run_db, factory() as observer:
        scanning = asyncio.create_task(
            index_folder_and_commit(
                scan_db, storage, LocalTextParser(), tenant_id=tenant.id,
                root_path=str(root), max_bytes=MAX_BYTES,
            )
        )
        running = None
        try:
            await asyncio.wait_for(held.wait(), 15)
            running = asyncio.create_task(
                run_pending(run_db, llm, GateEmbedding(), tenant.id, source_id)
            )
            assert await settled(observer, running) == "blocked"
            release.set()
            scan = await asyncio.wait_for(scanning, 30)
            summary = await asyncio.wait_for(running, 30)
            assert scan.changed == 1 and scan.successful == 1
            rows = await folder_rows(observer, tenant.id, source_id)
            assert rows[0].candidate_document_id != doc_a
            # The run snapshotted A before the scan; its persistence is discarded.
            assert summary.failed == 0 and summary.superseded == 1, summary
            old = await observer.scalar(
                select(func.count()).select_from(CandidateProfileVersion).where(
                    CandidateProfileVersion.candidate_document_id == doc_a
                )
            )
            assert old == 0
        finally:
            release.set()
            await stop(scanning, running)


# ---------------------------------------------------------------------------
# S9 corrective 2: repeated no-change reconciliation must quiesce
# ---------------------------------------------------------------------------


async def diverge_dedup_linked_paths(factory, storage, tenant_id, base, scans):
    root = base / "cvs"
    source_id, candidate_id, shared_doc = await seed(
        factory, storage, tenant_id, root, "a.docx", "Java"
    )
    (root / "b.docx").write_bytes((root / "a.docx").read_bytes())  # byte-identical
    await rescan(factory, storage, tenant_id, root)
    write_docx(root / "a.docx", "Skills: Python")
    if scans == "separate-scans":
        await rescan(factory, storage, tenant_id, root)
    write_docx(root / "b.docx", "Skills: Go")
    await rescan(factory, storage, tenant_id, root)
    return source_id, candidate_id, shared_doc


async def version_snapshot(observer, tenant_id):
    await observer.rollback()
    ids = {}
    for model in (CandidateProfileVersion, CandidateIdentityVersion, CandidateEmbeddingVersion):
        ids[model.__name__] = sorted(
            (await observer.scalars(select(model.id).where(model.tenant_id == tenant_id))).all()
        )
    return ids


@pytest.mark.parametrize("scans", ["same-scan", "separate-scans"])
async def test_diverged_documents_quiesce_on_repeated_no_change_reconciliation(
    env, factory, scans
):
    tenant, _, storage, base = env
    source_id, candidate_id, _ = await diverge_dedup_linked_paths(
        factory, storage, tenant.id, base, scans
    )
    async with factory() as db, factory() as observer:
        first = await run_pending(db, GateLLM(), GateEmbedding(), tenant.id, source_id)
        assert first.superseded == 0 and first.candidates_considered == 2, first
        assert first.failed == 0, f"first run: {first}"
        snapshot = await version_snapshot(observer, tenant.id)
        effective = await get_effective_profile_version(
            observer, tenant_id=tenant.id, candidate_id=candidate_id
        )
        assert effective is not None
        for run in (2, 3):
            llm, embedder = GateLLM(), GateEmbedding()
            summary = await run_pending(db, llm, embedder, tenant.id, source_id)
            assert summary.failed == 0 and summary.superseded == 0 and summary.deferred == 0, (
                f"run {run} did not quiesce: {summary}"
            )
            assert summary.already_ready == summary.candidates_considered == 2, summary
            assert llm.calls == {"profile": 0, "identity": 0}, "unnecessary local inference"
            assert embedder.call_count == 0, "unnecessary embedding inference"
            assert await version_snapshot(observer, tenant.id) == snapshot
            await observer.rollback()
            again = await get_effective_profile_version(
                observer, tenant_id=tenant.id, candidate_id=candidate_id
            )
            assert again is not None and again.id == effective.id
            await assert_embedding_provenance(observer, tenant.id)


async def test_diverged_documents_keep_d100_search_authority_and_own_embeddings(env, factory):
    """READY is document-level (D-111); search/evaluation authority stays D-100: exactly
    one effective profile per Candidate, no cross-document fallback. Each tracked document
    owns an evidence-authorized profile + identity + embedding; only the effective
    profile's facts are searchable."""
    tenant, _, storage, base = env
    source_id, candidate_id, _ = await diverge_dedup_linked_paths(
        factory, storage, tenant.id, base, "separate-scans"
    )
    async with factory() as db, factory() as observer:
        await run_pending(db, GateLLM(), GateEmbedding(), tenant.id, source_id)
        rows = {
            r.relative_path: r.candidate_document_id
            for r in await folder_rows(observer, tenant.id, source_id)
        }
        effective = await get_effective_profile_version(
            observer, tenant_id=tenant.id, candidate_id=candidate_id
        )
        assert effective is not None
        effective_document = effective.candidate_document_id
        skills = {"a.docx": "Python", "b.docx": "Go"}
        effective_path = next(
            path for path, document_id in rows.items() if document_id == effective_document
        )
        other_path = next(path for path in rows if path != effective_path)
        assert candidate_id in await skills_found(observer, tenant.id, skills[effective_path])
        assert candidate_id not in await skills_found(observer, tenant.id, skills[other_path])
        assert candidate_id not in await skills_found(observer, tenant.id, "Java")
        # Every tracked document has exactly one embedding bound to its own profile.
        for document_id in rows.values():
            profile_id = await observer.scalar(
                select(CandidateProfileVersion.id).where(
                    CandidateProfileVersion.candidate_document_id == document_id,
                    CandidateProfileVersion.status == "COMPLETED",
                )
            )
            count = await observer.scalar(
                select(func.count()).select_from(CandidateEmbeddingVersion).where(
                    CandidateEmbeddingVersion.candidate_profile_version_id == profile_id
                )
            )
            assert count == 1
        await assert_embedding_provenance(observer, tenant.id)

        # A later change of one path is processed alone, then reconciliation quiesces again.
        write_docx(base / "cvs" / other_path, "Skills: Rust")
        await rescan(factory, storage, tenant.id, base / "cvs")
        changed = await run_pending(db, GateLLM(), GateEmbedding(), tenant.id, source_id)
        assert changed.failed == 0 and changed.superseded == 0
        assert (changed.processed, changed.already_ready) == (1, 1), changed
        quiet_llm, quiet_embedding = GateLLM(), GateEmbedding()
        quiet = await run_pending(db, quiet_llm, quiet_embedding, tenant.id, source_id)
        assert quiet.failed == 0 and quiet.already_ready == quiet.candidates_considered == 2
        assert quiet_llm.calls == {"profile": 0, "identity": 0}
        assert quiet_embedding.call_count == 0
        # The newly processed document is the latest attempt, hence the effective one.
        assert candidate_id in await skills_found(observer, tenant.id, "Rust")

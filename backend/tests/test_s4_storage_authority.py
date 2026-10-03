"""Issue #46 S4: original-CV storage <-> PostgreSQL recovery (synthetic data only).

Module-level imports are limited to pre-S4 APIs so the reproduction tests can be
run against the unfixed tree; S4-only APIs are imported inside the tests.
"""

import logging
import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.models.audit_event import AuditEvent
from meyar.models.candidate import Candidate
from meyar.models.candidate_document import CandidateDocument
from meyar.models.canonical_document import CanonicalDocument
from meyar.services import candidate_document_service as doc_service
from meyar.services import candidate_service
from meyar.services.candidate_document_service import (
    persist_candidate_document,
    prepare_candidate_document,
)
from meyar.services.candidate_repo import create_candidate
from meyar.services.candidate_service import delete_candidate_cascade
from meyar.storage.local import LocalFilesystemStorage
from meyar.storage.photo import LocalPhotoStorage

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/synthetic_cvs"
VALID_PDF = (FIXTURES / "valid_cv.pdf").read_bytes()
VALID_DOCX = (FIXTURES / "valid_cv.docx").read_bytes()
MALFORMED_PDF = (FIXTURES / "malformed.pdf").read_bytes()
MAX_BYTES = 10 * 1024 * 1024

pytestmark = pytest.mark.usefixtures("enabled_diagnostic_loggers")


def files_under(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


async def prepared(data: bytes = VALID_PDF, name: str = "synthetic.pdf"):
    return await prepare_candidate_document(
        LocalTextParser(), filename=name, content_type="", data=data, max_bytes=MAX_BYTES
    )


async def count(db: AsyncSession, model) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


@pytest.fixture(autouse=True)
def _restore_session_commit(db_session: AsyncSession):
    """Tests patch db_session.commit; restore it before db_session's own teardown."""
    yield
    if "commit" in db_session.__dict__:
        db_session.commit = AsyncSession.commit.__get__(db_session)  # type: ignore[method-assign]


@pytest.fixture
def storage(tmp_path: Path) -> LocalFilesystemStorage:
    return LocalFilesystemStorage(root=str(tmp_path / "storage"))


@pytest.fixture
def photos(tmp_path: Path) -> LocalPhotoStorage:
    return LocalPhotoStorage(root=str(tmp_path / "storage"))


async def add_document(db, storage, tenant_id, candidate_id, data=VALID_PDF, name="s.pdf"):
    document = await persist_candidate_document(
        db, storage, await prepared(data, name), tenant_id=tenant_id,
        candidate_id=candidate_id, filename=name,
    )
    return document


async def candidate_with_documents(db, storage, tenant_id, n=1):
    """Committed candidate with n distinct historical originals."""
    candidate = await create_candidate(db, tenant_id=tenant_id)
    docs = []
    for i in range(n):
        data = VALID_PDF if i == 0 else VALID_PDF + b"\n%" + str(i).encode()
        docs.append(await add_document(db, storage, tenant_id, candidate.id, data, f"s{i}.pdf"))
    await db.commit()
    return candidate.id, [(d.id, d.storage_key) for d in docs]


async def delete_now(db, storage, photos, tenant_id, candidate_id):
    return await delete_candidate_cascade(
        db, storage, tenant_id=tenant_id, candidate_id=candidate_id,
        photo_storage=photos, actor_id=uuid.uuid4(),
    )


# ---------------------------------------------------------------- create / save


async def test_1_document_creation_failure_removes_saved_original(
    db_session, tenant_and_key, storage, tmp_path, monkeypatch
):
    tenant, _, _ = tenant_and_key
    candidate = await create_candidate(db_session, tenant_id=tenant.id)
    await db_session.commit()

    async def boom(*args, **kwargs):
        raise RuntimeError("synthetic document row failure")

    monkeypatch.setattr(doc_service, "create_candidate_document", boom)
    with pytest.raises(RuntimeError):
        await add_document(db_session, storage, tenant.id, candidate.id)
    assert files_under(tmp_path / "storage") == []
    assert await count(db_session, CandidateDocument) == 0


@pytest.mark.parametrize("failing", ["canonical", "audit"])
async def test_2_canonical_or_audit_failure_rolls_back_and_removes_original(
    db_session, tenant_and_key, storage, tmp_path, monkeypatch, failing
):
    tenant, _, _ = tenant_and_key
    candidate = await create_candidate(db_session, tenant_id=tenant.id)
    await db_session.commit()

    async def boom(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(
        doc_service, "create_canonical_document" if failing == "canonical" else "record_event",
        boom,
    )
    with pytest.raises(RuntimeError):
        await add_document(db_session, storage, tenant.id, candidate.id)
    # Without any caller rollback: the savepoint already removed this call's rows.
    assert await count(db_session, CandidateDocument) == 0
    assert await count(db_session, CanonicalDocument) == 0
    assert files_under(tmp_path / "storage") == []


@pytest.mark.parametrize("data,name", [(VALID_PDF, "ok.pdf"), (MALFORMED_PDF, "terminal.pdf")],
                         ids=["parsed", "terminal-parse-failure"])
async def test_3_4_commit_failure_after_persist_removes_original(
    db_session, tenant_and_key, storage, tmp_path, monkeypatch, data, name
):
    from meyar.services.storage_recovery import recover_on_failure

    tenant, _, _ = tenant_and_key
    candidate = await create_candidate(db_session, tenant_id=tenant.id)
    await db_session.commit()
    candidate_id = candidate.id

    async def fail_commit():
        raise OSError("synthetic commit failure")

    with pytest.raises(OSError):
        async with recover_on_failure(db_session):
            document = await add_document(db_session, storage, tenant.id, candidate_id, data, name)
            assert files_under(tmp_path / "storage")  # saved, not yet durable
            assert document.id is not None
            monkeypatch.setattr(db_session, "commit", fail_commit)
            await db_session.commit()
    assert files_under(tmp_path / "storage") == []
    assert await count(db_session, CandidateDocument) == 0
    assert await count(db_session, CanonicalDocument) == 0
    assert await db_session.get(Candidate, candidate_id) is not None


async def test_commit_with_recovery_success_keeps_original(
    db_session, tenant_and_key, storage, tmp_path
):
    from meyar.services.storage_recovery import commit_with_recovery

    tenant, _, _ = tenant_and_key
    candidate = await create_candidate(db_session, tenant_id=tenant.id)
    document = await add_document(db_session, storage, tenant.id, candidate.id)
    await commit_with_recovery(db_session)
    assert await storage.read(storage_key=document.storage_key) == VALID_PDF
    assert await count(db_session, CandidateDocument) == 1


async def test_ambiguous_commit_that_became_durable_never_deletes_original(
    db_session, tenant_and_key, storage, tmp_path, monkeypatch
):
    """COMMIT succeeded server-side but the client saw an error: the DB, not the
    exception, decides — a referenced original must survive."""
    from meyar.services.storage_recovery import commit_with_recovery

    tenant, _, _ = tenant_and_key
    candidate = await create_candidate(db_session, tenant_id=tenant.id)
    document = await add_document(db_session, storage, tenant.id, candidate.id)
    key = document.storage_key
    real_commit = db_session.commit

    async def commit_then_error():
        await real_commit()
        raise OSError("synthetic lost commit acknowledgement")

    monkeypatch.setattr(db_session, "commit", commit_then_error)
    with pytest.raises(OSError):
        await commit_with_recovery(db_session)
    assert await storage.read(storage_key=key) == VALID_PDF
    assert await count(db_session, CandidateDocument) == 1


async def test_failure_before_save_performs_no_storage_compensation(
    db_session, tenant_and_key, storage, tmp_path, monkeypatch
):
    from meyar.ingestion.parser import ParseError, ParseFailureCode

    tenant, _, _ = tenant_and_key
    candidate = await create_candidate(db_session, tenant_id=tenant.id)
    calls: list[str] = []

    async def spy(self, **kwargs):
        calls.append("delete")

    monkeypatch.setattr(LocalFilesystemStorage, "delete_owned", spy, raising=False)
    operational = await prepared()
    object.__setattr__(operational, "outcome", ParseError(ParseFailureCode.PARSER_BUSY))
    with pytest.raises(ParseError):
        await persist_candidate_document(
            db_session, storage, operational, tenant_id=tenant.id,
            candidate_id=candidate.id, filename="x.pdf",
        )
    assert calls == [] and files_under(tmp_path / "storage") == []


async def test_5_storage_save_failure_leaves_no_rows_or_temp_file(
    db_session, tenant_and_key, storage, tmp_path, monkeypatch
):
    tenant, _, _ = tenant_and_key
    candidate = await create_candidate(db_session, tenant_id=tenant.id)
    original_write = Path.write_bytes

    def partial_write(self, data):
        original_write(self, data[: len(data) // 2])
        raise OSError("synthetic disk write failure")

    monkeypatch.setattr(Path, "write_bytes", partial_write)
    with pytest.raises(OSError):
        await add_document(db_session, storage, tenant.id, candidate.id)
    assert files_under(tmp_path / "storage") == []
    assert await count(db_session, CandidateDocument) == 0


async def test_6_replace_failure_cleans_temp_and_reports_no_final_object(
    tmp_path, monkeypatch
):
    from meyar.storage import local as local_module

    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))

    def fail_replace(*args, **kwargs):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(local_module.os, "replace", fail_replace)
    with pytest.raises(OSError):
        await storage.save(tenant_id=uuid.uuid4(), content=b"synthetic bytes")
    assert files_under(tmp_path / "storage") == []


@pytest.mark.parametrize("data,name,ctype", [
    (VALID_PDF, "ok.pdf", "application/pdf"),
    (MALFORMED_PDF, "terminal.pdf", "application/pdf"),
], ids=["parsed", "terminal-parse-failure"])
async def test_7_api_upload_commit_failure_is_safe_and_leaves_no_orphan(
    client: AsyncClient, db_session, tenant_and_key, tmp_path, monkeypatch, data, name, ctype
):
    tenant, _, plaintext = tenant_and_key
    created = await client.post(
        "/api/v1/candidates", headers={"Authorization": f"Bearer {plaintext}"}
    )
    candidate_id = created.json()["id"]
    root = tmp_path / "storage"
    real_commit = db_session.commit
    armed = True

    async def fail_commit():
        nonlocal armed
        if armed and files_under(root):  # only the post-persist commit
            armed = False
            raise OSError("synthetic upload commit failure")
        await real_commit()

    monkeypatch.setattr(db_session, "commit", fail_commit)
    response = await client.post(
        f"/api/v1/candidates/{candidate_id}/documents",
        headers={"Authorization": f"Bearer {plaintext}"},
        files={"file": (name, data, ctype)},
    )
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    assert files_under(root) == []
    assert await count(db_session, CandidateDocument) == 0
    assert await count(db_session, CanonicalDocument) == 0
    uploaded = await db_session.scalar(
        select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == "CANDIDATE_DOCUMENT_UPLOADED"
        )
    )
    assert uploaded == 0


async def test_8_shared_folder_ingestion_failure_compensates_every_saved_original(
    db_session, tenant_and_key, storage, tmp_path, monkeypatch
):
    from meyar.services.folder_indexer_service import index_folder
    from meyar.services.storage_recovery import recover_on_failure

    tenant, _, _ = tenant_and_key
    tenant_id = tenant.id
    root = tmp_path / "synthetic"
    root.mkdir()
    (root / "a.pdf").write_bytes(VALID_PDF)
    (root / "b.pdf").write_bytes(VALID_PDF + b"\n%b")
    (root / "c.docx").write_bytes(VALID_DOCX)

    # (a) caller-owned commit fails after every file was persisted
    async def fail_commit():
        raise OSError("synthetic folder commit failure")

    real_commit = db_session.commit
    with pytest.raises(OSError):
        async with recover_on_failure(db_session):
            summary = await index_folder(
                db_session, storage, LocalTextParser(), tenant_id=tenant_id,
                root_path=str(root), max_bytes=MAX_BYTES,
            )
            assert summary.successful == 3 and len(files_under(tmp_path / "storage")) == 3
            monkeypatch.setattr(db_session, "commit", fail_commit)
            await db_session.commit()
    monkeypatch.setattr(db_session, "commit", real_commit)
    assert files_under(tmp_path / "storage") == []
    assert await count(db_session, CandidateDocument) == 0

    # (b) a mid-scan persistence failure compensates earlier files in the transaction
    calls = 0
    original = doc_service.create_canonical_document

    async def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic second-file failure")
        return await original(*args, **kwargs)

    monkeypatch.setattr(doc_service, "create_canonical_document", fail_second)
    with pytest.raises(RuntimeError):
        async with recover_on_failure(db_session):
            await index_folder(
                db_session, storage, LocalTextParser(), tenant_id=tenant_id,
                root_path=str(root), max_bytes=MAX_BYTES,
            )
    assert files_under(tmp_path / "storage") == []
    assert await count(db_session, CandidateDocument) == 0


async def test_direct_rollback_is_compensated_by_next_persist(
    db_session, tenant_and_key, storage, tmp_path
):
    tenant, _, _ = tenant_and_key
    tenant_id = tenant.id
    candidate = await create_candidate(db_session, tenant_id=tenant_id)
    await db_session.commit()
    candidate_id = candidate.id
    await add_document(db_session, storage, tenant_id, candidate_id)
    await db_session.rollback()  # a caller that bypassed the recovery helpers
    survivor = await add_document(
        db_session, storage, tenant_id, candidate_id, VALID_DOCX, "d.docx"
    )
    await db_session.commit()
    remaining = [p.name for p in files_under(tmp_path / "storage")]
    assert remaining == [survivor.storage_key.split("/")[1]]


# --------------------------------------------------------------- candidate delete


@pytest.mark.parametrize("fault", ["row_delete", "audit", "commit"])
async def test_9_delete_with_one_original_restores_exact_bytes_on_later_failure(
    db_session, tenant_and_key, storage, photos, tmp_path, monkeypatch, fault
):
    tenant, _, _ = tenant_and_key
    candidate_id, docs = await candidate_with_documents(db_session, storage, tenant.id)
    key = docs[0][1]
    before = await storage.read(storage_key=key)

    async def boom(*args, **kwargs):
        raise RuntimeError("synthetic later failure")

    if fault == "row_delete":
        monkeypatch.setattr(candidate_service, "delete_candidate_row", boom)
    elif fault == "audit":
        monkeypatch.setattr(candidate_service, "record_event", boom)
    else:
        monkeypatch.setattr(db_session, "commit", boom)
    with pytest.raises(RuntimeError):
        await delete_now(db_session, storage, photos, tenant.id, candidate_id)
    assert await db_session.get(Candidate, candidate_id) is not None
    assert await count(db_session, CandidateDocument) == 1
    assert await storage.read(storage_key=key) == before
    assert not [p for p in files_under(tmp_path / "storage") if ".trash" in p.parts]


async def test_10_multiple_originals_partial_failure_restores_every_one_without_reading_bytes(
    db_session, tenant_and_key, storage, photos, tmp_path, monkeypatch
):
    tenant, _, _ = tenant_and_key
    candidate_id, docs = await candidate_with_documents(db_session, storage, tenant.id, n=4)
    before = {key: await storage.read(storage_key=key) for _, key in docs}
    original_stage = LocalFilesystemStorage.stage_delete
    calls = 0

    async def fail_fourth(self, *, tenant_id, storage_key):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("synthetic fourth stage failure")
        return await original_stage(self, tenant_id=tenant_id, storage_key=storage_key)

    def no_buffering(self):
        raise AssertionError("delete/restore must not load original bytes into memory")

    monkeypatch.setattr(LocalFilesystemStorage, "stage_delete", fail_fourth)
    monkeypatch.setattr(Path, "read_bytes", no_buffering)
    with pytest.raises(OSError):
        await delete_now(db_session, storage, photos, tenant.id, candidate_id)
    monkeypatch.undo()
    assert await count(db_session, CandidateDocument) == 4
    for key, content in before.items():
        assert await storage.read(storage_key=key) == content
    assert not [p for p in files_under(tmp_path / "storage") if ".trash" in p.parts]


async def test_11_originals_already_missing_are_not_fabricated_on_rollback(
    db_session, tenant_and_key, storage, photos, tmp_path, monkeypatch
):
    tenant, _, _ = tenant_and_key
    candidate_id, docs = await candidate_with_documents(db_session, storage, tenant.id, n=2)
    missing_key, present_key = docs[0][1], docs[1][1]
    present_bytes = await storage.read(storage_key=present_key)
    await storage.delete(storage_key=missing_key)  # pre-existing inconsistency

    async def boom(*args, **kwargs):
        raise RuntimeError("synthetic later failure")

    monkeypatch.setattr(candidate_service, "delete_candidate_row", boom)
    with pytest.raises(RuntimeError):
        await delete_now(db_session, storage, photos, tenant.id, candidate_id)
    with pytest.raises(FileNotFoundError):
        await storage.read(storage_key=missing_key)
    assert await storage.read(storage_key=present_key) == present_bytes
    assert await count(db_session, CandidateDocument) == 2  # DB authority intact


async def test_12_restore_collision_fails_closed_and_never_overwrites(tmp_path):
    from meyar.storage.staging import StorageRestoreConflictError

    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))
    tenant_id = uuid.uuid4()
    key = await storage.save(tenant_id=tenant_id, content=b"original synthetic bytes")
    staged = await storage.stage_delete(tenant_id=tenant_id, storage_key=key)
    assert staged is not None
    (tmp_path / "storage" / key).write_bytes(b"foreign different bytes")
    with pytest.raises(StorageRestoreConflictError):
        await storage.restore_staged(staged)
    assert await storage.read(storage_key=key) == b"foreign different bytes"
    # the staged object is still intact (nothing was lost) and restore stays blocked
    assert (tmp_path / "storage" / staged.trash_ref).read_bytes() == b"original synthetic bytes"
    (tmp_path / "storage" / key).write_bytes(b"original synthetic bytes")  # identical: idempotent
    await storage.restore_staged(staged)
    assert await storage.read(storage_key=key) == b"original synthetic bytes"
    assert not (tmp_path / "storage" / staged.trash_ref).exists()


async def test_12b_cascade_restore_collision_is_unresolved_not_success(
    db_session, tenant_and_key, storage, photos, tmp_path, monkeypatch, caplog
):
    from meyar.services.storage_recovery import StorageCompensationError

    tenant, _, _ = tenant_and_key
    candidate_id, docs = await candidate_with_documents(db_session, storage, tenant.id)
    key = docs[0][1]
    original_stage = LocalFilesystemStorage.stage_delete

    async def stage_then_occupy(self, *, tenant_id, storage_key):
        staged = await original_stage(self, tenant_id=tenant_id, storage_key=storage_key)
        (tmp_path / "storage" / storage_key).write_bytes(b"foreign synthetic bytes")
        return staged

    async def boom(*args, **kwargs):
        raise RuntimeError("synthetic later failure")

    monkeypatch.setattr(LocalFilesystemStorage, "stage_delete", stage_then_occupy)
    monkeypatch.setattr(candidate_service, "delete_candidate_row", boom)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(StorageCompensationError) as info:
            await delete_now(db_session, storage, photos, tenant.id, candidate_id)
    assert isinstance(info.value.__cause__, RuntimeError)
    assert await storage.read(storage_key=key) == b"foreign synthetic bytes"
    assert "code=STORAGE_COMPENSATION_UNRESOLVED" in caplog.text
    assert key not in caplog.text and str(tmp_path) not in caplog.text


async def _photo_row(db, storage, photos, tenant_id, candidate_id, document):
    from meyar.services.candidate_photo_repo import create_photo_version

    content = b"synthetic derived photo " + document.id.bytes
    key = await photos.save(tenant_id=tenant_id, content=content)
    await create_photo_version(
        db, tenant_id=tenant_id, candidate_id=candidate_id, document_id=document.id,
        extractor_version="synthetic-s4",
        outcome={"status": "AVAILABLE", "derived_sha256": "0" * 64, "width": 2, "height": 2},
        derived_storage_key=key,
    )
    return key


async def test_13_mixed_photos_and_originals_all_restored_after_partial_removal(
    db_session, tenant_and_key, storage, photos, tmp_path, monkeypatch
):
    tenant, _, _ = tenant_and_key
    tenant_id = tenant.id
    candidate_id, docs = await candidate_with_documents(db_session, storage, tenant_id, n=2)
    rows = (
        await db_session.scalars(
            select(CandidateDocument).order_by(CandidateDocument.created_at)
        )
    ).all()
    photo_keys = [
        await _photo_row(db_session, storage, photos, tenant_id, candidate_id, d) for d in rows
    ]
    await db_session.commit()
    before_photos = {k: await photos.read(tenant_id=tenant_id, storage_key=k) for k in photo_keys}
    before_docs = {k: await storage.read(storage_key=k) for _, k in docs}
    original_stage = LocalFilesystemStorage.stage_delete
    calls = 0

    async def fail_second_original(self, *, tenant_id, storage_key):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic original stage failure")
        return await original_stage(self, tenant_id=tenant_id, storage_key=storage_key)

    monkeypatch.setattr(LocalFilesystemStorage, "stage_delete", fail_second_original)
    with pytest.raises(OSError):
        await delete_now(db_session, storage, photos, tenant_id, candidate_id)
    monkeypatch.undo()
    for key, content in before_photos.items():
        assert await photos.read(tenant_id=tenant_id, storage_key=key) == content
    for key, content in before_docs.items():
        assert await storage.read(storage_key=key) == content
    assert await db_session.get(Candidate, candidate_id) is not None

    # 14. the successful delete afterwards removes everything and keeps audit semantics
    assert await delete_now(db_session, storage, photos, tenant_id, candidate_id) == 2
    assert await db_session.get(Candidate, candidate_id) is None
    assert await count(db_session, CandidateDocument) == 0
    assert files_under(tmp_path / "storage") == []
    audit = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.event_type == "CANDIDATE_DELETED")
    )
    assert audit is not None and audit.event_metadata == {
        "candidate_id": str(candidate_id), "document_count": 2,
    }


async def test_ambiguous_delete_commit_that_became_durable_purges_not_restores(
    db_session, tenant_and_key, storage, photos, tmp_path, monkeypatch
):
    tenant, _, _ = tenant_and_key
    candidate_id, docs = await candidate_with_documents(db_session, storage, tenant.id)
    real_commit = db_session.commit

    async def commit_then_error():
        await real_commit()
        raise OSError("synthetic lost commit acknowledgement")

    monkeypatch.setattr(db_session, "commit", commit_then_error)
    with pytest.raises(OSError):
        await delete_now(db_session, storage, photos, tenant.id, candidate_id)
    assert await db_session.get(Candidate, candidate_id) is None
    assert files_under(tmp_path / "storage") == []  # no orphan bytes of a deleted candidate


# ----------------------------------------------------- isolation, idempotency, privacy


async def test_15_tenant_isolation_of_all_compensation_primitives(
    db_session, tenant_and_key, storage, photos, tmp_path
):
    from meyar.services.tenant_repo import create_tenant
    from meyar.storage.staging import StagedObject

    tenant_a, _, _ = tenant_and_key
    tenant_b = await create_tenant(db_session, name=f"B-{uuid.uuid4().hex[:6]}")
    candidate_b, docs_b = await candidate_with_documents(db_session, storage, tenant_b.id)
    key_b = docs_b[0][1]
    before = await storage.read(storage_key=key_b)

    with pytest.raises(ValueError):
        await storage.delete_owned(tenant_id=tenant_a.id, storage_key=key_b)
    with pytest.raises(ValueError):
        await storage.stage_delete(tenant_id=tenant_a.id, storage_key=key_b)
    forged = StagedObject("document", tenant_a.id, key_b, f".trash/{tenant_b.id.hex}/{'0' * 32}")
    with pytest.raises(ValueError):
        await storage.purge_staged(forged)
    with pytest.raises(ValueError):
        await storage.restore_staged(forged)
    with pytest.raises(ValueError):
        await storage.stage_delete(tenant_id=tenant_a.id, storage_key="../../etc/passwd")
    assert await delete_now(db_session, storage, photos, tenant_a.id, candidate_b) is None
    assert await storage.read(storage_key=key_b) == before
    assert await db_session.get(Candidate, candidate_b) is not None


async def test_16_compensation_primitives_are_idempotent(tmp_path):
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))
    tenant_id = uuid.uuid4()
    key = await storage.save(tenant_id=tenant_id, content=b"synthetic")
    await storage.delete_owned(tenant_id=tenant_id, storage_key=key)
    await storage.delete_owned(tenant_id=tenant_id, storage_key=key)
    assert await storage.stage_delete(tenant_id=tenant_id, storage_key=key) is None
    key = await storage.save(tenant_id=tenant_id, content=b"synthetic")
    staged = await storage.stage_delete(tenant_id=tenant_id, storage_key=key)
    assert staged is not None
    await storage.restore_staged(staged)
    await storage.restore_staged(staged)
    assert await storage.read(storage_key=key) == b"synthetic"
    staged = await storage.stage_delete(tenant_id=tenant_id, storage_key=key)
    assert staged is not None
    await storage.purge_staged(staged)
    await storage.purge_staged(staged)
    await storage.restore_staged(staged)  # nothing left to restore: no resurrection
    assert files_under(tmp_path / "storage") == []


async def test_17_compensation_failure_is_not_success_and_leaks_nothing(
    db_session, tenant_and_key, storage, tmp_path, monkeypatch, caplog
):
    from meyar.services.storage_recovery import StorageCompensationError

    tenant, _, _ = tenant_and_key
    candidate = await create_candidate(db_session, tenant_id=tenant.id)
    await db_session.commit()
    secret = "/srv/secret-path/candidate-name@example.invalid"

    async def failing_create(*args, **kwargs):
        raise RuntimeError(f"primary failure carrying {secret}")

    async def failing_delete(self, **kwargs):
        raise OSError(f"cleanup failure carrying {secret}")

    monkeypatch.setattr(doc_service, "create_candidate_document", failing_create)
    monkeypatch.setattr(LocalFilesystemStorage, "delete_owned", failing_delete)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(StorageCompensationError) as info:
            await add_document(db_session, storage, tenant.id, candidate.id)
    assert str(info.value) == "Storage compensation incomplete."
    assert info.value.unresolved == 1
    assert isinstance(info.value.__cause__, RuntimeError)  # primary problem preserved
    assert "code=STORAGE_COMPENSATION_UNRESOLVED" in caplog.text
    assert "error_type=OSError" in caplog.text
    assert secret not in caplog.text and "secret-path" not in caplog.text
    assert str(tmp_path) not in caplog.text
    assert len(files_under(tmp_path / "storage")) == 1  # unresolved orphan stays observable on disk


async def test_purge_failure_after_durable_delete_does_not_fail_the_operation(
    db_session, tenant_and_key, storage, photos, tmp_path, monkeypatch, caplog
):
    tenant, _, _ = tenant_and_key
    candidate_id, docs = await candidate_with_documents(db_session, storage, tenant.id)

    async def failing_purge(self, staged):
        raise OSError("synthetic purge failure")

    monkeypatch.setattr(LocalFilesystemStorage, "purge_staged", failing_purge)
    with caplog.at_level(logging.ERROR):
        assert await delete_now(db_session, storage, photos, tenant.id, candidate_id) == 1
    assert await db_session.get(Candidate, candidate_id) is None
    assert "code=STORAGE_PURGE_UNRESOLVED" in caplog.text
    assert docs[0][1] not in caplog.text


async def test_reset_demo_stages_originals_and_photos_and_restores_on_failure(
    db_session, storage, photos, tmp_path, monkeypatch
):
    from meyar.services.audit_repo import record_event
    from meyar.services.demo_seed_service import (
        DEMO_TENANT_MARKER_EVENT,
        DEMO_TENANT_NAME,
        reset_demo,
    )
    from meyar.services.storage_recovery import commit_with_recovery, recover_on_failure
    from meyar.services.tenant_repo import create_tenant

    tenant = await create_tenant(db_session, name=DEMO_TENANT_NAME)
    tenant_id = tenant.id
    await record_event(db_session, tenant_id=tenant_id, event_type=DEMO_TENANT_MARKER_EVENT)
    candidate_id, docs = await candidate_with_documents(db_session, storage, tenant_id)
    key = docs[0][1]
    before = await storage.read(storage_key=key)
    document = (await db_session.scalars(select(CandidateDocument))).one()
    photo_key = await _photo_row(db_session, storage, photos, tenant_id, candidate_id, document)
    await db_session.commit()
    photo_before = await photos.read(tenant_id=tenant_id, storage_key=photo_key)

    async def boom():
        raise OSError("synthetic reset commit failure")

    real_commit = db_session.commit
    with pytest.raises(OSError):
        async with recover_on_failure(db_session):
            assert await reset_demo(db_session, storage, photos)
            monkeypatch.setattr(db_session, "commit", boom)
            await commit_with_recovery(db_session)
    monkeypatch.setattr(db_session, "commit", real_commit)
    assert await storage.read(storage_key=key) == before
    assert await photos.read(tenant_id=tenant_id, storage_key=photo_key) == photo_before
    assert await count(db_session, CandidateDocument) == 1

    assert await reset_demo(db_session, storage, photos)
    await commit_with_recovery(db_session)
    assert files_under(tmp_path / "storage") == []
    assert await count(db_session, CandidateDocument) == 0

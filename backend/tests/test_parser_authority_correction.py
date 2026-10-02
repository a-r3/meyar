"""PR #108 correction: parser failure authority and request DB lifecycle."""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from conftest import TEST_DATABASE_URL
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_parser_isolation import VALID, docx_bytes

from meyar.config import Settings, get_settings
from meyar.db import get_db
from meyar.ingestion import parser_supervisor as supervisor
from meyar.ingestion.admission import AdmissionGate
from meyar.ingestion.dependency import get_document_parser
from meyar.ingestion.parser import PARSE_FAILURE_MESSAGES, ParseError, ParseFailureCode
from meyar.ingestion.parser_policy import OutputLimits
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.ingestion.parsers.local_text_sync import parse_sync
from meyar.main import app
from meyar.models.api_key import ApiKey
from meyar.models.candidate import Candidate
from meyar.models.candidate_document import CandidateDocument
from meyar.models.canonical_document import CanonicalDocument
from meyar.services.candidate_repo import create_candidate
from meyar.services.folder_indexed_file_repo import list_folder_indexed_files
from meyar.services.folder_indexer_service import index_folder
from meyar.storage.dependency import get_document_storage
from meyar.storage.local import LocalFilesystemStorage

TERMINAL = {
    ParseFailureCode.INVALID_DOCUMENT,
    ParseFailureCode.INSUFFICIENT_EXTRACTABLE_TEXT,
    ParseFailureCode.PARSER_OUTPUT_LIMIT,
}
OPERATIONAL = set(ParseFailureCode) - TERMINAL


@pytest.mark.parametrize("code", list(ParseFailureCode))
def test_closed_failure_authority(code):
    assert ParseError(code).is_terminal == (code in TERMINAL)


@pytest.mark.parametrize("code", sorted(OPERATIONAL))
async def test_operational_upload_is_not_document_authority_and_retries(
    client, db_session, tenant_and_key, tmp_path, monkeypatch, code
):
    _, _, key = tenant_and_key
    headers = {"Authorization": f"Bearer {key}"}
    candidate = (await client.post("/api/v1/candidates", headers=headers)).json()["id"]

    async def failed(self, **kwargs):
        raise ParseError(code)

    with monkeypatch.context() as patch:
        patch.setattr(LocalTextParser, "parse", failed)
        response = await client.post(
            f"/api/v1/candidates/{candidate}/documents",
            headers=headers,
            files={"file": ("synthetic.pdf", VALID, "application/pdf")},
        )
    assert response.status_code == 503
    assert response.json()["detail"] == PARSE_FAILURE_MESSAGES[code]
    assert await db_session.scalar(select(CandidateDocument)) is None
    assert await db_session.scalar(select(CanonicalDocument)) is None
    assert not [p for p in (tmp_path / "storage").rglob("*") if p.is_file()]
    retry = await client.post(
        f"/api/v1/candidates/{candidate}/documents",
        headers=headers,
        files={"file": ("synthetic.pdf", VALID, "application/pdf")},
    )
    assert retry.status_code == 201 and retry.json()["parser_status"] == "PARSED"


@pytest.mark.parametrize("code", sorted(TERMINAL))
async def test_terminal_upload_keeps_authorized_original_without_canonical(
    client, db_session, tenant_key_and_user, monkeypatch, code
):
    _, _, key, user, password, _ = tenant_key_and_user
    app.dependency_overrides[get_settings] = lambda: Settings(ui_cookie_secure=False)
    headers = {"Authorization": f"Bearer {key}"}
    candidate = (await client.post("/api/v1/candidates", headers=headers)).json()["id"]

    async def terminal(self, **kwargs):
        raise ParseError(code)

    with monkeypatch.context() as patch:
        patch.setattr(LocalTextParser, "parse", terminal)
        response = await client.post(
            f"/api/v1/candidates/{candidate}/documents",
            headers=headers,
            files={"file": ("synthetic.pdf", VALID, "application/pdf")},
        )
    assert response.status_code == 201
    body = response.json()
    assert body["parser_status"] == "PARSE_FAILED" and body["canonical"] is None
    assert body["parse_error_code"] == code
    assert body["parse_error_message"] == PARSE_FAILURE_MESSAGES[code]
    assert await db_session.scalar(select(CanonicalDocument)) is None
    assert (
        await client.post("/ui/login", data={"username": user.username, "password": password})
    ).status_code == 303
    original = await client.get(f"/ui/candidates/{candidate}/documents/{body['id']}/original")
    assert original.status_code == 200 and original.content == VALID


async def test_folder_busy_remains_failed_and_unchanged_retry_succeeds(
    db_session, tenant_and_key, tmp_path
):
    tenant, _, _ = tenant_and_key
    root = tmp_path / "synthetic"
    root.mkdir()
    (root / "synthetic.pdf").write_bytes(VALID)
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))

    class Busy:
        async def parse(self, **kwargs):
            raise ParseError(ParseFailureCode.PARSER_BUSY)

    kwargs = dict(tenant_id=tenant.id, root_path=str(root), max_bytes=10 * 1024 * 1024)
    failed = await index_folder(db_session, storage, Busy(), **kwargs)
    assert failed.failed == 1 and failed.successful == 0
    row = (
        await list_folder_indexed_files(
            db_session, tenant_id=tenant.id, folder_source_id=failed.folder_source_id
        )
    )[0]
    candidate_id = row.candidate_id
    assert row.index_status == "FAILED" and row.candidate_document_id is None
    assert row.failure_code == ParseFailureCode.PARSER_BUSY
    assert await db_session.scalar(select(CandidateDocument)) is None
    assert not [p for p in (tmp_path / "storage").rglob("*") if p.is_file()]
    retried = await index_folder(db_session, storage, LocalTextParser(), **kwargs)
    assert retried.retried == 1 and retried.successful == 1
    assert row.index_status == "INDEXED" and row.candidate_document_id is not None
    assert row.candidate_id == candidate_id


@pytest.mark.parametrize("failures", [1, 2])
async def test_changed_and_retry_transient_preserve_previous_document(
    db_session, tenant_and_key, tmp_path, failures
):
    tenant, _, _ = tenant_and_key
    root = tmp_path / "synthetic"
    root.mkdir()
    file = root / "synthetic.docx"
    file.write_bytes(docx_bytes(["Original synthetic skill"]))
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))
    kwargs = dict(tenant_id=tenant.id, root_path=str(root), max_bytes=10 * 1024 * 1024)
    summary = await index_folder(db_session, storage, LocalTextParser(), **kwargs)
    row = (
        await list_folder_indexed_files(
            db_session, tenant_id=tenant.id, folder_source_id=summary.folder_source_id
        )
    )[0]
    old_document_id = row.candidate_document_id
    old_canonical = await db_session.scalar(select(CanonicalDocument))
    old_content = old_canonical.content.copy()
    files_before = {p for p in (tmp_path / "storage").rglob("*") if p.is_file()}
    file.write_bytes(docx_bytes(["Changed synthetic skill"]))

    class Busy:
        async def parse(self, **kwargs):
            raise ParseError(ParseFailureCode.PARSER_BUSY)

    for attempt in range(failures):
        failed = await index_folder(db_session, storage, Busy(), **kwargs)
        assert (failed.changed if attempt == 0 else failed.retried) == 1
        assert failed.failed == 1 and row.index_status == "FAILED"
        assert row.candidate_document_id == old_document_id
        assert len((await db_session.scalars(select(CandidateDocument))).all()) == 1
        assert {p for p in (tmp_path / "storage").rglob("*") if p.is_file()} == files_before
        assert old_canonical.content == old_content
    retried = await index_folder(db_session, storage, LocalTextParser(), **kwargs)
    assert retried.retried == 1 and row.index_status == "INDEXED"
    assert row.candidate_document_id != old_document_id
    assert old_canonical.content == old_content


async def test_active_parser_and_four_waiters_hold_no_request_db_connections(
    db_session, tenant_and_key, tmp_path, monkeypatch
):
    tenant, _, key = tenant_and_key
    candidate = await create_candidate(db_session, tenant_id=tenant.id)
    candidate_id = candidate.id
    await db_session.commit()
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=2, max_overflow=0, pool_timeout=2)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    checked_out = 0

    @event.listens_for(engine.sync_engine.pool, "checkout")
    def checkout(*args):
        nonlocal checked_out
        checked_out += 1

    @event.listens_for(engine.sync_engine.pool, "checkin")
    def checkin(*args):
        nonlocal checked_out
        checked_out -= 1

    sessions = []

    async def request_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            sessions.append(session)
            yield session

    monkeypatch.setitem(app.dependency_overrides, get_db, request_db)
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))
    monkeypatch.setitem(app.dependency_overrides, get_document_storage, lambda: storage)
    active = asyncio.Event()
    waiting = asyncio.Event()
    release = asyncio.Event()

    class ObservedGate(AdmissionGate):
        async def acquire(self):
            if self.active == self.capacity and len(self.waiters) == 3:
                waiting.set()
            await super().acquire()

    gate = ObservedGate(seconds=10)
    monkeypatch.setattr(supervisor, "gate", lambda: gate)
    output = parse_sync(VALID, "PDF", OutputLimits()).model_dump_json().encode()

    async def held(*args):
        active.set()
        await release.wait()
        return output

    monkeypatch.setattr(supervisor, "_run", held)

    # Photo handling is a separate accepted feature, outside this parser invariant.
    async def no_photo(*args, **kwargs):
        pass

    monkeypatch.setattr("meyar.api.v1.candidates.process_photo_for_document", no_photo)
    headers = {"Authorization": f"Bearer {key}"}
    tasks = []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:

            async def upload():
                return await http.post(
                    f"/api/v1/candidates/{candidate_id}/documents",
                    headers=headers,
                    files={"file": ("synthetic.pdf", VALID, "application/pdf")},
                )

            tasks = [asyncio.create_task(upload()) for _ in range(5)]
            await asyncio.wait_for(active.wait(), 5)
            await asyncio.wait_for(waiting.wait(), 5)
            assert gate.active == 1 and len(gate.waiters) == 4
            assert len(sessions) == 5
            assert checked_out == 0
            assert all(not session.in_transaction() for session in sessions)
            response = await asyncio.wait_for(
                http.get(f"/api/v1/candidates/{candidate_id}", headers=headers), 3
            )
            assert response.status_code == 200 and not release.is_set()
            assert checked_out == 0
            release.set()
            responses = await asyncio.gather(*tasks)
            assert all(r.status_code == 201 for r in responses)
            assert checked_out == 0
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()


@pytest.mark.parametrize(
    ("change", "status_code"),
    [
        ("revoke", 401),
        ("expire", 401),
        ("scope", 403),
        ("delete_candidate", 404),
        ("move_candidate", 404),
    ],
)
async def test_upload_revalidates_live_authority_before_storing(
    client, db_session, tenant_and_key, tmp_path, monkeypatch, change, status_code
):
    tenant, api_key, key = tenant_and_key
    key_id = api_key.id
    headers = {"Authorization": f"Bearer {key}"}
    candidate = (await client.post("/api/v1/candidates", headers=headers)).json()["id"]
    reached = asyncio.Event()
    release = asyncio.Event()
    actual = LocalTextParser().parse

    class Held:
        async def parse(self, **kwargs):
            reached.set()
            await release.wait()
            return await actual(**kwargs)

    monkeypatch.setitem(app.dependency_overrides, get_document_parser, lambda: Held())
    task = asyncio.create_task(
        client.post(
            f"/api/v1/candidates/{candidate}/documents",
            headers=headers,
            files={"file": ("synthetic.pdf", VALID, "application/pdf")},
        )
    )
    actor_engine = create_async_engine(TEST_DATABASE_URL)
    try:
        await asyncio.wait_for(reached.wait(), 5)
        assert not db_session.in_transaction()
        async with async_sessionmaker(actor_engine)() as actor:
            if change in ("revoke", "expire", "scope"):
                values = (
                    {"revoked_at": datetime.now(UTC)}
                    if change == "revoke"
                    else {"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
                    if change == "expire"
                    else {"scopes": ["candidates:read"]}
                )
                await actor.execute(update(ApiKey).where(ApiKey.id == key_id).values(**values))
            elif change == "delete_candidate":
                await actor.execute(delete(Candidate).where(Candidate.id == uuid.UUID(candidate)))
            else:
                # Existing other synthetic tenant, created solely for this ownership race.
                from meyar.services.tenant_repo import create_tenant

                other = await create_tenant(actor, name="Synthetic other tenant")
                await actor.execute(
                    update(Candidate)
                    .where(Candidate.id == uuid.UUID(candidate))
                    .values(tenant_id=other.id)
                )
            await actor.commit()
        release.set()
        response = await task
        assert response.status_code == status_code
        assert await db_session.scalar(select(CandidateDocument)) is None
        assert not [p for p in (tmp_path / "storage").rglob("*") if p.is_file()]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await actor_engine.dispose()

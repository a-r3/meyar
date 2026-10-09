"""PR-2 public/persistence/reconciliation failure authority; synthetic only."""

import asyncio
import uuid

import pytest
from sqlalchemy import select
from test_parser_isolation import VALID, docx_bytes, pdf

from meyar.config import Settings, get_settings
from meyar.extraction.identity_service import (
    IdentityExtractionPreconditionError,
    extract_candidate_identity,
)
from meyar.extraction.service import ExtractionPreconditionError, extract_candidate_profile
from meyar.ingestion import validation
from meyar.ingestion.admission import AdmissionGate
from meyar.ingestion.parser import PARSE_FAILURE_MESSAGES, ParseError, ParseFailureCode
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.main import app
from meyar.models.candidate_document import CandidateDocument
from meyar.models.candidate_identity_version import CandidateIdentityVersion
from meyar.models.candidate_profile_version import CandidateProfileVersion
from meyar.models.canonical_document import CanonicalDocument
from meyar.services.candidate_document_service import ingest_candidate_document
from meyar.services.candidate_repo import create_candidate
from meyar.services.folder_indexed_file_repo import list_folder_indexed_files
from meyar.services.folder_indexer_service import index_folder
from meyar.storage.local import LocalFilesystemStorage


@pytest.mark.parametrize("mode", ["timeout", "resource", "blank", "malformed", "legacy_raw"])
async def test_upload_closed_failures_no_canonical_inference_or_lost_original(
    client, db_session, tenant_key_and_user, monkeypatch, tmp_path, mode
):
    tenant, _, key, user, password, _ = tenant_key_and_user
    headers = {"Authorization": f"Bearer {key}"}
    candidate = (await client.post("/api/v1/candidates", headers=headers)).json()["id"]
    original = asyncio.create_subprocess_exec

    async def fault(*args, **kwargs):
        if "meyar.ingestion.parser_worker" in args:
            if mode == "resource":
                return await original(
                    args[0],
                    "-c",
                    "from meyar.ingestion.parser_worker import establish_memory_limit; "
                    "establish_memory_limit();\ntry: x=bytearray(1024*1024*1024)\n"
                    "except MemoryError: raise SystemExit(73)",
                    **kwargs,
                )
        return await original(*args, **kwargs)

    from meyar.ingestion import parser_supervisor

    run = parser_supervisor.run_bounded_worker

    async def fast_timeout(data, kind, limits, seconds):
        return await run(data, kind, limits, 0.00001)

    code = {
        "timeout": ParseFailureCode.PARSER_TIMEOUT,
        "resource": ParseFailureCode.PARSER_RESOURCE_LIMIT,
        "blank": ParseFailureCode.INSUFFICIENT_EXTRACTABLE_TEXT,
        "malformed": ParseFailureCode.INVALID_DOCUMENT,
        "legacy_raw": ParseFailureCode.INVALID_DOCUMENT,
    }[mode]
    with monkeypatch.context() as patch:
        if mode == "timeout":
            patch.setattr(parser_supervisor, "run_bounded_worker", fast_timeout)
        if mode == "resource":
            patch.setattr(asyncio, "create_subprocess_exec", fault)
        if mode == "legacy_raw":

            async def legacy(self, **kwargs):
                raise ParseError("Synthetic CV text /private/secret member.xml")

            patch.setattr(LocalTextParser, "parse", legacy)
        data = (
            pdf([None])
            if mode == "blank"
            else b"%PDF-1.7 raw /secret candidate"
            if mode == "malformed"
            else VALID
        )
        upload = await client.post(
            f"/api/v1/candidates/{candidate}/documents",
            headers=headers,
            files={"file": ("synthetic.pdf", data, "application/pdf")},
        )
    if mode in ("timeout", "resource"):
        assert upload.status_code == 503
        assert upload.json()["detail"] == PARSE_FAILURE_MESSAGES[code]
        assert await db_session.scalar(select(CandidateDocument)) is None
        assert await db_session.scalar(select(CanonicalDocument)) is None
        assert not [p for p in (tmp_path / "storage").rglob("*") if p.is_file()]
        retry = await client.post(
            f"/api/v1/candidates/{candidate}/documents",
            headers=headers,
            files={"file": ("synthetic.pdf", VALID, "application/pdf")},
        )
        assert retry.status_code == 201 and retry.json()["parser_status"] == "PARSED"
        return
    assert upload.status_code == 201
    body = upload.json()
    assert body["canonical"] is None and body["parser_status"] == "PARSE_FAILED"
    assert body["parse_error_code"] == code
    assert body["parse_error_message"] == PARSE_FAILURE_MESSAGES[code]
    document = await db_session.get(CandidateDocument, uuid.UUID(body["id"]))
    assert document.parse_error_code == code
    assert document.parse_error_message == PARSE_FAILURE_MESSAGES[code]
    assert await db_session.scalar(select(CanonicalDocument)) is None
    detail = await client.get(
        f"/api/v1/candidates/{candidate}/documents/{body['id']}", headers=headers
    )
    assert detail.json()["parse_error_message"] == PARSE_FAILURE_MESSAGES[code]
    # Existing authorized original-CV route is unchanged, including failed parses.
    original_url = f"/ui/candidates/{candidate}/documents/{body['id']}/original"
    assert (await client.get(original_url)).status_code == 303
    app.dependency_overrides[get_settings] = lambda: Settings(ui_cookie_secure=False)
    login = await client.post("/ui/login", data={"username": user.username, "password": password})
    assert login.status_code == 303
    original_response = await client.get(original_url)
    assert original_response.status_code == 200 and original_response.content == data

    class NoInference:
        async def extract_candidate_profile(self, *args):
            raise AssertionError("model called")

        async def extract_candidate_identity(self, *args):
            raise AssertionError("model called")

    kwargs = dict(
        tenant_id=tenant.id,
        candidate_id=uuid.UUID(candidate),
        candidate_document=document,
        model_provider_name="synthetic",
        max_input_chars=20000,
    )
    with pytest.raises(ExtractionPreconditionError):
        await extract_candidate_profile(db_session, NoInference(), **kwargs)
    with pytest.raises(IdentityExtractionPreconditionError):
        await extract_candidate_identity(db_session, NoInference(), **kwargs)
    assert await db_session.scalar(select(CandidateProfileVersion)) is None
    assert await db_session.scalar(select(CandidateIdentityVersion)) is None


@pytest.mark.parametrize("mode", ["blank", "table", "timeout", "resource"])
async def test_folder_parse_failure_indexed_continues_and_no_version_backfill(
    db_session, tenant_and_key, tmp_path, monkeypatch, mode
):
    tenant, _, _ = tenant_and_key
    root = tmp_path / "synthetic"
    root.mkdir()
    broken = docx_bytes([], table=True) if mode == "table" else pdf([None])
    suffix = "docx" if mode == "table" else "pdf"
    (root / f"a-failure.{suffix}").write_bytes(broken)
    (root / "b-valid.pdf").write_bytes(VALID)
    parser = LocalTextParser()
    if mode in ("timeout", "resource"):
        actual_parse = parser.parse

        async def failure_once(*, data, document_type):
            if data == broken:
                raise ParseError(
                    ParseFailureCode.PARSER_TIMEOUT
                    if mode == "timeout"
                    else ParseFailureCode.PARSER_RESOURCE_LIMIT
                )
            return await actual_parse(data=data, document_type=document_type)

        monkeypatch.setattr(parser, "parse", failure_once)
    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))
    kwargs = dict(tenant_id=tenant.id, root_path=str(root), max_bytes=10 * 1024 * 1024)
    summary = await index_folder(db_session, storage, parser, **kwargs)
    rows = await list_folder_indexed_files(
        db_session, tenant_id=tenant.id, folder_source_id=summary.folder_source_id
    )
    if mode in ("timeout", "resource"):
        assert summary.successful == 1 and summary.failed == 1
        assert {row.index_status for row in rows} == {"INDEXED", "FAILED"}
        monkeypatch.setattr(parser, "parse", actual_parse)
    else:
        assert summary.successful == 2 and summary.failed == 0
        assert all(row.index_status == "INDEXED" for row in rows)
    docs = (
        await db_session.scalars(
            select(CandidateDocument).order_by(CandidateDocument.original_filename)
        )
    ).all()
    assert {d.parser_status for d in docs} == (
        {"PARSED"} if mode in ("timeout", "resource", "table") else {"PARSED", "PARSE_FAILED"}
    )
    canonical = (await db_session.scalars(select(CanonicalDocument))).all()
    expected_canonicals = 2 if mode == "table" else 1
    assert len(canonical) == expected_canonicals
    # Historical rows are immutable and unchanged files are not reparsed on version drift.
    canonical[0].parser_version = "1.0.0"
    await db_session.flush()
    previous = canonical[0].content.copy()
    again = await index_folder(db_session, storage, parser, **kwargs)
    if mode in ("timeout", "resource"):
        assert again.unchanged == 1 and again.retried == 1 and again.successful == 1
    else:
        assert again.unchanged == 2 and again.successful == 0
    assert canonical[0].parser_version == "1.0.0" and canonical[0].content == previous
    assert len((await db_session.scalars(select(CanonicalDocument))).all()) == expected_canonicals


async def test_validation_busy_before_storage_or_document_mutation(
    client, db_session, tenant_and_key, monkeypatch, tmp_path
):
    tenant, _, key = tenant_and_key
    gate = AdmissionGate(active=1, waiters=0)
    await gate.acquire()
    monkeypatch.setattr(validation, "gate", lambda **_: gate)
    candidate = await create_candidate(db_session, tenant_id=tenant.id)
    storage = LocalFilesystemStorage(root=str(tmp_path / "store"))
    with pytest.raises(ParseError):
        await ingest_candidate_document(
            db_session,
            storage,
            LocalTextParser(),
            tenant_id=tenant.id,
            candidate_id=candidate.id,
            filename="synthetic.docx",
            content_type="",
            data=docx_bytes(["Hi"]),
            max_bytes=100000,
        )
    assert not list((tmp_path / "store").rglob("*"))
    assert await db_session.scalar(select(CandidateDocument)) is None
    response = await client.post(
        f"/api/v1/candidates/{candidate.id}/documents",
        headers={"Authorization": f"Bearer {key}"},
        files={"file": ("synthetic.pdf", VALID, "application/pdf")},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == PARSE_FAILURE_MESSAGES[ParseFailureCode.PARSER_BUSY]
    gate.release()

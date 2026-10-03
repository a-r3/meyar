"""Real parser/server/DB boundaries plus synthetic provider/CLI failures."""

import ast
import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path

import httpx
import pytest
from fakes import FakeLLMProvider
from sqlalchemy import select
from test_folder_reconciliation import _process, _storage, _write_single_paragraph_docx
from test_identity_embedding_cli import _patch_session, _seed_document
from test_logging_privacy import CANARIES, UNSAFE, assert_private
from test_ops_build_release import (
    _commit_all,
    _default_request,
    _init_repo,
    _write_fixture_repo_files,
)
from test_parser_isolation import VALID
from test_ui_routes import _login_and_csrf

from meyar import cli
from meyar.config import Settings, get_settings
from meyar.embedding.ollama_provider import OllamaEmbeddingProvider
from meyar.embedding.provider import EmbeddingInvalidOutputError
from meyar.extraction.identity_service import extract_candidate_identity
from meyar.extraction.service import extract_candidate_profile
from meyar.extraction.view import ModelInputBlock, ProfessionalDocumentView
from meyar.ingestion.parser import ParseError, ParseFailureCode
from meyar.ingestion.parsers.local_text_parser import LocalTextParser
from meyar.llm.ollama_provider import OllamaLLMProvider
from meyar.llm.provider import ModelSchemaInvalidError, ModelTimeoutError, ModelUnavailableError
from meyar.main import app
from meyar.models.audit_event import AuditEvent
from meyar.ops.archive_safety import ArchiveSafetyViolation
from meyar.ops.redact import safe_exception_text
from meyar.schemas.candidate_profile import CandidateProfileExtraction, EvidenceRef, SkillItem
from meyar.services import folder_reconciliation_service
from meyar.services.folder_indexer_service import index_folder
from meyar.storage.dependency import get_document_storage

pytestmark = pytest.mark.usefixtures("enabled_diagnostic_loggers")


async def test_authentication_failures_do_not_log_api_password_cookie_or_csrf(client, caplog):
    caplog.set_level(logging.DEBUG)
    denied = await client.get(
        "/api/v1/candidates/00000000-0000-0000-0000-000000000001/detail",
        headers={"Authorization": "Bearer " + CANARIES[5]},
    )
    login = await client.post(
        "/ui/login", data={"username": CANARIES[1], "password": CANARIES[5]},
        headers={"Cookie": "meyar_session=" + CANARIES[5]},
    )
    assert denied.status_code == 401 and login.status_code == 401
    assert_private(caplog.text + denied.text + login.text)


async def test_api_validation_does_not_report_input_or_exception_context(
    client, tenant_and_key, caplog,
):
    caplog.set_level(logging.DEBUG)
    response = await client.post(
        "/api/v1/search", headers={"Authorization": "Bearer " + tenant_and_key[2]},
        json={"mode": "STRUCTURED_ONLY", "limit": UNSAFE, CANARIES[0]: UNSAFE},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid request."}
    assert_private(caplog.text + response.text)


async def test_csrf_refusal_never_logs_form_or_session_content(
    client, tenant_and_user, caplog, monkeypatch,
):
    monkeypatch.setitem(
        app.dependency_overrides, get_settings, lambda: Settings(ui_cookie_secure=False)
    )
    _, user, password, _ = tenant_and_user
    await _login_and_csrf(client, user.username, password)
    caplog.set_level(logging.DEBUG)
    response = await client.post(
        "/ui/search", data={"csrf_token": CANARIES[5], "query": UNSAFE}
    )
    assert response.status_code == 403
    assert_private(caplog.text + response.text)


@pytest.mark.parametrize("kind", ["profile", "identity"])
@pytest.mark.parametrize("error", [
    ModelUnavailableError, ModelTimeoutError, ModelSchemaInvalidError,
])
async def test_processing_failure_records_and_cli_do_not_carry_provider_payload(
    db_session, tenant_and_key, monkeypatch, capsys, caplog, kind, error,
):
    tenant_id = tenant_and_key[0].id
    candidate, document, _ = await _seed_document(db_session, tenant_id)
    candidate_id, document_id = candidate.id, document.id
    await db_session.commit()
    provider = FakeLLMProvider(error=error(UNSAFE))
    service = extract_candidate_profile if kind == "profile" else extract_candidate_identity
    version = await service(
        db_session, provider, tenant_id=tenant_id, candidate_id=candidate_id,
        candidate_document=document, model_provider_name="fake", max_input_chars=20000,
    )
    assert version.error_code == error.code
    assert_private(version.error_message)
    await db_session.commit()
    _patch_session(monkeypatch, db_session)
    monkeypatch.setattr(cli, "get_llm_provider", lambda: provider)
    command = cli._extract_profile if kind == "profile" else cli._extract_identity
    await command(str(tenant_id), str(candidate_id), str(document_id))
    streams = capsys.readouterr()
    assert_private(streams.out + streams.err + caplog.text)
    assert error.code in streams.out


async def test_candidate_evidence_failure_does_not_store_model_authored_label(
    db_session, tenant_and_key,
):
    tenant_id = tenant_and_key[0].id
    candidate, document, _ = await _seed_document(db_session, tenant_id)
    provider = FakeLLMProvider(extraction=CandidateProfileExtraction(
        skills=[SkillItem(
            name=CANARIES[3], evidence=[EvidenceRef(page=1, block_index=0, quote="Python")]
        )]
    ))
    version = await extract_candidate_profile(
        db_session, provider, tenant_id=tenant_id, candidate_id=candidate.id,
        candidate_document=document, model_provider_name="fake", max_input_chars=20000,
    )
    assert version.status == "FAILED"
    assert version.error_code == "CLAIM_EVIDENCE_UNSUPPORTED"
    assert_private(version.error_message)


async def test_storage_failure_has_generic_response_and_safe_error_log(
    client, tenant_and_key, monkeypatch, caplog,
):
    class BrokenStorage:
        async def save(self, **kwargs):
            raise OSError(UNSAFE)

    monkeypatch.setitem(app.dependency_overrides, get_document_storage, BrokenStorage)
    headers = {"Authorization": "Bearer " + tenant_and_key[2]}
    candidate = (await client.post("/api/v1/candidates", headers=headers)).json()["id"]
    response = await client.post(
        f"/api/v1/candidates/{candidate}/documents", headers=headers,
        files={"file": (CANARIES[0] + ".pdf", VALID, "application/pdf")},
    )
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error."}
    assert_private(response.text + caplog.text)
    assert "UNEXPECTED_ERROR" in caplog.text and "OSError" in caplog.text


def test_exception_projection_never_evaluates_str_repr_or_args():
    class UnprintableError(Exception):
        def __str__(self):
            raise AssertionError("str must not be evaluated")

        def __repr__(self):
            raise AssertionError("repr must not be evaluated")

    assert safe_exception_text(UnprintableError(UNSAFE)) == "UnprintableError"


def test_release_failure_finding_does_not_echo_unsafe_archive_member(tmp_path, monkeypatch):
    import importlib

    module = importlib.import_module("meyar.ops.build_release")
    repo = _init_repo(tmp_path)
    _write_fixture_repo_files(repo, include_disallowed=False)
    head = _commit_all(repo)
    output = tmp_path / "out"
    output.mkdir()
    monkeypatch.setattr(module, "inspect_archive_members", lambda *a, **kw: [
        ArchiveSafetyViolation(member_name=CANARIES[8], reason=UNSAFE)
    ])
    result = module.build_release(
        _default_request(source_sha=head, output_dir=output), invocation_cwd=repo
    )
    finding = next(item for item in result.findings if item.component == "archive_self_check")
    assert finding.code == "UNSAFE_ARCHIVE_MEMBER"
    assert_private(result.model_dump_json())


@pytest.mark.parametrize("method", ["profile", "identity", "planner"])
@pytest.mark.parametrize("output", ["schema", "json"])
async def test_malformed_model_output_has_closed_error_without_validation_input(
    caplog, method, output,
):
    content = json.dumps({"unexpected": UNSAFE}) if output == "schema" else UNSAFE
    provider = OllamaLLMProvider(
        base_url="http://127.0.0.1:11434", model="synthetic-s2", timeout_seconds=1,
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"message": {"content": content}}
        )),
    )
    view = ProfessionalDocumentView(
        canonical_document_id=uuid.uuid4(),
        blocks=[ModelInputBlock(page=1, block_index=0, text=UNSAFE)],
    )
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ModelSchemaInvalidError) as caught:
        if method == "profile":
            await provider.extract_candidate_profile(view)
        elif method == "identity":
            await provider.extract_candidate_identity(view)
        else:
            await provider.plan_candidate_search(UNSAFE)
    assert_private(caplog.text + "".join(traceback.format_exception(caught.value)))
    assert caught.value.code == "MODEL_SCHEMA_INVALID"


@pytest.mark.parametrize("provider", ["llm", "embedding", "llm-envelope"])
async def test_invalid_provider_http_json_has_safe_typed_error(caplog, provider):
    def invalid(request):
        if provider == "llm-envelope":
            return httpx.Response(200, json={"model": UNSAFE, "message": {"content": UNSAFE}})
        return httpx.Response(200, text=UNSAFE)

    options = dict(
        base_url="http://127.0.0.1:11434", model="synthetic-s2", timeout_seconds=1,
        transport=httpx.MockTransport(invalid),
    )
    caplog.set_level(logging.DEBUG)
    expected = EmbeddingInvalidOutputError if provider == "embedding" else ModelSchemaInvalidError
    with pytest.raises(expected) as caught:
        if provider != "embedding":
            await OllamaLLMProvider(**options)._chat(
                system_prompt=UNSAFE, user_prompt=UNSAFE, schema={}
            )
        else:
            await OllamaEmbeddingProvider(**options).embed(UNSAFE)
    assert_private(caplog.text + "".join(traceback.format_exception(caught.value)))


async def test_parser_unsafe_stderr_and_failure_payload_are_discarded(monkeypatch, caplog, capfd):
    original = asyncio.create_subprocess_exec

    async def unsafe_worker(*args, **kwargs):
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        # A real child writes a sensitive diagnostic and an unrecognized failure.
        script = (
            "import sys; sys.stdin.buffer.read(); "
            f"sys.stderr.write({UNSAFE!r}); "
            f"sys.stdout.write({json.dumps({'error': UNSAFE})!r})"
        )
        return await original(args[0], "-c", script, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unsafe_worker)
    with pytest.raises(ParseError) as caught:
        await LocalTextParser().parse(data=UNSAFE.encode(), document_type="PDF")
    assert caught.value.code == ParseFailureCode.INVALID_PARSER_OUTPUT
    streams = capfd.readouterr()
    assert_private(caplog.text + streams.out + streams.err + str(caught.value))


async def test_real_malformed_parser_document_never_logs_candidate_text(caplog, capfd):
    with pytest.raises(ParseError) as caught:
        await LocalTextParser().parse(data=b"%PDF-1.7 " + UNSAFE.encode(), document_type="PDF")
    assert caught.value.code == ParseFailureCode.INVALID_DOCUMENT
    streams = capfd.readouterr()
    assert_private(caplog.text + streams.out + streams.err + str(caught.value))


async def test_folder_unexpected_failure_logs_only_structural_diagnostic(
    db_session, tenant_and_key, tmp_path, monkeypatch, caplog,
):
    tenant_id = tenant_and_key[0].id
    root = tmp_path / "synthetic-folder"
    _write_single_paragraph_docx(root / (CANARIES[0] + ".docx"), UNSAFE)
    scan = await index_folder(
        db_session, _storage(tmp_path), LocalTextParser(), tenant_id=tenant_id,
        root_path=str(root), max_bytes=1024 * 1024, stability_window_seconds=0,
    )
    await db_session.commit()

    async def unexpected(*args, **kwargs):
        raise RuntimeError(UNSAFE)

    monkeypatch.setattr(
        folder_reconciliation_service, "_process_one_candidate_document", unexpected
    )
    caplog.set_level(logging.ERROR)
    result = await _process(db_session, tmp_path, tenant_id, scan.folder_source_id)
    assert result.failed == 1
    assert_private(caplog.text)
    assert "folder_reconciliation" in caplog.text
    assert "CANDIDATE_PROCESSING_FAILED" in caplog.text and "RuntimeError" in caplog.text
    event = await db_session.scalar(select(AuditEvent).where(
        AuditEvent.event_type == "FOLDER_RECONCILE_CANDIDATE_FAILED"
    ))
    assert event.event_metadata["error_type"] == "RuntimeError"
    assert_private(json.dumps(event.event_metadata))


async def test_cli_invalid_search_request_reports_no_input_or_path(tmp_path, capsys):
    request_file = tmp_path / (CANARIES[0] + ".json")
    request_file.write_text(json.dumps({"mode": "STRUCTURED_ONLY", "limit": UNSAFE}))
    with pytest.raises(SystemExit) as caught:
        await cli._search_candidates(str(uuid.uuid4()), str(request_file))
    assert caught.value.code == 2
    streams = capsys.readouterr()
    assert_private(streams.out + streams.err)
    assert "REQUEST_INVALID" in streams.out


def test_cli_unexpected_exception_has_closed_diagnostic(monkeypatch, capsys):
    def unsafe_settings():
        raise ValueError(UNSAFE)

    monkeypatch.setattr(cli, "get_settings", unsafe_settings)
    monkeypatch.setattr(sys, "argv", ["meyar", "create-tenant", "--name", "synthetic-s2"])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert_private(output.out + output.err)
    assert "component=cli code=UNEXPECTED_ERROR error_type=ValueError" in output.out


def test_cli_argument_errors_do_not_echo_tokens_or_query(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["meyar", "--unknown", UNSAFE])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert_private(output.out + output.err)
    assert "CLI_ARGUMENTS_INVALID" in output.err


def test_all_production_engine_constructors_hide_bound_parameters():
    backend = Path(__file__).resolve().parents[1]
    sources = list((backend / "src").rglob("*.py")) + [backend / "alembic/env.py"]
    constructors = []
    for source in sources:
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in (
                    "create_async_engine", "async_engine_from_config", "create_engine",
                ):
                    constructors.append(source)
                    assert any(
                        kw.arg == "hide_parameters" and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True for kw in node.keywords
                    ), str(source)
    assert len(constructors) == 7


def test_real_uvicorn_failure_and_access_logs_are_private(tmp_path):
    # Imported real app, real ASGI stack, default Uvicorn access/error formatters.
    source = tmp_path / "synthetic_s2_app.py"
    source.write_text(
        "from meyar.main import app\n"
        "@app.get('/synthetic-failure')\n"
        "async def failure():\n"
        f"    raise RuntimeError({UNSAFE!r})\n"
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.set_inheritable(True)
        port = listener.getsockname()[1]
        environment = {**os.environ, "PYTHONPATH": str(tmp_path)}
        log_file = tmp_path / "synthetic-server.log"
        with log_file.open("w+") as logs:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "synthetic_s2_app:app", "--fd",
                 str(listener.fileno()), "--log-level", "debug", "--lifespan", "off"],
                env=environment, pass_fds=(listener.fileno(),), stdout=logs, stderr=logs,
            )
            try:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        response = httpx.get(
                            f"http://127.0.0.1:{port}/synthetic-failure",
                            params={"query": UNSAFE},
                            headers={"Authorization": "Bearer " + CANARIES[5]},
                            timeout=0.5, trust_env=False,
                        )
                        break
                    except httpx.TransportError:
                        assert process.poll() is None
                        assert time.monotonic() < deadline
                assert response.status_code == 500
                assert response.json() == {"detail": "Internal server error."}
                assert_private(response.text)
            finally:
                process.terminate()
                process.wait(timeout=10)
            logs.seek(0)
            output = logs.read()
    assert_private(output)
    assert "UNEXPECTED_ERROR" in output and "RuntimeError" in output
    assert '"GET private HTTP/1.1" 500' in output
    assert "Traceback" not in output

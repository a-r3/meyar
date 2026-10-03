"""Issue #46 S2: synthetic diagnostic canaries, never real candidate data."""

import json
import logging
import traceback

import httpx
import pytest
from conftest import TEST_DATABASE_URL
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from meyar.config import Settings
from meyar.db import get_db, make_engine
from meyar.embedding.ollama_provider import OllamaEmbeddingProvider
from meyar.llm.ollama_provider import OllamaLLMProvider
from meyar.main import app
from meyar.ops import cli as ops_cli
from meyar.ops.redact import safe_exception_text

pytestmark = pytest.mark.usefixtures("enabled_diagnostic_loggers")

CANARIES = (
    "SYNTHETIC_NAME_S2_Aria",
    "synthetic.s2@example.invalid",
    "+000-SYNTHETIC-PHONE-S2",
    "SYNTHETIC_CV_TEXT_S2",
    "SYNTHETIC_HR_QUERY_JD_S2",
    "SYNTHETIC_API_COOKIE_CSRF_PASSWORD_S2",
    "SYNTHETIC_DB_PASSWORD_S2",
    "SYNTHETIC_MODEL_OUTPUT_S2",
    "/synthetic/operator/SYNTHETIC_PRIVATE_PATH_S2/candidate.pdf",
)
UNSAFE = " | ".join(CANARIES)


def assert_private(output):
    for canary in CANARIES:
        assert canary not in output


async def test_real_sql_logs_and_driver_exception_do_not_emit_bound_candidate_values(
    caplog, monkeypatch,
):
    import meyar.db as database

    settings = Settings(
        _env_file=None, env="production", pending_login_secret=CANARIES[5],
        database_url=f"postgresql+asyncpg://synthetic:{CANARIES[6]}@127.0.0.1:5432/synthetic",
        ollama_model="synthetic-s2:v1", ollama_embedding_model="synthetic-s2-embed:v1",
    )
    monkeypatch.setattr(database, "get_settings", lambda: settings)
    # Real production settings/pool policy, disposable test DB credentials.
    engine = make_engine(TEST_DATABASE_URL)
    caplog.set_level(logging.DEBUG, logger="sqlalchemy.engine")
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT :private"), {"private": UNSAFE})
            assert result.scalar_one() == UNSAFE
            with pytest.raises(DBAPIError) as caught:
                await connection.execute(
                    text("SELECT CAST(:private AS INTEGER)"), {"private": UNSAFE}
                )
        assert_private(caplog.text + safe_exception_text(caught.value))
        assert engine.sync_engine.hide_parameters is True
        assert "database" in caplog.text
    finally:
        await engine.dispose()


@pytest.mark.parametrize("path", [
    "/ui", "/api/v1/candidates/00000000-0000-0000-0000-000000000001/detail",
])
async def test_global_failure_has_safe_response_and_structural_log(monkeypatch, caplog, path):
    async def broken_db():
        raise RuntimeError(UNSAFE)
        yield  # noqa: B018

    monkeypatch.setitem(app.dependency_overrides, get_db, broken_db)
    caplog.set_level(logging.ERROR)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.get(path, headers={"Authorization": "Bearer " + CANARIES[5]})
    assert response.status_code == 500
    assert_private(response.text + caplog.text)
    assert "RuntimeError" in caplog.text
    assert "UNEXPECTED_ERROR" in caplog.text


def test_ops_uncaught_failure_does_not_render_exception_payload(monkeypatch, capsys):
    def broken():
        raise RuntimeError(UNSAFE)

    monkeypatch.setattr(ops_cli, "run_preflight", broken)
    with pytest.raises(SystemExit) as caught:
        ops_cli.main(["preflight"])
    assert caught.value.code == 3
    output = capsys.readouterr()
    assert_private(output.out + output.err)
    finding = json.loads(output.out)["findings"][0]
    assert finding["component"] == "preflight"
    assert finding["code"] == "UNCAUGHT_EXCEPTION"
    assert "RuntimeError" in finding["message"]


@pytest.mark.parametrize("provider", ["llm", "embedding"])
@pytest.mark.parametrize("failure", ["connect", "timeout"])
async def test_provider_failure_diagnostics_exclude_prompt_response_and_cause(
    caplog, provider, failure,
):
    def fail(request):
        error = httpx.ConnectError if failure == "connect" else httpx.ReadTimeout
        raise error(UNSAFE, request=request)

    options = dict(base_url="http://127.0.0.1:11434", model="synthetic-s2", timeout_seconds=1,
                   transport=httpx.MockTransport(fail))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(Exception) as caught:
        if provider == "llm":
            await OllamaLLMProvider(**options)._chat(
                system_prompt=UNSAFE, user_prompt=UNSAFE, schema={}
            )
        else:
            await OllamaEmbeddingProvider(**options).embed(UNSAFE)
    diagnostic = "".join(traceback.format_exception(caught.value))
    assert_private(caplog.text + diagnostic)
    assert "Ollama" in str(caught.value)


@pytest.mark.parametrize("logger_name", ["sqlalchemy.pool", "uvicorn.error", "httpx", "httpcore"])
def test_library_diagnostics_cannot_emit_raw_payloads_even_at_debug(caplog, logger_name):
    caplog.set_level(logging.DEBUG)
    try:
        raise ValueError(UNSAFE)
    except ValueError:
        logging.getLogger(logger_name).error("failure %s", UNSAFE, exc_info=True, stack_info=True)
    assert_private(caplog.text)
    assert "ValueError" in caplog.text


def test_access_log_does_not_emit_request_target_or_client_identity(caplog):
    caplog.set_level(logging.INFO)
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', CANARIES[0], "POST", "/?query=" + UNSAFE, "1.1", 401
    )
    assert_private(caplog.text)
    assert "401" in caplog.text and "POST" in caplog.text

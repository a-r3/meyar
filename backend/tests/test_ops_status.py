"""`meyar-ops status` (issue #35 PR1 §5/§12). Unreachable dependencies
must never be silently reported as healthy."""

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from conftest import TEST_DATABASE_URL
from fakes import FakeEmbeddingProvider, FakeLLMProvider
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import meyar.ops.status as status_module
from meyar.config import Settings
from meyar.ops.alembic_introspect import get_code_alembic_heads
from meyar.ops.config import resolve_alembic_ini_path
from meyar.ops.release_manifest import compute_release_id
from meyar.ops.result import FindingStatus


@asynccontextmanager
async def _alembic_version_row(revision: str):
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
        )
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": revision}
        )
    try:
        yield
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await engine.dispose()


def _by_component(result, component: str):
    matches = [f for f in result.findings if f.component == component]
    assert matches, f"no finding for component {component!r}"
    return matches[0]


def _real_code_head() -> str:
    ini_path = resolve_alembic_ini_path()
    assert ini_path is not None
    heads = get_code_alembic_heads(ini_path)
    assert len(heads) == 1
    return heads[0]


async def test_status_returns_findings_for_every_documented_component() -> None:
    result = await status_module.run_status()
    components = {f.component for f in result.findings}
    assert components == {
        "package_version",
        "release_identity",
        "python_version",
        "platform",
        "code_alembic_head",
        "db_current_revision",
        "storage_root_accessible",
        "ollama_reachability",
        "configured_llm_identity",
        "configured_embedding_identity",
    }


async def test_database_unavailable_is_a_fail_not_silently_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unreachable = Settings(database_url="postgresql+asyncpg://meyar:x@127.0.0.1:1/meyar")
    monkeypatch.setattr(status_module, "get_settings", lambda: unreachable)
    monkeypatch.setattr(status_module, "get_llm_provider", lambda: FakeLLMProvider())
    monkeypatch.setattr(status_module, "get_embedding_provider", lambda: FakeEmbeddingProvider())

    result = await status_module.run_status()
    finding = _by_component(result, "db_current_revision")
    assert finding.status is FindingStatus.FAIL
    assert finding.code == "DATABASE_UNREACHABLE"
    assert result.ok is False


async def test_no_db_revision_recorded_is_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL)
    monkeypatch.setattr(status_module, "get_settings", lambda: settings)
    monkeypatch.setattr(status_module, "get_llm_provider", lambda: FakeLLMProvider())
    monkeypatch.setattr(status_module, "get_embedding_provider", lambda: FakeEmbeddingProvider())

    result = await status_module.run_status()
    finding = _by_component(result, "db_current_revision")
    assert finding.status is FindingStatus.WARN
    assert finding.code == "NO_DB_REVISION"


async def test_db_revision_matching_code_head_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL)
    monkeypatch.setattr(status_module, "get_settings", lambda: settings)
    monkeypatch.setattr(status_module, "get_llm_provider", lambda: FakeLLMProvider())
    monkeypatch.setattr(status_module, "get_embedding_provider", lambda: FakeEmbeddingProvider())

    async with _alembic_version_row(_real_code_head()):
        result = await status_module.run_status()
    finding = _by_component(result, "db_current_revision")
    assert finding.status is FindingStatus.OK
    assert finding.code == "DB_REVISION_CURRENT"


async def test_stale_db_revision_is_warn_schema_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL)
    monkeypatch.setattr(status_module, "get_settings", lambda: settings)
    monkeypatch.setattr(status_module, "get_llm_provider", lambda: FakeLLMProvider())
    monkeypatch.setattr(status_module, "get_embedding_provider", lambda: FakeEmbeddingProvider())

    async with _alembic_version_row("0" * 12):
        result = await status_module.run_status()
    finding = _by_component(result, "db_current_revision")
    assert finding.status is FindingStatus.WARN
    assert finding.code == "DB_REVISION_STALE"


async def test_ollama_unreachable_is_a_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL)
    monkeypatch.setattr(status_module, "get_settings", lambda: settings)
    unreachable_llm = FakeLLMProvider(
        health_result={"reachable": False, "model": "x", "model_available": False}
    )
    monkeypatch.setattr(status_module, "get_llm_provider", lambda: unreachable_llm)
    monkeypatch.setattr(status_module, "get_embedding_provider", lambda: FakeEmbeddingProvider())

    result = await status_module.run_status()
    finding = _by_component(result, "ollama_reachability")
    assert finding.status is FindingStatus.FAIL
    assert result.ok is False


async def test_configured_model_unavailable_is_warn_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL)
    monkeypatch.setattr(status_module, "get_settings", lambda: settings)
    unavailable_llm = FakeLLMProvider(
        health_result={"reachable": True, "model": "x", "model_available": False}
    )
    monkeypatch.setattr(status_module, "get_llm_provider", lambda: unavailable_llm)
    monkeypatch.setattr(status_module, "get_embedding_provider", lambda: FakeEmbeddingProvider())

    result = await status_module.run_status()
    finding = _by_component(result, "configured_llm_identity")
    assert finding.status is FindingStatus.WARN
    assert finding.code == "LLM_MODEL_UNAVAILABLE"


async def test_release_identity_reported_when_manifest_provided(tmp_path: Path) -> None:
    sha = "a" * 40
    manifest = {
        "release_version": "0.1.0",
        "release_id": compute_release_id(release_version="0.1.0", source_sha=sha),
        "source_sha": sha,
        "built_at": "2026-09-23T00:00:00Z",
        "required_python_version": ">=3.12",
        "uv_lock_sha256": "b" * 64,
        "alembic_heads": ["6f4c2a9d8e10"],
        "rollback_compatibility": "BACKUP_RESTORE_REQUIRED",
        "model_manifest": {"reference": "x", "status": "DEVELOPMENT_INTEGRATION"},
        "artifact_format": "tar.gz",
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))

    result = await status_module.run_status(manifest_path=manifest_path)
    finding = _by_component(result, "release_identity")
    assert finding.status is FindingStatus.OK
    assert manifest["release_id"] in finding.message


async def test_release_identity_invalid_manifest_fails(tmp_path: Path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{not valid json")
    result = await status_module.run_status(manifest_path=manifest_path)
    finding = _by_component(result, "release_identity")
    assert finding.status is FindingStatus.FAIL


async def test_no_manifest_provided_is_skipped_not_failed() -> None:
    result = await status_module.run_status(manifest_path=None)
    finding = _by_component(result, "release_identity")
    assert finding.status is FindingStatus.SKIPPED


async def test_status_never_exposes_database_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unreachable = Settings(
        database_url="postgresql+asyncpg://meyar:super-secret-password@127.0.0.1:1/meyar"
    )
    monkeypatch.setattr(status_module, "get_settings", lambda: unreachable)
    monkeypatch.setattr(status_module, "get_llm_provider", lambda: FakeLLMProvider())
    monkeypatch.setattr(status_module, "get_embedding_provider", lambda: FakeEmbeddingProvider())

    result = await status_module.run_status()
    payload = result.model_dump_json()
    assert "super-secret-password" not in payload


# ---------------------------------------------------------------------
# Corrective review (PR #52, Blocker 3) — apply the same provider-health
# exception isolation to `status`: one broken provider must not discard
# the package/version/platform/DB findings already collected.
# ---------------------------------------------------------------------

_ALL_STATUS_COMPONENTS = {
    "package_version",
    "release_identity",
    "python_version",
    "platform",
    "code_alembic_head",
    "db_current_revision",
    "storage_root_accessible",
    "ollama_reachability",
    "configured_llm_identity",
    "configured_embedding_identity",
}


class _CrashingHealthProvider:
    async def health(self) -> dict:
        raise RuntimeError("simulated provider health check crash")


def _status_by_component(result, component: str):
    matches = [f for f in result.findings if f.component == component]
    assert matches, f"no finding for component {component!r}"
    return matches[0]


async def test_llm_health_raise_does_not_discard_earlier_status_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL)
    monkeypatch.setattr(status_module, "get_settings", lambda: settings)
    monkeypatch.setattr(status_module, "get_llm_provider", lambda: _CrashingHealthProvider())
    monkeypatch.setattr(status_module, "get_embedding_provider", lambda: FakeEmbeddingProvider())

    result = await status_module.run_status()  # must not raise

    assert {f.component for f in result.findings} == _ALL_STATUS_COMPONENTS
    assert _status_by_component(result, "ollama_reachability").status is FindingStatus.FAIL
    assert _status_by_component(result, "configured_llm_identity").status is FindingStatus.WARN
    # earlier findings survive
    assert _status_by_component(result, "package_version").status is FindingStatus.OK
    assert _status_by_component(result, "platform").status is FindingStatus.OK
    assert result.ok is False


async def test_embedding_health_raise_does_not_discard_llm_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL)
    monkeypatch.setattr(status_module, "get_settings", lambda: settings)
    monkeypatch.setattr(status_module, "get_llm_provider", lambda: FakeLLMProvider())
    monkeypatch.setattr(
        status_module, "get_embedding_provider", lambda: _CrashingHealthProvider()
    )

    result = await status_module.run_status()  # must not raise

    assert {f.component for f in result.findings} == _ALL_STATUS_COMPONENTS
    assert (
        _status_by_component(result, "configured_embedding_identity").status
        is FindingStatus.FAIL
    )
    # the independent LLM provider check still ran and succeeded
    assert _status_by_component(result, "ollama_reachability").status is FindingStatus.OK
    assert result.ok is False

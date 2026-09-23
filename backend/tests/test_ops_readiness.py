"""`meyar-ops readiness` (issue #35 PR1 §6/§12). Every component finding
survives even when another component fails, and readiness never reads
candidate data."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from conftest import TEST_DATABASE_URL
from fakes import FakeEmbeddingProvider, FakeLLMProvider
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import meyar.ops.readiness as readiness_module
from meyar.config import Settings
from meyar.ops.alembic_introspect import AlembicRevisionQueryError, get_code_alembic_heads
from meyar.ops.config import resolve_alembic_ini_path
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


def _patch_healthy_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(readiness_module, "get_llm_provider", lambda: FakeLLMProvider())
    monkeypatch.setattr(
        readiness_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )


async def test_readiness_returns_findings_for_every_documented_component(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL, storage_root=str(tmp_path))
    monkeypatch.setattr(readiness_module, "get_settings", lambda: settings)
    _patch_healthy_providers(monkeypatch)

    async with _alembic_version_row(_real_code_head()):
        result = await readiness_module.run_readiness()
    assert {f.component for f in result.findings} == {
        "database",
        "db_migration",
        "storage_write",
        "ollama",
        "llm_model",
        "embedding_model",
    }


async def test_happy_path_is_fully_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL, storage_root=str(tmp_path))
    monkeypatch.setattr(readiness_module, "get_settings", lambda: settings)
    _patch_healthy_providers(monkeypatch)

    async with _alembic_version_row(_real_code_head()):
        result = await readiness_module.run_readiness()
    assert result.ok is True
    assert all(f.status is FindingStatus.OK for f in result.findings)


async def test_database_unavailable_fails_and_skips_migration_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unreachable = Settings(
        database_url="postgresql+asyncpg://meyar:x@127.0.0.1:1/meyar", storage_root=str(tmp_path)
    )
    monkeypatch.setattr(readiness_module, "get_settings", lambda: unreachable)
    _patch_healthy_providers(monkeypatch)

    result = await readiness_module.run_readiness()
    database_finding = _by_component(result, "database")
    migration_finding = _by_component(result, "db_migration")
    assert database_finding.status is FindingStatus.FAIL
    assert database_finding.code == "DATABASE_UNREACHABLE"
    assert migration_finding.status is FindingStatus.SKIPPED
    assert result.ok is False
    # every other component finding still present despite the DB failure
    assert {f.component for f in result.findings} == {
        "database",
        "db_migration",
        "storage_write",
        "ollama",
        "llm_model",
        "embedding_model",
    }


async def test_schema_mismatch_fails_readiness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL, storage_root=str(tmp_path))
    monkeypatch.setattr(readiness_module, "get_settings", lambda: settings)
    _patch_healthy_providers(monkeypatch)

    async with _alembic_version_row("0" * 12):  # deliberately wrong revision
        result = await readiness_module.run_readiness()
    finding = _by_component(result, "db_migration")
    assert finding.status is FindingStatus.FAIL
    assert finding.code == "SCHEMA_MISMATCH"
    assert result.ok is False


async def test_no_db_revision_yet_fails_migration_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL, storage_root=str(tmp_path))
    monkeypatch.setattr(readiness_module, "get_settings", lambda: settings)
    _patch_healthy_providers(monkeypatch)

    result = await readiness_module.run_readiness()
    finding = _by_component(result, "db_migration")
    assert finding.status is FindingStatus.FAIL
    assert finding.code == "NO_DB_REVISION"


async def test_ollama_unavailable_fails_readiness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL, storage_root=str(tmp_path))
    monkeypatch.setattr(readiness_module, "get_settings", lambda: settings)
    unreachable_llm = FakeLLMProvider(
        health_result={"reachable": False, "model": "x", "model_available": False}
    )
    monkeypatch.setattr(readiness_module, "get_llm_provider", lambda: unreachable_llm)
    monkeypatch.setattr(
        readiness_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )

    async with _alembic_version_row(_real_code_head()):
        result = await readiness_module.run_readiness()
    ollama_finding = _by_component(result, "ollama")
    llm_finding = _by_component(result, "llm_model")
    assert ollama_finding.status is FindingStatus.FAIL
    assert llm_finding.status is FindingStatus.FAIL
    assert result.ok is False


async def test_llm_model_unavailable_fails_readiness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL, storage_root=str(tmp_path))
    monkeypatch.setattr(readiness_module, "get_settings", lambda: settings)
    unavailable_llm = FakeLLMProvider(
        health_result={"reachable": True, "model": "x", "model_available": False}
    )
    monkeypatch.setattr(readiness_module, "get_llm_provider", lambda: unavailable_llm)
    monkeypatch.setattr(
        readiness_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )

    async with _alembic_version_row(_real_code_head()):
        result = await readiness_module.run_readiness()
    finding = _by_component(result, "llm_model")
    assert finding.status is FindingStatus.FAIL
    assert finding.code == "LLM_MODEL_UNAVAILABLE"


async def test_embedding_model_unavailable_fails_readiness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL, storage_root=str(tmp_path))
    monkeypatch.setattr(readiness_module, "get_settings", lambda: settings)
    monkeypatch.setattr(readiness_module, "get_llm_provider", lambda: FakeLLMProvider())
    unavailable_embedding = FakeEmbeddingProvider(
        health_result={"reachable": True, "model": "x", "model_available": False}
    )
    monkeypatch.setattr(
        readiness_module, "get_embedding_provider", lambda: unavailable_embedding
    )

    async with _alembic_version_row(_real_code_head()):
        result = await readiness_module.run_readiness()
    finding = _by_component(result, "embedding_model")
    assert finding.status is FindingStatus.FAIL
    assert finding.code == "EMBEDDING_MODEL_UNAVAILABLE"


async def test_storage_unwritable_fails_readiness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "readonly"
    root.mkdir()
    os.chmod(root, 0o500)
    try:
        settings = Settings(database_url=TEST_DATABASE_URL, storage_root=str(root))
        monkeypatch.setattr(readiness_module, "get_settings", lambda: settings)
        _patch_healthy_providers(monkeypatch)

        async with _alembic_version_row(_real_code_head()):
            result = await readiness_module.run_readiness()
        finding = _by_component(result, "storage_write")
        assert finding.status is FindingStatus.FAIL
        assert finding.code == "STORAGE_NOT_WRITABLE"
    finally:
        os.chmod(root, 0o700)


async def test_storage_probe_leaves_no_temp_file_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    settings = Settings(database_url=TEST_DATABASE_URL, storage_root=str(root))
    monkeypatch.setattr(readiness_module, "get_settings", lambda: settings)
    _patch_healthy_providers(monkeypatch)

    async with _alembic_version_row(_real_code_head()):
        result = await readiness_module.run_readiness()
    finding = _by_component(result, "storage_write")
    assert finding.status is FindingStatus.OK
    assert list(root.iterdir()) == []


async def test_missing_storage_root_fails_readiness_and_is_never_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Readiness must not provision storage: a never-provisioned root
    fails truthfully, and neither it nor any parent directory is created
    as a side effect of running readiness."""
    root = tmp_path / "not" / "provisioned" / "yet"
    settings = Settings(database_url=TEST_DATABASE_URL, storage_root=str(root))
    monkeypatch.setattr(readiness_module, "get_settings", lambda: settings)
    _patch_healthy_providers(monkeypatch)

    async with _alembic_version_row(_real_code_head()):
        result = await readiness_module.run_readiness()
    finding = _by_component(result, "storage_write")
    assert finding.status is FindingStatus.FAIL
    assert finding.code == "STORAGE_NOT_WRITABLE"
    assert not root.exists()
    assert not root.parent.exists()
    assert result.ok is False


async def test_multiple_db_revisions_fails_migration_check_with_distinct_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL, storage_root=str(tmp_path))
    monkeypatch.setattr(readiness_module, "get_settings", lambda: settings)
    _patch_healthy_providers(monkeypatch)

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
            text("INSERT INTO alembic_version (version_num) VALUES (:r)"),
            {"r": _real_code_head()},
        )
        await conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": "0" * 12}
        )
    try:
        result = await readiness_module.run_readiness()
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await engine.dispose()

    finding = _by_component(result, "db_migration")
    assert finding.status is FindingStatus.FAIL
    assert finding.code == "MULTIPLE_DB_REVISIONS"
    assert result.ok is False


async def test_alembic_revision_query_failure_is_distinct_from_database_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A genuine query/permission failure reading alembic_version must
    never be reported as DATABASE_UNREACHABLE (connectivity was fine) or
    as NO_DB_REVISION (that would hide a real failure as an expected
    pre-migration state)."""
    settings = Settings(database_url=TEST_DATABASE_URL, storage_root=str(tmp_path))
    monkeypatch.setattr(readiness_module, "get_settings", lambda: settings)
    _patch_healthy_providers(monkeypatch)

    async def _boom(engine: object) -> None:
        raise AlembicRevisionQueryError("simulated permission-denied reading alembic_version")

    monkeypatch.setattr(readiness_module, "get_db_alembic_revision", _boom)

    result = await readiness_module.run_readiness()
    finding = _by_component(result, "db_migration")
    assert finding.status is FindingStatus.FAIL
    assert finding.code == "ALEMBIC_REVISION_QUERY_FAILED"
    database_finding = _by_component(result, "database")
    assert database_finding.status is FindingStatus.OK
    assert result.ok is False


async def test_readiness_never_exposes_database_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    unreachable = Settings(
        database_url="postgresql+asyncpg://meyar:super-secret-password@127.0.0.1:1/meyar",
        storage_root=str(tmp_path),
    )
    monkeypatch.setattr(readiness_module, "get_settings", lambda: unreachable)
    _patch_healthy_providers(monkeypatch)

    result = await readiness_module.run_readiness()
    payload = result.model_dump_json()
    assert "super-secret-password" not in payload


# ---------------------------------------------------------------------
# Corrective review (PR #52, Blocker 3) — readiness component exception
# isolation. A non-normalized exception from provider construction or
# `.health()` must never escape `run_readiness()`: doing so would drop
# every already-collected finding (database/db_migration/storage_write)
# and surface only a generic UNCAUGHT_EXCEPTION at the CLI boundary.
# ---------------------------------------------------------------------

_ALL_READINESS_COMPONENTS = {
    "database",
    "db_migration",
    "storage_write",
    "ollama",
    "llm_model",
    "embedding_model",
}


class _CrashingHealthProvider:
    """A provider whose `.health()` raises a non-normalized exception —
    simulating a malformed local Ollama response or an unexpected
    provider-internal failure, not a documented reachability outcome."""

    async def health(self) -> dict:
        raise RuntimeError("simulated provider health check crash")


def _raise_on_construction() -> None:
    raise RuntimeError("simulated provider construction failure")


async def test_llm_health_raise_is_isolated_and_embedding_still_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL, storage_root=str(tmp_path))
    monkeypatch.setattr(readiness_module, "get_settings", lambda: settings)
    monkeypatch.setattr(readiness_module, "get_llm_provider", lambda: _CrashingHealthProvider())
    embedding = FakeEmbeddingProvider()
    monkeypatch.setattr(readiness_module, "get_embedding_provider", lambda: embedding)

    async with _alembic_version_row(_real_code_head()):
        result = await readiness_module.run_readiness()  # must not raise

    assert {f.component for f in result.findings} == _ALL_READINESS_COMPONENTS
    assert _by_component(result, "ollama").status is FindingStatus.FAIL
    assert _by_component(result, "llm_model").status is FindingStatus.SKIPPED
    # the independent embedding provider still ran and succeeded
    assert _by_component(result, "embedding_model").status is FindingStatus.OK
    # already-collected findings from earlier components survive
    assert _by_component(result, "database").status is FindingStatus.OK
    assert _by_component(result, "db_migration").status is FindingStatus.OK
    assert _by_component(result, "storage_write").status is FindingStatus.OK
    assert result.ok is False


async def test_llm_provider_construction_raise_is_isolated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL, storage_root=str(tmp_path))
    monkeypatch.setattr(readiness_module, "get_settings", lambda: settings)
    monkeypatch.setattr(readiness_module, "get_llm_provider", _raise_on_construction)
    monkeypatch.setattr(
        readiness_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )

    async with _alembic_version_row(_real_code_head()):
        result = await readiness_module.run_readiness()  # must not raise

    assert {f.component for f in result.findings} == _ALL_READINESS_COMPONENTS
    assert _by_component(result, "ollama").status is FindingStatus.FAIL
    assert _by_component(result, "llm_model").status is FindingStatus.SKIPPED
    assert _by_component(result, "embedding_model").status is FindingStatus.OK
    assert result.ok is False


async def test_embedding_health_raise_is_isolated_and_does_not_discard_llm_findings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(database_url=TEST_DATABASE_URL, storage_root=str(tmp_path))
    monkeypatch.setattr(readiness_module, "get_settings", lambda: settings)
    monkeypatch.setattr(readiness_module, "get_llm_provider", lambda: FakeLLMProvider())
    monkeypatch.setattr(
        readiness_module, "get_embedding_provider", lambda: _CrashingHealthProvider()
    )

    async with _alembic_version_row(_real_code_head()):
        result = await readiness_module.run_readiness()  # must not raise

    assert {f.component for f in result.findings} == _ALL_READINESS_COMPONENTS
    assert _by_component(result, "embedding_model").status is FindingStatus.FAIL
    # the independent LLM provider still ran and succeeded
    assert _by_component(result, "ollama").status is FindingStatus.OK
    assert _by_component(result, "llm_model").status is FindingStatus.OK
    assert _by_component(result, "database").status is FindingStatus.OK
    assert _by_component(result, "db_migration").status is FindingStatus.OK
    assert _by_component(result, "storage_write").status is FindingStatus.OK
    assert result.ok is False


async def test_provider_health_exception_text_is_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _LeakyProvider:
        async def health(self) -> dict:
            raise RuntimeError(
                "upstream call to postgresql+asyncpg://meyar:supersecret@127.0.0.1/meyar failed"
            )

    settings = Settings(database_url=TEST_DATABASE_URL, storage_root=str(tmp_path))
    monkeypatch.setattr(readiness_module, "get_settings", lambda: settings)
    monkeypatch.setattr(readiness_module, "get_llm_provider", lambda: _LeakyProvider())
    monkeypatch.setattr(
        readiness_module, "get_embedding_provider", lambda: FakeEmbeddingProvider()
    )

    async with _alembic_version_row(_real_code_head()):
        result = await readiness_module.run_readiness()

    payload = result.model_dump_json()
    assert "supersecret" not in payload

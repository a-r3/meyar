"""PR7's active-release and empty-database-only migration boundary."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from conftest import ADMIN_DATABASE_URL
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

import meyar
from meyar.ops import offline_host, schema_init
from meyar.ops.alembic_introspect import DbAlembicRevisionResult
from meyar.ops.cli import _build_parser, main

RELEASE_ID = "meyar-test+abcdef123456"


@pytest.fixture
def active_root(ops_host_root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = ops_host_root
    release = root / "releases" / RELEASE_ID
    for directory, _, _ in os.walk(release):
        Path(directory).chmod(0o755)
    (release / "install_state.json").chmod(0o644)
    source = release / "backend/src/meyar"
    (source / "__init__.py").write_text("")
    (source / "ops").mkdir()
    (source / "ops/schema_init.py").write_text("# synthetic active command\n")
    backend = release / "backend"
    (backend / "alembic.ini").write_text("[alembic]\nscript_location = %(here)s/alembic\n")
    versions = backend / "alembic/versions"
    versions.mkdir(parents=True)
    (backend / "alembic/env.py").write_text("# synthetic migration environment\n")
    (versions / "0001.py").write_text("revision = 'rev1'\ndown_revision = None\n")
    (release / "release_manifest.json").write_text(json.dumps({"alembic_heads": ["rev1"]}))
    _seal_release(release)
    monkeypatch.setattr(meyar, "__file__", str(source / "__init__.py"))
    monkeypatch.setattr(schema_init, "__file__", str(source / "ops/schema_init.py"))
    monkeypatch.setattr(sys, "executable", str(release / ".venv/bin/python"))
    return root


def _seal_release(release: Path) -> None:
    state_path = release / "install_state.json"
    state_path.chmod(0o644)
    state = json.loads(state_path.read_text())
    state["tree_sha256"] = offline_host._tree_digest(release)
    state_path.write_text(json.dumps(state))
    for directory, _, files in os.walk(release, topdown=False):
        for name in files:
            path = Path(directory) / name
            path.chmod(0o555 if name == "python" else 0o444)
        Path(directory).chmod(0o555)


def test_valid_operator_owned_active_release_and_canonical_migrations(active_root: Path) -> None:
    config, head = schema_init._active_config(active_root)
    assert head == "rev1"
    assert Path(config.config_file_name or "") == (
        active_root / "releases" / RELEASE_ID / "backend/alembic.ini"
    )


def test_dev_checkout_cannot_target_unrelated_host(
    active_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(meyar, "__file__", __file__)
    with pytest.raises(schema_init.SchemaInitFailure, match="ACTIVE_RELEASE_INVALID"):
        schema_init._active_config(active_root)


def test_wrong_owner_and_service_identity_refused(
    active_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(schema_init.os, "geteuid", lambda: os.getuid() + 1)
    assert schema_init.run_schema_init(active_root).findings[0].code == "ACTIVE_RELEASE_INVALID"


def test_tampered_active_release_refused(active_root: Path) -> None:
    path = active_root / "releases" / RELEASE_ID / "backend/alembic.ini"
    path.chmod(0o644)
    path.write_text(path.read_text() + "# tampered\n")
    with pytest.raises(schema_init.SchemaInitFailure, match="ACTIVE_RELEASE_INVALID"):
        schema_init._active_config(active_root)


def test_manifest_head_mismatch_refused(active_root: Path) -> None:
    release = active_root / "releases" / RELEASE_ID
    path = release / "release_manifest.json"
    path.chmod(0o644)
    path.write_text(json.dumps({"alembic_heads": ["other"]}))
    _seal_release(release)
    with pytest.raises(schema_init.SchemaInitFailure, match="MIGRATION_IDENTITY_MISMATCH"):
        schema_init._active_config(active_root)


def test_multiple_code_heads_refused(active_root: Path) -> None:
    release = active_root / "releases" / RELEASE_ID
    versions = release / "backend/alembic/versions"
    versions.chmod(0o755)
    (versions / "0002.py").write_text("revision = 'rev2'\ndown_revision = None\n")
    _seal_release(release)
    with pytest.raises(schema_init.SchemaInitFailure, match="MULTIPLE_OR_NO_HEADS"):
        schema_init._active_config(active_root)


def test_no_code_head_refused(active_root: Path) -> None:
    release = active_root / "releases" / RELEASE_ID
    (release / "backend/alembic/versions").chmod(0o755)
    version = release / "backend/alembic/versions/0001.py"
    version.unlink()
    _seal_release(release)
    with pytest.raises(schema_init.SchemaInitFailure, match="MULTIPLE_OR_NO_HEADS"):
        schema_init._active_config(active_root)


def test_shared_pr4_lock_contention_and_inode_unchanged(active_root: Path) -> None:
    lock = active_root / ".meyar-ops.lock"
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    before = lock.stat()
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = schema_init.run_schema_init(active_root)
        assert result.findings[0].code == "OPERATION_BUSY"
    finally:
        os.close(descriptor)
    after = lock.stat()
    assert (after.st_ino, after.st_uid, after.st_mode) == (
        before.st_ino,
        before.st_uid,
        before.st_mode,
    )


def test_cli_accepts_only_install_root_not_credentials(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = _build_parser()
    args = parser.parse_args(["schema-init", "--install-root", "/opt/meyar"])
    assert args.install_root == Path("/opt/meyar")
    with pytest.raises(SystemExit) as caught:
        main(
            ["schema-init", "--install-root", "/opt/meyar", "--database-url", "secret"]
        )
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert "secret" not in captured.out + captured.err


def test_ambient_database_override_is_not_used(
    active_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEYAR_DATABASE_URL", "postgresql+asyncpg://ambient:secret@host/db")
    received: list[str] = []

    async def run(_config: Config, _head: str, database_url: str) -> str:
        received.append(database_url)
        return "SCHEMA_ALREADY_CURRENT"

    monkeypatch.setattr(schema_init, "_run_database", run)
    result = schema_init.run_schema_init(active_root)
    assert result.findings[0].code == "SCHEMA_ALREADY_CURRENT"
    assert received == [
        "postgresql+asyncpg://synthetic:synthetic@localhost:5432/meyar"
    ]
    assert "secret" not in result.model_dump_json()


class _FakeEngine:
    def __init__(self) -> None:
        self.upgrade_calls = 0
        self.disposed = False

    def begin(self) -> Any:
        engine = self

        class _Context:
            async def __aenter__(self) -> Any:
                class _Connection:
                    async def run_sync(self, callback: Any) -> None:
                        engine.upgrade_calls += 1
                        callback(object())

                return _Connection()

            async def __aexit__(self, *_args: object) -> None:
                return None

        return _Context()

    async def dispose(self) -> None:
        self.disposed = True


@pytest.mark.parametrize(
    ("revisions", "tables", "expected"),
    [
        (["head"], set(), "SCHEMA_ALREADY_CURRENT"),
        ([], {"candidates"}, "UNVERSIONED_SCHEMA_PRESENT"),
        (["old"], set(), "SCHEMA_UPGRADE_REQUIRES_UPDATE_WORKFLOW"),
        (["other"], set(), "SCHEMA_UPGRADE_REQUIRES_UPDATE_WORKFLOW"),
        (["a", "b"], set(), "MULTIPLE_DB_REVISIONS"),
    ],
)
def test_unsafe_database_states_do_not_upgrade(
    monkeypatch: pytest.MonkeyPatch, revisions: list[str], tables: set[str], expected: str
) -> None:
    engine = _FakeEngine()
    monkeypatch.setattr(schema_init, "create_async_engine", lambda *_args, **_kwargs: engine)

    async def inspect(_engine: Any) -> tuple[list[str], set[str]]:
        return revisions, tables

    monkeypatch.setattr(schema_init, "_inspect_database", inspect)
    config = Config()
    if expected == "SCHEMA_ALREADY_CURRENT":
        assert asyncio.run(schema_init._run_database(config, "head", "synthetic")) == expected
    else:
        with pytest.raises(schema_init.SchemaInitFailure, match=expected):
            asyncio.run(schema_init._run_database(config, "head", "synthetic"))
    assert engine.upgrade_calls == 0
    assert engine.disposed


def test_fresh_database_upgrades_once_and_postchecks(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _FakeEngine()
    monkeypatch.setattr(schema_init, "create_async_engine", lambda *_args, **_kwargs: engine)
    states = iter([([], set()), (["head"], {"candidates"})])

    async def inspect(_engine: Any) -> tuple[list[str], set[str]]:
        return next(states)

    monkeypatch.setattr(schema_init, "_inspect_database", inspect)
    monkeypatch.setattr(schema_init.command, "upgrade", lambda _config, _head: None)
    assert asyncio.run(schema_init._run_database(Config(), "head", "synthetic")) == (
        "SCHEMA_INITIALIZED"
    )
    assert engine.upgrade_calls == 1


def test_postcheck_mismatch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _FakeEngine()
    monkeypatch.setattr(schema_init, "create_async_engine", lambda *_args, **_kwargs: engine)
    states = iter([([], set()), (["wrong"], set())])

    async def inspect(_engine: Any) -> tuple[list[str], set[str]]:
        return next(states)

    monkeypatch.setattr(schema_init, "_inspect_database", inspect)
    monkeypatch.setattr(schema_init.command, "upgrade", lambda _config, _head: None)
    with pytest.raises(schema_init.SchemaInitFailure, match="SCHEMA_POSTCHECK_FAILED"):
        asyncio.run(schema_init._run_database(Config(), "head", "synthetic"))


def test_unreachable_database_error_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "synthetic-secret-password"

    def fail(*_args: object, **_kwargs: object) -> Any:
        raise RuntimeError(secret)

    monkeypatch.setattr(schema_init, "create_async_engine", fail)
    with pytest.raises(schema_init.SchemaInitFailure, match="DATABASE_UNREACHABLE") as caught:
        asyncio.run(schema_init._run_database(Config(), "head", secret))
    assert secret not in str(caught.value)


def test_connection_failure_is_database_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "synthetic-secret-password"

    async def fail(_engine: Any) -> Any:
        raise SQLAlchemyError(secret)

    monkeypatch.setattr(schema_init, "get_db_alembic_revision", fail)
    with pytest.raises(schema_init.SchemaInitFailure, match="DATABASE_UNREACHABLE") as caught:
        asyncio.run(schema_init._inspect_database(object()))  # type: ignore[arg-type]
    assert secret not in str(caught.value)


def test_failed_migration_result_and_output_hide_credentials(
    active_root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "synthetic-secret-password"

    async def fail(_config: Config, _head: str, _database_url: str) -> str:
        try:
            raise RuntimeError(secret)
        except RuntimeError as exc:
            raise schema_init.SchemaInitFailure("SCHEMA_INITIALIZATION_FAILED") from exc

    monkeypatch.setattr(schema_init, "_run_database", fail)
    result = schema_init.run_schema_init(active_root)
    captured = capsys.readouterr()
    assert result.findings[0].code == "SCHEMA_INITIALIZATION_FAILED"
    assert secret not in result.model_dump_json() + captured.out + captured.err


def test_database_eligibility_reads_only_revision_and_table_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    async def revision(_engine: Any) -> DbAlembicRevisionResult:
        return DbAlembicRevisionResult(table_exists=False, revisions=[])

    class _Rows:
        def fetchall(self) -> list[tuple[str]]:
            return [("some_existing_table",)]

    class _Connection:
        async def execute(self, statement: Any) -> _Rows:
            statements.append(str(statement))
            return _Rows()

    class _ConnectionContext:
        async def __aenter__(self) -> _Connection:
            return _Connection()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class _Engine:
        def connect(self) -> _ConnectionContext:
            return _ConnectionContext()

    monkeypatch.setattr(schema_init, "get_db_alembic_revision", revision)
    revisions, tables = asyncio.run(schema_init._inspect_database(_Engine()))  # type: ignore[arg-type]
    assert revisions == []
    assert tables == {"some_existing_table"}
    assert statements == [
        "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = current_schema()"
    ]
    assert all("candidate" not in statement.lower() for statement in statements)


def test_failed_migration_is_sanitized_and_never_calls_service(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    engine = _FakeEngine()
    monkeypatch.setattr(schema_init, "create_async_engine", lambda *_args, **_kwargs: engine)

    async def inspect(_engine: Any) -> tuple[list[str], set[str]]:
        return [], set()

    monkeypatch.setattr(schema_init, "_inspect_database", inspect)
    secret = "synthetic-secret-password"

    def fail(_config: Config, _head: str) -> None:
        raise RuntimeError(secret)

    monkeypatch.setattr(schema_init.command, "upgrade", fail)
    with pytest.raises(
        schema_init.SchemaInitFailure, match="SCHEMA_INITIALIZATION_FAILED"
    ) as caught:
        asyncio.run(schema_init._run_database(Config(), "head", "synthetic"))
    assert secret not in str(caught.value)
    assert secret not in capsys.readouterr().out
    assert engine.upgrade_calls == 1


def test_real_fresh_postgres_pgvector_migration_and_idempotency() -> None:
    """A separate disposable database, never the suite's shared candidate DB."""
    name = "meyar_schema_init_" + uuid.uuid4().hex[:16]
    admin = create_async_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT")
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["meyar_schema_init"] = True
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1
    database_url = admin.url.set(database=name).render_as_string(hide_password=False)

    async def run() -> None:
        try:
            async with admin.connect() as connection:
                await connection.execute(text(f"CREATE DATABASE {name}"))
            assert await schema_init._run_database(config, heads[0], database_url) == (
                "SCHEMA_INITIALIZED"
            )
            assert await schema_init._run_database(config, heads[0], database_url) == (
                "SCHEMA_ALREADY_CURRENT"
            )
        finally:
            async with admin.connect() as connection:
                await connection.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
            await admin.dispose()

    asyncio.run(run())

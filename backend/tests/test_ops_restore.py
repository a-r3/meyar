"""Isolated restore contract using only synthetic artifacts and database probes."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url
from test_ops_backup import create

from meyar.ops import restore
from meyar.ops.cli import _build_parser

pytest_plugins = ("test_ops_backup",)


@pytest.fixture(scope="session", autouse=True)
def _test_database_ready() -> None:
    """This module uses synthetic probes and needs no PostgreSQL service."""


def code(result: restore.OpsResult) -> str:
    return result.findings[0].code


@pytest.fixture
def ready(
    host: tuple[Path, Path, object, Path], monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    root, _, _, tools = host
    assert code(create(host)) == "BACKUP_CREATED"
    release_id = "meyar-test+abcdef123456"
    monkeypatch.setattr(restore, "_active_config", lambda *_a, **_kw: (None, "head"))
    monkeypatch.setattr(restore, "verify_active_release", lambda _root: release_id)
    manifest = root / "shared/backups/daily_01/backup_manifest.json"
    head = json.loads(manifest.read_text())["alembic_head"]
    monkeypatch.setattr(restore, "_active_config", lambda *_a, **_kw: (None, head))

    async def inspect(_url: object, *, expected_head: str | None) -> None:
        assert expected_head in (None, head)

    monkeypatch.setattr(restore, "_inspect_target", inspect)
    return root, tools


def run(
    ready: tuple[Path, Path], restore_id: str = "isolated_01", database: str = "meyar_restore"
) -> restore.OpsResult:
    root, tools = ready
    return restore.run_restore(root, "daily_01", restore_id, database, tools)


def test_success_bytes_manifest_no_clobber(ready: tuple[Path, Path]) -> None:
    root, _ = ready
    result = run(ready)
    assert code(result) == "RESTORE_COMPLETED"
    final = root / "shared/restores/isolated_01"
    assert (final / "storage/cv/example.pdf").read_bytes() == b"synthetic-cv-bytes"
    assert final.stat().st_mode & 0o777 == 0o700
    assert (final / "storage/cv/example.pdf").stat().st_mode & 0o777 == 0o600
    payload = json.loads((final / "restore_manifest.json").read_text())
    assert payload["storage_file_count"] == 1
    file_hash = hashlib.sha256(b"synthetic-cv-bytes").digest()
    assert (
        payload["storage_tree_sha256"]
        == hashlib.sha256(b"cv/example.pdf\0" + file_hash).hexdigest()
    )
    assert code(run(ready)) == "RESTORE_ID_CONFLICT"
    assert (final / "storage/cv/example.pdf").read_bytes() == b"synthetic-cv-bytes"


@pytest.mark.parametrize("value", ["", ".", "..", "a..b", "/abs", "a/b", "a\\b", "a\n", "a" * 81])
def test_invalid_restore_id(ready: tuple[Path, Path], value: str) -> None:
    assert code(run(ready, restore_id=value)) == "RESTORE_ID_INVALID"


@pytest.mark.parametrize(
    "value",
    ["", "MixedCase", "9bad", "bad-name", "a" * 64, "a/b", "a;drop", "postgres", "template1"],
)
def test_invalid_target_name(ready: tuple[Path, Path], value: str) -> None:
    assert code(run(ready, database=value)) == "RESTORE_TARGET_INVALID"


def test_cli_rejects_credentials_and_destinations() -> None:
    parser = _build_parser()
    args = [
        "restore",
        "--install-root",
        "/host",
        "--backup-id",
        "a",
        "--restore-id",
        "b",
        "--target-database",
        "isolated",
        "--pg-bin-dir",
        "/pg/bin",
    ]
    assert set(vars(parser.parse_args(args))) == {
        "command",
        "install_root",
        "backup_id",
        "restore_id",
        "target_database",
        "pg_bin_dir",
    }
    for option in ("--database-url", "--password", "--output", "--storage-destination", "--sql"):
        with pytest.raises(SystemExit):
            parser.parse_args([*args, option, "secret"])


def test_production_target_refused(ready: tuple[Path, Path]) -> None:
    root, _ = ready
    from meyar.ops.backup import _database_fields
    from meyar.ops.host_config import load_host_settings

    production = _database_fields(load_host_settings(root))[3]
    assert code(run(ready, database=production)) == "RESTORE_TARGET_IS_PRODUCTION"


def test_unsafe_backup_refused_before_db(
    ready: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = ready
    artifact = root / "shared/backups/daily_01/storage.tar"
    with artifact.open("ab") as output:
        output.write(b"tampered")
    touched = False

    async def inspect(_url: object, *, expected_head: str | None) -> None:
        nonlocal touched
        touched = True

    monkeypatch.setattr(restore, "_inspect_target", inspect)
    assert code(run(ready)) == "BACKUP_INVALID"
    assert not touched


@pytest.mark.parametrize("failure", ["RESTORE_TARGET_UNAVAILABLE", "RESTORE_TARGET_NOT_EMPTY"])
def test_target_unavailable_or_nonempty(
    ready: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async def inspect(_url: object, *, expected_head: str | None) -> None:
        raise restore.RestoreFailure(failure)

    monkeypatch.setattr(restore, "_inspect_target", inspect)
    assert code(run(ready)) == failure


def test_database_failure_leaves_no_completion(
    ready: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise restore.RestoreFailure("RESTORE_DATABASE_FAILED")

    monkeypatch.setattr(restore, "_pg_restore", fail)
    assert code(run(ready)) == "RESTORE_DATABASE_FAILED"
    assert not (ready[0] / "shared/restores/isolated_01").exists()


def test_postcheck_failure_preserves_isolated_stage(
    ready: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def inspect(_url: object, *, expected_head: str | None) -> None:
        if expected_head is not None:
            raise restore.RestoreFailure("RESTORE_DATABASE_VERIFY_FAILED")

    monkeypatch.setattr(restore, "_inspect_target", inspect)
    assert code(run(ready)) == "RESTORE_INCOMPLETE_ISOLATED_TARGET"
    assert not (ready[0] / "shared/restores/isolated_01").exists()
    assert len(list((ready[0] / "shared/restores").glob(".restore-*"))) == 1


def test_schema_mismatch_refused_before_database(
    ready: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    touched = False

    async def inspect(_url: object, *, expected_head: str | None) -> None:
        nonlocal touched
        touched = True

    monkeypatch.setattr(restore, "_inspect_target", inspect)
    monkeypatch.setattr(restore, "_active_config", lambda *_a, **_kw: (None, "different"))
    assert code(run(ready)) == "RESTORE_SCHEMA_INCOMPATIBLE"
    assert not touched


def test_untrusted_pg_tool_refused(ready: tuple[Path, Path]) -> None:
    _, tools = ready
    executable = tools / "pg_restore"
    executable.chmod(0o666)
    assert code(run(ready)) == "PG_TOOLS_UNSAFE"


def test_storage_failure_precedes_database_restore(
    ready: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    touched = False

    def extraction(*_args: object) -> None:
        raise restore.RestoreFailure("RESTORE_STORAGE_INVALID")

    def pg(*_args: object) -> None:
        nonlocal touched
        touched = True

    monkeypatch.setattr(restore, "_extract_storage", extraction)
    monkeypatch.setattr(restore, "_pg_restore", pg)
    assert code(run(ready)) == "RESTORE_STORAGE_INVALID"
    assert not touched


def test_publication_failure_after_database_is_incomplete(
    ready: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def publication(*_args: object) -> None:
        raise restore.RestoreFailure("ATOMIC_PUBLICATION_FAILED")

    monkeypatch.setattr(restore, "_publish", publication)
    assert code(run(ready)) == "RESTORE_INCOMPLETE_ISOLATED_TARGET"
    assert not (ready[0] / "shared/restores/isolated_01").exists()
    assert len(list((ready[0] / "shared/restores").glob(".restore-*"))) == 1


def test_pgpass_cleanup_failure_after_commit_is_incomplete(
    ready: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    def cleanup_failure(*_args: object) -> None:
        raise restore.RestoreFailure(
            "RESTORE_INCOMPLETE_ISOLATED_TARGET", database_restored=True, preserve_stage=False
        )

    monkeypatch.setattr(restore, "_pg_restore", cleanup_failure)
    assert code(run(ready)) == "RESTORE_INCOMPLETE_ISOLATED_TARGET"
    assert not (ready[0] / "shared/restores/isolated_01").exists()
    assert not list((ready[0] / "shared/restores").glob(".restore-*"))


def test_no_exfiltration_on_failed_pg_restore(
    ready: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "a:b\\c@d%e"
    candidate = "SYNTHETIC PRIVATE CV"

    def failure(*_args: object) -> None:
        raise RuntimeError(f"{secret} {candidate} cv/example.pdf")

    monkeypatch.setattr(restore, "_pg_restore", failure)
    result = run(ready)
    captured = capsys.readouterr()
    output = result.model_dump_json() + captured.out + captured.err
    assert code(result) == "RESTORE_FAILED"
    for value in (secret, candidate, "cv/example.pdf", "example.pdf", "postgresql+asyncpg://"):
        assert value not in output


def test_special_password_never_enters_success_result_or_manifest(ready: tuple[Path, Path]) -> None:
    root, _ = ready
    config = root / "shared/config/.env"
    raw = config.read_text()
    config.write_text(
        raw.replace(
            "synthetic:synthetic@localhost",
            "synthetic:a%3Ab%5Cc%40d%25e@localhost",
        )
    )
    result = run(ready)
    assert code(result) == "RESTORE_COMPLETED"
    manifest = (root / "shared/restores/isolated_01/restore_manifest.json").read_text()
    for value in ("a:b\\c@d%e", "a%3Ab%5Cc%40d%25e", "postgresql+asyncpg://", "example.pdf"):
        assert value not in result.model_dump_json() + manifest


@pytest.mark.asyncio
async def test_catalog_empty_target_and_postcheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    nonempty = False

    class Scalar:
        def __init__(self, value: bool) -> None:
            self.value = value

        def scalar(self) -> bool:
            return self.value

    class Connection:
        async def __aenter__(self) -> Connection:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, statement: object) -> Scalar:
            sql = str(statement)
            seen.append(sql)
            return Scalar(nonempty if "pg_catalog.pg_class c" in sql else False)

    class Engine:
        def connect(self) -> Connection:
            return Connection()

        async def dispose(self) -> None:
            return None

    engine = Engine()
    monkeypatch.setattr(restore, "create_async_engine", lambda *_a, **_kw: engine)
    url = make_url("postgresql+asyncpg://synthetic:secret@localhost/isolated")
    await restore._inspect_target(url, expected_head=None)
    assert any("pg_catalog.pg_class c" in sql for sql in seen)
    assert any("pg_catalog.pg_proc p" in sql for sql in seen)
    assert any("pg_catalog.pg_type t" in sql for sql in seen)
    nonempty = True
    with pytest.raises(restore.RestoreFailure, match="RESTORE_TARGET_NOT_EMPTY"):
        await restore._inspect_target(url, expected_head=None)

    class Revision:
        revisions = ["rev1"]

    async def revision(_engine: object) -> Revision:
        return Revision()

    monkeypatch.setattr(restore, "get_db_alembic_revision", revision)
    await restore._inspect_target(url, expected_head="rev1")
    with pytest.raises(restore.RestoreFailure, match="RESTORE_DATABASE_VERIFY_FAILED"):
        await restore._inspect_target(url, expected_head="rev2")
    for invalid_revisions in ([], ["rev1", "rev2"], ["rev2"]):
        Revision.revisions = invalid_revisions
        with pytest.raises(restore.RestoreFailure, match="RESTORE_DATABASE_VERIFY_FAILED"):
            await restore._inspect_target(url, expected_head="rev1")


def test_pg_restore_fixed_argv_and_private_pgpass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    dump = tmp_path / "database.dump"
    dump.write_bytes(b"synthetic")
    password = "a:b\\c@d%e"
    observed: list[str] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.extend(argv)
        assert kwargs["env"]["PGPASSFILE"] == str(stage / ".pgpass")  # type: ignore[index]
        assert (stage / ".pgpass").stat().st_mode & 0o777 == 0o600
        assert password not in " ".join(argv)
        assert "a\\:b\\\\c@d%e" in (stage / ".pgpass").read_text()
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(restore.subprocess, "run", runner)
    restore._pg_restore(
        Path("/trusted/pg_restore"), dump, stage, ("localhost", "5432", "user", "target", password)
    )
    assert observed == [
        "/trusted/pg_restore",
        "--exit-on-error",
        "--single-transaction",
        "--no-owner",
        "--no-privileges",
        "--no-password",
        "-h",
        "localhost",
        "-p",
        "5432",
        "-U",
        "user",
        "-d",
        "target",
        str(dump),
    ]
    assert "--clean" not in observed and "--create" not in observed
    assert not (stage / ".pgpass").exists()


@pytest.mark.parametrize("typeflag", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE])
def test_unsafe_archive_member_rejected(tmp_path: Path, typeflag: bytes) -> None:
    archive = tmp_path / "storage.tar"
    with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as stream:
        info = tarfile.TarInfo("cv/hidden.pdf")
        info.type = typeflag
        info.linkname = "../escape"
        stream.addfile(info, io.BytesIO())
    with pytest.raises(restore.RestoreFailure):
        restore._extract_storage(archive, tmp_path / "storage", 1)


def test_traversal_archive_member_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "storage.tar"
    with tarfile.open(archive, "w", format=tarfile.USTAR_FORMAT) as stream:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        stream.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(restore.RestoreFailure):
        restore._extract_storage(archive, tmp_path / "storage", 1)
    assert not (tmp_path / "escape").exists()

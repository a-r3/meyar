"""Synthetic installed-host backup and verification contracts."""

from __future__ import annotations

import fcntl
import io
import json
import os
import subprocess
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_ops_deployment_ready import (  # noqa: F401
    deployment as deployment,
)
from test_ops_deployment_ready import (
    schema_active_root as schema_active_root,
)
from test_ops_schema_init import _seal_release  # noqa: F401
from test_ops_schema_init import active_root as active_root

from meyar.ops import backup
from meyar.ops.cli import _build_parser
from meyar.ops.service_lifecycle import ServicePrincipal
from meyar.ops.service_plist import ServiceSpec


@pytest.fixture
def host(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Path, ServiceSpec, Path]]:
    root, directory, spec, _ = deployment
    release = root / "releases/meyar-test+abcdef123456"
    (release / "backend/src/meyar/ops").chmod(0o755)
    (release / "backend/src/meyar/ops/backup.py").write_text("# synthetic backup\n")
    manifest = release / "release_manifest.json"
    manifest.chmod(0o644)
    payload = json.loads(manifest.read_text())
    payload["source_sha"] = "a" * 40
    manifest.write_text(json.dumps(payload))
    _seal_release(release)
    monkeypatch.setattr(backup, "__file__", str(release / "backend/src/meyar/ops/backup.py"))
    (root / "shared/backups").chmod(0o750)
    (root / "shared/storage/cv").mkdir()
    (root / "shared/storage/cv/example.pdf").write_bytes(b"synthetic-cv-bytes")

    tools = tmp_path / "pg-bin"
    tools.mkdir()
    tools.chmod(0o750)
    for name in ("pg_dump", "pg_restore"):
        path = tools / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o700)

    async def database(_url: str, _head: str) -> tuple[str, str]:
        return "DATABASE_REACHABLE", "DB_REVISION_CURRENT"

    monkeypatch.setattr(backup, "_probe_database", database)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if Path(argv[0]).name == "pg_dump":
            Path(argv[argv.index("-f") + 1]).write_bytes(b"PGDMPsynthetic")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(backup.subprocess, "run", fake_run)

    def refused(_address: object, timeout: float) -> None:
        raise ConnectionRefusedError

    monkeypatch.setattr(backup.socket, "create_connection", refused)
    yield root, directory, spec, tools


def create(
    host: tuple[Path, Path, ServiceSpec, Path],
    *,
    runner: object = None,
) -> backup.OpsResult:
    root, directory, spec, tools = host
    return backup.run_backup_create(
        root,
        spec.label,
        "daily_01",
        tools,
        platform_system="Darwin",
        plist_directory=directory,
        system_uid=os.geteuid(),
        principal_resolver=lambda _user, gid: ServicePrincipal(os.geteuid() + 1, frozenset({gid})),
        runner=runner or (lambda argv: subprocess.CompletedProcess(argv, 113)),
    )


def code(result: backup.OpsResult) -> str:
    return result.findings[0].code


def test_create_verify_and_storage_bytes(host: tuple[Path, Path, ServiceSpec, Path]) -> None:
    root, _, _, tools = host
    lock = root / ".meyar-ops.lock"
    before = (lock.stat().st_dev, lock.stat().st_ino, lock.stat().st_mode)
    assert code(create(host)) == "BACKUP_CREATED"
    directory = root / "shared/backups/daily_01"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert {p.name for p in directory.iterdir()} == {
        "database.dump",
        "storage.tar",
        "backup_manifest.json",
    }
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir())
    with tarfile.open(directory / "storage.tar", "r:") as archive:
        members = archive.getmembers()
        assert [m.name for m in members] == ["cv", "cv/example.pdf"]
        assert archive.extractfile("cv/example.pdf").read() == b"synthetic-cv-bytes"  # type: ignore[union-attr]
        assert all(m.uid == 0 and m.gid == 0 for m in members)
    assert code(backup.run_backup_verify(root, "daily_01", tools)) == "BACKUP_VERIFIED"
    assert before == (lock.stat().st_dev, lock.stat().st_ino, lock.stat().st_mode)
    assert code(create(host)) == "BACKUP_ID_CONFLICT"


@pytest.mark.parametrize("value", ["", ".", "..", "a..b", "/abs", "a/b", "a\\b", "a\n", "a" * 81])
def test_unsafe_backup_ids(host: tuple[Path, Path, ServiceSpec, Path], value: str) -> None:
    root, _, spec, tools = host
    result = backup.run_backup_create(root, spec.label, value, tools, platform_system="Darwin")
    assert code(result) == "BACKUP_ID_INVALID"
    assert code(backup.run_backup_verify(root, value, tools)) == "BACKUP_ID_INVALID"


def test_cli_has_no_credentials_or_output_path() -> None:
    parser = _build_parser()
    args = parser.parse_args(
        [
            "backup-create",
            "--install-root",
            "/host",
            "--label",
            "com.bank.meyar",
            "--backup-id",
            "a",
            "--pg-bin-dir",
            "/pg/bin",
        ]
    )
    assert set(vars(args)) == {"command", "install_root", "label", "backup_id", "pg_bin_dir"}
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "backup-verify",
                "--install-root",
                "/host",
                "--backup-id",
                "a",
                "--pg-bin-dir",
                "/pg/bin",
                "--output",
                "/tmp/other",
            ]
        )


@pytest.mark.parametrize(
    "exit_code,expected",
    [(0, "SERVICE_MUST_BE_STOPPED"), (42, "SERVICE_PROBE_FAILED"), (113, "BACKUP_CREATED")],
)
def test_launchctl_mapping(
    host: tuple[Path, Path, ServiceSpec, Path], exit_code: int, expected: str
) -> None:
    assert (
        code(create(host, runner=lambda argv: subprocess.CompletedProcess(argv, exit_code)))
        == expected
    )


def test_probe_timeout_and_live_port(
    host: tuple[Path, Path, ServiceSpec, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        backup.socket,
        "create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()),
    )
    assert code(create(host)) == "APPLICATION_PROBE_FAILED"
    monkeypatch.setattr(backup.socket, "create_connection", lambda *_args, **_kwargs: io.BytesIO())
    assert code(create(host)) == "SERVICE_MUST_BE_STOPPED"


def test_lock_contention(host: tuple[Path, Path, ServiceSpec, Path]) -> None:
    root, _, _, _ = host
    with (root / ".meyar-ops.lock").open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert code(create(host)) == "OPERATION_BUSY"


def test_pg_tool_trust(host: tuple[Path, Path, ServiceSpec, Path]) -> None:
    root, _, _, tools = host
    assert code(backup.run_backup_verify(root, "x", Path("relative"))) == "PG_TOOLS_UNSAFE"
    (tools / "pg_restore").unlink()
    (tools / "pg_restore").symlink_to(tools / "pg_dump")
    assert code(create(host)) == "PG_TOOLS_UNSAFE"
    (tools / "pg_restore").unlink()
    (tools / "pg_restore").write_text("stub")
    (tools / "pg_restore").chmod(0o777)
    assert code(create(host)) == "PG_TOOLS_UNSAFE"


def test_dump_password_transport(
    host: tuple[Path, Path, ServiceSpec, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, _ = host
    config = root / "shared/config/.env"
    value = "p:@\\%"
    config.write_text(
        config.read_text().replace("synthetic:synthetic@", "synthetic:p%3A%40%5C%25@")
    )
    config.chmod(0o640)
    observed: list[str] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if Path(argv[0]).name == "pg_dump":
            env = kwargs["env"]
            assert isinstance(env, dict)
            passfile = Path(env["PGPASSFILE"])
            assert passfile.stat().st_mode & 0o777 == 0o600
            observed.append(passfile.read_text())
            assert value not in " ".join(argv)
            Path(argv[argv.index("-f") + 1]).write_bytes(b"PGDMPsynthetic")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    result = create(host)
    assert code(result) == "BACKUP_CREATED"
    assert observed == ["localhost:5432:meyar:synthetic:p\\:@\\\\%\n"]
    manifest = (root / "shared/backups/daily_01/backup_manifest.json").read_text()
    assert value not in result.model_dump_json() + manifest
    assert not (root / "shared/backups/daily_01/.pgpass").exists()


@pytest.mark.parametrize("filename", ["database.dump", "storage.tar", "backup_manifest.json"])
def test_tampering_fails(host: tuple[Path, Path, ServiceSpec, Path], filename: str) -> None:
    root, _, _, tools = host
    assert code(create(host)) == "BACKUP_CREATED"
    path = root / "shared/backups/daily_01" / filename
    path.write_bytes(path.read_bytes() + b"tamper")
    assert not backup.run_backup_verify(root, "daily_01", tools).ok


def test_unsafe_tar_rejected(host: tuple[Path, Path, ServiceSpec, Path]) -> None:
    root, _, _, tools = host
    assert code(create(host)) == "BACKUP_CREATED"
    directory = root / "shared/backups/daily_01"
    path = directory / "storage.tar"
    with tarfile.open(path, "w") as archive:
        info = tarfile.TarInfo("../outside")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    manifest_path = directory / "backup_manifest.json"
    payload = json.loads(manifest_path.read_text())
    payload["storage_archive_sha256"], payload["storage_archive_size"] = backup._hash_size(path)
    manifest_path.write_text(json.dumps(payload))
    assert (
        code(backup.run_backup_verify(root, "daily_01", tools)) == "BACKUP_STORAGE_ARCHIVE_INVALID"
    )


def test_storage_symlink_rejected(host: tuple[Path, Path, ServiceSpec, Path]) -> None:
    root, _, _, _ = host
    (root / "shared/storage/link").symlink_to("cv/example.pdf")
    assert code(create(host)) == "BACKUP_STORAGE_UNSAFE"
    assert not (root / "shared/backups/daily_01").exists()


def test_final_snapshot_change_refuses_publication(
    host: tuple[Path, Path, ServiceSpec, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, _ = host
    real = backup._snapshot
    calls = 0

    def changed(*args: object, **kwargs: object) -> backup.Snapshot:
        nonlocal calls
        calls += 1
        result = real(*args, **kwargs)  # type: ignore[arg-type]
        if calls == 2:
            return backup.Snapshot(
                "other", result.source_sha, result.head, result.settings, result.plist, result.port
            )
        return result

    monkeypatch.setattr(backup, "_snapshot", changed)
    assert code(create(host)) == "BACKUP_SNAPSHOT_CHANGED"
    assert not (root / "shared/backups/daily_01").exists()


def test_dump_failure_is_sanitized_and_unpublished(
    host: tuple[Path, Path, ServiceSpec, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, _ = host

    def fail(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["stderr"] == subprocess.DEVNULL
        return subprocess.CompletedProcess(argv, 2, "secret", "secret")

    monkeypatch.setattr(backup.subprocess, "run", fail)
    result = create(host)
    assert code(result) == "DATABASE_DUMP_FAILED"
    assert "secret" not in result.model_dump_json()
    assert not (root / "shared/backups/daily_01").exists()
    assert not list((root / "shared/backups").glob(".backup-*"))


def test_schema_and_release_trust(
    host: tuple[Path, Path, ServiceSpec, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, _ = host

    async def stale(_url: str, _head: str) -> tuple[str, str]:
        return "DATABASE_REACHABLE", "SCHEMA_MISMATCH"

    monkeypatch.setattr(backup, "_probe_database", stale)
    assert code(create(host)) == "SCHEMA_MISMATCH"
    monkeypatch.setattr(backup, "__file__", __file__)
    assert code(create(host)) == "ACTIVE_RELEASE_INVALID"
    assert not (root / "shared/backups/daily_01").exists()


def test_config_plist_and_service_restart_final_check(
    host: tuple[Path, Path, ServiceSpec, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, directory, spec, _ = host
    original = backup._dump
    config = root / "shared/config/.env"
    plist = directory / f"{spec.label}.plist"
    for path in (config, plist):
        before = path.read_bytes()

        def mutate(
            stage: Path,
            executable: Path,
            settings: object,
            path: Path = path,
            before: bytes = before,
        ) -> None:
            original(stage, executable, settings)  # type: ignore[arg-type]
            path.write_bytes(
                before.replace(b"synthetic-host-test-secret", b"changed-host-test-secret")
                if path == config
                else before + b"\n"
            )

        monkeypatch.setattr(backup, "_dump", mutate)
        assert code(create(host)) == "BACKUP_SNAPSHOT_CHANGED"
        assert not (root / "shared/backups/daily_01").exists()
        path.write_bytes(before)
    monkeypatch.setattr(backup, "_dump", original)
    calls = 0

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(argv, 113 if calls == 1 else 0)

    assert code(create(host, runner=runner)) == "BACKUP_SNAPSHOT_CHANGED"
    assert not (root / "shared/backups/daily_01").exists()


def test_verify_does_not_read_config_or_extract(
    host: tuple[Path, Path, ServiceSpec, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, tools = host
    assert code(create(host)) == "BACKUP_CREATED"
    monkeypatch.setattr(
        backup,
        "load_host_settings",
        lambda _root: (_ for _ in ()).throw(AssertionError("config read")),
    )
    monkeypatch.setattr(
        tarfile.TarFile,
        "extractall",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("extract")),
    )
    assert code(backup.run_backup_verify(root, "daily_01", tools)) == "BACKUP_VERIFIED"


def test_pg_restore_failure_rejected(
    host: tuple[Path, Path, ServiceSpec, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, tools = host
    assert code(create(host)) == "BACKUP_CREATED"
    monkeypatch.setattr(
        backup.subprocess, "run", lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1)
    )
    assert code(backup.run_backup_verify(root, "daily_01", tools)) == "BACKUP_DATABASE_DUMP_INVALID"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "malformed"])
def test_other_tar_tampering_fails(host: tuple[Path, Path, ServiceSpec, Path], kind: str) -> None:
    root, _, _, tools = host
    assert code(create(host)) == "BACKUP_CREATED"
    directory = root / "shared/backups/daily_01"
    path = directory / "storage.tar"
    if kind == "malformed":
        path.write_bytes(b"not a tar archive")
    else:
        with tarfile.open(path, "w") as archive:
            info = tarfile.TarInfo("link")
            info.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            info.linkname = "cv/example.pdf"
            archive.addfile(info)
    manifest_path = directory / "backup_manifest.json"
    payload = json.loads(manifest_path.read_text())
    payload["storage_archive_sha256"], payload["storage_archive_size"] = backup._hash_size(path)
    manifest_path.write_text(json.dumps(payload))
    assert code(backup.run_backup_verify(root, "daily_01", tools)) == (
        "BACKUP_STORAGE_ARCHIVE_INVALID"
    )


def test_lock_held_during_dump_and_archive(
    host: tuple[Path, Path, ServiceSpec, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, _ = host
    real_dump = backup._dump
    real_archive = backup._archive_storage
    observed = 0

    def assert_locked() -> None:
        nonlocal observed
        with (root / ".meyar-ops.lock").open("rb") as stream:
            with pytest.raises(BlockingIOError):
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observed += 1

    def dump(*args: object) -> None:
        assert_locked()
        real_dump(*args)  # type: ignore[arg-type]

    def archive(*args: object) -> int:
        assert_locked()
        return real_archive(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(backup, "_dump", dump)
    monkeypatch.setattr(backup, "_archive_storage", archive)
    assert code(create(host)) == "BACKUP_CREATED"
    assert observed == 2


@pytest.mark.parametrize("failure", ["timeout", "oserror"])
def test_launchctl_uncertainty_refuses_backup(
    host: tuple[Path, Path, ServiceSpec, Path], failure: str
) -> None:
    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        raise OSError("secret diagnostic")

    result = create(host, runner=runner)
    assert code(result) == "SERVICE_PROBE_FAILED"
    assert "secret diagnostic" not in result.model_dump_json()


def test_final_port_reactivation_refuses_publication(
    host: tuple[Path, Path, ServiceSpec, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, _ = host
    calls = 0

    def connect(_address: object, timeout: float) -> io.BytesIO:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionRefusedError
        return io.BytesIO()

    monkeypatch.setattr(backup.socket, "create_connection", connect)
    assert code(create(host)) == "BACKUP_SNAPSHOT_CHANGED"
    assert not (root / "shared/backups/daily_01").exists()


def test_wrong_owner_and_unsafe_config_refuse(
    host: tuple[Path, Path, ServiceSpec, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, _ = host
    real_uid = os.geteuid()
    monkeypatch.setattr(backup.os, "geteuid", lambda: real_uid + 1)
    assert code(create(host)) == "BACKUP_LAYOUT_UNSAFE"
    monkeypatch.setattr(backup.os, "geteuid", lambda: real_uid)
    config = root / "shared/config/.env"
    config.chmod(0o666)
    assert code(create(host)) == "BACKUP_PREREQUISITE_FAILED"


def test_tampered_active_release_refuses(host: tuple[Path, Path, ServiceSpec, Path]) -> None:
    root, _, _, _ = host
    path = root / "releases/meyar-test+abcdef123456/backend/src/meyar/ops/backup.py"
    path.chmod(0o644)
    path.write_text("tampered")
    assert code(create(host)) == "ACTIVE_RELEASE_INVALID"


def test_special_storage_node_refused(host: tuple[Path, Path, ServiceSpec, Path]) -> None:
    root, _, _, _ = host
    os.mkfifo(root / "shared/storage/fifo")
    assert code(create(host)) == "BACKUP_STORAGE_UNSAFE"
    assert not (root / "shared/backups/daily_01").exists()


def test_manifest_digest_tamper_fails(host: tuple[Path, Path, ServiceSpec, Path]) -> None:
    root, _, _, tools = host
    assert code(create(host)) == "BACKUP_CREATED"
    manifest = root / "shared/backups/daily_01/backup_manifest.json"
    payload = json.loads(manifest.read_text())
    payload["database_dump_sha256"] = "0" * 64
    manifest.write_text(json.dumps(payload))
    assert code(backup.run_backup_verify(root, "daily_01", tools)) == "BACKUP_HASH_MISMATCH"


def test_kernel_publication_never_replaces_existing_directory(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    final = tmp_path / "final"
    stage.mkdir()
    final.mkdir()
    (final / "sentinel").write_text("existing")
    with pytest.raises(backup.BackupFailure, match="BACKUP_ID_CONFLICT"):
        backup._publish(stage, final)
    assert (final / "sentinel").read_text() == "existing"
    assert stage.is_dir()

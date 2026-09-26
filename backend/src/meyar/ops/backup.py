"""Quiesced installed-host backup creation and read-only artifact verification."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from meyar.config import Settings
from meyar.ops.deployment_ready import _installed_spec, _probe_database
from meyar.ops.host_config import load_host_settings
from meyar.ops.offline_host import (
    InstallFailure,
    _load_json,
    _real_directory,
    privileged_operation_lock,
    verify_active_release,
)
from meyar.ops.result import FindingStatus, OpsResult, build_single_finding_result
from meyar.ops.schema_init import SchemaInitFailure, _active_config
from meyar.ops.service_lifecycle import (
    LifecycleFailure,
    PrincipalResolver,
    _verify_runtime,
    resolve_service_principal,
)
from meyar.ops.service_status import LaunchctlRunner, run_service_status

DATABASE_NAME = "database.dump"
STORAGE_NAME = "storage.tar"
MANIFEST_NAME = "backup_manifest.json"
FORMAT_VERSION = 1
MAX_MANIFEST_BYTES = 4096
MAX_MEMBERS = 1_000_000
MAX_MEMBER_NAME = 1024
MAX_STORAGE_BYTES = 10 * 1024**4
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class BackupFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Snapshot:
    release_id: str
    source_sha: str
    head: str
    settings: Settings
    plist: bytes
    port: int


def _result(action: str, code: str, *, ok: bool = False) -> OpsResult:
    return build_single_finding_result(
        action=action,
        component="backup",
        status=FindingStatus.OK if ok else FindingStatus.FAIL,
        code=code,
        message="backup verified"
        if ok and action == "backup-verify"
        else "backup created"
        if ok
        else "backup operation refused or failed",
    )


def _check_id(backup_id: str) -> None:
    if SAFE_ID.fullmatch(backup_id) is None:
        raise BackupFailure("BACKUP_ID_INVALID")


def _backups_root(root: Path, owner_uid: int) -> Path:
    if owner_uid <= 0 or not root.is_absolute() or str(root) != os.path.normpath(root):
        raise BackupFailure("BACKUP_LAYOUT_UNSAFE")
    try:
        _real_directory(root / "shared/backups")
        system_uid = Path("/").stat().st_uid
        for parent in root.parents:
            metadata = parent.stat()
            sticky_system = metadata.st_uid == system_uid and metadata.st_mode & stat.S_ISVTX
            if metadata.st_uid not in (system_uid, owner_uid) or (
                metadata.st_mode & 0o022 and not sticky_system
            ):
                raise BackupFailure("BACKUP_LAYOUT_UNSAFE")
        for path in (root, root / "shared", root / "shared/backups"):
            metadata = path.stat()
            if metadata.st_uid != owner_uid or metadata.st_mode & 0o022:
                raise BackupFailure("BACKUP_LAYOUT_UNSAFE")
        if (root / "shared/backups").stat().st_mode & 0o100 == 0:
            raise BackupFailure("BACKUP_LAYOUT_UNSAFE")
    except (InstallFailure, OSError) as exc:
        raise BackupFailure("BACKUP_LAYOUT_UNSAFE") from exc
    return root / "shared/backups"


def _pg_tools(directory: Path, owner_uid: int) -> tuple[Path, Path]:
    if not directory.is_absolute() or str(directory) != os.path.normpath(directory):
        raise BackupFailure("PG_TOOLS_UNSAFE")
    try:
        _real_directory(directory)
        system_uid = Path("/").stat().st_uid
        for path in (*reversed(directory.parents), directory):
            metadata = path.stat()
            sticky_root = metadata.st_uid == system_uid and metadata.st_mode & stat.S_ISVTX
            if metadata.st_uid not in (system_uid, owner_uid) or (
                metadata.st_mode & 0o022 and not sticky_root
            ):
                raise BackupFailure("PG_TOOLS_UNSAFE")
        tools = tuple(directory / name for name in ("pg_dump", "pg_restore"))
        for path in tools:
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid not in (system_uid, owner_uid)
                or metadata.st_mode & 0o022
                or metadata.st_mode & 0o111 == 0
            ):
                raise BackupFailure("PG_TOOLS_UNSAFE")
        return tools[0], tools[1]
    except (InstallFailure, OSError) as exc:
        raise BackupFailure("PG_TOOLS_UNSAFE") from exc


def _service_stopped(label: str, port: int, runner: LaunchctlRunner | None) -> None:
    result = run_service_status(label=label, platform_system="Darwin", runner=runner)
    code = result.findings[0].code
    if code == "SERVICE_VISIBLE":
        raise BackupFailure("SERVICE_MUST_BE_STOPPED")
    if code != "SERVICE_NOT_VISIBLE":
        raise BackupFailure("SERVICE_PROBE_FAILED")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            raise BackupFailure("SERVICE_MUST_BE_STOPPED")
    except ConnectionRefusedError:
        return
    except (TimeoutError, OSError) as exc:
        raise BackupFailure("APPLICATION_PROBE_FAILED") from exc


def _snapshot(
    root: Path,
    label: str,
    *,
    plist_directory: Path,
    system_uid: int,
    principal_resolver: PrincipalResolver,
    runner: LaunchctlRunner | None,
) -> Snapshot:
    owner = os.geteuid()
    try:
        _, head = _active_config(root, implementation_file=Path(__file__))
        release_id = verify_active_release(root)
        settings = load_host_settings(root)
        spec, plist = _installed_spec(root, label, plist_directory, system_uid, owner)
        gid = (root / "shared/config").stat().st_gid
        principal = principal_resolver(spec.user_name, gid)
        if principal.uid <= 0 or principal.uid == owner or gid not in principal.groups:
            raise BackupFailure("SERVICE_PRINCIPAL_INVALID")
        _verify_runtime(root, owner, gid, principal)
        _service_stopped(label, spec.port, runner)
        manifest = _load_json(root / "releases" / release_id / "release_manifest.json")
        source_sha = manifest.get("source_sha")
        if not isinstance(source_sha, str) or HEX40.fullmatch(source_sha) is None:
            raise BackupFailure("ACTIVE_RELEASE_INVALID")
        return Snapshot(release_id, source_sha, head, settings, plist, spec.port)
    except (SchemaInitFailure, InstallFailure) as exc:
        raise BackupFailure("ACTIVE_RELEASE_INVALID") from exc
    except (LifecycleFailure, ValueError, OSError) as exc:
        raise BackupFailure("BACKUP_PREREQUISITE_FAILED") from exc


def _database_fields(settings: Settings) -> tuple[str, str, str, str, str]:
    try:
        url = make_url(settings.database_url)
        if (
            url.drivername != "postgresql+asyncpg"
            or url.query
            or not url.host
            or not url.username
            or not url.database
        ):
            raise ValueError
        host = url.host
        port = str(url.port or 5432)
        user = url.username
        database = url.database
        password = url.password or ""
        if not 1 <= int(port) <= 65535 or any(
            "\n" in value or "\r" in value or "\0" in value
            for value in (host, port, user, database, password)
        ):
            raise ValueError
        return host, port, user, database, password
    except Exception as exc:  # noqa: BLE001 - URL may contain a password
        raise BackupFailure("DATABASE_CONNECTION_INVALID") from exc


def _pgpass_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _dump(stage: Path, executable: Path, settings: Settings) -> None:
    host, port, user, database, password = _database_fields(settings)
    passfile = stage / ".pgpass"
    fd = os.open(passfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(
                ":".join(_pgpass_escape(x) for x in (host, port, database, user, password)) + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        target = stage / DATABASE_NAME
        argv = [
            str(executable),
            "-Fc",
            "--serializable-deferrable",
            "--no-password",
            "-h",
            host,
            "-p",
            port,
            "-U",
            user,
            "-d",
            database,
            "-f",
            str(target),
        ]
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/var/empty",
            "PGPASSFILE": str(passfile),
            "PGCONNECT_TIMEOUT": "10",
            "LC_ALL": "C",
        }
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=3600,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackupFailure("DATABASE_DUMP_FAILED") from exc
        if completed.returncode != 0:
            raise BackupFailure("DATABASE_DUMP_FAILED")
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size == 0:
            raise BackupFailure("DATABASE_DUMP_FAILED")
        target.chmod(0o600)
    finally:
        passfile.unlink(missing_ok=True)


def _safe_member(name: str) -> bool:
    parts = name.split("/")
    return bool(
        name
        and len(name) <= MAX_MEMBER_NAME
        and not name.startswith("/")
        and "\\" not in name
        and all(ord(char) >= 32 and ord(char) != 127 for char in name)
        and all(part not in ("", ".", "..") for part in parts)
    )


def _archive_storage(source: Path, target: Path) -> int:
    count = 0
    total = 0
    try:
        _real_directory(source)
        with (
            target.open("xb") as output,
            tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive,
        ):
            for directory, dirs, files in os.walk(source, followlinks=False):
                parent = Path(directory)
                _real_directory(parent)
                for name in sorted(dirs + files):
                    path = parent / name
                    relative = path.relative_to(source).as_posix()
                    metadata = path.lstat()
                    if not _safe_member(relative):
                        raise BackupFailure("BACKUP_STORAGE_UNSAFE")
                    info = tarfile.TarInfo(relative)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    if stat.S_ISDIR(metadata.st_mode):
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o700
                        archive.addfile(info)
                    elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                        count += 1
                        total += metadata.st_size
                        if count > MAX_MEMBERS or total > MAX_STORAGE_BYTES:
                            raise BackupFailure("BACKUP_STORAGE_UNSAFE")
                        info.size = metadata.st_size
                        info.mode = 0o600
                        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                        with os.fdopen(fd, "rb") as stream:
                            opened = os.fstat(stream.fileno())
                            if (opened.st_dev, opened.st_ino, opened.st_size) != (
                                metadata.st_dev,
                                metadata.st_ino,
                                metadata.st_size,
                            ):
                                raise BackupFailure("BACKUP_STORAGE_UNSAFE")
                            archive.addfile(info, stream)
                    else:
                        raise BackupFailure("BACKUP_STORAGE_UNSAFE")
        target.chmod(0o600)
    except (InstallFailure, OSError, tarfile.TarError, EOFError, ValueError) as exc:
        raise BackupFailure("BACKUP_STORAGE_UNSAFE") from exc
    return count


def _hash_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _manifest(stage: Path, backup_id: str, snapshot: Snapshot, count: int) -> None:
    db_hash, db_size = _hash_size(stage / DATABASE_NAME)
    storage_hash, storage_size = _hash_size(stage / STORAGE_NAME)
    payload = {
        "format_version": FORMAT_VERSION,
        "backup_id": backup_id,
        "created_at": datetime.now(UTC).isoformat(),
        "created_by_uid": os.geteuid(),
        "release_id": snapshot.release_id,
        "source_sha": snapshot.source_sha,
        "alembic_head": snapshot.head,
        "database_dump_filename": DATABASE_NAME,
        "database_dump_sha256": db_hash,
        "database_dump_size": db_size,
        "storage_archive_filename": STORAGE_NAME,
        "storage_archive_sha256": storage_hash,
        "storage_archive_size": storage_size,
        "storage_file_count": count,
    }
    fd = os.open(stage / MANIFEST_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")


def _checked_file(path: Path, directory: Path, *, maximum: int | None = None) -> int:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != directory.stat().st_uid
        or metadata.st_mode & 0o7777 != 0o600
        or metadata.st_size == 0
        or maximum is not None
        and metadata.st_size > maximum
    ):
        raise BackupFailure("BACKUP_LAYOUT_UNSAFE")
    return metadata.st_size


def _verify_tar(path: Path) -> int:
    count = 0
    total = 0
    seen: set[str] = set()
    regular_paths: set[str] = set()
    try:
        # Scan raw USTAR headers before Python's tar reader can process an
        # attacker-supplied PAX/GNU extension with an unbounded declared size.
        with path.open("rb") as archive:
            while True:
                header = archive.read(512)
                if len(header) != 512:
                    raise BackupFailure("BACKUP_STORAGE_ARCHIVE_INVALID")
                if header == bytes(512):
                    if archive.read(512) != bytes(512):
                        raise BackupFailure("BACKUP_STORAGE_ARCHIVE_INVALID")
                    while chunk := archive.read(1024 * 1024):
                        if any(chunk):
                            raise BackupFailure("BACKUP_STORAGE_ARCHIVE_INVALID")
                    break
                if header[257:263] != b"ustar\0" or header[156:157] not in (b"0", b"\0", b"5"):
                    raise BackupFailure("BACKUP_STORAGE_ARCHIVE_INVALID")
                member = tarfile.TarInfo.frombuf(header, "utf-8", "surrogateescape")
                name = member.name.rstrip("/")
                parts = name.split("/")
                if (
                    not _safe_member(name)
                    or name in seen
                    or len(seen) >= MAX_MEMBERS
                    or member.size < 0
                    or member.uid != 0
                    or member.gid != 0
                    or any(
                        "/".join(parts[:index]) in regular_paths for index in range(1, len(parts))
                    )
                    or member.isfile()
                    and any(item.startswith(name + "/") for item in seen)
                    or member.isdir()
                    and member.size != 0
                ):
                    raise BackupFailure("BACKUP_STORAGE_ARCHIVE_INVALID")
                seen.add(name)
                if member.isfile():
                    regular_paths.add(name)
                    count += 1
                    total += member.size
                    if total > MAX_STORAGE_BYTES:
                        raise BackupFailure("BACKUP_STORAGE_ARCHIVE_INVALID")
                remaining = ((member.size + 511) // 512) * 512
                while remaining:
                    chunk = archive.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise BackupFailure("BACKUP_STORAGE_ARCHIVE_INVALID")
                    remaining -= len(chunk)
    except (OSError, tarfile.TarError, EOFError, ValueError) as exc:
        raise BackupFailure("BACKUP_STORAGE_ARCHIVE_INVALID") from exc
    return count


def _verify_artifact(directory: Path, backup_id: str, pg_restore: Path) -> None:
    try:
        _real_directory(directory)
        metadata = directory.stat()
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o7777 != 0o700:
            raise BackupFailure("BACKUP_LAYOUT_UNSAFE")
        if {entry.name for entry in directory.iterdir()} != {
            DATABASE_NAME,
            STORAGE_NAME,
            MANIFEST_NAME,
        }:
            raise BackupFailure("BACKUP_LAYOUT_UNSAFE")
        _checked_file(directory / MANIFEST_NAME, directory, maximum=MAX_MANIFEST_BYTES)
        raw = (directory / MANIFEST_NAME).read_bytes()
        payload = json.loads(raw)
        expected = {
            "format_version",
            "backup_id",
            "created_at",
            "created_by_uid",
            "release_id",
            "source_sha",
            "alembic_head",
            "database_dump_filename",
            "database_dump_sha256",
            "database_dump_size",
            "storage_archive_filename",
            "storage_archive_sha256",
            "storage_archive_size",
            "storage_file_count",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or type(payload["format_version"]) is not int
            or payload["format_version"] != FORMAT_VERSION
            or payload["backup_id"] != backup_id
            or payload["database_dump_filename"] != DATABASE_NAME
            or payload["storage_archive_filename"] != STORAGE_NAME
            or type(payload["created_by_uid"]) is not int
            or payload["created_by_uid"] <= 0
            or not isinstance(payload["release_id"], str)
            or len(payload["release_id"]) > 128
            or not isinstance(payload["source_sha"], str)
            or HEX40.fullmatch(payload["source_sha"]) is None
            or not isinstance(payload["alembic_head"], str)
            or not 1 <= len(payload["alembic_head"]) <= 128
            or not isinstance(payload["created_at"], str)
            or len(payload["created_at"]) > 40
            or any(
                type(payload[key]) is not int or not 0 <= payload[key] <= MAX_STORAGE_BYTES
                for key in ("database_dump_size", "storage_archive_size", "storage_file_count")
            )
            or any(
                not isinstance(payload[key], str) or HEX64.fullmatch(payload[key]) is None
                for key in ("database_dump_sha256", "storage_archive_sha256")
            )
        ):
            raise BackupFailure("BACKUP_MANIFEST_INVALID")
        created_at = datetime.fromisoformat(payload["created_at"])
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise BackupFailure("BACKUP_MANIFEST_INVALID")
        if payload["storage_file_count"] > MAX_MEMBERS:
            raise BackupFailure("BACKUP_MANIFEST_INVALID")
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise BackupFailure("BACKUP_MANIFEST_INVALID") from exc
    dump = directory / DATABASE_NAME
    storage = directory / STORAGE_NAME
    if _checked_file(dump, directory) != payload["database_dump_size"]:
        raise BackupFailure("BACKUP_DATABASE_DUMP_INVALID")
    if _checked_file(storage, directory) != payload["storage_archive_size"]:
        raise BackupFailure("BACKUP_STORAGE_ARCHIVE_INVALID")
    if (
        _hash_size(dump)[0] != payload["database_dump_sha256"]
        or _hash_size(storage)[0] != payload["storage_archive_sha256"]
    ):
        raise BackupFailure("BACKUP_HASH_MISMATCH")
    try:
        completed = subprocess.run(
            [str(pg_restore), "--list", str(dump)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "HOME": "/var/empty", "LC_ALL": "C"},
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupFailure("PG_RESTORE_UNAVAILABLE") from exc
    if completed.returncode != 0:
        raise BackupFailure("BACKUP_DATABASE_DUMP_INVALID")
    if _verify_tar(storage) != payload["storage_file_count"]:
        raise BackupFailure("BACKUP_STORAGE_ARCHIVE_INVALID")


def _publish(stage: Path, final: Path) -> None:
    """Kernel no-replace rename; no time-of-check overwrite window."""
    libc = ctypes.CDLL(None, use_errno=True)
    old = os.fsencode(stage)
    new = os.fsencode(final)
    if sys.platform == "darwin":
        result = libc.renamex_np(ctypes.c_char_p(old), ctypes.c_char_p(new), ctypes.c_uint(0x4))
    elif sys.platform.startswith("linux"):
        result = libc.renameat2(
            ctypes.c_int(-100),
            ctypes.c_char_p(old),
            ctypes.c_int(-100),
            ctypes.c_char_p(new),
            ctypes.c_uint(1),
        )
    else:
        raise BackupFailure("ATOMIC_PUBLICATION_UNAVAILABLE")
    if result != 0:
        code = ctypes.get_errno()
        raise BackupFailure(
            "BACKUP_ID_CONFLICT" if code == errno.EEXIST else "ATOMIC_PUBLICATION_FAILED"
        )


def run_backup_create(
    root: Path,
    label: str,
    backup_id: str,
    pg_bin_dir: Path,
    *,
    platform_system: str | None = None,
    plist_directory: Path = Path("/Library/LaunchDaemons"),
    system_uid: int = 0,
    principal_resolver: PrincipalResolver = resolve_service_principal,
    runner: LaunchctlRunner | None = None,
) -> OpsResult:
    action = "backup-create"
    stage: Path | None = None
    stage_identity: tuple[int, int] | None = None
    try:
        _check_id(backup_id)
        if (platform_system or platform.system()) != "Darwin":
            raise BackupFailure("PLATFORM_UNSUPPORTED")
        owner = os.geteuid()
        backups = _backups_root(root, owner)
        pg_dump, pg_restore = _pg_tools(pg_bin_dir, owner)
        with privileged_operation_lock(root, owner):
            final = backups / backup_id
            if os.path.lexists(final):
                raise BackupFailure("BACKUP_ID_CONFLICT")
            first = _snapshot(
                root,
                label,
                plist_directory=plist_directory,
                system_uid=system_uid,
                principal_resolver=principal_resolver,
                runner=runner,
            )
            database, schema = asyncio.run(_probe_database(first.settings.database_url, first.head))
            if database != "DATABASE_REACHABLE":
                raise BackupFailure("DATABASE_UNREACHABLE")
            if schema != "DB_REVISION_CURRENT":
                raise BackupFailure("SCHEMA_MISMATCH")
            stage = Path(tempfile.mkdtemp(prefix=".backup-", dir=backups))
            stage.chmod(0o700)
            metadata = stage.stat()
            stage_identity = metadata.st_dev, metadata.st_ino
            _dump(stage, pg_dump, first.settings)
            count = _archive_storage(root / "shared/storage", stage / STORAGE_NAME)
            _manifest(stage, backup_id, first, count)
            _verify_artifact(stage, backup_id, pg_restore)
            try:
                second = _snapshot(
                    root,
                    label,
                    plist_directory=plist_directory,
                    system_uid=system_uid,
                    principal_resolver=principal_resolver,
                    runner=runner,
                )
            except BackupFailure as exc:
                raise BackupFailure("BACKUP_SNAPSHOT_CHANGED") from exc
            if second != first:
                raise BackupFailure("BACKUP_SNAPSHOT_CHANGED")
            for name in (DATABASE_NAME, STORAGE_NAME, MANIFEST_NAME):
                fd = os.open(stage / name, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            _publish(stage, final)
            stage = None
            fd = os.open(backups, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return _result(action, "BACKUP_CREATED", ok=True)
    except BackupFailure as exc:
        return _result(action, exc.code)
    except InstallFailure as exc:
        return _result(
            action, "OPERATION_BUSY" if exc.code == "OPERATION_BUSY" else "BACKUP_LAYOUT_UNSAFE"
        )
    except Exception:  # noqa: BLE001 - never expose paths or secrets
        return _result(action, "BACKUP_FAILED")
    finally:
        if stage is not None and stage_identity is not None:
            try:
                metadata = stage.lstat()
                if (
                    stat.S_ISDIR(metadata.st_mode)
                    and (metadata.st_dev, metadata.st_ino) == stage_identity
                ):
                    shutil.rmtree(stage)
            except OSError:
                pass


def run_backup_verify(root: Path, backup_id: str, pg_bin_dir: Path) -> OpsResult:
    action = "backup-verify"
    try:
        _check_id(backup_id)
        backups = _backups_root(root, os.geteuid())
        _, pg_restore = _pg_tools(pg_bin_dir, os.geteuid())
        target = backups / backup_id
        if not os.path.lexists(target):
            raise BackupFailure("BACKUP_NOT_FOUND")
        _verify_artifact(target, backup_id, pg_restore)
        return _result(action, "BACKUP_VERIFIED", ok=True)
    except BackupFailure as exc:
        return _result(action, exc.code)
    except Exception:  # noqa: BLE001 - never expose archive names or paths
        return _result(action, "BACKUP_LAYOUT_UNSAFE")

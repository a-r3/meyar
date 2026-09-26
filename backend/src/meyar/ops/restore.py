"""Restore a verified backup into an operator-provisioned isolated database."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from meyar.ops.alembic_introspect import get_db_alembic_revision
from meyar.ops.backup import (
    DATABASE_NAME,
    MANIFEST_NAME,
    MAX_MEMBERS,
    MAX_STORAGE_BYTES,
    STORAGE_NAME,
    BackupFailure,
    _backups_root,
    _check_id,
    _database_fields,
    _pg_tools,
    _pgpass_escape,
    _publish,
    _safe_member,
    _same_directory,
    _verify_artifact,
)
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

DATABASE_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")
RESTORE_MANIFEST = "restore_manifest.json"


class RestoreFailure(Exception):
    def __init__(
        self, code: str, *, database_restored: bool = False, preserve_stage: bool = True
    ) -> None:
        self.code = code
        self.database_restored = database_restored
        self.preserve_stage = preserve_stage
        super().__init__(code)


def _result(code: str, *, ok: bool = False) -> OpsResult:
    return build_single_finding_result(
        action="restore",
        component="restore",
        status=FindingStatus.OK if ok else FindingStatus.FAIL,
        code=code,
        message="isolated restore completed" if ok else "isolated restore refused or incomplete",
    )


def _restore_root(root: Path, owner: int) -> Path:
    # The accepted backup root check also establishes trusted root/shared ancestry.
    try:
        _backups_root(root, owner)
    except BackupFailure as exc:
        raise RestoreFailure("RESTORE_LAYOUT_UNSAFE") from exc
    parent = root / "shared/restores"
    try:
        if not os.path.lexists(parent):
            parent.mkdir(mode=0o700)
            parent_fd = os.open(root / "shared", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        _real_directory(parent)
        metadata = parent.stat()
        if metadata.st_uid != owner or metadata.st_mode & 0o022 or not metadata.st_mode & 0o700:
            raise RestoreFailure("RESTORE_LAYOUT_UNSAFE")
    except (InstallFailure, OSError) as exc:
        raise RestoreFailure("RESTORE_LAYOUT_UNSAFE") from exc
    return parent


def _tree_hash(items: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name, file_hash in sorted(items.items()):
        digest.update(name.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(file_hash))
    return digest.hexdigest()


def _extract_storage(archive_path: Path, storage: Path, expected_count: int) -> tuple[int, str]:
    """Stream only regular USTAR files; compare archive and independent disk trees."""
    archive_files: dict[str, str] = {}
    seen: set[str] = set()
    total = 0
    try:
        storage.mkdir(mode=0o700)
        storage.chmod(0o700)
        with tarfile.open(archive_path, mode="r:") as archive:
            while member := archive.next():
                name = member.name.rstrip("/")
                if (
                    not _safe_member(name)
                    or name in seen
                    or len(seen) >= MAX_MEMBERS
                    or not (member.isfile() or member.isdir())
                ):
                    raise RestoreFailure("RESTORE_STORAGE_INVALID")
                seen.add(name)
                target = storage.joinpath(*name.split("/"))
                parent = storage
                for part in name.split("/")[:-1]:
                    parent = parent / part
                    if not os.path.lexists(parent):
                        parent.mkdir(mode=0o700)
                    metadata = parent.lstat()
                    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
                        raise RestoreFailure("RESTORE_STORAGE_INVALID")
                if member.isdir():
                    if not os.path.lexists(target):
                        target.mkdir(mode=0o700)
                    if not stat.S_ISDIR(target.lstat().st_mode):
                        raise RestoreFailure("RESTORE_STORAGE_INVALID")
                    target.chmod(0o700)
                    continue
                total += member.size
                if total > MAX_STORAGE_BYTES:
                    raise RestoreFailure("RESTORE_STORAGE_INVALID")
                source = archive.extractfile(member)
                if source is None:
                    raise RestoreFailure("RESTORE_STORAGE_INVALID")
                digest = hashlib.sha256()
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as output:
                    os.fchmod(output.fileno(), 0o600)
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise RestoreFailure("RESTORE_STORAGE_INVALID")
                        output.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                archive_files[name] = digest.hexdigest()
        if len(archive_files) != expected_count:
            raise RestoreFailure("RESTORE_STORAGE_INVALID")
        disk_files: dict[str, str] = {}
        disk_total = 0
        for directory, dirs, files in os.walk(storage, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                metadata = path.lstat()
                if name in dirs:
                    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o077:
                        raise RestoreFailure("RESTORE_STORAGE_INVALID")
                    continue
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise RestoreFailure("RESTORE_STORAGE_INVALID")
                relative = path.relative_to(storage).as_posix()
                if not _safe_member(relative):
                    raise RestoreFailure("RESTORE_STORAGE_INVALID")
                disk_total += metadata.st_size
                if len(disk_files) >= MAX_MEMBERS or disk_total > MAX_STORAGE_BYTES:
                    raise RestoreFailure("RESTORE_STORAGE_INVALID")
                digest = hashlib.sha256()
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                disk_files[relative] = digest.hexdigest()
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        if disk_total != total or disk_files != archive_files:
            raise RestoreFailure("RESTORE_STORAGE_INVALID")
        return len(disk_files), _tree_hash(disk_files)
    except (OSError, ValueError, tarfile.TarError, EOFError) as exc:
        raise RestoreFailure("RESTORE_STORAGE_INVALID") from exc


async def _inspect_target(url: URL, *, expected_head: str | None) -> None:
    try:
        engine = create_async_engine(url, connect_args={"timeout": 10})
    except Exception as exc:  # noqa: BLE001 - driver messages may include credentials
        raise RestoreFailure("RESTORE_TARGET_UNAVAILABLE") from exc
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
            if expected_head is None:
                # Extension-owned objects (including pgvector) are infrastructure.
                # Every other user relation, even an empty Alembic table, refuses restore.
                objects = await connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
                        "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                        "AND n.nspname NOT LIKE 'pg_toast%' "
                        "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d "
                        "WHERE d.classid = 'pg_catalog.pg_class'::regclass "
                        "AND d.objid = c.oid AND d.deptype = 'e'))"
                    )
                )
                schemas = await connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_namespace "
                        "WHERE nspname NOT IN ('public', 'pg_catalog', 'information_schema') "
                        "AND nspname NOT LIKE 'pg_toast%' AND nspname NOT LIKE 'pg_temp_%')"
                    )
                )
                routines = await connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_proc p "
                        "JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                        "AND n.nspname NOT LIKE 'pg_toast%' "
                        "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d "
                        "WHERE d.classid = 'pg_catalog.pg_proc'::regclass "
                        "AND d.objid = p.oid AND d.deptype = 'e'))"
                    )
                )
                types = await connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_type t "
                        "JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace "
                        "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') "
                        "AND n.nspname NOT LIKE 'pg_toast%' "
                        "AND t.typtype IN ('e', 'd', 'r', 'm') "
                        "AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_depend d "
                        "WHERE d.classid = 'pg_catalog.pg_type'::regclass "
                        "AND d.objid = t.oid AND d.deptype = 'e'))"
                    )
                )
                if objects.scalar() or schemas.scalar() or routines.scalar() or types.scalar():
                    raise RestoreFailure("RESTORE_TARGET_NOT_EMPTY")
            else:
                revision = await get_db_alembic_revision(engine)
                if revision.revisions != [expected_head]:
                    raise RestoreFailure("RESTORE_DATABASE_VERIFY_FAILED")
    except RestoreFailure:
        raise
    except Exception as exc:  # noqa: BLE001 - never propagate driver text
        raise RestoreFailure(
            "RESTORE_TARGET_UNAVAILABLE"
            if expected_head is None
            else "RESTORE_DATABASE_VERIFY_FAILED"
        ) from exc
    finally:
        await engine.dispose()


def _pg_restore(
    executable: Path, dump: Path, stage: Path, fields: tuple[str, str, str, str, str]
) -> None:
    host, port, user, database, password = fields
    passfile = stage / ".pgpass"
    fd = os.open(passfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    restored = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(
                ":".join(_pgpass_escape(x) for x in (host, port, database, user, password)) + "\n"
            )
            output.flush()
            os.fsync(output.fileno())
        argv = [
            str(executable),
            "--exit-on-error",
            "--single-transaction",
            "--no-owner",
            "--no-privileges",
            "--no-password",
            "-h",
            host,
            "-p",
            port,
            "-U",
            user,
            "-d",
            database,
            str(dump),
        ]
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": "/var/empty",
                    "PGPASSFILE": str(passfile),
                    "PGCONNECT_TIMEOUT": "10",
                    "LC_ALL": "C",
                },
                timeout=3600,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RestoreFailure("RESTORE_DATABASE_FAILED") from exc
        if completed.returncode != 0:
            raise RestoreFailure("RESTORE_DATABASE_FAILED")
        restored = True
    finally:
        try:
            passfile.unlink(missing_ok=True)
        except OSError as exc:
            raise RestoreFailure(
                "RESTORE_INCOMPLETE_ISOLATED_TARGET" if restored else "RESTORE_DATABASE_FAILED",
                database_restored=restored,
                preserve_stage=False,
            ) from exc


def _write_manifest(final: Path, payload: dict[str, object]) -> None:
    temporary = final / ".restore-manifest.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, final / RESTORE_MANIFEST, follow_symlinks=False)
        temporary.unlink()
        directory_fd = os.open(final, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        (final / RESTORE_MANIFEST).unlink(missing_ok=True)
        raise


def run_restore(
    root: Path, backup_id: str, restore_id: str, target_database: str, pg_bin_dir: Path
) -> OpsResult:
    stage: Path | None = None
    stage_identity: tuple[int, int] | None = None
    database_restored = False
    preserve_stage = False
    try:
        try:
            _check_id(backup_id)
        except BackupFailure as exc:
            raise RestoreFailure("BACKUP_INVALID") from exc
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", restore_id):
            raise RestoreFailure("RESTORE_ID_INVALID")
        if DATABASE_IDENTIFIER.fullmatch(target_database) is None or target_database in {
            "postgres",
            "template0",
            "template1",
        }:
            raise RestoreFailure("RESTORE_TARGET_INVALID")
        owner = os.geteuid()
        if owner == 0:
            raise RestoreFailure("RESTORE_LAYOUT_UNSAFE")
        try:
            _, pg_restore = _pg_tools(pg_bin_dir, owner)
        except BackupFailure as exc:
            raise RestoreFailure("PG_TOOLS_UNSAFE") from exc
        with privileged_operation_lock(root, owner):
            restores = _restore_root(root, owner)
            final = restores / restore_id
            if os.path.lexists(final):
                raise RestoreFailure("RESTORE_ID_CONFLICT")
            backup = _backups_root(root, owner) / backup_id
            if not os.path.lexists(backup):
                raise RestoreFailure("BACKUP_INVALID")
            try:
                _verify_artifact(backup, backup_id, pg_restore)
                source = _load_json(backup / MANIFEST_NAME)
            except (BackupFailure, InstallFailure, OSError, ValueError) as exc:
                raise RestoreFailure("BACKUP_INVALID") from exc
            try:
                _, head = _active_config(root, implementation_file=Path(__file__))
                release_id = verify_active_release(root)
                active = _load_json(root / "releases" / release_id / "release_manifest.json")
            except (SchemaInitFailure, InstallFailure, OSError, ValueError) as exc:
                raise RestoreFailure("ACTIVE_RELEASE_INVALID") from exc
            if (
                source["alembic_head"] != head
                or source["release_id"] != release_id
                or source["source_sha"] != active.get("source_sha")
            ):
                raise RestoreFailure("RESTORE_SCHEMA_INCOMPATIBLE")
            try:
                settings = load_host_settings(root)
                host, port, user, production, password = _database_fields(settings)
                url = make_url(settings.database_url).set(database=target_database)
            except (ValueError, BackupFailure) as exc:
                raise RestoreFailure("HOST_CONFIG_INVALID") from exc
            if target_database == production:
                raise RestoreFailure("RESTORE_TARGET_IS_PRODUCTION")
            asyncio.run(_inspect_target(url, expected_head=None))
            stage = Path(tempfile.mkdtemp(prefix=".restore-", dir=restores))
            stage.chmod(0o700)
            metadata = stage.stat()
            stage_identity = metadata.st_dev, metadata.st_ino
            expected_count = source["storage_file_count"]
            if type(expected_count) is not int:
                raise RestoreFailure("BACKUP_INVALID")
            count, tree_hash = _extract_storage(
                backup / STORAGE_NAME, stage / "storage", expected_count
            )
            # A second full verifier catches a changed published artifact before DB writes.
            try:
                _verify_artifact(backup, backup_id, pg_restore)
            except BackupFailure as exc:
                raise RestoreFailure("BACKUP_INVALID") from exc
            _pg_restore(
                pg_restore,
                backup / DATABASE_NAME,
                stage,
                (host, port, user, target_database, password),
            )
            database_restored = True
            asyncio.run(_inspect_target(url, expected_head=head))
            stage_fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(stage_fd)
            finally:
                os.close(stage_fd)
            if os.path.lexists(final):
                raise RestoreFailure("RESTORE_INCOMPLETE_ISOLATED_TARGET")
            parent_fd = os.open(restores, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                parent = os.fstat(parent_fd)
                named = restores.lstat()
                if (
                    not stat.S_ISDIR(parent.st_mode)
                    or (parent.st_dev, parent.st_ino) != (named.st_dev, named.st_ino)
                    or parent.st_uid != owner
                    or parent.st_mode & 0o022
                ):
                    raise RestoreFailure("RESTORE_INCOMPLETE_ISOLATED_TARGET")
                _publish(stage, final)
                stage = None
                if not _same_directory(final, stage_identity):
                    raise RestoreFailure("RESTORE_INCOMPLETE_ISOLATED_TARGET")
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            _write_manifest(
                final,
                {
                    "format_version": 1,
                    "restore_id": restore_id,
                    "backup_id": backup_id,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "restored_by_uid": owner,
                    "source_release_id": release_id,
                    "source_sha": source["source_sha"],
                    "alembic_head": head,
                    "target_database": target_database,
                    "storage_file_count": count,
                    "storage_tree_sha256": tree_hash,
                },
            )
        return _result("RESTORE_COMPLETED", ok=True)
    except RestoreFailure as exc:
        database_restored = database_restored or exc.database_restored
        code = "RESTORE_INCOMPLETE_ISOLATED_TARGET" if database_restored else exc.code
        preserve_stage = database_restored and exc.preserve_stage
        return _result(code)
    except InstallFailure as exc:
        preserve_stage = database_restored
        return _result(
            "RESTORE_INCOMPLETE_ISOLATED_TARGET"
            if database_restored
            else "OPERATION_BUSY"
            if exc.code == "OPERATION_BUSY"
            else "RESTORE_LAYOUT_UNSAFE"
        )
    except Exception:  # noqa: BLE001 - no raw paths, candidate data, or credentials
        preserve_stage = database_restored
        return _result(
            "RESTORE_INCOMPLETE_ISOLATED_TARGET" if database_restored else "RESTORE_FAILED"
        )
    finally:
        if not preserve_stage and stage is not None and stage_identity is not None:
            try:
                if _same_directory(stage, stage_identity):
                    shutil.rmtree(stage)
            except OSError:
                pass

"""Small private, no-clobber evidence artifacts for installed-host operations."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from meyar.ops.backup import SAFE_ID, _publish, _rollback_publication, _same_directory
from meyar.ops.offline_host import InstallFailure, _real_directory

MAX_FILE = 16 * 1024


class EvidenceFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def area(root: Path, name: str, *, create: bool = False) -> Path:
    if name not in {"diagnostics", "reboots", "acceptance", "edges", "smoke"}:
        raise EvidenceFailure("EVIDENCE_LAYOUT_UNSAFE")
    owner = os.geteuid()
    if owner == 0 or not root.is_absolute() or ".." in root.parts:
        raise EvidenceFailure("EVIDENCE_LAYOUT_UNSAFE")
    shared = root / "shared"
    try:
        _real_directory(shared)
        for path in (root, shared):
            info = path.stat()
            if info.st_uid != owner or info.st_mode & 0o022:
                raise EvidenceFailure("EVIDENCE_LAYOUT_UNSAFE")
        target = shared / name
        if create and not os.path.lexists(target):
            target.mkdir(mode=0o700)
            sync_dir(shared)
        _real_directory(target)
        info = target.stat()
        if info.st_uid != owner or info.st_mode & 0o7777 != 0o700:
            raise EvidenceFailure("EVIDENCE_LAYOUT_UNSAFE")
        return target
    except (InstallFailure, OSError) as exc:
        raise EvidenceFailure("EVIDENCE_LAYOUT_UNSAFE") from exc


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _safe_file(path: Path, maximum: int = MAX_FILE) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o7777 != 0o600
            or info.st_size > maximum
        ):
            raise EvidenceFailure("EVIDENCE_FILE_UNSAFE")
        data = os.read(fd, maximum + 1)
        if len(data) != info.st_size:
            raise EvidenceFailure("EVIDENCE_FILE_UNSAFE")
        return data
    finally:
        os.close(fd)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_safe_file(path), object_pairs_hook=unique_object)
        if not isinstance(value, dict):
            raise ValueError("not object")
        return value
    except (OSError, ValueError, UnicodeError) as exc:
        raise EvidenceFailure("EVIDENCE_INVALID") from exc


def _write(stage: Path, name: str, data: bytes) -> None:
    if len(data) > MAX_FILE:
        raise EvidenceFailure("EVIDENCE_TOO_LARGE")
    fd = os.open(stage / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def verify_bundle(path: Path, names: frozenset[str]) -> dict[str, dict[str, Any]]:
    try:
        _real_directory(path)
        info = path.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o7777 != 0o700:
            raise EvidenceFailure("EVIDENCE_LAYOUT_UNSAFE")
        if {entry.name for entry in path.iterdir()} != names | {"checksums.sha256"}:
            raise EvidenceFailure("EVIDENCE_MEMBERS_INVALID")
        checksums = _safe_file(path / "checksums.sha256", maximum=4096).decode("ascii")
        expected = {name: hashlib.sha256(_safe_file(path / name)).hexdigest() for name in names}
        actual: dict[str, str] = {}
        for line in checksums.splitlines():
            digest, separator, name = line.partition("  ")
            if not separator or name in actual or name not in names or len(digest) != 64:
                raise EvidenceFailure("EVIDENCE_HASH_INVALID")
            actual[name] = digest
        if actual != expected:
            raise EvidenceFailure("EVIDENCE_HASH_INVALID")
        return {name: read_json(path / name) for name in names}
    except (InstallFailure, OSError, UnicodeError, ValueError) as exc:
        raise EvidenceFailure("EVIDENCE_INVALID") from exc


def publish_bundle(parent: Path, item_id: str, files: dict[str, dict[str, Any]]) -> Path:
    if SAFE_ID.fullmatch(item_id) is None or not files or "checksums.sha256" in files:
        raise EvidenceFailure("EVIDENCE_ID_INVALID")
    final = parent / item_id
    if os.path.lexists(final):
        raise EvidenceFailure("EVIDENCE_ID_CONFLICT")
    stage = Path(tempfile.mkdtemp(prefix=".evidence-", dir=parent))
    stage.chmod(0o700)
    info = stage.stat()
    identity = info.st_dev, info.st_ino
    published = False
    try:
        hashes: dict[str, str] = {}
        for name, payload in sorted(files.items()):
            if not name.endswith(".json") or "/" in name or name.startswith("."):
                raise EvidenceFailure("EVIDENCE_MEMBERS_INVALID")
            data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
            _write(stage, name, data)
            hashes[name] = hashlib.sha256(data).hexdigest()
        checksum_data = "".join(f"{digest}  {name}\n" for name, digest in sorted(hashes.items()))
        _write(stage, "checksums.sha256", checksum_data.encode("ascii"))
        sync_dir(stage)
        verify_bundle(stage, frozenset(files))
        _publish(stage, final)
        published = True
        try:
            sync_dir(parent)
        except OSError as exc:
            if _rollback_publication(stage, final, identity):
                sync_dir(parent)
            raise EvidenceFailure("EVIDENCE_PUBLICATION_UNCERTAIN") from exc
        return final
    except EvidenceFailure:
        raise
    except OSError as exc:
        raise EvidenceFailure("EVIDENCE_PUBLICATION_UNCERTAIN") from exc
    finally:
        # Only remove an unpublished stage whose inode and exact files are ours.
        if not published and _same_directory(stage, identity):
            for name in (*files, "checksums.sha256"):
                candidate = stage / name
                if candidate.exists() and candidate.is_file() and not candidate.is_symlink():
                    candidate.unlink()
            if not any(stage.iterdir()):
                stage.rmdir()

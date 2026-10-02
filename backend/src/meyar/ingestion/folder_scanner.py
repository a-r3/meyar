import errno
import hashlib
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from meyar.ingestion.validation import SUPPORTED_EXTENSIONS


class InvalidSourceRootError(ValueError):
    """The configured local folder does not exist or is not a directory."""


@dataclass(frozen=True)
class DiscoveredFile:
    """One supported CV file found under a scan root. relative_path is
    POSIX-normalized and always relative to the scan root — never a raw
    absolute path — so the same source is portable across machines.
    mtime is the filesystem modification time (seconds since epoch) at
    scan time, used by the caller to apply a file-stability window
    (Slice 14) — never interpreted here, since the stability policy
    (threshold, whether to apply it) belongs to the service layer."""

    relative_path: str
    data: bytes
    byte_size: int
    sha256_hash: str
    mtime: float


@dataclass(frozen=True)
class OversizedFile:
    """A supported file whose size, taken from filesystem metadata of the
    opened file, exceeds the ingestion cap. Its content was never read, so
    it carries no bytes and no hash (a hash would require reading it)."""

    relative_path: str
    byte_size: int
    mtime: float


@dataclass(frozen=True)
class UnstableFile:
    """A supported file that vanished, stopped being a regular file, or
    changed while it was being read. It is not imported this pass and is
    not a permanent failure: a later scan observes it again."""

    relative_path: str


# Closed set of scan outcomes; callers must handle each explicitly.
ScanEntry = DiscoveredFile | OversizedFile | UnstableFile

_READ_CHUNK_BYTES = 1024 * 1024


def _identity(result: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        result.st_size,
        result.st_mtime_ns,
        result.st_ctime_ns,
        result.st_ino,
        result.st_dev,
    )


def _read_bounded_stable(path: Path, relative_path: str, max_bytes: int) -> ScanEntry:
    """Open once, decide from that descriptor's metadata, read at most
    ``max_bytes + 1`` bytes, then re-check the same descriptor. Never an
    unbounded read; the hash is computed only from bytes accepted as a
    stable snapshot."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return UnstableFile(relative_path=relative_path)
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # a symlink replaced the file after the walk
            return UnstableFile(relative_path=relative_path)
        raise
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return UnstableFile(relative_path=relative_path)
        if before.st_size > max_bytes:
            return OversizedFile(
                relative_path=relative_path, byte_size=before.st_size, mtime=before.st_mtime
            )
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            chunk = os.read(fd, min(_READ_CHUNK_BYTES, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if total > max_bytes or total != before.st_size or _identity(before) != _identity(after):
        return UnstableFile(relative_path=relative_path)
    data = b"".join(chunks)
    return DiscoveredFile(
        relative_path=relative_path,
        data=data,
        byte_size=total,
        sha256_hash=hashlib.sha256(data).hexdigest(),
        mtime=before.st_mtime,
    )


def resolve_source_root(root_path: str) -> Path:
    """Validates root_path eagerly (not lazily, unlike a generator body)
    so a caller can check it before making any state change — e.g.
    before creating a FolderSource row. Raises InvalidSourceRootError if
    missing, not a directory, or itself a symlink."""
    root = Path(root_path).expanduser()
    if not root.is_dir() or root.is_symlink():
        raise InvalidSourceRootError(f"Source root does not exist or is not a directory: {root}")
    return root.resolve()


def scan_source_root(root_path: str, *, max_bytes: int) -> Iterator[ScanEntry]:
    """Recursively discovers supported (PDF/DOCX, case-insensitive) files
    under root_path, in deterministic relative-path order. Symlinks are
    never followed — neither directories nor files — so a scan can never
    escape the configured root via a symlink (see docs/DECISIONS.md).
    Unsupported extensions are silently skipped, not reported as
    failures. Raises InvalidSourceRootError if root_path is missing or
    not a directory. Every file is opened once and read at most
    ``max_bytes + 1`` bytes (see ``_read_bounded_stable``); an oversized
    file is reported as ``OversizedFile`` without being read, and a file
    that changes during the read as ``UnstableFile``."""
    root = resolve_source_root(root_path)

    relative_paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Sort in place so both traversal order and yield order are
        # deterministic across runs/platforms.
        dirnames[:] = sorted(
            d for d in dirnames if not (Path(dirpath) / d).is_symlink()
        )
        current_dir = Path(dirpath)
        for filename in sorted(filenames):
            file_path = current_dir / filename
            if file_path.is_symlink():
                continue
            if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            relative = file_path.relative_to(root).as_posix()
            relative_paths.append(relative)

    for relative in sorted(relative_paths):
        file_path = root / relative
        # Defense in depth: the file must still resolve under root even
        # though os.walk(followlinks=False) already prevents traversal
        # through a symlinked directory.
        resolved = file_path.resolve()
        if not resolved.is_relative_to(root):
            continue
        yield _read_bounded_stable(resolved, relative, max_bytes)

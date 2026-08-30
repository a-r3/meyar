import hashlib
import os
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


def resolve_source_root(root_path: str) -> Path:
    """Validates root_path eagerly (not lazily, unlike a generator body)
    so a caller can check it before making any state change — e.g.
    before creating a FolderSource row. Raises InvalidSourceRootError if
    missing, not a directory, or itself a symlink."""
    root = Path(root_path).expanduser()
    if not root.is_dir() or root.is_symlink():
        raise InvalidSourceRootError(f"Source root does not exist or is not a directory: {root}")
    return root.resolve()


def scan_source_root(root_path: str) -> Iterator[DiscoveredFile]:
    """Recursively discovers supported (PDF/DOCX, case-insensitive) files
    under root_path, in deterministic relative-path order. Symlinks are
    never followed — neither directories nor files — so a scan can never
    escape the configured root via a symlink (see docs/DECISIONS.md).
    Unsupported extensions are silently skipped, not reported as
    failures. Raises InvalidSourceRootError if root_path is missing or
    not a directory; does not raise on individual unreadable files within
    the tree (those simply cannot be discovered)."""
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
        stat_result = resolved.stat()
        data = resolved.read_bytes()
        yield DiscoveredFile(
            relative_path=relative,
            data=data,
            byte_size=len(data),
            sha256_hash=hashlib.sha256(data).hexdigest(),
            mtime=stat_result.st_mtime,
        )

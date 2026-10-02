"""Issue #46 PR-1 — bounded, stable folder-scanner reads. Synthetic bytes
only; nothing large is written."""

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from meyar.ingestion import folder_scanner
from meyar.ingestion.folder_scanner import (
    DiscoveredFile,
    OversizedFile,
    UnstableFile,
    scan_source_root,
)


def _mutate_once_on(target: Path, mutation: Any):  # noqa: ANN202
    """An os.read replacement that runs ``mutation`` once, only for reads of
    ``target`` (os.read is process-global, so unrelated reads are untouched)."""
    real_read = os.read
    inode = target.stat().st_ino
    done = {"value": False}

    def patched(fd: int, size: int) -> bytes:
        chunk = real_read(fd, size)
        if not done["value"] and os.fstat(fd).st_ino == inode:
            done["value"] = True
            mutation()
        return chunk

    return patched


def _append(path: Path, extra: bytes) -> Any:
    def mutate() -> None:
        with path.open("ab") as handle:
            handle.write(extra)

    return mutate


def _write(path: Path, size: int, byte: bytes = b"a") -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = byte * size
    path.write_bytes(data)
    return data


def test_oversized_file_is_never_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path / "big.pdf", 5000)

    def forbidden(*_a: Any, **_k: Any) -> bytes:
        raise AssertionError("an oversized file must not be read")

    monkeypatch.setattr(folder_scanner.os, "read", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    entries = list(scan_source_root(str(tmp_path), max_bytes=1000))
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, OversizedFile)
    assert (entry.relative_path, entry.byte_size) == ("big.pdf", 5000)
    assert not hasattr(entry, "data") and not hasattr(entry, "sha256_hash")


def test_oversized_file_does_not_abort_the_rest_of_the_scan(tmp_path: Path) -> None:
    _write(tmp_path / "a.pdf", 100, b"a")
    _write(tmp_path / "b.pdf", 5000, b"b")
    _write(tmp_path / "sub" / "c.docx", 200, b"c")
    entries = list(scan_source_root(str(tmp_path), max_bytes=1000))
    assert [(type(e).__name__, e.relative_path) for e in entries] == [
        ("DiscoveredFile", "a.pdf"),
        ("OversizedFile", "b.pdf"),
        ("DiscoveredFile", "sub/c.docx"),
    ]


def test_within_limit_file_is_read_boundedly_with_correct_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _write(tmp_path / "ok.pdf", 3_000_000)  # spans several read chunks
    real_read = os.read
    requested: list[int] = []
    delivered: list[int] = []

    def recording(fd: int, size: int) -> bytes:
        assert size > 0
        requested.append(size)
        chunk = real_read(fd, size)
        delivered.append(len(chunk))
        return chunk

    monkeypatch.setattr(folder_scanner.os, "read", recording)
    (entry,) = scan_source_root(str(tmp_path), max_bytes=3_000_000)
    assert isinstance(entry, DiscoveredFile)
    assert entry.data == data and entry.byte_size == len(data)
    assert entry.sha256_hash == hashlib.sha256(data).hexdigest()
    assert sum(delivered) <= 3_000_000 + 1  # never more than max_bytes + 1
    assert max(requested) <= 1_048_576


def test_file_exactly_at_limit_is_accepted(tmp_path: Path) -> None:
    _write(tmp_path / "edge.pdf", 1000)
    (entry,) = scan_source_root(str(tmp_path), max_bytes=1000)
    assert isinstance(entry, DiscoveredFile)


def test_file_changed_during_read_is_unstable_not_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "moving.pdf"
    _write(target, 500)
    monkeypatch.setattr(
        folder_scanner.os, "read", _mutate_once_on(target, _append(target, b"more"))
    )
    (entry,) = scan_source_root(str(tmp_path), max_bytes=1000)
    assert entry == UnstableFile(relative_path="moving.pdf")


def test_file_growing_past_the_limit_during_read_is_unstable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "grow.pdf"
    _write(target, 1000)
    monkeypatch.setattr(
        folder_scanner.os, "read", _mutate_once_on(target, _append(target, b"x" * 50))
    )
    (entry,) = scan_source_root(str(tmp_path), max_bytes=1000)
    assert isinstance(entry, UnstableFile)


def test_file_vanishing_after_discovery_is_unstable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / "gone.pdf", 10)
    real_open = os.open

    def vanish(path: Any, flags: int, *a: Any, **k: Any) -> int:
        if str(path).endswith("gone.pdf"):
            raise FileNotFoundError
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(folder_scanner.os, "open", vanish)
    (entry,) = scan_source_root(str(tmp_path), max_bytes=1000)
    assert entry == UnstableFile(relative_path="gone.pdf")


def test_symlink_swapped_in_after_walk_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    root = tmp_path / "root"
    _write(root / "swap.pdf", 10)
    real_open = os.open

    def swap_then_open(path: Any, flags: int, *a: Any, **k: Any) -> int:
        if str(path).endswith("swap.pdf"):
            Path(path).unlink()
            Path(path).symlink_to(outside)
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(folder_scanner.os, "open", swap_then_open)
    (entry,) = scan_source_root(str(root), max_bytes=1000)
    assert entry == UnstableFile(relative_path="swap.pdf")

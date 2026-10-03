"""Issue #46 PR-1 — bounded, stable folder-scanner reads. Synthetic bytes
only; nothing large is written."""

import errno
import hashlib
import os
import stat
import sys
import threading
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
    assert sum(delivered) == 2 * len(data)  # stable acquisition + bounded verification
    assert sum(delivered) <= 2 * (3_000_000 + 1)
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


# --- special files (FIFO) must never block a scan ------------------------

_needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo unavailable")


@_needs_fifo
def test_fifo_with_supported_extension_is_unstable_and_never_blocks(tmp_path: Path) -> None:
    _write(tmp_path / "a_before.pdf", 50, b"a")
    os.mkfifo(tmp_path / "pipe.pdf")  # no writer: a plain open() would hang
    os.mkfifo(tmp_path / "pipe2.DOCX")
    _write(tmp_path / "z_after.pdf", 60, b"z")

    result: list[Any] = []
    worker = threading.Thread(
        target=lambda: result.extend(scan_source_root(str(tmp_path), max_bytes=1000)),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "scanning blocked on a FIFO"

    assert [(type(e).__name__, e.relative_path) for e in result] == [
        ("DiscoveredFile", "a_before.pdf"),
        ("UnstableFile", "pipe.pdf"),
        ("UnstableFile", "pipe2.DOCX"),
        ("DiscoveredFile", "z_after.pdf"),
    ]


@_needs_fifo
def test_regular_file_swapped_for_fifo_after_precheck_cannot_block_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lstat pre-check alone is not enough: replace the regular file
    with a FIFO between the pre-check and the open."""
    target = tmp_path / "swap.pdf"
    _write(target, 10)
    real_lstat = os.lstat
    swapped = {"value": False}

    def lstat_then_swap(path: Any, *a: Any, **k: Any) -> os.stat_result:
        result = real_lstat(path, *a, **k)
        # Only the scanner's own pre-check (not the directory walk's
        # symlink probes) is followed by the swap to a FIFO.
        caller = sys._getframe(1).f_code.co_name
        if caller == "_read_bounded_stable" and not swapped["value"]:
            swapped["value"] = True
            assert stat.S_ISREG(result.st_mode)  # pre-check sees a regular file ...
            Path(path).unlink()
            os.mkfifo(path)  # ... which is a FIFO by the time open() runs
        return result

    monkeypatch.setattr(folder_scanner.os, "lstat", lstat_then_swap)
    outcome: list[Any] = []
    worker = threading.Thread(
        target=lambda: outcome.extend(scan_source_root(str(tmp_path), max_bytes=1000)),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "open() blocked on a FIFO swapped in after the pre-check"
    assert swapped["value"] is True
    assert outcome == [UnstableFile(relative_path="swap.pdf")]


def test_permission_error_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / "locked.pdf", 10)
    real_open = os.open

    def deny(path: Any, flags: int, *a: Any, **k: Any) -> int:
        if str(path).endswith("locked.pdf"):
            raise PermissionError(errno.EACCES, "denied")
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(folder_scanner.os, "open", deny)
    with pytest.raises(PermissionError):
        list(scan_source_root(str(tmp_path), max_bytes=1000))

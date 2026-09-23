"""Storage write probe (issue #35 PR1 §6/§12): writable, unwritable,
and always-cleaned-up."""

import os
from pathlib import Path

from meyar.ops.storage_probe import probe_storage_writable


def test_writable_root_reports_writable_and_leaves_no_probe_file(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    result = probe_storage_writable(root)
    assert result.writable is True
    remaining = list(root.iterdir())
    assert remaining == [], f"probe file(s) left behind: {remaining}"


def test_creates_root_if_missing(tmp_path: Path) -> None:
    root = tmp_path / "does" / "not" / "exist" / "yet"
    assert not root.exists()
    result = probe_storage_writable(root)
    assert result.writable is True
    assert root.is_dir()


def test_unwritable_root_reports_not_writable(tmp_path: Path) -> None:
    root = tmp_path / "readonly"
    root.mkdir()
    os.chmod(root, 0o500)  # read + execute, no write
    try:
        result = probe_storage_writable(root)
        assert result.writable is False
        assert "not writable" in result.message
    finally:
        os.chmod(root, 0o700)  # restore so tmp_path cleanup can remove it


def test_never_overwrites_an_existing_file(tmp_path: Path) -> None:
    """The probe always uses a fresh UUID name, so an existing file in the
    storage root (some other operator/tenant content) is left untouched."""
    root = tmp_path / "storage"
    root.mkdir()
    sentinel = root / "do-not-touch.txt"
    sentinel.write_text("precious")
    probe_storage_writable(root)
    assert sentinel.read_text() == "precious"


def test_probe_cleans_up_even_after_success(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    probe_storage_writable(root)
    probe_storage_writable(root)  # run twice: no leftover accumulation
    assert list(root.iterdir()) == []

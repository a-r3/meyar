"""Storage write probe (issue #35 PR1 §6/§12): writable, unwritable,
always-cleaned-up, and — per the corrective review — never provisions
host state (never creates the storage root or any parent directory)."""

import os
import uuid
from pathlib import Path

import meyar.ops.storage_probe as storage_probe_module
from meyar.ops.storage_probe import probe_storage_writable


def test_writable_root_reports_writable_and_leaves_no_probe_file(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    result = probe_storage_writable(root)
    assert result.writable is True
    remaining = list(root.iterdir())
    assert remaining == [], f"probe file(s) left behind: {remaining}"


def test_missing_root_reports_not_writable_and_is_never_created(tmp_path: Path) -> None:
    """A readiness probe must not provision host state: a missing storage
    root is reported truthfully as not writable, and the root (and any
    missing parent directory) must remain absent afterward."""
    root = tmp_path / "does" / "not" / "exist" / "yet"
    assert not root.exists()
    result = probe_storage_writable(root)
    assert result.writable is False
    assert "does not exist" in result.message
    assert not root.exists()
    assert not root.parent.exists()
    assert not root.parent.parent.exists()


def test_root_that_is_a_regular_file_reports_not_writable(tmp_path: Path) -> None:
    root = tmp_path / "not-a-directory"
    root.write_text("i am a file, not a storage root")
    result = probe_storage_writable(root)
    assert result.writable is False
    assert "not a directory" in result.message


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
    root.mkdir()
    probe_storage_writable(root)
    probe_storage_writable(root)  # run twice: no leftover accumulation
    assert list(root.iterdir()) == []


def test_uuid_collision_never_deletes_a_preexisting_file(
    tmp_path: Path, monkeypatch
) -> None:
    """Corrective review (PR #52, Blocker 2): the probe path always uses a
    fresh UUID, but `finally: probe_path.unlink(missing_ok=True)`
    previously ran unconditionally — so a forced/collided UUID whose
    O_CREAT|O_EXCL open failed (because that exact name already existed)
    would still be deleted by this invocation, even though it never
    created it. Force the collision deterministically and prove the
    pre-existing file is left completely untouched."""
    root = tmp_path / "storage"
    root.mkdir()
    fixed_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    monkeypatch.setattr(storage_probe_module.uuid, "uuid4", lambda: fixed_uuid)

    colliding_path = root / f".meyar-ops-probe-{fixed_uuid.hex}"
    colliding_path.write_text("pre-existing content — must never be deleted")

    result = probe_storage_writable(root)

    assert result.writable is False
    assert colliding_path.exists()
    assert colliding_path.read_text() == "pre-existing content — must never be deleted"


def test_uuid_no_collision_still_creates_and_cleans_up(
    tmp_path: Path, monkeypatch
) -> None:
    """Companion to the collision regression above: with the same fixed
    UUID but no pre-existing file, the probe must still successfully
    create, write, report writable, and clean up after itself — the fix
    must not accidentally suppress cleanup of a probe file this
    invocation actually created."""
    root = tmp_path / "storage"
    root.mkdir()
    fixed_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    monkeypatch.setattr(storage_probe_module.uuid, "uuid4", lambda: fixed_uuid)

    result = probe_storage_writable(root)

    assert result.writable is True
    assert list(root.iterdir()) == []

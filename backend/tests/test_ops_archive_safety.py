"""Archive member safety inspection (issue #35 PR1 §10/§11). Every
fixture here is built synthetically in-memory/tmp_path — no binary
fixture is committed to the repo, and `inspect_archive_members` never
extracts anything to disk."""

import io
import tarfile
from pathlib import Path

import pytest

from meyar.ops.archive_safety import inspect_archive_members

EXPECTED_ROOT = "meyar-0.1.0+abcdef012345"


def _make_archive(
    tmp_path: Path, members: list[tarfile.TarInfo], name: str = "artifact.tar"
) -> Path:
    archive_path = tmp_path / name
    with tarfile.open(archive_path, mode="w") as tf:
        for info in members:
            if info.isfile():
                tf.addfile(info, fileobj=io.BytesIO(b""))
            else:
                tf.addfile(info)
    return archive_path


def _file_member(name: str) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name=name)
    info.size = 0
    return info


def test_safe_archive_has_no_violations(tmp_path: Path) -> None:
    members = [
        _file_member(f"{EXPECTED_ROOT}/release_manifest.json"),
        _file_member(f"{EXPECTED_ROOT}/app/main.py"),
    ]
    archive = _make_archive(tmp_path, members)
    violations = inspect_archive_members(archive, expected_root=EXPECTED_ROOT)
    assert violations == []


def test_absolute_path_rejected(tmp_path: Path) -> None:
    members = [_file_member("/etc/passwd")]
    archive = _make_archive(tmp_path, members)
    violations = inspect_archive_members(archive, expected_root=EXPECTED_ROOT)
    assert any("absolute path" in v.reason for v in violations)


def test_dot_dot_traversal_rejected(tmp_path: Path) -> None:
    members = [_file_member(f"{EXPECTED_ROOT}/../../escape.txt")]
    archive = _make_archive(tmp_path, members)
    violations = inspect_archive_members(archive, expected_root=EXPECTED_ROOT)
    assert any("traversal" in v.reason for v in violations)


def test_symlink_rejected(tmp_path: Path) -> None:
    info = tarfile.TarInfo(name=f"{EXPECTED_ROOT}/evil-link")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    archive = _make_archive(tmp_path, [info])
    violations = inspect_archive_members(archive, expected_root=EXPECTED_ROOT)
    assert any("symlink" in v.reason for v in violations)


def test_hardlink_rejected(tmp_path: Path) -> None:
    info = tarfile.TarInfo(name=f"{EXPECTED_ROOT}/evil-hardlink")
    info.type = tarfile.LNKTYPE
    info.linkname = f"{EXPECTED_ROOT}/app/main.py"
    archive = _make_archive(tmp_path, [info])
    violations = inspect_archive_members(archive, expected_root=EXPECTED_ROOT)
    assert any("hard link" in v.reason for v in violations)


@pytest.mark.parametrize(
    "member_type",
    [tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE],
)
def test_device_and_special_files_rejected(tmp_path: Path, member_type: bytes) -> None:
    info = tarfile.TarInfo(name=f"{EXPECTED_ROOT}/evil-device")
    info.type = member_type
    archive = _make_archive(tmp_path, [info])
    violations = inspect_archive_members(archive, expected_root=EXPECTED_ROOT)
    assert any("device/special" in v.reason for v in violations)


def test_path_escaping_expected_root_rejected(tmp_path: Path) -> None:
    members = [_file_member("some-other-root/app/main.py")]
    archive = _make_archive(tmp_path, members)
    violations = inspect_archive_members(archive, expected_root=EXPECTED_ROOT)
    assert any("escapes expected artifact root" in v.reason for v in violations)


def test_bare_root_directory_entry_itself_is_allowed(tmp_path: Path) -> None:
    info = tarfile.TarInfo(name=EXPECTED_ROOT)
    info.type = tarfile.DIRTYPE
    archive = _make_archive(tmp_path, [info])
    violations = inspect_archive_members(archive, expected_root=EXPECTED_ROOT)
    assert violations == []


def test_multiple_violations_all_reported_not_just_first(tmp_path: Path) -> None:
    members = [
        _file_member("/etc/passwd"),
        _file_member(f"{EXPECTED_ROOT}/../../escape.txt"),
        _file_member("wrong-root/x"),
    ]
    archive = _make_archive(tmp_path, members)
    violations = inspect_archive_members(archive, expected_root=EXPECTED_ROOT)
    assert len(violations) >= 3

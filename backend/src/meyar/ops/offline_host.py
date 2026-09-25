"""Standalone, stdlib-only offline host installer for a MEYAR deployment bundle.

This file is copied byte-for-byte from the selected application release into
the bundle as ``meyar-ops.py``. Run it with the bank-provisioned Python 3.12
interpreter; no uv, project dependency, Git checkout, or network is used.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Never

MAX_MANIFEST = 1024 * 1024
MAX_MEMBERS = 5000
MAX_MEMBER_NAME = 400
MAX_UNCOMPRESSED = 1024 * 1024 * 1024
MAX_FILE = 1024 * 1024 * 1024
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SAFE_RELEASE = re.compile(r"^meyar-[A-Za-z0-9.+_-]{1,80}\+[0-9a-f]{12}$")
ROLLBACK_VALUES = {
    "APP_ONLY",
    "FORWARD_COMPATIBLE_SCHEMA",
    "BACKUP_RESTORE_REQUIRED",
    "PROHIBITED_PENDING_PROCEDURE",
}


class InstallFailure(Exception):
    """Safe error code only; never includes input paths or subprocess output."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _safe_component(name: str) -> bool:
    return bool(
        name
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and all(32 <= ord(char) < 127 for char in name)
    )


def _real_directory(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise InstallFailure("UNSAFE_PATH")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise InstallFailure("DIRECTORY_UNAVAILABLE") from exc
        if not stat.S_ISDIR(mode):
            raise InstallFailure("UNSAFE_PATH")


def _regular_file(path: Path, *, maximum: int = MAX_FILE) -> None:
    if not _safe_component(path.name):
        raise InstallFailure("UNSAFE_PATH")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise InstallFailure("FILE_UNAVAILABLE") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
        raise InstallFailure("UNSAFE_FILE")


def _owned_directory(path: Path) -> None:
    _real_directory(path)
    metadata = path.stat()
    if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
        raise InstallFailure("INSTALL_ROOT_PERMISSIONS_UNSAFE")


def _load_json(path: Path) -> dict[str, object]:
    _regular_file(path, maximum=MAX_MANIFEST)
    with path.open("rb") as stream:
        data = stream.read(MAX_MANIFEST + 1)
    if len(data) > MAX_MANIFEST:
        raise InstallFailure("MANIFEST_TOO_LARGE")
    try:
        value = json.loads(data)
    except (UnicodeError, ValueError) as exc:
        raise InstallFailure("MANIFEST_INVALID") from exc
    if not isinstance(value, dict):
        raise InstallFailure("MANIFEST_INVALID")
    return value


def _required_text(value: object, *, limit: int = 300) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise InstallFailure("MANIFEST_INVALID")
    return value


def _manifest_digest(value: object) -> str:
    text = _required_text(value, limit=64)
    if HEX64.fullmatch(text) is None:
        raise InstallFailure("MANIFEST_INVALID")
    return text


def _verify_bundle(bundle: Path) -> dict[str, object]:
    _real_directory(bundle)
    manifest = _load_json(bundle / "deployment_manifest.json")
    release_id = _required_text(manifest.get("release_id"), limit=128)
    if SAFE_RELEASE.fullmatch(release_id) is None or manifest.get("bundle_format_version") != 1:
        raise InstallFailure("BUNDLE_IDENTITY_INVALID")
    if manifest.get("application_artifact") != f"{release_id}.tar.gz":
        raise InstallFailure("BUNDLE_IDENTITY_INVALID")
    if manifest.get("target_os") != "Darwin" or manifest.get("target_architecture") != "arm64":
        raise InstallFailure("TARGET_UNSUPPORTED")
    if manifest.get("target_wheel_platform") != "macosx_11_0_arm64":
        raise InstallFailure("TARGET_UNSUPPORTED")
    if manifest.get("required_python_version") != ">=3.12":
        raise InstallFailure("PYTHON_REQUIREMENT_UNSUPPORTED")
    rollback = manifest.get("rollback_compatibility")
    if not isinstance(rollback, str) or rollback not in ROLLBACK_VALUES:
        raise InstallFailure("ROLLBACK_CLASSIFICATION_INVALID")
    source_sha = _required_text(manifest.get("source_sha"), limit=40)
    if len(source_sha) != 40 or any(c not in "0123456789abcdef" for c in source_sha):
        raise InstallFailure("BUNDLE_IDENTITY_INVALID")
    if not release_id.endswith("+" + source_sha[:12]):
        raise InstallFailure("BUNDLE_IDENTITY_INVALID")
    files = [
        (
            _required_text(manifest.get("application_artifact"), limit=255),
            manifest.get("application_artifact_sha256"),
        ),
        (f"{release_id}.release-manifest.json", manifest.get("release_manifest_sha256")),
        ("SHA256SUMS", manifest.get("sha256sums_sha256")),
        ("meyar-ops.py", manifest.get("installer_sha256")),
    ]
    for name, digest in files:
        if not _safe_component(name):
            raise InstallFailure("UNSAFE_PATH")
        path = bundle / name
        _regular_file(path)
        if _sha256(path) != _manifest_digest(digest):
            raise InstallFailure("BUNDLE_HASH_MISMATCH")
    requirements = bundle / "requirements.txt"
    _regular_file(requirements, maximum=MAX_MANIFEST)
    requirement_bytes = requirements.read_bytes()
    wheels = manifest.get("wheels")
    if not isinstance(wheels, list) or not 1 <= len(wheels) <= 150:
        raise InstallFailure("DEPENDENCY_LIST_INVALID")
    names: set[str] = set()
    expected_wheel_files: set[str] = set()
    digest_bytes = bytearray()
    expected_lines: list[str] = []
    for wheel in wheels:
        if not isinstance(wheel, dict):
            raise InstallFailure("DEPENDENCY_LIST_INVALID")
        name = _required_text(wheel.get("name"), limit=128)
        version = _required_text(wheel.get("version"), limit=64)
        filename = _required_text(wheel.get("filename"), limit=255)
        digest = _manifest_digest(wheel.get("sha256"))
        if not _safe_component(filename) or not filename.endswith(".whl"):
            raise InstallFailure("UNSAFE_WHEEL_NAME")
        if filename in expected_wheel_files:
            raise InstallFailure("DEPENDENCY_LIST_INVALID")
        if name in names or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
            raise InstallFailure("DEPENDENCY_LIST_INVALID")
        names.add(name)
        expected_wheel_files.add(filename)
        _real_directory(bundle / "wheels")
        wheel_path = bundle / "wheels" / filename
        _regular_file(wheel_path)
        if _sha256(wheel_path) != digest:
            raise InstallFailure("DEPENDENCY_HASH_MISMATCH")
        digest_bytes.extend(bytes.fromhex(digest))
        expected_lines.append(f"{name}=={version} --hash=sha256:{digest}\n")
    if "pillow" not in names or "greenlet" not in names:
        raise InstallFailure("DEPENDENCY_MISSING")
    if {path.name for path in (bundle / "wheels").iterdir()} != expected_wheel_files:
        raise InstallFailure("DEPENDENCY_LIST_INVALID")
    if requirement_bytes != "".join(expected_lines).encode():
        raise InstallFailure("REQUIREMENTS_MISMATCH")
    payload_sha = hashlib.sha256(requirement_bytes + digest_bytes).hexdigest()
    if payload_sha != _manifest_digest(manifest.get("dependency_payload_sha256")):
        raise InstallFailure("DEPENDENCY_HASH_MISMATCH")
    release_manifest = _load_json(bundle / f"{release_id}.release-manifest.json")
    for key in (
        "release_id",
        "source_sha",
        "uv_lock_sha256",
        "required_python_version",
        "alembic_heads",
        "rollback_compatibility",
    ):
        if manifest.get(key) != release_manifest.get(key):
            raise InstallFailure("RELEASE_MANIFEST_MISMATCH")
    model = release_manifest.get("model_manifest")
    if (
        not isinstance(model, dict)
        or manifest.get("model_manifest_reference") != model.get("reference")
        or manifest.get("model_approval_status") != model.get("status")
    ):
        raise InstallFailure("RELEASE_MANIFEST_MISMATCH")
    checksums = (bundle / "SHA256SUMS").read_text()
    expected = {
        files[0][0]: _manifest_digest(files[0][1]),
        files[1][0]: _manifest_digest(files[1][1]),
    }
    actual: dict[str, str] = {}
    for line in checksums.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        digest, name = parts
        if name in actual:
            raise InstallFailure("CHECKSUM_AMBIGUOUS")
        actual[name] = digest
    if any(actual.get(name) != digest for name, digest in expected.items()):
        raise InstallFailure("APPLICATION_HASH_MISMATCH")
    allowed = {
        "deployment_manifest.json",
        "requirements.txt",
        "meyar-ops.py",
        "SHA256SUMS",
        "wheels",
        files[0][0],
        files[1][0],
    }
    if {path.name for path in bundle.iterdir()} != allowed:
        raise InstallFailure("BUNDLE_CONTENT_UNEXPECTED")
    return manifest


def _check_host_runtime(manifest: dict[str, object]) -> None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise InstallFailure("PLATFORM_UNSUPPORTED")
    version = ".".join(map(str, sys.version_info[:3]))
    if version != manifest.get("runtime_version") or sys.implementation.name != "cpython":
        raise InstallFailure("PYTHON_RUNTIME_MISMATCH")
    if _sha256(Path(sys.executable).resolve()) != _manifest_digest(
        manifest.get("runtime_executable_sha256")
    ):
        raise InstallFailure("PYTHON_RUNTIME_MISMATCH")
    mac_version = platform.mac_ver()[0]
    try:
        major = int(mac_version.split(".")[0])
    except (ValueError, IndexError) as exc:
        raise InstallFailure("MACOS_VERSION_UNKNOWN") from exc
    if major < 11:
        raise InstallFailure("MACOS_VERSION_UNSUPPORTED")


@contextlib.contextmanager
def _operation_lock(root: Path):
    _owned_directory(root)
    descriptor = os.open(root / ".meyar-ops.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        lock_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(lock_stat.st_mode)
            or lock_stat.st_nlink != 1
            or lock_stat.st_uid != os.geteuid()
            or lock_stat.st_mode & 0o022
        ):
            raise InstallFailure("LOCK_UNSAFE")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallFailure("OPERATION_BUSY") from exc
        yield
    finally:
        os.close(descriptor)


def _ensure_layout(root: Path) -> None:
    _owned_directory(root)
    for relative in (
        "releases",
        "activations",
        "shared",
        "shared/config",
        "shared/storage",
        "shared/backups",
        "shared/logs",
    ):
        path = root / relative
        try:
            path.mkdir(mode=0o750)
        except FileExistsError:
            pass
        _owned_directory(path)


def _extract_application(
    artifact: Path,
    release_id: str,
    destination: Path,
    expected_lock_sha: str,
    expected_artifact_sha: str,
) -> None:
    _regular_file(artifact)
    seen: set[str] = set()
    count = 0
    total = 0
    lock_found = False
    with artifact.open("rb") as source:
        digest = hashlib.sha256()
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
        if digest.hexdigest() != expected_artifact_sha:
            raise InstallFailure("APPLICATION_HASH_MISMATCH")
        source.seek(0)
        with tarfile.open(fileobj=source, mode="r:gz") as archive:
            while member := archive.next():
                count += 1
                total += max(member.size, 0)
                name = member.name
                path = PurePosixPath(name)
                if count > MAX_MEMBERS or total > MAX_UNCOMPRESSED or len(name) > MAX_MEMBER_NAME:
                    raise InstallFailure("ARCHIVE_BOUND_EXCEEDED")
                if (
                    name in seen
                    or path.is_absolute()
                    or ".." in path.parts
                    or "\\" in name
                    or any(ord(char) < 32 or ord(char) == 127 for char in name)
                    or member.size < 0
                    or not path.parts
                    or path.parts[0] != release_id
                    or not member.isfile()
                ):
                    raise InstallFailure("ARCHIVE_UNSAFE")
                seen.add(name)
                relative = Path(*path.parts[1:])
                if not relative.parts:
                    raise InstallFailure("ARCHIVE_UNSAFE")
                relative_text = relative.as_posix()
                allowed_files = {
                    "release_manifest.json",
                    "backend/pyproject.toml",
                    "backend/uv.lock",
                    "backend/alembic.ini",
                }
                if (
                    relative_text not in allowed_files
                    and not relative_text.startswith("backend/src/meyar/")
                    and not relative_text.startswith("backend/alembic/")
                ):
                    raise InstallFailure("APPLICATION_CONTENT_UNEXPECTED")
                if relative_text.startswith("backend/src/meyar/") and not relative_text.endswith(
                    (".py", ".html", ".js", ".css")
                ):
                    raise InstallFailure("APPLICATION_CONTENT_UNEXPECTED")
                if "/tests/" in relative_text or relative_text.endswith(".env"):
                    raise InstallFailure("APPLICATION_CONTENT_UNEXPECTED")
                output = destination / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                if stream is None:
                    raise InstallFailure("ARCHIVE_UNSAFE")
                with output.open("xb") as target:
                    remaining = member.size
                    while remaining:
                        block = stream.read(min(1024 * 1024, remaining))
                        if not block:
                            raise InstallFailure("ARCHIVE_TRUNCATED")
                        target.write(block)
                        remaining -= len(block)
                if relative.as_posix() == "backend/uv.lock":
                    lock_found = _sha256(output) == expected_lock_sha
    if not lock_found or f"{release_id}/release_manifest.json" not in seen:
        raise InstallFailure("APPLICATION_CONTENT_MISMATCH")


def _verify_wheels_against_lock(stage: Path, manifest: dict[str, object]) -> None:
    try:
        lock = tomllib.loads((stage / "backend" / "uv.lock").read_text())
    except (OSError, UnicodeError, ValueError) as exc:
        raise InstallFailure("LOCKFILE_INVALID") from exc
    package_by_name = {
        entry.get("name"): entry for entry in lock.get("package", []) if isinstance(entry, dict)
    }
    wheels = manifest.get("wheels")
    if not isinstance(wheels, list):
        raise InstallFailure("DEPENDENCY_LIST_INVALID")
    for entry in wheels:
        if not isinstance(entry, dict):
            raise InstallFailure("DEPENDENCY_LIST_INVALID")
        name, version = entry.get("name"), entry.get("version")
        filename, digest = entry.get("filename"), entry.get("sha256")
        package = package_by_name.get(name)
        if not isinstance(package, dict) or package.get("version") != version:
            raise InstallFailure("DEPENDENCY_NOT_LOCKED")
        if not any(
            isinstance(wheel, dict)
            and str(wheel.get("url", "")).endswith("/" + str(filename))
            and wheel.get("hash") == "sha256:" + str(digest)
            for wheel in package.get("wheels", [])
        ):
            raise InstallFailure("DEPENDENCY_NOT_LOCKED")


def _tree_digest(root: Path, *, require_read_only: bool = False) -> str:
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        if require_read_only and parent.stat().st_mode & 0o222:
            raise InstallFailure("INSTALLED_PERMISSIONS_UNSAFE")
        for name in sorted(dirs):
            if (parent / name).is_symlink():
                raise InstallFailure("INSTALLED_SYMLINK_UNSAFE")
        for name in sorted(files):
            path = parent / name
            if path.name == "install_state.json" and path.parent == root:
                continue
            _regular_file(path)
            if require_read_only and path.stat().st_mode & 0o222:
                raise InstallFailure("INSTALLED_PERMISSIONS_UNSAFE")
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode() + b"\0" + bytes.fromhex(_sha256(path)))
    return digest.hexdigest()


def _verify_install(root: Path, release_id: str) -> dict[str, object]:
    _real_directory(root)
    if SAFE_RELEASE.fullmatch(release_id) is None:
        raise InstallFailure("RELEASE_ID_INVALID")
    release = root / "releases" / release_id
    _real_directory(release)
    if (release / "install_state.json").stat().st_mode & 0o222:
        raise InstallFailure("INSTALLED_PERMISSIONS_UNSAFE")
    state = _load_json(release / "install_state.json")
    if state.get("release_id") != release_id:
        raise InstallFailure("INSTALLED_IDENTITY_MISMATCH")
    rollback = state.get("rollback_compatibility")
    if not isinstance(rollback, str) or rollback not in ROLLBACK_VALUES:
        raise InstallFailure("ROLLBACK_CLASSIFICATION_INVALID")
    if _tree_digest(release, require_read_only=True) != _manifest_digest(state.get("tree_sha256")):
        raise InstallFailure("INSTALLED_CONTENT_MISMATCH")
    pointer = release / ".venv" / "lib" / "python3.12" / "site-packages" / "meyar-source.pth"
    _regular_file(pointer, maximum=1024)
    if pointer.read_text() != str(release / "backend" / "src") + "\n":
        raise InstallFailure("INSTALLED_SOURCE_PATH_MISMATCH")
    return state


def _install_dependencies(
    stage: Path, bundle: Path, final: Path, manifest: dict[str, object]
) -> None:
    wheel_dir = stage / ".wheelhouse"
    wheel_dir.mkdir()
    wheels = manifest["wheels"]
    assert isinstance(wheels, list)
    for wheel in wheels:
        assert isinstance(wheel, dict)
        path = bundle / "wheels" / str(wheel["filename"])
        _regular_file(path)
        copied = wheel_dir / path.name
        shutil.copyfile(path, copied)
        if _sha256(copied) != wheel["sha256"]:
            raise InstallFailure("DEPENDENCY_HASH_MISMATCH")
    requirements = stage / ".requirements.txt"
    shutil.copyfile(bundle / "requirements.txt", requirements)
    expected_requirements = "".join(
        f"{wheel['name']}=={wheel['version']} --hash=sha256:{wheel['sha256']}\n" for wheel in wheels
    ).encode()
    if requirements.read_bytes() != expected_requirements:
        raise InstallFailure("REQUIREMENTS_MISMATCH")
    venv = stage / ".venv"
    environment = {
        "PATH": "/usr/bin:/bin",
        "PIP_CONFIG_FILE": os.devnull,
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONNOUSERSITE": "1",
    }
    commands = [
        [sys.executable, "-m", "venv", "--copies", str(venv)],
        [
            str(venv / "bin" / "python"),
            "-m",
            "pip",
            "--isolated",
            "install",
            "--no-index",
            "--no-cache-dir",
            "--no-deps",
            "--require-hashes",
            "--only-binary=:all:",
            "--find-links",
            str(wheel_dir),
            "-r",
            str(requirements),
        ],
    ]
    for argv in commands:
        completed = subprocess.run(
            argv, check=False, capture_output=True, timeout=300, env=environment
        )
        if completed.returncode != 0:
            raise InstallFailure("OFFLINE_DEPENDENCY_INSTALL_FAILED")
    site = venv / "lib" / "python3.12" / "site-packages"
    if not site.is_dir():
        raise InstallFailure("VENV_LAYOUT_UNEXPECTED")
    (site / "meyar-source.pth").write_text(str(final / "backend" / "src") + "\n")
    shutil.rmtree(wheel_dir)
    requirements.unlink()


def _make_release_read_only(stage: Path) -> None:
    for directory, dirs, files in os.walk(stage, topdown=False, followlinks=False):
        parent = Path(directory)
        for name in files:
            path = parent / name
            _regular_file(path)
            executable = bool(path.stat().st_mode & 0o111)
            path.chmod(0o555 if executable else 0o444)
        for name in dirs:
            path = parent / name
            if path.is_symlink():
                raise InstallFailure("INSTALLED_SYMLINK_UNSAFE")
            path.chmod(0o555)
    stage.chmod(0o555)


def install_release(bundle: Path, root: Path) -> str:
    manifest = _verify_bundle(bundle)
    _check_host_runtime(manifest)
    release_id = str(manifest["release_id"])
    with _operation_lock(root):
        _ensure_layout(root)
        final = root / "releases" / release_id
        if final.exists() or final.is_symlink():
            state = _verify_install(root, release_id)
            if state.get("bundle_sha256") != _sha256(bundle / "deployment_manifest.json"):
                raise InstallFailure("RELEASE_ID_CONFLICT")
            return "INSTALL_ALREADY_PRESENT"
        with tempfile.TemporaryDirectory(prefix=".install-", dir=root / "releases") as temporary:
            stage = Path(temporary)
            _extract_application(
                bundle / str(manifest["application_artifact"]),
                release_id,
                stage,
                str(manifest["uv_lock_sha256"]),
                str(manifest["application_artifact_sha256"]),
            )
            embedded = _load_json(stage / "release_manifest.json")
            external = _load_json(bundle / f"{release_id}.release-manifest.json")
            if embedded != external:
                raise InstallFailure("APPLICATION_MANIFEST_MISMATCH")
            _verify_wheels_against_lock(stage, manifest)
            _install_dependencies(stage, bundle, final, manifest)
            state = {
                "release_id": release_id,
                "bundle_sha256": _sha256(bundle / "deployment_manifest.json"),
                "tree_sha256": _tree_digest(stage),
                "rollback_compatibility": manifest["rollback_compatibility"],
                "previous_release_id": None,
            }
            (stage / "install_state.json").write_text(json.dumps(state, sort_keys=True) + "\n")
            _make_release_read_only(stage)
            os.rename(stage, final)
        return "RELEASE_INSTALLED"


def verify_install(root: Path, release_id: str) -> str:
    _verify_install(root, release_id)
    return "INSTALL_VERIFIED"


def _current_release(root: Path) -> str | None:
    _real_directory(root)
    _real_directory(root / "activations")
    _real_directory(root / "releases")
    pointer = root / "current"
    if not os.path.lexists(pointer):
        return None
    if not pointer.is_symlink():
        raise InstallFailure("ACTIVATION_POINTER_UNSAFE")
    parts = PurePosixPath(os.readlink(pointer)).parts
    if (
        len(parts) != 3
        or parts[0] != "activations"
        or parts[2] != "current"
        or not _safe_component(parts[1])
        or not parts[1].startswith("g-")
    ):
        raise InstallFailure("ACTIVATION_POINTER_UNSAFE")
    generation = parts[1]
    _real_directory(root / "activations" / generation)
    state = _load_json(root / "activations" / generation / "state.json")
    release_id = _required_text(state.get("release_id"), limit=128)
    if SAFE_RELEASE.fullmatch(release_id) is None:
        raise InstallFailure("ACTIVATION_POINTER_UNSAFE")
    link = root / "activations" / generation / "current"
    if not link.is_symlink() or os.readlink(link) != f"../../releases/{release_id}":
        raise InstallFailure("ACTIVATION_POINTER_UNSAFE")
    return release_id


def verify_active_release(root: Path) -> str:
    """Verify PR4's public activation chain and its installed release.

    This remains stdlib-only for the copied offline installer. The normal
    meyar-ops service module imports this authority, never the reverse.
    """
    release_id = _current_release(root)
    if release_id is None:
        raise InstallFailure("ACTIVE_RELEASE_MISSING")
    _verify_install(root, release_id)
    release = root / "releases" / release_id
    _regular_file(release / ".venv" / "bin" / "python")
    _regular_file(release / "backend" / "src" / "meyar" / "main.py")
    return release_id


def activate_release(root: Path, release_id: str) -> str:
    with _operation_lock(root):
        _ensure_layout(root)
        state = _verify_install(root, release_id)
        previous = _current_release(root)
        if previous == release_id:
            return "ACTIVATION_ALREADY_CURRENT"
        if previous is not None:
            _verify_install(root, previous)
            if state.get("rollback_compatibility") == "PROHIBITED_PENDING_PROCEDURE":
                raise InstallFailure("ACTIVATION_PROHIBITED_PENDING_PROCEDURE")
            if state.get("rollback_compatibility") == "BACKUP_RESTORE_REQUIRED":
                raise InstallFailure("ACTIVATION_REQUIRES_BACKUP_WORKFLOW")
        public = root / "current"
        generation = "g-" + uuid.uuid4().hex
        directory = root / "activations" / generation
        directory.mkdir(mode=0o750)
        try:
            (directory / "state.json").write_text(
                json.dumps(
                    {
                        "release_id": release_id,
                        "previous_release_id": previous,
                        "rollback_compatibility": state["rollback_compatibility"],
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            os.symlink(f"../../releases/{release_id}", directory / "current")
            temporary = root / (".next-current-" + uuid.uuid4().hex)
            os.symlink(f"activations/{generation}/current", temporary)
            try:
                os.replace(temporary, public)
            finally:
                if temporary.is_symlink():
                    temporary.unlink()
        except BaseException:
            # A fully created generation is harmless if a process dies before
            # pointer replacement. Never remove a generation that may be active.
            raise
        return "RELEASE_ACTIVATED"


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> Never:
        raise InstallFailure("INVALID_INVOCATION")


def main(argv: list[str] | None = None) -> int:
    parser = _SafeArgumentParser(prog="meyar-ops.py")
    commands = parser.add_subparsers(dest="command", required=True)
    install = commands.add_parser("install-release")
    install.add_argument("--bundle-dir", type=Path, required=True)
    install.add_argument("--install-root", type=Path, required=True)
    verify = commands.add_parser("verify-install")
    verify.add_argument("--install-root", type=Path, required=True)
    verify.add_argument("--release-id", required=True)
    activate = commands.add_parser("activate-release")
    activate.add_argument("--install-root", type=Path, required=True)
    activate.add_argument("--release-id", required=True)
    started = datetime.now(UTC)
    action = "invalid-invocation"
    exit_code = 0
    try:
        args = parser.parse_args(argv)
        action = args.command
        if args.command == "install-release":
            code = install_release(args.bundle_dir, args.install_root)
        elif args.command == "verify-install":
            code = verify_install(args.install_root, args.release_id)
        else:
            code = activate_release(args.install_root, args.release_id)
        ok = True
    except InstallFailure as exc:
        code, ok = exc.code, False
        exit_code = 2 if exc.code == "INVALID_INVOCATION" else 1
    except Exception:  # noqa: BLE001 - no raw exception/path/secret may escape the CLI
        code, ok = "INSTALL_INFRASTRUCTURE_FAILURE", False
        exit_code = 3
    result = {
        "action": action,
        "ok": ok,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "findings": [
            {
                "component": action,
                "status": "OK" if ok else "FAIL",
                "code": code,
                "message": code.replace("_", " ").capitalize() + ".",
            }
        ],
    }
    print(json.dumps(result, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

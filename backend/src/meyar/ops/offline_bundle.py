"""Build a hash-bound macOS arm64 dependency payload from a verified application release.

Only wheel archives are downloaded. Foreign-platform code is never imported or
installed by this builder. The host installation is implemented in the
stdlib-only ``offline_host.py`` shipped alongside the bundle.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tarfile
import tempfile
import tomllib
import urllib.request
import zipfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath

from packaging.markers import Marker
from packaging.tags import compatible_tags, cpython_tags, mac_platforms
from packaging.utils import canonicalize_name, parse_wheel_filename
from pydantic import BaseModel, Field, field_validator

from meyar.ops.archive_safety import inspect_archive_members
from meyar.ops.release_manifest import ReleaseManifest
from meyar.ops.result import FindingStatus, OpsResult, OpsResultBuilder
from meyar.ops.verify_release import verify_release

FORMAT_VERSION = 1
MAX_BUNDLE_FILE = 1024 * 1024 * 1024
MAX_WHEELS = 150
TARGET_PLATFORM = "macosx_11_0_arm64"
TARGET_PYTHON = "3.12"
_HEX = set("0123456789abcdef")


class WheelEntry(BaseModel):
    model_config = {"extra": "forbid"}
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    filename: str = Field(min_length=1, max_length=255)
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if len(value) != 64 or any(char not in _HEX for char in value):
            raise ValueError("invalid SHA-256")
        return value


class DeploymentManifest(BaseModel):
    model_config = {"extra": "forbid"}
    bundle_format_version: int = Field(default=FORMAT_VERSION, ge=1, le=FORMAT_VERSION)
    release_id: str = Field(min_length=1, max_length=128)
    source_sha: str = Field(min_length=40, max_length=40)
    application_artifact: str = Field(min_length=1, max_length=255)
    application_artifact_sha256: str
    release_manifest_sha256: str
    sha256sums_sha256: str
    uv_lock_sha256: str
    required_python_version: str = Field(min_length=1, max_length=32)
    runtime_version: str = Field(min_length=1, max_length=32)
    runtime_executable_sha256: str
    target_os: str = "Darwin"
    target_architecture: str = "arm64"
    target_wheel_platform: str = TARGET_PLATFORM
    dependency_payload_sha256: str
    wheels: list[WheelEntry] = Field(min_length=1, max_length=MAX_WHEELS)
    alembic_heads: list[str] = Field(min_length=1)
    model_manifest_reference: str = Field(min_length=1, max_length=300)
    model_approval_status: str = Field(min_length=1, max_length=64)
    rollback_compatibility: str = Field(min_length=1, max_length=64)
    installer_sha256: str

    @field_validator(
        "application_artifact_sha256",
        "release_manifest_sha256",
        "sha256sums_sha256",
        "uv_lock_sha256",
        "runtime_executable_sha256",
        "dependency_payload_sha256",
        "installer_sha256",
    )
    @classmethod
    def _sha(cls, value: str) -> str:
        return WheelEntry._digest(value)


@dataclass(frozen=True)
class BundleBuildRequest:
    artifact_path: Path
    release_manifest_path: Path
    sha256sums_path: Path
    output_dir: Path
    runtime_version: str
    runtime_executable_sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _target_environment(runtime_version: str) -> dict[str, str]:
    return {
        "implementation_name": "cpython",
        "implementation_version": TARGET_PYTHON,
        "os_name": "posix",
        "platform_machine": "arm64",
        "platform_python_implementation": "CPython",
        "platform_release": "",
        "platform_system": "Darwin",
        "platform_version": "",
        "python_full_version": runtime_version,
        "python_version": TARGET_PYTHON,
        "sys_platform": "darwin",
        "extra": "",
    }


def _locked_runtime_packages(
    lock_bytes: bytes, *, runtime_version: str = "3.12.0"
) -> list[dict[str, object]]:
    lock = tomllib.loads(lock_bytes.decode("utf-8"))
    if lock.get("version") != 1 or lock.get("requires-python") != ">=3.12":
        raise ValueError("unsupported uv.lock format or Python requirement")
    all_packages = lock.get("package")
    if not isinstance(all_packages, list):
        raise ValueError("uv.lock package list missing")
    by_name: dict[str, dict[str, object]] = {}
    for package in all_packages:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            raise ValueError("invalid locked package")
        name = str(canonicalize_name(package["name"]))
        if name in by_name:
            raise ValueError("multiple locked versions require a new bundle resolver")
        by_name[name] = package
    if "meyar" not in by_name:
        raise ValueError("meyar root absent from uv.lock")
    selected: set[str] = set()
    queue: list[tuple[str, frozenset[str]]] = [("meyar", frozenset())]
    seen: set[tuple[str, frozenset[str]]] = set()
    if not re.fullmatch(r"3\.12\.[0-9]+", runtime_version):
        raise ValueError("unsupported target Python runtime version")
    environment = _target_environment(runtime_version)
    while queue:
        name, extras = queue.pop()
        if (name, extras) in seen:
            continue
        seen.add((name, extras))
        package = by_name.get(name)
        if package is None:
            raise ValueError("locked dependency is missing")
        if name != "meyar":
            selected.add(name)
        edges = package.get("dependencies", [])
        optional = package.get("optional-dependencies", {})
        if not isinstance(edges, list) or not isinstance(optional, dict):
            raise ValueError("invalid locked dependency edges")
        for extra in extras:
            more = optional.get(extra)
            if not isinstance(more, list):
                raise ValueError("locked extra is missing")
            edges = [*edges, *more]
        for edge in edges:
            if not isinstance(edge, dict) or not isinstance(edge.get("name"), str):
                raise ValueError("invalid locked dependency edge")
            marker = edge.get("marker")
            if marker is not None and not Marker(str(marker)).evaluate(environment=environment):
                continue
            child = str(canonicalize_name(edge["name"]))
            extra = edge.get("extra", [])
            if not isinstance(extra, list) or any(not isinstance(item, str) for item in extra):
                raise ValueError("invalid locked extra selection")
            queue.append((child, frozenset(extra)))
    if "pillow" not in selected or "greenlet" not in selected:
        raise ValueError("Pillow/SQLAlchemy greenlet missing from deployment dependency graph")
    return [by_name[name] for name in sorted(selected)]


@lru_cache(maxsize=1)
def _target_tag_rank() -> dict[object, int]:
    platforms = list(mac_platforms(version=(11, 0), arch="arm64"))
    tags = list(cpython_tags(python_version=(3, 12), abis=["cp312"], platforms=platforms))
    tags.extend(compatible_tags(python_version=(3, 12), interpreter="cp312", platforms=platforms))
    return {tag: index for index, tag in enumerate(tags)}


def _selected_wheel(package: dict[str, object]) -> tuple[str, str]:
    rank = _target_tag_rank()
    candidates: list[tuple[int, str, str]] = []
    wheels = package.get("wheels", [])
    if not isinstance(wheels, list):
        raise ValueError("invalid locked wheel list")
    for wheel in wheels:
        if not isinstance(wheel, dict):
            continue
        url, locked_hash = wheel.get("url"), wheel.get("hash")
        if not isinstance(url, str) or not isinstance(locked_hash, str):
            continue
        filename = url.rsplit("/", 1)[-1]
        try:
            name, version, _build, wheel_tags = parse_wheel_filename(filename)
        except ValueError:
            continue
        if name != canonicalize_name(str(package["name"])) or str(version) != package["version"]:
            continue
        hits = [rank[tag] for tag in wheel_tags if tag in rank]
        if hits:
            candidates.append((min(hits), url, locked_hash))
    if not candidates:
        raise ValueError("a locked macOS arm64 wheel is unavailable")
    _rank, url, locked_hash = min(candidates)
    if not url.startswith("https://files.pythonhosted.org/packages/"):
        raise ValueError("wheel URL is outside the pinned package host")
    if not locked_hash.startswith("sha256:"):
        raise ValueError("wheel has no SHA-256 in uv.lock")
    return url, locked_hash.removeprefix("sha256:")


def _download_locked_wheel(url: str, expected_sha: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "meyar-offline-bundle/1"})
    with urllib.request.urlopen(request, timeout=30) as response, destination.open("xb") as output:
        if response.url != url:
            raise ValueError("wheel download redirected")
        remaining = MAX_BUNDLE_FILE
        while block := response.read(min(1024 * 1024, remaining + 1)):
            remaining -= len(block)
            if remaining < 0:
                raise ValueError("wheel size bound exceeded")
            output.write(block)
    if _sha256(destination) != expected_sha:
        raise ValueError("downloaded wheel hash differs from uv.lock")
    with zipfile.ZipFile(destination) as wheel:
        if len(wheel.infolist()) > 50_000:
            raise ValueError("wheel member bound exceeded")
        total = 0
        for info in wheel.infolist():
            path = PurePosixPath(info.filename)
            total += info.file_size
            if (
                path.is_absolute()
                or ".." in path.parts
                or info.filename.startswith("/")
                or (info.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("unsafe wheel member")
            if total > MAX_BUNDLE_FILE:
                raise ValueError("wheel uncompressed size bound exceeded")


def _application_member(artifact_path: Path, release_id: str, relative: str) -> bytes:
    if inspect_archive_members(artifact_path, expected_root=release_id):
        raise ValueError("application archive has unsafe members")
    wanted = f"{release_id}/{relative}"
    found: bytes | None = None
    with tarfile.open(artifact_path, "r:gz") as archive:
        while member := archive.next():
            if member.name != wanted:
                continue
            if found is not None or not member.isfile() or member.size > 16 * 1024 * 1024:
                raise ValueError("application member is ambiguous or invalid")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("application member unreadable")
            found = stream.read(16 * 1024 * 1024 + 1)
    if found is None or len(found) > 16 * 1024 * 1024:
        raise ValueError("required application member missing or oversized")
    return found


def _application_allowlist(artifact_path: Path, release_id: str) -> None:
    allowed_files = {
        "release_manifest.json",
        "backend/pyproject.toml",
        "backend/uv.lock",
        "backend/alembic.ini",
    }
    with tarfile.open(artifact_path, "r:gz") as archive:
        while member := archive.next():
            prefix = f"{release_id}/"
            if not member.name.startswith(prefix) or not member.isfile():
                raise ValueError("application archive includes an unsupported member")
            relative = member.name.removeprefix(prefix)
            if (
                relative not in allowed_files
                and not relative.startswith("backend/src/meyar/")
                and not relative.startswith("backend/alembic/")
            ):
                raise ValueError("application archive includes non-application data")
            if relative.startswith("backend/src/meyar/") and not relative.endswith(
                (".py", ".html", ".js", ".css")
            ):
                raise ValueError("application archive includes unsupported source asset")
            if "/tests/" in relative or relative.endswith(".env"):
                raise ValueError("application archive includes excluded data")


def _real_directory(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("directory path is unsafe")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise ValueError("directory path has a symlink or non-directory")


def _copy_input(source: Path, destination: Path) -> None:
    _real_directory(source.parent)
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_BUNDLE_FILE:
            raise ValueError("input file is unsafe or oversized")
        with os.fdopen(descriptor, "rb", closefd=False) as input_stream:
            with destination.open("xb") as output_stream:
                remaining = MAX_BUNDLE_FILE
                while block := input_stream.read(min(1024 * 1024, remaining + 1)):
                    remaining -= len(block)
                    if remaining < 0:
                        raise ValueError("input file grew beyond size bound")
                    output_stream.write(block)
    finally:
        os.close(descriptor)


def build_deployment_bundle(request: BundleBuildRequest) -> OpsResult:
    builder = OpsResultBuilder(action="bundle-build")
    try:
        if not re.fullmatch(r"3\.12\.[0-9]+", request.runtime_version):
            raise ValueError("runtime must be an exact Python 3.12 patch version")
        if len(request.runtime_executable_sha256) != 64 or any(
            char not in _HEX for char in request.runtime_executable_sha256
        ):
            raise ValueError("runtime executable SHA-256 is invalid")
        _real_directory(request.output_dir)
        with tempfile.TemporaryDirectory(
            prefix=".meyar-bundle-", dir=request.output_dir
        ) as temporary:
            stage = Path(temporary)
            if request.artifact_path.name in {"SHA256SUMS", "deployment_manifest.json"}:
                raise ValueError("application artifact name conflicts with bundle layout")
            if request.release_manifest_path.name in {
                "SHA256SUMS",
                "deployment_manifest.json",
                request.artifact_path.name,
            }:
                raise ValueError("release manifest name conflicts with bundle layout")
            artifact_path = stage / request.artifact_path.name
            release_manifest_path = stage / request.release_manifest_path.name
            checksums_path = stage / "SHA256SUMS"
            _copy_input(request.artifact_path, artifact_path)
            _copy_input(request.release_manifest_path, release_manifest_path)
            _copy_input(request.sha256sums_path, checksums_path)
            verified = verify_release(
                manifest_path=release_manifest_path,
                sha256sums_path=checksums_path,
                artifact_path=artifact_path,
            )
            if not verified.ok:
                raise ValueError("application release verification failed")
            release = ReleaseManifest.model_validate_json(release_manifest_path.read_bytes())
            if release.required_python_version != ">=3.12":
                raise ValueError("application Python requirement is unsupported")
            if artifact_path.name != f"{release.release_id}.tar.gz":
                raise ValueError("application artifact name differs from release identity")
            if release_manifest_path.name != f"{release.release_id}.release-manifest.json":
                raise ValueError("application manifest name differs from release identity")
            final = request.output_dir / f"{release.release_id}.deployment"
            if final.exists() or final.is_symlink():
                raise ValueError("deployment bundle target already exists")
            _application_allowlist(artifact_path, release.release_id)
            lock_bytes = _application_member(artifact_path, release.release_id, "backend/uv.lock")
            if hashlib.sha256(lock_bytes).hexdigest() != release.uv_lock_sha256:
                raise ValueError("application uv.lock identity mismatch")
            packages = _locked_runtime_packages(lock_bytes, runtime_version=request.runtime_version)
            installer = _application_member(
                artifact_path, release.release_id, "backend/src/meyar/ops/offline_host.py"
            )
            wheels_dir = stage / "wheels"
            wheels_dir.mkdir()
            entries: list[WheelEntry] = []
            total_wheel_bytes = 0
            for package in packages:
                url, digest = _selected_wheel(package)
                filename = url.rsplit("/", 1)[-1]
                _download_locked_wheel(url, digest, wheels_dir / filename)
                total_wheel_bytes += (wheels_dir / filename).stat().st_size
                if total_wheel_bytes > MAX_BUNDLE_FILE:
                    raise ValueError("dependency payload exceeds size bound")
                entries.append(
                    WheelEntry(
                        name=canonicalize_name(str(package["name"])),
                        version=str(package["version"]),
                        filename=filename,
                        sha256=digest,
                    )
                )
            requirements = "".join(
                f"{entry.name}=={entry.version} --hash=sha256:{entry.sha256}\n" for entry in entries
            ).encode()
            (stage / "requirements.txt").write_bytes(requirements)
            (stage / "meyar-ops.py").write_bytes(installer)
            payload_hash = hashlib.sha256(
                requirements + b"".join(bytes.fromhex(entry.sha256) for entry in entries)
            ).hexdigest()
            manifest = DeploymentManifest(
                release_id=release.release_id,
                source_sha=release.source_sha,
                application_artifact=artifact_path.name,
                application_artifact_sha256=_sha256(artifact_path),
                release_manifest_sha256=_sha256(release_manifest_path),
                sha256sums_sha256=_sha256(checksums_path),
                uv_lock_sha256=release.uv_lock_sha256,
                required_python_version=release.required_python_version,
                runtime_version=request.runtime_version,
                runtime_executable_sha256=request.runtime_executable_sha256,
                dependency_payload_sha256=payload_hash,
                wheels=entries,
                alembic_heads=release.alembic_heads,
                model_manifest_reference=release.model_manifest.reference,
                model_approval_status=release.model_manifest.status.value,
                rollback_compatibility=release.rollback_compatibility.value,
                installer_sha256=hashlib.sha256(installer).hexdigest(),
            )
            (stage / "deployment_manifest.json").write_bytes(
                manifest.model_dump_json(indent=2).encode() + b"\n"
            )
            os.rename(stage, final)
        builder.add(
            component="bundle",
            status=FindingStatus.OK,
            code="BUNDLE_BUILT",
            message=f"Verified offline deployment bundle built for {release.release_id}.",
        )
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        # Inputs and paths are deliberately not echoed: they may contain secrets.
        builder.add(
            component="bundle",
            status=FindingStatus.FAIL,
            code="BUNDLE_BUILD_FAILED",
            message=f"Offline bundle construction failed ({type(exc).__name__}).",
        )
    return builder.build()

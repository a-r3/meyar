"""Offline Ollama transport, installation, and installed-model authority.

The transport manifest is deliberately separate from ModelManifest. All public
results use fixed codes; untrusted file, process, and HTTP text is never emitted.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import platform
import plistlib
import pwd
import re
import shutil
import stat
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from meyar.config import Settings
from meyar.ops.model_manifest import ModelManifest, ModelRole
from meyar.ops.offline_host import (
    InstallFailure,
    _operation_lock,
    _real_directory,
    privileged_operation_lock,
    verify_active_release,
)
from meyar.ops.release_manifest import ReleaseManifest
from meyar.ops.result import FindingStatus, OpsResult, build_single_finding_result
from meyar.ops.service_status import LaunchctlRunner

HEX = re.compile(r"^[0-9a-f]{64}$")
NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}(?::[a-z0-9][a-z0-9._-]{0,99})?$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9._-]+)?$")
MAX_META = 1024 * 1024
MAX_MODEL = 100 * 1024 * 1024 * 1024
MAX_RUNTIME = 1024 * 1024 * 1024
HTTP_LIMIT = 1024 * 1024
SYNTHETIC_PROBE = "MEYAR synthetic embedding dimension check"


class AIFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RuntimeArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform: str
    architecture: str
    version: str
    filename: str
    sha256: str


class ModelArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: ModelRole
    model_name: str
    filename: str
    sha256: str


class AIBundleManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format_version: int = Field(ge=1, le=1)
    ollama: RuntimeArtifact
    model_manifest_filename: str
    model_manifest_sha256: str
    models: list[ModelArtifact] = Field(min_length=2, max_length=2)


@dataclass(frozen=True)
class VerifiedBundle:
    path: Path
    transport: AIBundleManifest
    models: ModelManifest


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _filename(name: str, suffix: str) -> None:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or not name.endswith(suffix)
        or len(name) > 200
        or not name.isascii()
        or any(ord(c) < 33 or ord(c) > 126 for c in name)
    ):
        raise AIFailure("AI_BUNDLE_INVALID")


def _file(path: Path, limit: int, owner_uid: int | None = None) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise AIFailure("AI_ARTIFACT_UNAVAILABLE") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size < 1
        or metadata.st_size > limit
        or metadata.st_mode & 0o022
        or (owner_uid is not None and metadata.st_uid != owner_uid)
    ):
        raise AIFailure("AI_ARTIFACT_UNSAFE")


def _json(path: Path) -> dict[str, Any]:
    _file(path, MAX_META)
    try:
        data = json.loads(path.read_bytes(), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, OSError):
        raise AIFailure("AI_MANIFEST_INVALID") from None
    if not isinstance(data, dict):
        raise AIFailure("AI_MANIFEST_INVALID")
    return data


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _trusted_directory(path: Path, owner_uid: int) -> None:
    try:
        _real_directory(path)
        for current in (path, *path.parents):
            metadata = current.stat()
            sticky_temp = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
            if metadata.st_uid not in (0, owner_uid) or (
                metadata.st_mode & 0o022 and not sticky_temp
            ):
                raise AIFailure("AI_BUNDLE_PATH_UNSAFE")
    except (InstallFailure, OSError):
        raise AIFailure("AI_BUNDLE_PATH_UNSAFE") from None


def _model_name(name: str) -> None:
    # A local MEYAR identity is a simple unqualified tag. In particular,
    # namespace, URL, and :cloud identities are never allowed to reach Ollama.
    if not NAME.fullmatch(name) or ":" not in name or name.endswith(":cloud"):
        raise AIFailure("CLOUD_OR_UNSAFE_MODEL_IDENTITY")


def _roles(manifest: ModelManifest) -> dict[ModelRole, Any]:
    entries: dict[ModelRole, Any] = {}
    if len(manifest.entries) != 2:
        raise AIFailure("MODEL_ROLES_INVALID")
    for entry in manifest.entries:
        _model_name(entry.model_name)
        if entry.role in entries:
            raise AIFailure("MODEL_ROLES_INVALID")
        if entry.digest is not None and not HEX.fullmatch(entry.digest):
            raise AIFailure("MODEL_DIGEST_INVALID")
        entries[entry.role] = entry
    if set(entries) != {ModelRole.LLM, ModelRole.EMBEDDING}:
        raise AIFailure("MODEL_ROLES_INVALID")
    if entries[ModelRole.LLM].model_name == entries[ModelRole.EMBEDDING].model_name:
        raise AIFailure("MODEL_ROLES_INVALID")
    return entries


def _macho_arm64(path: Path) -> bool:
    # Thin 64-bit Mach-O arm64, or a fat Mach-O with an arm64 slice.
    with path.open("rb") as stream:
        header = stream.read(8)
        if len(header) != 8 or path.stat().st_size < 32:
            return False
        magic = header[:4]
        if magic in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
            endian = "<" if magic[0] == 0xCF else ">"
            return struct.unpack(endian + "I", header[4:])[0] == 0x0100000C
        if magic not in (
            b"\xca\xfe\xba\xbe",
            b"\xbe\xba\xfe\xca",
            b"\xca\xfe\xba\xbf",
            b"\xbf\xba\xfe\xca",
        ):
            return False
        endian = ">" if magic[0] == 0xCA else "<"
        wide = magic in (b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca")
        count = struct.unpack(endian + "I", header[4:])[0]
        if not 1 <= count <= 32:
            return False
        total = path.stat().st_size
        for _ in range(count):
            item = stream.read(32 if wide else 20)
            if len(item) != (32 if wide else 20):
                return False
            cpu = struct.unpack(endian + "I", item[:4])[0]
            if wide:
                offset, size = struct.unpack(endian + "QQ", item[8:24])
            else:
                offset, size = struct.unpack(endian + "II", item[8:16])
            if cpu == 0x0100000C and size >= 32 and offset + size <= total:
                position = stream.tell()
                stream.seek(offset)
                slice_header = stream.read(8)
                stream.seek(position)
                if slice_header[:4] in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
                    slice_endian = "<" if slice_header[0] == 0xCF else ">"
                    if struct.unpack(slice_endian + "I", slice_header[4:])[0] == cpu:
                        return True
        return False


def verify_ai_bundle(bundle_dir: Path, *, owner_uid: int | None = None) -> VerifiedBundle:
    owner = os.geteuid() if owner_uid is None else owner_uid
    _trusted_directory(bundle_dir, owner)
    path = bundle_dir / "ai_bundle_manifest.json"
    _file(path, MAX_META, owner)
    try:
        transport = AIBundleManifest.model_validate(_json(path))
    except ValidationError:
        raise AIFailure("AI_MANIFEST_INVALID") from None
    runtime = transport.ollama
    if runtime.platform != "darwin" or runtime.architecture != "arm64":
        raise AIFailure("AI_TARGET_UNSUPPORTED")
    if not VERSION.fullmatch(runtime.version):
        raise AIFailure("OLLAMA_VERSION_INVALID")
    _filename(runtime.filename, ".bin")
    _filename(transport.model_manifest_filename, ".json")
    if not HEX.fullmatch(runtime.sha256) or not HEX.fullmatch(transport.model_manifest_sha256):
        raise AIFailure("AI_MANIFEST_INVALID")
    names = {"ai_bundle_manifest.json", runtime.filename, transport.model_manifest_filename}
    if len(names) != 3:
        raise AIFailure("AI_MANIFEST_INVALID")
    runtime_path = bundle_dir / runtime.filename
    _file(runtime_path, MAX_RUNTIME, owner)
    if _sha(runtime_path) != runtime.sha256:
        raise AIFailure("OLLAMA_RUNTIME_HASH_MISMATCH")
    if not _macho_arm64(runtime_path):
        raise AIFailure("OLLAMA_RUNTIME_ARCH_MISMATCH")
    manifest_path = bundle_dir / transport.model_manifest_filename
    _file(manifest_path, MAX_META, owner)
    if _sha(manifest_path) != transport.model_manifest_sha256:
        raise AIFailure("MODEL_MANIFEST_HASH_MISMATCH")
    try:
        models = ModelManifest.model_validate(_json(manifest_path))
    except ValidationError:
        raise AIFailure("MODEL_MANIFEST_INVALID") from None
    roles = _roles(models)
    if {item.role for item in transport.models} != set(roles):
        raise AIFailure("MODEL_ROLES_INVALID")
    for item in transport.models:
        _model_name(item.model_name)
        _filename(item.filename, ".gguf")
        if item.filename in names or not HEX.fullmatch(item.sha256):
            raise AIFailure("AI_MANIFEST_INVALID")
        names.add(item.filename)
        if item.model_name != roles[item.role].model_name:
            raise AIFailure("MODEL_MANIFEST_MISMATCH")
        model_path = bundle_dir / item.filename
        _file(model_path, MAX_MODEL, owner)
        if _sha(model_path) != item.sha256:
            raise AIFailure("MODEL_ARTIFACT_HASH_MISMATCH")
        with model_path.open("rb") as stream:
            if stream.read(4) != b"GGUF":
                raise AIFailure("MODEL_ARTIFACT_FORMAT_INVALID")
    return VerifiedBundle(bundle_dir, transport, models)


def _result(action: str, code: str, ok: bool) -> OpsResult:
    return build_single_finding_result(
        action=action,
        component=action,
        status=FindingStatus.OK if ok else FindingStatus.FAIL,
        code=code,
        message="operation verified" if ok else "operation failed",
    )


def run_ai_bundle_verify(bundle_dir: Path) -> OpsResult:
    try:
        verify_ai_bundle(bundle_dir)
        return _result("ai-bundle-verify", "AI_BUNDLE_VERIFIED", True)
    except (AIFailure, InstallFailure) as exc:
        return _result("ai-bundle-verify", exc.code, False)
    except OSError:
        return _result("ai-bundle-verify", "AI_BUNDLE_UNAVAILABLE", False)


def _ai_root(root: Path) -> Path:
    return root / "shared" / "ollama"


def _safe_install_root(root: Path) -> None:
    # This path is later embedded in a generated Modelfile FROM line.
    if (
        not root.is_absolute()
        or ".." in root.parts
        or not re.fullmatch(r"/[A-Za-z0-9/_.+-]+", str(root))
    ):
        raise AIFailure("INSTALL_ROOT_UNSAFE")
    try:
        _real_directory(root)
    except InstallFailure:
        raise AIFailure("INSTALL_ROOT_UNSAFE") from None


def _runtime_state(root: Path) -> dict[str, Any]:
    ai = _ai_root(root)
    _real_directory(ai)
    state = _json(ai / "runtime.json")
    if (
        set(state) != {"version", "sha256", "platform", "architecture"}
        or state.get("platform") != "darwin"
        or state.get("architecture") != "arm64"
    ):
        raise AIFailure("OLLAMA_RUNTIME_INVALID")
    digest = state.get("sha256")
    version = state.get("version")
    if not isinstance(digest, str) or not HEX.fullmatch(digest):
        raise AIFailure("OLLAMA_RUNTIME_INVALID")
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise AIFailure("OLLAMA_RUNTIME_INVALID")
    binary = ai / "runtimes" / digest / "ollama"
    _real_directory(binary.parent)
    _file(binary, MAX_RUNTIME, root.stat().st_uid)
    if not binary.stat().st_mode & 0o111 or _sha(binary) != digest or not _macho_arm64(binary):
        raise AIFailure("OLLAMA_RUNTIME_INVALID")
    return state


def _write_new(path: Path, data: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        remaining = memoryview(data)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write")
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, mode)


def _runtime_version(binary: Path, expected: str) -> None:
    try:
        run = subprocess.run(
            [str(binary), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
            env={"PATH": "/usr/bin:/bin", "HOME": "/var/empty", "OLLAMA_NO_CLOUD": "1"},
        )
    except (OSError, subprocess.TimeoutExpired):
        raise AIFailure("OLLAMA_VERSION_UNVERIFIED") from None
    if run.returncode != 0 or len(run.stdout) > 200:
        raise AIFailure("OLLAMA_VERSION_UNVERIFIED")
    try:
        output = run.stdout.decode("ascii").strip()
    except UnicodeError:
        raise AIFailure("OLLAMA_VERSION_UNVERIFIED") from None
    if output != f"ollama version is {expected}":
        raise AIFailure("OLLAMA_VERSION_UNVERIFIED")


def install_ollama(
    root: Path, bundle_dir: Path, *, system: str | None = None, machine: str | None = None
) -> OpsResult:
    action = "ollama-install"
    try:
        _safe_install_root(root)
        if (system or platform.system()) != "Darwin" or (machine or platform.machine()) != "arm64":
            raise AIFailure("AI_TARGET_UNSUPPORTED")
        if os.geteuid() == 0:
            raise AIFailure("INSTALL_OWNER_INVALID")
        with _operation_lock(root):
            verify_active_release(root)
            bundle = verify_ai_bundle(bundle_dir)
            ai = _ai_root(root)
            ai.mkdir(mode=0o751, exist_ok=True)
            _real_directory(ai)
            if ai.stat().st_uid != os.geteuid() or ai.stat().st_mode & 0o022:
                raise AIFailure("OLLAMA_LAYOUT_UNSAFE")
            runtimes = ai / "runtimes"
            runtimes.mkdir(mode=0o755, exist_ok=True)
            digest = bundle.transport.ollama.sha256
            version = bundle.transport.ollama.version
            expected = {
                "version": version,
                "sha256": digest,
                "platform": "darwin",
                "architecture": "arm64",
            }
            state_path = ai / "runtime.json"
            if state_path.exists():
                if _runtime_state(root) != expected:
                    raise AIFailure("OLLAMA_RUNTIME_CONFLICT")
                return _result(action, "OLLAMA_RUNTIME_ALREADY_INSTALLED", True)
            if (runtimes / digest).exists():
                raise AIFailure("OLLAMA_RUNTIME_CONFLICT")
            stage = Path(tempfile.mkdtemp(prefix=".runtime-", dir=runtimes))
            try:
                target = stage / "ollama"
                shutil.copyfile(bundle.path / bundle.transport.ollama.filename, target)
                if _sha(target) != digest or not _macho_arm64(target):
                    raise AIFailure("OLLAMA_RUNTIME_HASH_MISMATCH")
                os.chmod(target, 0o555)
                _runtime_version(target, version)
                os.chmod(stage, 0o555)
                stage.rename(runtimes / digest)
                _write_new(
                    state_path, (json.dumps(expected, sort_keys=True) + "\n").encode(), 0o444
                )
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
            return _result(action, "OLLAMA_RUNTIME_INSTALLED", True)
    except (AIFailure, InstallFailure) as exc:
        return _result(action, exc.code, False)
    except (OSError, ValueError):
        return _result(action, "OLLAMA_INSTALL_FAILED", False)


def _endpoint(settings: Settings) -> int:
    url = urlsplit(settings.ollama_base_url)
    if (
        url.scheme != "http"
        or url.hostname != "127.0.0.1"
        or url.username
        or url.password
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise AIFailure("OLLAMA_ENDPOINT_UNSAFE")
    try:
        port = url.port or 80
    except ValueError:
        raise AIFailure("OLLAMA_ENDPOINT_UNSAFE") from None
    if not 1 <= port <= 65535:
        raise AIFailure("OLLAMA_ENDPOINT_UNSAFE")
    return port


def _request(
    port: int, method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        payload = None if body is None else json.dumps(body).encode()
        headers = {} if payload is None else {"Content-Type": "application/json"}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise AIFailure("OLLAMA_RESPONSE_INVALID")
        raw = response.read(HTTP_LIMIT + 1)
        if len(raw) > HTTP_LIMIT:
            raise AIFailure("OLLAMA_RESPONSE_INVALID")
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise AIFailure("OLLAMA_RESPONSE_INVALID")
        return value
    except (OSError, TimeoutError, http.client.HTTPException):
        raise AIFailure("OLLAMA_UNREACHABLE") from None
    except (ValueError, UnicodeError):
        raise AIFailure("OLLAMA_RESPONSE_INVALID") from None
    finally:
        connection.close()


def _tags(port: int) -> dict[str, str]:
    value = _request(port, "GET", "/api/tags")
    models = value.get("models")
    if not isinstance(models, list) or len(models) > 10000:
        raise AIFailure("OLLAMA_RESPONSE_INVALID")
    tags: dict[str, str] = {}
    for model in models:
        if not isinstance(model, dict):
            raise AIFailure("OLLAMA_RESPONSE_INVALID")
        name, digest = model.get("name"), model.get("digest")
        if not isinstance(name, str) or not isinstance(digest, str) or not HEX.fullmatch(digest):
            raise AIFailure("OLLAMA_RESPONSE_INVALID")
        if name in tags:
            raise AIFailure("OLLAMA_RESPONSE_INVALID")
        tags[name] = digest
    return tags


def _daemon(port: int, expected_version: str) -> dict[str, str]:
    version = _request(port, "GET", "/api/version").get("version")
    if version != expected_version:
        raise AIFailure("OLLAMA_DAEMON_VERSION_MISMATCH")
    return _tags(port)


def _service_visible(label: str) -> None:
    from meyar.ops.service_status import run_service_status

    result = run_service_status(label=label)
    if not result.ok or result.findings[0].code != "SERVICE_VISIBLE":
        raise AIFailure("OLLAMA_SERVICE_NOT_VISIBLE")


def _release_manifest(root: Path) -> tuple[str, ReleaseManifest, str]:
    _safe_install_root(root)
    release_id = verify_active_release(root)
    raw = _json(root / "releases" / release_id / "release_manifest.json")
    try:
        release = ReleaseManifest.model_validate(raw)
    except ValidationError:
        raise AIFailure("ACTIVE_RELEASE_INVALID") from None
    reference = release.model_manifest.reference
    if not reference.startswith("sha256:") or not HEX.fullmatch(reference[7:]):
        raise AIFailure("MODEL_REFERENCE_UNSAFE")
    return release_id, release, reference[7:]


def _installed_manifest(root: Path, digest: str) -> ModelManifest:
    path = _ai_root(root) / "manifests" / f"{digest}.json"
    _file(path, MAX_META, root.stat().st_uid)
    if _sha(path) != digest:
        raise AIFailure("MODEL_MANIFEST_HASH_MISMATCH")
    try:
        manifest = ModelManifest.model_validate(_json(path))
    except ValidationError:
        raise AIFailure("MODEL_MANIFEST_INVALID") from None
    _roles(manifest)
    return manifest


def _receipt(root: Path, digest: str) -> dict[str, Any]:
    path = _ai_root(root) / "manifests" / f"{digest}.receipt.json"
    _file(path, MAX_META, root.stat().st_uid)
    receipt = _json(path)
    if receipt.get("manifest_sha256") != digest:
        raise AIFailure("MODEL_RECEIPT_INVALID")
    return receipt


def _check_config(settings: Settings, entries: dict[ModelRole, Any]) -> int:
    if (
        settings.ollama_model != entries[ModelRole.LLM].model_name
        or settings.ollama_embedding_model != entries[ModelRole.EMBEDDING].model_name
    ):
        raise AIFailure("CONFIGURED_MODEL_MISMATCH")
    dimension = entries[ModelRole.EMBEDDING].embedding_dimensions
    if dimension is None or dimension != settings.embedding_dimensions:
        raise AIFailure("EMBEDDING_DIMENSIONS_MISMATCH")
    return _endpoint(settings)


def _embedding_dimension(port: int, model_name: str, expected: int) -> None:
    result = _request(
        port,
        "POST",
        "/api/embed",
        {"model": model_name, "input": SYNTHETIC_PROBE, "truncate": False},
    )
    vectors = result.get("embeddings")
    if (
        result.get("model") != model_name
        or not isinstance(vectors, list)
        or len(vectors) != 1
        or not isinstance(vectors[0], list)
        or len(vectors[0]) != expected
    ):
        raise AIFailure("EMBEDDING_DIMENSIONS_MISMATCH")
    if not all(type(value) in (float, int) for value in vectors[0]):
        raise AIFailure("OLLAMA_RESPONSE_INVALID")


def _verify_installed(
    root: Path, settings: Settings, *, probe: bool = True
) -> tuple[str, dict[str, Any]]:
    _, release, digest = _release_manifest(root)
    models = _installed_manifest(root, digest)
    entries = _roles(models)
    if any(entry.approval_status != release.model_manifest.status for entry in entries.values()):
        raise AIFailure("MODEL_APPROVAL_STATUS_MISMATCH")
    port = _check_config(settings, entries)
    runtime = _runtime_state(root)
    service = _service_state(root)
    if service["port"] != port:
        raise AIFailure("OLLAMA_SERVICE_INVALID")
    _installed_plist(root, service, Path("/Library/LaunchDaemons"), 0)
    receipt = _receipt(root, digest)
    if receipt.get("runtime_sha256") != runtime["sha256"]:
        raise AIFailure("MODEL_RECEIPT_INVALID")
    stored = receipt.get("models")
    if not isinstance(stored, dict) or set(stored) != {"LLM", "EMBEDDING"}:
        raise AIFailure("MODEL_RECEIPT_INVALID")
    for role, entry in entries.items():
        item = stored.get(role.value)
        if (
            not isinstance(item, dict)
            or item.get("model_name") != entry.model_name
            or not isinstance(item.get("source_sha256"), str)
            or not HEX.fullmatch(item["source_sha256"])
        ):
            raise AIFailure("MODEL_RECEIPT_INVALID")
        tag_digest = item.get("ollama_digest")
        if (
            not isinstance(tag_digest, str)
            or not HEX.fullmatch(tag_digest)
            or (entry.digest is not None and entry.digest != tag_digest)
        ):
            raise AIFailure("MODEL_DIGEST_MISMATCH")
        if entry.runtime_version is not None and entry.runtime_version != runtime["version"]:
            raise AIFailure("OLLAMA_RUNTIME_VERSION_MISMATCH")
    if probe:
        _service_visible(service["label"])
        tags = _daemon(port, runtime["version"])
        for role, entry in entries.items():
            if tags.get(entry.model_name) != stored[role.value]["ollama_digest"]:
                raise AIFailure("MODEL_DIGEST_MISMATCH")
        _embedding_dimension(
            port, entries[ModelRole.EMBEDDING].model_name, settings.embedding_dimensions
        )
    return digest, receipt


def verify_installed_models(
    root: Path, settings: Settings, *, probe: bool = True
) -> tuple[str, dict[str, Any]]:
    """Internal strict authority; callers must sanitize AIFailure to fixed codes."""
    return _verify_installed(root, settings, probe=probe)


def assert_runtime_local_identity(settings: Settings) -> None:
    """Check tags before any production candidate-bearing provider request."""
    cwd = Path.cwd()
    if cwd.name != "config" or cwd.parent.name != "shared":
        raise AIFailure("HOST_BINDING_INVALID")
    root = cwd.parent.parent
    digest, receipt = _verify_installed(root, settings, probe=False)
    service = _service_state(root)
    _installed_plist(root, service, Path("/Library/LaunchDaemons"), 0)
    tags = _daemon(_endpoint(settings), _runtime_state(root)["version"])
    for item in receipt["models"].values():
        if tags.get(item["model_name"]) != item["ollama_digest"]:
            raise AIFailure("MODEL_DIGEST_MISMATCH")
    if digest != _release_manifest(root)[2]:
        raise AIFailure("MODEL_STATE_CHANGED")


def run_model_verify(root: Path) -> OpsResult:
    from meyar.ops.host_config import load_host_settings

    action = "model-verify"
    try:
        if platform.system() != "Darwin" or platform.machine() != "arm64" or os.geteuid() == 0:
            raise AIFailure("AI_TARGET_UNSUPPORTED")
        owner = os.geteuid()
        with privileged_operation_lock(root, owner):
            settings = load_host_settings(root)
            initial = _verify_installed(root, settings, probe=False)
        _verify_installed(root, settings, probe=True)
        with privileged_operation_lock(root, owner):
            if _verify_installed(root, load_host_settings(root), probe=True) != initial:
                raise AIFailure("MODEL_STATE_CHANGED")
        return _result(action, "LOCAL_MODELS_VERIFIED", True)
    except (AIFailure, InstallFailure) as exc:
        return _result(action, exc.code, False)
    except (OSError, ValueError, TypeError):
        return _result(action, "MODEL_VERIFICATION_FAILED", False)


def _service_state(root: Path) -> dict[str, Any]:
    state = _json(_ai_root(root) / "service.json")
    if set(state) != {"label", "app_label", "user_name", "uid", "gid", "port", "runtime_sha256"}:
        raise AIFailure("OLLAMA_SERVICE_INVALID")
    try:
        account = pwd.getpwnam(state["user_name"])
    except (KeyError, TypeError):
        raise AIFailure("OLLAMA_SERVICE_INVALID") from None
    if (
        account.pw_uid != state["uid"]
        or account.pw_gid != state["gid"]
        or account.pw_uid in (0, root.stat().st_uid)
    ):
        raise AIFailure("OLLAMA_SERVICE_INVALID")
    if _runtime_state(root)["sha256"] != state["runtime_sha256"]:
        raise AIFailure("OLLAMA_SERVICE_INVALID")
    return state


def _service_plist(root: Path, state: dict[str, Any]) -> bytes:
    ai = _ai_root(root)
    binary = ai / "runtimes" / state["runtime_sha256"] / "ollama"
    port = state["port"]
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise AIFailure("OLLAMA_SERVICE_INVALID")
    payload = {
        "Label": state["label"],
        "UserName": state["user_name"],
        "ProgramArguments": [str(binary), "serve"],
        "WorkingDirectory": str(ai / "state"),
        "StandardOutPath": str(root / "shared" / "logs" / "ollama.stdout.log"),
        "StandardErrorPath": str(root / "shared" / "logs" / "ollama.stderr.log"),
        "KeepAlive": True,
        "EnvironmentVariables": {
            "OLLAMA_HOST": f"127.0.0.1:{port}",
            "OLLAMA_MODELS": str(ai / "models"),
            "OLLAMA_NO_CLOUD": "1",
            "HOME": str(ai / "state"),
            "PATH": "/usr/bin:/bin",
        },
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def _installed_plist(root: Path, state: dict[str, Any], directory: Path, system_uid: int) -> bytes:
    from meyar.ops.service_lifecycle import LifecycleFailure, _installed_bytes, _system_directory

    try:
        _system_directory(directory, system_uid)
        path = directory / f"{state['label']}.plist"
        data = _installed_bytes(path, system_uid)
        if (
            data is None
            or data != _service_plist(root, state)
            or path.stat().st_mode & 0o7777 != 0o644
        ):
            raise AIFailure("OLLAMA_PLIST_CONFLICT")
        return data
    except LifecycleFailure:
        raise AIFailure("OLLAMA_PLIST_CONFLICT") from None


def _service_layout(root: Path, uid: int, gid: int) -> None:
    ai = _ai_root(root)
    if ai.stat().st_uid != root.stat().st_uid or ai.stat().st_mode & 0o022:
        raise AIFailure("OLLAMA_LAYOUT_UNSAFE")
    # Other users receive traversal only through root/shared/logs; they
    # cannot list config, releases, candidate storage, or backups.
    for directory, mode in (
        (root, 0o751),
        (root / "shared", 0o2751),
        (root / "shared/logs", 0o2771),
    ):
        _real_directory(directory)
        metadata = directory.stat()
        if metadata.st_uid != root.stat().st_uid or metadata.st_mode & 0o002:
            raise AIFailure("OLLAMA_LAYOUT_UNSAFE")
        os.chmod(directory, mode)
    os.chmod(ai, 0o751)
    for name in ("models", "state"):
        path = ai / name
        if path.exists():
            _real_directory(path)
            metadata = path.stat()
            if (
                metadata.st_uid != uid
                or metadata.st_gid != gid
                or metadata.st_mode & 0o7777 != 0o700
            ):
                raise AIFailure("OLLAMA_LAYOUT_UNSAFE")
        else:
            path.mkdir(mode=0o700)
            os.chown(path, uid, gid)
            os.chmod(path, 0o700)
    stage = ai / "staging"
    if not stage.exists():
        stage.mkdir(mode=0o700)
        os.chown(stage, root.stat().st_uid, gid)
        os.chmod(stage, 0o2750)
    metadata = stage.stat()
    if (
        metadata.st_uid != root.stat().st_uid
        or metadata.st_gid != gid
        or metadata.st_mode & 0o7777 != 0o2750
    ):
        raise AIFailure("OLLAMA_LAYOUT_UNSAFE")
    for name in ("ollama.stdout.log", "ollama.stderr.log"):
        path = root / "shared" / "logs" / name
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
            os.chown(path, uid, gid)
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != uid
            or metadata.st_gid != gid
            or metadata.st_mode & 0o7777 != 0o600
        ):
            raise AIFailure("OLLAMA_LAYOUT_UNSAFE")


def run_ollama_service(
    action: str,
    root: Path,
    label: str,
    app_label: str,
    user_name: str,
    owner_uid: int,
    *,
    platform_system: str | None = None,
    platform_machine: str | None = None,
    effective_uid: int | None = None,
    plist_directory: Path = Path("/Library/LaunchDaemons"),
    system_uid: int = 0,
    system_gid: int = 0,
    runner: LaunchctlRunner | None = None,
) -> OpsResult:
    from meyar.ops.deployment_ready import _installed_spec
    from meyar.ops.host_config import load_host_settings
    from meyar.ops.service_lifecycle import (
        LifecycleFailure,
        _installed_bytes,
        _mutate_launchctl,
        _publish_plist,
        _system_directory,
        default_lifecycle_runner,
    )
    from meyar.ops.service_plist import validate_label, validate_user_name

    try:
        _safe_install_root(root)
        if action not in {
            "ollama-service-install",
            "ollama-service-start",
            "ollama-service-stop",
            "ollama-service-restart",
        }:
            raise AIFailure("INVALID_OPERATION")
        if (
            (platform_system or platform.system()) != "Darwin"
            or (platform_machine or platform.machine()) != "arm64"
        ):
            raise AIFailure("AI_TARGET_UNSUPPORTED")
        if (
            os.geteuid() if effective_uid is None else effective_uid
        ) != 0:
            raise AIFailure("PRIVILEGES_REQUIRED")
        if owner_uid <= 0:
            raise AIFailure("INSTALL_OWNER_INVALID")
        try:
            validate_label(label)
            validate_label(app_label)
            validate_user_name(user_name)
        except ValueError:
            raise AIFailure("OLLAMA_SERVICE_INVALID") from None
        with privileged_operation_lock(root, owner_uid):
            settings = load_host_settings(root, expected_owner_uid=owner_uid)
            port = _endpoint(settings)
            verify_active_release(root)
            runtime = _runtime_state(root)
            app, _ = _installed_spec(root, app_label, plist_directory, system_uid, owner_uid)
            try:
                account = pwd.getpwnam(user_name)
            except KeyError:
                raise AIFailure("OLLAMA_SERVICE_USER_INVALID") from None
            if account.pw_uid in (0, owner_uid) or user_name == app.user_name or label == app_label:
                raise AIFailure("OLLAMA_SERVICE_USER_INVALID")
            state = {
                "label": label,
                "app_label": app_label,
                "user_name": user_name,
                "uid": account.pw_uid,
                "gid": account.pw_gid,
                "port": port,
                "runtime_sha256": runtime["sha256"],
            }
            ai = _ai_root(root)
            state_path = ai / "service.json"
            if state_path.exists() and _json(state_path) != state:
                raise AIFailure("OLLAMA_SERVICE_CONFLICT")
            _system_directory(plist_directory, system_uid)
            path = plist_directory / f"{label}.plist"
            expected = _service_plist(root, state)
            if action == "ollama-service-install":
                existing = _installed_bytes(path, system_uid)
                if existing is not None and existing != expected:
                    raise AIFailure("OLLAMA_PLIST_CONFLICT")
                _service_layout(root, account.pw_uid, account.pw_gid)
                if not state_path.exists():
                    _write_new(
                        state_path, (json.dumps(state, sort_keys=True) + "\n").encode(), 0o444
                    )
                code = _publish_plist(path, expected, system_uid, system_gid)
            else:
                _service_state(root)
                _installed_plist(root, state, plist_directory, system_uid)
                code = _mutate_launchctl(
                    action.replace("ollama-", ""), label, path, runner or default_lifecycle_runner
                )
        return _result(action, code, True)
    except (AIFailure, InstallFailure, LifecycleFailure) as exc:
        return _result(action, exc.code, False)
    except (OSError, ValueError, TypeError, KeyError):
        return _result(action, "OLLAMA_SERVICE_FAILED", False)


def run_ollama_service_status(
    label: str,
    *,
    platform_system: str | None = None,
    runner: LaunchctlRunner | None = None,
) -> OpsResult:
    from meyar.ops.service_status import run_service_status

    result = run_service_status(label=label, platform_system=platform_system, runner=runner)
    return result.model_copy(update={"action": "ollama-service-status"})


def install_models(root: Path, bundle_dir: Path) -> OpsResult:
    from meyar.ops.host_config import load_host_settings

    action = "model-install"
    try:
        _safe_install_root(root)
        if platform.system() != "Darwin" or platform.machine() != "arm64" or os.geteuid() == 0:
            raise AIFailure("AI_TARGET_UNSUPPORTED")
        with _operation_lock(root):
            bundle = verify_ai_bundle(bundle_dir)
            release_id, release, digest = _release_manifest(root)
            if digest != bundle.transport.model_manifest_sha256:
                raise AIFailure("MODEL_MANIFEST_MISMATCH")
            entries = _roles(bundle.models)
            if any(
                entry.approval_status != release.model_manifest.status for entry in entries.values()
            ):
                raise AIFailure("MODEL_APPROVAL_STATUS_MISMATCH")
            settings = load_host_settings(root)
            port = _check_config(settings, entries)
            runtime = _runtime_state(root)
            if (
                runtime["sha256"] != bundle.transport.ollama.sha256
                or runtime["version"] != bundle.transport.ollama.version
            ):
                raise AIFailure("OLLAMA_RUNTIME_CONFLICT")
            service = _service_state(root)
            if service["port"] != port:
                raise AIFailure("OLLAMA_SERVICE_INVALID")
            _installed_plist(root, service, Path("/Library/LaunchDaemons"), 0)
            _service_visible(service["label"])
            tags = _daemon(port, runtime["version"])
            ai = _ai_root(root)
            manifests = ai / "manifests"
            manifests.mkdir(mode=0o755, exist_ok=True)
            _real_directory(manifests)
            if (
                manifests.stat().st_uid != os.geteuid()
                or manifests.stat().st_mode & 0o7777 != 0o755
            ):
                raise AIFailure("MODEL_MANIFEST_STORE_UNSAFE")
            receipt_path = manifests / f"{digest}.receipt.json"
            if receipt_path.exists():
                stored_digest, receipt = _verify_installed(root, settings)
                if stored_digest != digest or any(
                    receipt["models"][item.role.value]["source_sha256"] != item.sha256
                    for item in bundle.transport.models
                ):
                    raise AIFailure("MODEL_INSTALL_CONFLICT")
                return _result(action, "LOCAL_MODELS_ALREADY_INSTALLED", True)
            if any(item.model_name in tags for item in bundle.transport.models):
                raise AIFailure("MODEL_INSTALL_CONFLICT")
            if (manifests / f"{digest}.json").exists():
                raise AIFailure("MODEL_MANIFEST_CONFLICT")
            stage_parent = ai / "staging"
            _real_directory(stage_parent)
            if (
                stage_parent.stat().st_uid != os.geteuid()
                or stage_parent.stat().st_gid != service["gid"]
                or stage_parent.stat().st_mode & 0o7777 != 0o2750
            ):
                raise AIFailure("OLLAMA_LAYOUT_UNSAFE")
            installed: dict[str, dict[str, str]] = {}
            cli_home = ai / "cli-home"
            cli_home.mkdir(mode=0o700, exist_ok=True)
            if cli_home.stat().st_uid != os.geteuid() or cli_home.stat().st_mode & 0o077:
                raise AIFailure("OLLAMA_LAYOUT_UNSAFE")
            for item in bundle.transport.models:
                stage = Path(tempfile.mkdtemp(prefix=".import-", dir=stage_parent))
                try:
                    os.chmod(stage, 0o2750)
                    source = stage / item.filename
                    shutil.copyfile(bundle.path / item.filename, source)
                    if _sha(source) != item.sha256:
                        raise AIFailure("MODEL_ARTIFACT_HASH_MISMATCH")
                    os.chmod(source, 0o440)
                    modelfile = stage / "Modelfile"
                    _write_new(modelfile, f"FROM {source}\n".encode(), 0o440)
                    binary = ai / "runtimes" / runtime["sha256"] / "ollama"
                    try:
                        run = subprocess.run(
                            [str(binary), "create", item.model_name, "-f", str(modelfile)],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                            timeout=1800,
                            env={
                                "PATH": "/usr/bin:/bin",
                                "HOME": str(cli_home),
                                "OLLAMA_HOST": f"127.0.0.1:{port}",
                                "OLLAMA_MODELS": str(ai / "models"),
                                "OLLAMA_NO_CLOUD": "1",
                            },
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        raise AIFailure("MODEL_IMPORT_FAILED") from None
                    if run.returncode != 0:
                        raise AIFailure("MODEL_IMPORT_FAILED")
                    after = _tags(port)
                    tag_digest = after.get(item.model_name)
                    if tag_digest is None or (
                        entries[item.role].digest is not None
                        and entries[item.role].digest != tag_digest
                    ):
                        raise AIFailure("MODEL_DIGEST_MISMATCH")
                    installed[item.role.value] = {
                        "model_name": item.model_name,
                        "source_sha256": item.sha256,
                        "ollama_digest": tag_digest,
                    }
                finally:
                    shutil.rmtree(stage)
            _embedding_dimension(
                port, entries[ModelRole.EMBEDDING].model_name, settings.embedding_dimensions
            )
            manifest_path = manifests / f"{digest}.json"
            _write_new(
                manifest_path,
                (bundle.path / bundle.transport.model_manifest_filename).read_bytes(),
                0o444,
            )
            receipt = {
                "manifest_sha256": digest,
                "release_id": release_id,
                "runtime_sha256": runtime["sha256"],
                "models": installed,
            }
            _write_new(receipt_path, (json.dumps(receipt, sort_keys=True) + "\n").encode(), 0o444)
            _verify_installed(root, settings)
        return _result(action, "LOCAL_MODELS_INSTALLED", True)
    except (AIFailure, InstallFailure) as exc:
        return _result(action, exc.code, False)
    except (OSError, ValueError, TypeError, KeyError):
        return _result(action, "MODEL_INSTALL_FAILED", False)

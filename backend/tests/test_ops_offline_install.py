"""Linux contract tests: no macOS wheel is executed by this suite."""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from meyar.ops import offline_host as host
from meyar.ops.offline_bundle import _locked_runtime_packages, _selected_wheel

SOURCE = "abcdef123456" + "0" * 28
RELEASE_ID = "meyar-0.1.0+abcdef123456"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


LOCK_BYTES = (
    "version = 1\n"
    '[[package]]\nname = "greenlet"\nversion = "3.5.5"\n'
    'wheels = [{url = "https://files.pythonhosted.org/packages/'
    f'greenlet-3.5.5-py3-none-any.whl", hash = "sha256:{sha(b"greenlet")}"}}]\n'
    '[[package]]\nname = "pillow"\nversion = "12.3.0"\n'
    'wheels = [{url = "https://files.pythonhosted.org/packages/'
    f'pillow-12.3.0-py3-none-any.whl", hash = "sha256:{sha(b"pillow")}"}}]\n'
).encode()


def _archive(
    path: Path, release_manifest: bytes, *, extra: tuple[str, bytes, bytes] | None = None
) -> None:
    members = [
        ("release_manifest.json", release_manifest, b"0"),
        ("backend/uv.lock", LOCK_BYTES, b"0"),
        ("backend/src/meyar/__init__.py", b"", b"0"),
    ]
    if extra:
        members.append(extra)
    with tarfile.open(path, "w:gz") as archive:
        for name, content, kind in members:
            info = tarfile.TarInfo(f"{RELEASE_ID}/{name}")
            info.size = len(content)
            info.type = kind
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = "../../outside"
                info.size = 0
            archive.addfile(info, io.BytesIO(content) if info.size else None)


def bundle_fixture(tmp_path: Path, *, extra: tuple[str, bytes, bytes] | None = None) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    release_manifest = {
        "release_id": RELEASE_ID,
        "source_sha": SOURCE,
        "uv_lock_sha256": sha(LOCK_BYTES),
        "required_python_version": ">=3.12",
        "alembic_heads": ["head1"],
        "rollback_compatibility": "APP_ONLY",
        "model_manifest": {"reference": "TBD", "status": "UNAPPROVED"},
    }
    manifest_bytes = json.dumps(release_manifest, sort_keys=True).encode()
    release_manifest_name = f"{RELEASE_ID}.release-manifest.json"
    (bundle / release_manifest_name).write_bytes(manifest_bytes)
    artifact_name = f"{RELEASE_ID}.tar.gz"
    _archive(bundle / artifact_name, manifest_bytes, extra=extra)
    artifact_digest = host._sha256(bundle / artifact_name)
    sums = (
        f"{artifact_digest}  {artifact_name}\n{sha(manifest_bytes)}  {release_manifest_name}\n"
    ).encode()
    (bundle / "SHA256SUMS").write_bytes(sums)
    installer = b"# synthetic installer fixture\n"
    (bundle / "meyar-ops.py").write_bytes(installer)
    wheels = [
        {
            "name": "greenlet",
            "version": "3.5.5",
            "filename": "greenlet-3.5.5-py3-none-any.whl",
            "sha256": sha(b"greenlet"),
        },
        {
            "name": "pillow",
            "version": "12.3.0",
            "filename": "pillow-12.3.0-py3-none-any.whl",
            "sha256": sha(b"pillow"),
        },
    ]
    (bundle / "wheels").mkdir()
    for wheel in wheels:
        (bundle / "wheels" / str(wheel["filename"])).write_bytes(str(wheel["name"]).encode())
    requirements = "".join(
        f"{wheel['name']}=={wheel['version']} --hash=sha256:{wheel['sha256']}\n" for wheel in wheels
    ).encode()
    (bundle / "requirements.txt").write_bytes(requirements)
    deployment = {
        "bundle_format_version": 1,
        "release_id": RELEASE_ID,
        "source_sha": SOURCE,
        "application_artifact": artifact_name,
        "application_artifact_sha256": artifact_digest,
        "release_manifest_sha256": sha(manifest_bytes),
        "sha256sums_sha256": sha(sums),
        "uv_lock_sha256": sha(LOCK_BYTES),
        "required_python_version": ">=3.12",
        "runtime_version": "3.12.9",
        "runtime_executable_sha256": "a" * 64,
        "target_os": "Darwin",
        "target_architecture": "arm64",
        "target_wheel_platform": "macosx_11_0_arm64",
        "dependency_payload_sha256": sha(
            requirements + b"".join(bytes.fromhex(str(wheel["sha256"])) for wheel in wheels)
        ),
        "wheels": wheels,
        "alembic_heads": ["head1"],
        "model_manifest_reference": "TBD",
        "model_approval_status": "UNAPPROVED",
        "rollback_compatibility": "APP_ONLY",
        "installer_sha256": sha(installer),
    }
    (bundle / "deployment_manifest.json").write_text(json.dumps(deployment))
    return bundle


def fake_dependency_install(
    stage: Path, _bundle: Path, final: Path, _manifest: dict[str, object]
) -> None:
    site = stage / ".venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "meyar-source.pth").write_text(str(final / "backend" / "src") + "\n")
    binary = stage / ".venv" / "bin"
    binary.mkdir()
    executable = binary / "python"
    executable.write_bytes(b"synthetic interpreter")
    executable.chmod(0o755)


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    bundle = bundle_fixture(tmp_path)
    root = tmp_path / "host"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(host, "_check_host_runtime", lambda _manifest: None)
    monkeypatch.setattr(host, "_install_dependencies", fake_dependency_install)
    assert host.install_release(bundle, root) == "RELEASE_INSTALLED"
    return bundle, root


def test_lock_graph_has_exact_mac_wheels_and_pillow_greenlet() -> None:
    packages = _locked_runtime_packages(Path("uv.lock").read_bytes())
    names = {package["name"] for package in packages}
    assert {"pillow", "greenlet", "asyncpg", "pgvector", "lxml"} <= names
    assert len(packages) == 42
    for package in packages:
        url, digest = _selected_wheel(package)
        assert url.endswith(".whl") and len(digest) == 64


def test_exact_bundle_installs_and_idempotent(installed: tuple[Path, Path]) -> None:
    bundle, root = installed
    assert host.verify_install(root, RELEASE_ID) == "INSTALL_VERIFIED"
    assert host.install_release(bundle, root) == "INSTALL_ALREADY_PRESENT"
    assert (root / "releases" / RELEASE_ID / "backend" / "uv.lock").read_bytes() == LOCK_BYTES
    assert (root / "releases" / RELEASE_ID).stat().st_mode & 0o222 == 0


def test_missing_dependency_wheel_rejected(tmp_path: Path) -> None:
    bundle = bundle_fixture(tmp_path)
    (bundle / "wheels" / "pillow-12.3.0-py3-none-any.whl").unlink()
    with pytest.raises(host.InstallFailure):
        host._verify_bundle(bundle)


def test_insecure_install_root_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = bundle_fixture(tmp_path)
    root = tmp_path / "host"
    root.mkdir(mode=0o700)
    root.chmod(0o777)
    monkeypatch.setattr(host, "_check_host_runtime", lambda _manifest: None)
    with pytest.raises(host.InstallFailure, match="INSTALL_ROOT_PERMISSIONS_UNSAFE"):
        host.install_release(bundle, root)


@pytest.mark.parametrize(
    "field",
    ["application_artifact", "wheels", "source_sha", "target_architecture", "runtime_version"],
)
def test_manifest_adversaries_rejected(
    tmp_path: Path, field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = bundle_fixture(tmp_path)
    path = bundle / "deployment_manifest.json"
    value = json.loads(path.read_text())
    if field == "application_artifact":
        (bundle / value[field]).write_bytes(b"tampered")
    elif field == "wheels":
        wheel = value[field][0]
        (bundle / "wheels" / wheel["filename"]).write_bytes(b"tampered")
    elif field == "source_sha":
        value[field] = "f" * 40
    elif field == "target_architecture":
        value[field] = "x86_64"
    else:
        value[field] = "3.13.0"
    path.write_text(json.dumps(value))
    if field == "runtime_version":
        monkeypatch.setattr(host.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(host.platform, "machine", lambda: "arm64")
        with pytest.raises(host.InstallFailure, match="PYTHON_RUNTIME_MISMATCH"):
            host._check_host_runtime(value)
    else:
        with pytest.raises(host.InstallFailure):
            host._verify_bundle(bundle)


@pytest.mark.parametrize(
    "entry",
    [
        ("../outside", b"x", b"0"),
        ("unsafe-link", b"", tarfile.SYMTYPE),
        ("unsafe-hardlink", b"", tarfile.LNKTYPE),
    ],
)
def test_unsafe_archive_leaves_no_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: tuple[str, bytes, bytes]
) -> None:
    bundle = bundle_fixture(tmp_path, extra=entry)
    root = tmp_path / "host"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(host, "_check_host_runtime", lambda _manifest: None)
    monkeypatch.setattr(host, "_install_dependencies", fake_dependency_install)
    with pytest.raises(host.InstallFailure, match="ARCHIVE_UNSAFE"):
        host.install_release(bundle, root)
    assert not os.path.lexists(root / "current")
    assert not (root / "releases" / RELEASE_ID).exists()


def test_candidate_storage_file_in_application_archive_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = bundle_fixture(
        tmp_path, extra=("backend/src/meyar/candidate_storage.pdf", b"synthetic-only", b"0")
    )
    root = tmp_path / "host"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(host, "_check_host_runtime", lambda _manifest: None)
    with pytest.raises(host.InstallFailure, match="APPLICATION_CONTENT_UNEXPECTED"):
        host.install_release(bundle, root)
    assert not os.path.lexists(root / "current")


def test_conflicting_release_and_tampering_rejected(installed: tuple[Path, Path]) -> None:
    bundle, root = installed
    release = root / "releases" / RELEASE_ID
    (release / "backend" / "uv.lock").chmod(0o644)
    (release / "backend" / "uv.lock").write_bytes(b"tampered")
    (release / "backend" / "uv.lock").chmod(0o444)
    with pytest.raises(host.InstallFailure, match="INSTALLED_CONTENT_MISMATCH"):
        host.verify_install(root, RELEASE_ID)
    with pytest.raises(host.InstallFailure):
        host.install_release(bundle, root)


def test_same_release_id_different_bundle_rejected(
    installed: tuple[Path, Path], tmp_path: Path
) -> None:
    _bundle, root = installed
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other = bundle_fixture(other_dir)
    manifest_path = other / "deployment_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["runtime_executable_sha256"] = "b" * 64
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(host.InstallFailure, match="RELEASE_ID_CONFLICT"):
        host.install_release(other, root)


def test_partial_install_never_activates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = bundle_fixture(tmp_path)
    root = tmp_path / "host"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(host, "_check_host_runtime", lambda _manifest: None)

    def fail_install(*_args: object) -> None:
        raise host.InstallFailure("SYNTHETIC_INSTALL_FAILURE")

    monkeypatch.setattr(host, "_install_dependencies", fail_install)
    with pytest.raises(host.InstallFailure, match="SYNTHETIC_INSTALL_FAILURE"):
        host.install_release(bundle, root)
    assert not (root / "releases" / RELEASE_ID).exists()
    assert not os.path.lexists(root / "current")


def test_atomic_activation_and_previous_identity(
    installed: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, root = installed
    assert host.activate_release(root, RELEASE_ID) == "RELEASE_ACTIVATED"
    assert (root / "current").resolve() == root / "releases" / RELEASE_ID
    assert host.activate_release(root, RELEASE_ID) == "ACTIVATION_ALREADY_CURRENT"
    second = RELEASE_ID.replace("abcdef123456", "123456abcdef")
    other = root / "releases" / second
    import shutil

    shutil.copytree(root / "releases" / RELEASE_ID, other)
    state_path = other / "install_state.json"
    state_path.chmod(0o644)
    state = json.loads(state_path.read_text())
    state["release_id"] = second
    source_pointer = other / ".venv" / "lib" / "python3.12" / "site-packages" / "meyar-source.pth"
    source_pointer.chmod(0o644)
    source_pointer.write_text(str(other / "backend" / "src") + "\n")
    source_pointer.chmod(0o444)
    state["tree_sha256"] = host._tree_digest(other)
    state_path.write_text(json.dumps(state))
    state_path.chmod(0o444)
    current_before = (root / "current").resolve()
    original_replace = host.os.replace
    monkeypatch.setattr(host.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("fail")))
    with pytest.raises(OSError):
        host.activate_release(root, second)
    assert (root / "current").resolve() == current_before
    monkeypatch.setattr(host.os, "replace", original_replace)
    assert host.activate_release(root, second) == "RELEASE_ACTIVATED"
    assert (root / "current").resolve() == other
    generation = Path(os.readlink(root / "current")).parts[1]
    activation_state = json.loads((root / "activations" / generation / "state.json").read_text())
    assert activation_state["previous_release_id"] == RELEASE_ID


def test_first_activation_replace_failure_leaves_no_public_current(
    installed: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _bundle, root = installed
    public = root / "current"
    generation_pointer = root / "activations" / "current"
    assert host._current_release(root) is None
    assert not os.path.lexists(public)
    assert not os.path.lexists(generation_pointer)

    original_replace = host.os.replace

    def fail_replace(_source: object, _destination: object) -> None:
        raise OSError("synthetic pointer replacement failure")

    monkeypatch.setattr(host.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic pointer replacement failure"):
        host.activate_release(root, RELEASE_ID)
    assert not os.path.lexists(public)
    assert not os.path.lexists(generation_pointer)

    monkeypatch.setattr(host.os, "replace", original_replace)
    assert host.activate_release(root, RELEASE_ID) == "RELEASE_ACTIVATED"
    assert public.resolve(strict=True) == root / "releases" / RELEASE_ID


def test_operation_lock_prevents_concurrent_activation(installed: tuple[Path, Path]) -> None:
    bundle, root = installed
    descriptor = os.open(root / ".meyar-ops.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(host.InstallFailure, match="OPERATION_BUSY"):
            host.activate_release(root, RELEASE_ID)
        with pytest.raises(host.InstallFailure, match="OPERATION_BUSY"):
            host.install_release(bundle, root)
    finally:
        os.close(descriptor)


def test_result_never_emits_input_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "secret-db-password"
    assert (
        host.main(["verify-install", "--install-root", str(root), "--release-id", RELEASE_ID]) == 1
    )
    output = capsys.readouterr()
    assert "secret-db-password" not in output.out + output.err
    assert json.loads(output.out)["ok"] is False


def test_invalid_invocation_never_echoes_secret(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert host.main(["install-release", "--password=top-secret"]) == 2
    output = capsys.readouterr()
    assert "top-secret" not in output.out + output.err
    assert json.loads(output.out)["findings"][0]["code"] == "INVALID_INVOCATION"


def test_wrong_runtime_executable_identity_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = bundle_fixture(tmp_path)
    manifest = host._verify_bundle(bundle)
    manifest["runtime_version"] = ".".join(map(str, host.sys.version_info[:3]))
    monkeypatch.setattr(host.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(host.platform, "machine", lambda: "arm64")
    with pytest.raises(host.InstallFailure, match="PYTHON_RUNTIME_MISMATCH"):
        host._check_host_runtime(manifest)


@pytest.mark.parametrize(
    "classification", ["BACKUP_RESTORE_REQUIRED", "PROHIBITED_PENDING_PROCEDURE"]
)
def test_unsafe_rollback_classification_blocks_replacement(
    installed: tuple[Path, Path], classification: str
) -> None:
    _bundle, root = installed
    host.activate_release(root, RELEASE_ID)
    second = RELEASE_ID.replace("abcdef123456", "123456abcdef")
    import shutil

    other = root / "releases" / second
    shutil.copytree(root / "releases" / RELEASE_ID, other)
    state_path = other / "install_state.json"
    state_path.chmod(0o644)
    state = json.loads(state_path.read_text())
    state["release_id"] = second
    state["rollback_compatibility"] = classification
    pointer = other / ".venv" / "lib" / "python3.12" / "site-packages" / "meyar-source.pth"
    pointer.chmod(0o644)
    pointer.write_text(str(other / "backend" / "src") + "\n")
    pointer.chmod(0o444)
    state["tree_sha256"] = host._tree_digest(other)
    state_path.write_text(json.dumps(state))
    state_path.chmod(0o444)
    with pytest.raises(host.InstallFailure, match="ACTIVATION_"):
        host.activate_release(root, second)
    assert (root / "current").resolve() == root / "releases" / RELEASE_ID

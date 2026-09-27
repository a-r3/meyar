"""Synthetic offline AI transport and installed-local-identity contracts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from meyar.config import Settings
from meyar.ops import ai_provision as ai
from meyar.ops import deployment_ready, host_config, service_lifecycle
from meyar.ops.model_manifest import (
    ModelApprovalStatus,
    ModelManifest,
    ModelManifestEntry,
    ModelRole,
)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    path = tmp_path / "bundle"
    path.mkdir()
    path.chmod(0o750)
    runtime = b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01" + b"\x00" * 24
    llm = b"GGUF\x03\x00\x00\x00" + b"synthetic llm"
    embedding = b"GGUF\x03\x00\x00\x00" + b"synthetic embedding"
    manifest = (
        ModelManifest(
            entries=[
                ModelManifestEntry(
                    role=ModelRole.LLM,
                    model_name="meyar-test-llm:v1",
                    approval_status=ModelApprovalStatus.DEVELOPMENT_INTEGRATION,
                ),
                ModelManifestEntry(
                    role=ModelRole.EMBEDDING,
                    model_name="meyar-test-embed:v1",
                    embedding_dimensions=3,
                    approval_status=ModelApprovalStatus.DEVELOPMENT_INTEGRATION,
                ),
            ]
        )
        .model_dump_json()
        .encode()
    )
    for name, data in (
        ("ollama.bin", runtime),
        ("llm.gguf", llm),
        ("embed.gguf", embedding),
        ("model_manifest.json", manifest),
    ):
        (path / name).write_bytes(data)
        (path / name).chmod(0o640)
    transport = {
        "format_version": 1,
        "ollama": {
            "platform": "darwin",
            "architecture": "arm64",
            "version": "0.12.0",
            "filename": "ollama.bin",
            "sha256": digest(runtime),
        },
        "model_manifest_filename": "model_manifest.json",
        "model_manifest_sha256": digest(manifest),
        "models": [
            {
                "role": "LLM",
                "model_name": "meyar-test-llm:v1",
                "filename": "llm.gguf",
                "sha256": digest(llm),
            },
            {
                "role": "EMBEDDING",
                "model_name": "meyar-test-embed:v1",
                "filename": "embed.gguf",
                "sha256": digest(embedding),
            },
        ],
    }
    (path / "ai_bundle_manifest.json").write_text(json.dumps(transport))
    (path / "ai_bundle_manifest.json").chmod(0o640)
    return path


def change_transport(bundle: Path, mutate: object) -> None:
    path = bundle / "ai_bundle_manifest.json"
    value = json.loads(path.read_text())
    mutate(value)  # type: ignore[operator]
    path.write_text(json.dumps(value))


def test_synthetic_bundle_is_verified_without_production_approval(bundle: Path) -> None:
    result = ai.run_ai_bundle_verify(bundle)
    assert result.ok
    assert result.findings[0].code == "AI_BUNDLE_VERIFIED"
    assert all(
        entry.approval_status == ModelApprovalStatus.DEVELOPMENT_INTEGRATION
        for entry in ai.verify_ai_bundle(bundle).models.entries
    )


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (lambda value: value["ollama"].update(platform="linux"), "AI_TARGET_UNSUPPORTED"),
        (lambda value: value["ollama"].update(architecture="x86_64"), "AI_TARGET_UNSUPPORTED"),
        (lambda value: value["ollama"].update(sha256="0" * 64), "OLLAMA_RUNTIME_HASH_MISMATCH"),
        (lambda value: value["models"][1].update(role="LLM"), "MODEL_ROLES_INVALID"),
        (
            lambda value: value["models"][0].update(model_name="remote:cloud"),
            "CLOUD_OR_UNSAFE_MODEL_IDENTITY",
        ),
        (lambda value: value["models"][0].update(filename="../llm.gguf"), "AI_BUNDLE_INVALID"),
    ],
)
def test_bundle_rejects_unsafe_contract(bundle: Path, mutation: object, expected: str) -> None:
    change_transport(bundle, mutation)
    assert ai.run_ai_bundle_verify(bundle).findings[0].code == expected


def test_bundle_rejects_symlink_runtime_wrong_shape_and_untrusted_ancestor(bundle: Path) -> None:
    runtime = bundle / "ollama.bin"
    runtime.rename(bundle / "other.bin")
    runtime.symlink_to(bundle / "other.bin")
    assert ai.run_ai_bundle_verify(bundle).findings[0].code == "AI_ARTIFACT_UNSAFE"
    runtime.unlink()
    runtime.write_bytes(b"ELF" + b"\x00" * 28)
    runtime.chmod(0o640)
    change_transport(
        bundle, lambda value: value["ollama"].update(sha256=digest(runtime.read_bytes()))
    )
    assert ai.run_ai_bundle_verify(bundle).findings[0].code == "OLLAMA_RUNTIME_ARCH_MISMATCH"
    bundle.chmod(0o777)
    assert ai.run_ai_bundle_verify(bundle).findings[0].code == "AI_BUNDLE_PATH_UNSAFE"


def test_model_manifest_roles_config_and_cloud_boundary(bundle: Path) -> None:
    verified = ai.verify_ai_bundle(bundle)
    entries = ai._roles(verified.models)
    repeated = verified.models.model_copy(
        update={
            "entries": [
                verified.models.entries[0],
                verified.models.entries[1].model_copy(
                    update={"model_name": verified.models.entries[0].model_name}
                ),
            ]
        }
    )
    with pytest.raises(ai.AIFailure, match="MODEL_ROLES_INVALID"):
        ai._roles(repeated)
    settings = Settings(
        _env_file=None,
        ollama_model="meyar-test-llm:v1",
        ollama_embedding_model="meyar-test-embed:v1",
        embedding_dimensions=3,
    )
    assert ai._check_config(settings, entries) == 11434
    with pytest.raises(ai.AIFailure, match="CONFIGURED_MODEL_MISMATCH"):
        ai._check_config(settings.model_copy(update={"ollama_model": "other:v1"}), entries)
    with pytest.raises(ai.AIFailure, match="EMBEDDING_DIMENSIONS_MISMATCH"):
        ai._check_config(settings.model_copy(update={"embedding_dimensions": 4}), entries)
    for name in ("remote:cloud", "https://example.invalid/model", "namespace/model:v1"):
        with pytest.raises(ai.AIFailure, match="CLOUD_OR_UNSAFE_MODEL_IDENTITY"):
            ai._model_name(name)


def test_ollama_plist_is_fixed_local_and_separate(bundle: Path, tmp_path: Path) -> None:
    root = tmp_path / "host"
    state = {
        "label": "com.bank.meyar.ollama",
        "user_name": "_meyarollama",
        "port": 11434,
        "runtime_sha256": ai.verify_ai_bundle(bundle).transport.ollama.sha256,
    }
    value = plistlib.loads(ai._service_plist(root, state))
    assert value["ProgramArguments"] == [
        str(root / "shared/ollama/runtimes" / state["runtime_sha256"] / "ollama"),
        "serve",
    ]
    assert value["EnvironmentVariables"]["OLLAMA_HOST"] == "127.0.0.1:11434"
    assert value["EnvironmentVariables"]["OLLAMA_MODELS"] == str(root / "shared/ollama/models")
    assert value["EnvironmentVariables"]["OLLAMA_NO_CLOUD"] == "1"
    assert value["UserName"] == "_meyarollama"
    assert "Program" not in value and "Shell" not in str(value)


def test_ollama_installed_plist_rejects_modified_bytes_and_symlink(
    bundle: Path, tmp_path: Path
) -> None:
    root = tmp_path / "host"
    root.mkdir()
    directory = tmp_path / "LaunchDaemons"
    directory.mkdir()
    directory.chmod(0o755)
    state = {
        "label": "com.bank.meyar.ollama",
        "user_name": "_meyarollama",
        "port": 11434,
        "runtime_sha256": ai.verify_ai_bundle(bundle).transport.ollama.sha256,
    }
    path = directory / "com.bank.meyar.ollama.plist"
    expected = ai._service_plist(root, state)
    path.write_bytes(expected)
    path.chmod(0o644)
    assert ai._installed_plist(root, state, directory, os.geteuid()) == expected
    path.write_bytes(expected + b"\n")
    with pytest.raises(ai.AIFailure, match="OLLAMA_PLIST_CONFLICT"):
        ai._installed_plist(root, state, directory, os.geteuid())
    path.rename(directory / "other.plist")
    path.symlink_to(directory / "other.plist")
    with pytest.raises(ai.AIFailure, match="OLLAMA_PLIST_CONFLICT"):
        ai._installed_plist(root, state, directory, os.geteuid())


def test_ollama_service_requires_distinct_user_and_uses_fixed_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "host"
    (root / "shared/ollama").mkdir(parents=True)
    directory = tmp_path / "LaunchDaemons"
    directory.mkdir()
    account = SimpleNamespace(pw_uid=os.geteuid() + 2, pw_gid=os.getgid())
    monkeypatch.setattr(ai.pwd, "getpwnam", lambda _name: account)
    monkeypatch.setattr(ai, "privileged_operation_lock", lambda *_args: nullcontext())
    monkeypatch.setattr(ai, "verify_active_release", lambda _root: "release")
    monkeypatch.setattr(
        ai, "_runtime_state", lambda _root: {"sha256": "a" * 64, "version": "0.12.0"}
    )
    monkeypatch.setattr(ai, "_service_layout", lambda *_args: None)
    monkeypatch.setattr(
        deployment_ready,
        "_installed_spec",
        lambda *_args: (SimpleNamespace(user_name="_meyar"), b"app-plist"),
    )
    monkeypatch.setattr(
        host_config,
        "load_host_settings",
        lambda _root, expected_owner_uid=None: Settings(_env_file=None),
    )
    monkeypatch.setattr(service_lifecycle, "_system_directory", lambda *_args: None)
    monkeypatch.setattr(service_lifecycle, "_installed_bytes", lambda *_args: None)
    published: list[bytes] = []

    def publish(_path: Path, data: bytes, *_args: object) -> str:
        published.append(data)
        return "SERVICE_PLIST_INSTALLED"

    monkeypatch.setattr(service_lifecycle, "_publish_plist", publish)
    common = (root, "com.bank.meyar.ollama", "com.bank.meyar", "_meyarollama", os.geteuid())
    result = ai.run_ollama_service(
        "ollama-service-install",
        *common,
        platform_system="Darwin",
        platform_machine="arm64",
        effective_uid=0,
        plist_directory=directory,
        system_uid=os.geteuid(),
    )
    assert result.ok
    assert len(published) == 1
    assert plistlib.loads(published[0])["ProgramArguments"][1] == "serve"
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(ai, "_installed_plist", lambda *_args: published[0])

    def mutate(action: str, label: str, _path: Path, _runner: object) -> str:
        calls.append((action, label))
        return "SERVICE_STARTED"

    monkeypatch.setattr(service_lifecycle, "_mutate_launchctl", mutate)
    started = ai.run_ollama_service(
        "ollama-service-start",
        *common,
        platform_system="Darwin",
        platform_machine="arm64",
        effective_uid=0,
        plist_directory=directory,
        system_uid=os.geteuid(),
    )
    assert started.ok and calls == [("service-start", "com.bank.meyar.ollama")]
    wrong = ai.run_ollama_service(
        "ollama-service-install",
        root,
        "com.bank.meyar.ollama",
        "com.bank.meyar",
        "_meyar",
        os.geteuid(),
        platform_system="Darwin",
        platform_machine="arm64",
        effective_uid=0,
        plist_directory=directory,
        system_uid=os.geteuid(),
    )
    assert wrong.findings[0].code == "OLLAMA_SERVICE_USER_INVALID"


def test_runtime_install_no_clobber_and_shared_lock(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "host"
    (root / "shared").mkdir(parents=True)
    root.chmod(0o750)
    monkeypatch.setattr(ai.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ai.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(ai, "verify_active_release", lambda _root: "synthetic")
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"ollama version is 0.12.0\n", b"")

    monkeypatch.setattr(ai.subprocess, "run", run)
    assert ai.install_ollama(root, bundle).findings[0].code == "OLLAMA_RUNTIME_INSTALLED"
    assert ai.install_ollama(root, bundle).findings[0].code == "OLLAMA_RUNTIME_ALREADY_INSTALLED"
    assert all(args[1:] == ["--version"] for args in calls)
    runtime = (
        ai._ai_root(root)
        / "runtimes"
        / ai.verify_ai_bundle(bundle).transport.ollama.sha256
        / "ollama"
    )
    assert runtime.stat().st_mode & 0o222 == 0
    lock = os.open(root / ".meyar-ops.lock", os.O_RDONLY)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert ai.install_ollama(root, bundle).findings[0].code == "OPERATION_BUSY"
    finally:
        os.close(lock)
    (ai._ai_root(root) / "runtime.json").chmod(0o644)
    (ai._ai_root(root) / "runtime.json").write_text("{}")
    assert ai.install_ollama(root, bundle).findings[0].code == "OLLAMA_RUNTIME_INVALID"


def test_installed_identity_checks_digest_dimension_and_sanitizes_response(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "host"
    root.mkdir()
    verified = ai.verify_ai_bundle(bundle)
    manifest_digest = verified.transport.model_manifest_sha256
    release = SimpleNamespace(
        model_manifest=SimpleNamespace(status=ModelApprovalStatus.DEVELOPMENT_INTEGRATION)
    )
    monkeypatch.setattr(
        ai, "_release_manifest", lambda _root: ("release", release, manifest_digest)
    )
    monkeypatch.setattr(ai, "_installed_manifest", lambda _root, _digest: verified.models)
    monkeypatch.setattr(
        ai, "_runtime_state", lambda _root: {"sha256": "a" * 64, "version": "0.12.0"}
    )
    monkeypatch.setattr(
        ai, "_service_state", lambda _root: {"port": 11434, "label": "com.bank.meyar.ollama"}
    )
    monkeypatch.setattr(ai, "_installed_plist", lambda *_args: b"plist")
    monkeypatch.setattr(ai, "_service_visible", lambda _label: None)
    receipt = {
        "manifest_sha256": manifest_digest,
        "release_id": "release",
        "runtime_sha256": "a" * 64,
        "models": {
            "LLM": {
                "model_name": "meyar-test-llm:v1",
                "source_sha256": "b" * 64,
                "ollama_digest": "c" * 64,
            },
            "EMBEDDING": {
                "model_name": "meyar-test-embed:v1",
                "source_sha256": "d" * 64,
                "ollama_digest": "e" * 64,
            },
        },
    }
    monkeypatch.setattr(ai, "_receipt", lambda _root, _digest: receipt)
    responses = {
        "/api/version": {"version": "0.12.0"},
        "/api/tags": {
            "models": [
                {"name": "meyar-test-llm:v1", "digest": "c" * 64},
                {"name": "meyar-test-embed:v1", "digest": "e" * 64},
            ]
        },
        "/api/embed": {"model": "meyar-test-embed:v1", "embeddings": [[0.1, 0.2, 0.3]]},
    }
    monkeypatch.setattr(ai, "_request", lambda _port, _method, path, _body=None: responses[path])
    settings = Settings(
        _env_file=None,
        ollama_model="meyar-test-llm:v1",
        ollama_embedding_model="meyar-test-embed:v1",
        embedding_dimensions=3,
    )
    assert ai.verify_installed_models(root, settings)[0] == manifest_digest
    responses["/api/tags"]["models"][0]["digest"] = "f" * 64
    with pytest.raises(ai.AIFailure, match="MODEL_DIGEST_MISMATCH"):
        ai.verify_installed_models(root, settings)
    responses["/api/tags"]["models"][0]["digest"] = "c" * 64
    responses["/api/embed"]["embeddings"] = [[0.1, 0.2]]
    with pytest.raises(ai.AIFailure, match="EMBEDDING_DIMENSIONS_MISMATCH"):
        ai.verify_installed_models(root, settings)
    responses["/api/tags"] = {"unexpected": "candidate secret"}
    with pytest.raises(ai.AIFailure, match="OLLAMA_RESPONSE_INVALID") as error:
        ai.verify_installed_models(root, settings)
    assert "candidate secret" not in str(error.value)


@pytest.mark.parametrize("fail_second", [False, True])
def test_local_import_fixed_argv_idempotency_and_partial_failure(
    bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_second: bool
) -> None:
    root = tmp_path / "host"
    ai_root = root / "shared/ollama"
    stage_parent = ai_root / "staging"
    stage_parent.mkdir(parents=True)
    root.chmod(0o750)
    stage_parent.chmod(0o2750)
    (root / ".meyar-ops.lock").touch(mode=0o600)
    (root / ".meyar-ops.lock").chmod(0o600)
    verified = ai.verify_ai_bundle(bundle)
    manifest_sha = verified.transport.model_manifest_sha256
    runtime_sha = verified.transport.ollama.sha256
    release = SimpleNamespace(
        model_manifest=SimpleNamespace(status=ModelApprovalStatus.DEVELOPMENT_INTEGRATION)
    )
    monkeypatch.setattr(ai.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ai.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(ai, "_release_manifest", lambda _root: ("release", release, manifest_sha))
    monkeypatch.setattr(
        ai, "_runtime_state", lambda _root: {"sha256": runtime_sha, "version": "0.12.0"}
    )
    monkeypatch.setattr(
        ai,
        "_service_state",
        lambda _root: {"port": 11434, "gid": os.getgid(), "label": "com.bank.meyar.ollama"},
    )
    monkeypatch.setattr(ai, "_installed_plist", lambda *_args: b"plist")
    monkeypatch.setattr(ai, "_service_visible", lambda _label: None)
    monkeypatch.setattr(ai, "_embedding_dimension", lambda *_args: None)
    monkeypatch.setattr(
        host_config,
        "load_host_settings",
        lambda _root: Settings(
            _env_file=None,
            ollama_model="meyar-test-llm:v1",
            ollama_embedding_model="meyar-test-embed:v1",
            embedding_dimensions=3,
        ),
    )
    tags: dict[str, str] = {}
    monkeypatch.setattr(ai, "_daemon", lambda _port, _version: dict(tags))
    monkeypatch.setattr(ai, "_tags", lambda _port: dict(tags))
    monkeypatch.setattr(
        ai,
        "_verify_installed",
        lambda _root, _settings, probe=True: (
            manifest_sha,
            json.loads((ai_root / "manifests" / f"{manifest_sha}.receipt.json").read_text()),
        ),
    )
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        assert argv[:2] == [str(ai_root / "runtimes" / runtime_sha / "ollama"), "create"]
        assert argv[3] == "-f" and len(argv) == 5
        modelfile = Path(argv[4])
        source = modelfile.parent / ("llm.gguf" if argv[2].endswith("llm:v1") else "embed.gguf")
        assert modelfile.read_text() == f"FROM {source}\n"
        assert source.read_bytes().startswith(b"GGUF")
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["env"]["OLLAMA_NO_CLOUD"] == "1"  # type: ignore[index]
        if fail_second and len(calls) == 2:
            return subprocess.CompletedProcess(argv, 1, b"candidate secret", b"DB URL secret")
        tags[argv[2]] = ("c" if len(calls) == 1 else "e") * 64
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(ai.subprocess, "run", run)
    result = ai.install_models(root, bundle)
    receipt = ai_root / "manifests" / f"{manifest_sha}.receipt.json"
    if fail_second:
        assert result.findings[0].code == "MODEL_IMPORT_FAILED"
        assert not receipt.exists()
        assert "candidate secret" not in result.model_dump_json()
        assert "DB URL secret" not in result.model_dump_json()
    else:
        assert result.findings[0].code == "LOCAL_MODELS_INSTALLED"
        assert receipt.exists()
        assert ai.install_models(root, bundle).findings[0].code == "LOCAL_MODELS_ALREADY_INSTALLED"
        assert len(calls) == 2
        record = json.loads(receipt.read_text())
        assert record["models"]["LLM"]["source_sha256"] != record["models"]["LLM"]["ollama_digest"]
    assert not list(stage_parent.iterdir())


def test_modelfile_root_must_not_contain_newline() -> None:
    with pytest.raises(ai.AIFailure, match="INSTALL_ROOT_UNSAFE"):
        ai._safe_install_root(Path("/opt/meyar\nFROM remote:cloud"))


def test_production_provider_refuses_identity_drift_before_client_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meyar import config
    from meyar.llm.loopback import build_local_only_async_client

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(env="production"))

    def drift(_settings: object) -> None:
        raise ai.AIFailure("MODEL_DIGEST_MISMATCH")

    monkeypatch.setattr(ai, "assert_runtime_local_identity", drift)
    with pytest.raises(ai.AIFailure, match="MODEL_DIGEST_MISMATCH"):
        build_local_only_async_client(timeout=5.0)


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [(0, "SERVICE_VISIBLE"), (113, "SERVICE_NOT_VISIBLE"), (1, "SERVICE_PROBE_FAILED")],
)
def test_ollama_status_preserves_launchctl_meaning(exit_code: int, expected: str) -> None:
    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        assert argv == ["/bin/launchctl", "print", "system/com.bank.meyar.ollama"]
        return subprocess.CompletedProcess(argv, exit_code, "secret", "candidate text")

    result = ai.run_ollama_service_status(
        "com.bank.meyar.ollama", platform_system="Darwin", runner=runner
    )
    assert result.findings[0].code == expected
    assert result.ok is (exit_code == 0)
    assert "secret" not in result.model_dump_json()
    assert "candidate text" not in result.model_dump_json()


def test_ollama_status_timeout_is_not_absence() -> None:
    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 10, output="candidate text")

    result = ai.run_ollama_service_status(
        "com.bank.meyar.ollama", platform_system="Darwin", runner=runner
    )
    assert result.findings[0].code == "LAUNCHCTL_TIMEOUT"
    assert "candidate text" not in result.model_dump_json()

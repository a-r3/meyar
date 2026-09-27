"""Installed synthetic smoke provenance and receipt regressions."""

from __future__ import annotations

import inspect
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from meyar import smoke_workload
from meyar.ops import lifecycle_acceptance
from meyar.ops.private_evidence import EvidenceFailure, area, publish_bundle
from meyar.smoke_fixture import SYNTHETIC_CV_PDF

IDENTITY = ("meyar-test+abcdef123456", "a" * 40, "head")
IMAGE_REF = "pgvector/pgvector@sha256:" + "d" * 64
IMAGE_DIGEST = "sha256:" + "d" * 64


def test_packaged_cv_is_the_reviewed_synthetic_fixture() -> None:
    fixture = Path(__file__).resolve().parent.parent.parent / "fixtures/synthetic_cvs/valid_cv.pdf"
    assert SYNTHETIC_CV_PDF == fixture.read_bytes()


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "installed"
    root.mkdir(mode=0o700)
    (root / "shared").mkdir(mode=0o700)
    return root


def _receipt(smoke_id: str, **changes: object) -> dict[str, object]:
    receipt: dict[str, object] = {
        "format_version": 2,
        "smoke_id": smoke_id,
        "status": "PASS",
        "release_id": IDENTITY[0],
        "source_sha": IDENTITY[1],
        "alembic_head": IDENTITY[2],
        "execution_mode": "installed-release",
        "disposable_database": True,
        "postgres_image_digest": IMAGE_DIGEST,
        "all_required_steps_passed": True,
        "passed_checks": sorted(smoke_workload.REQUIRED_CHECKS),
    }
    receipt.update(changes)
    return receipt


def test_evidence_launcher_uses_active_python_and_module(tmp_path: Path) -> None:
    root = _root(tmp_path)
    backend = root / "current/backend"
    backend.mkdir(parents=True)
    python = root / "current/.venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "open(os.environ['SMOKE_CAPTURE'], 'w').write(json.dumps([sys.argv[1:], os.getcwd()]))\n"
    )
    python.chmod(0o700)
    capture = tmp_path / "capture.json"
    env = {**os.environ, "SMOKE_CAPTURE": str(capture)}
    script = Path(__file__).resolve().parent.parent / "scripts/fresh_deployment_smoke.py"
    run = subprocess.run(
        [
            sys.executable,
            str(script),
            "--ops-install-root",
            str(root),
            "--smoke-id",
            "smoke-1",
            "--app-label",
            "com.bank.meyar",
            "--postgres-image-ref",
            IMAGE_REF,
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    argv, cwd = json.loads(capture.read_text())
    assert argv == [
        "-I",
        "-m",
        "meyar.smoke_workload",
        "--ops-install-root",
        str(root),
        "--smoke-id",
        "smoke-1",
        "--app-label",
        "com.bank.meyar",
        "--postgres-image-ref",
        IMAGE_REF,
    ]
    assert cwd == str(backend)


def test_checkout_worker_cannot_claim_installed_release(ops_host_root: Path) -> None:
    with pytest.raises(RuntimeError, match="not the active installed release"):
        smoke_workload._installed_identity(ops_host_root, "com.bank.meyar")
    assert not (ops_host_root / "shared/smoke").exists()


def test_installed_commands_use_one_python_and_migration_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python = Path("/installed/releases/release/.venv/bin/python")
    monkeypatch.setattr(smoke_workload, "installed_python", python)
    assert smoke_workload._app_command(
        "alembic", "-c", "/installed/releases/release/backend/alembic.ini", "upgrade", "head"
    ) == [
        str(python),
        "-I",
        "-m",
        "alembic",
        "-c",
        "/installed/releases/release/backend/alembic.ini",
        "upgrade",
        "head",
    ]
    for module in ("meyar.cli", "uvicorn"):
        assert smoke_workload._app_command(module, "synthetic") == [
            str(python),
            "-I",
            "-m",
            module,
            "synthetic",
        ]


def test_installed_server_inherits_bound_loopback_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = Path("/installed/releases/release/.venv/bin/python")
    monkeypatch.setattr(smoke_workload, "installed_python", python)
    monkeypatch.setattr(smoke_workload, "installed_backend", tmp_path)
    captured: dict[str, object] = {}

    class Process:
        def poll(self) -> None:
            return None

    def launch(command: list[str], **kwargs: object) -> Process:
        captured["command"] = command
        captured["pass_fds"] = kwargs["pass_fds"]
        assert kwargs["pass_fds"]
        return Process()

    monkeypatch.setattr(smoke_workload.subprocess, "Popen", launch)
    monkeypatch.setattr(
        smoke_workload.httpx,
        "get",
        lambda *_args, **_kwargs: SimpleNamespace(status_code=200),
    )
    smoke_workload._start_app({}, tmp_path / "app.log")
    assert captured["command"][:5] == [str(python), "-I", "-m", "uvicorn", "meyar.main:app"]
    assert captured["command"][5] == "--fd"
    assert int(captured["command"][6]) == captured["pass_fds"][0]


def test_synthetic_env_excludes_production_database_and_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEYAR_DATABASE_URL", "postgresql+asyncpg://prod/real")
    monkeypatch.setenv("MEYAR_STORAGE_ROOT", "/production/candidates")
    monkeypatch.setenv("PYTHONPATH", "/checkout")
    monkeypatch.setattr(
        smoke_workload,
        "load_host_settings",
        lambda _: SimpleNamespace(
            ollama_base_url="http://127.0.0.1:11434",
            ollama_model="local:v1",
            ollama_embedding_model="embed:v1",
            embedding_dimensions=768,
        ),
    )
    env = smoke_workload._smoke_env(tmp_path / "disposable-storage", tmp_path)
    assert env["MEYAR_DATABASE_URL"] == smoke_workload.DATABASE_URL
    assert "@127.0.0.1:" in env["MEYAR_DATABASE_URL"]
    assert env["MEYAR_STORAGE_ROOT"] == str(tmp_path / "disposable-storage")
    assert env["MEYAR_ENV"] == "test"
    assert "PYTHONPATH" not in env
    assert not any(
        "prod/real" in value or "/production/candidates" in value for value in env.values()
    )


def test_smoke_requires_local_docker_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock = tmp_path / "docker.sock"
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(sock))
        monkeypatch.setenv("DOCKER_HOST", "tcp://remote.example:2376")
        monkeypatch.setenv("DOCKER_CONTEXT", "remote")
        docker = tmp_path / "docker"
        docker.write_text("#!/bin/sh\nexit 0\n")
        docker.chmod(0o700)
        monkeypatch.setattr(smoke_workload.shutil, "which", lambda _: str(docker))
        seen: dict[str, object] = {}

        def inspect(argv: list[str], **kwargs: object) -> SimpleNamespace:
            seen["argv"] = argv
            seen["env"] = kwargs["env"]
            return SimpleNamespace(stdout=f"unix://{sock}\n")

        monkeypatch.setattr(smoke_workload.subprocess, "run", inspect)
        smoke_workload._require_tools(True)
        assert "DOCKER_HOST" not in seen["env"]
        assert "DOCKER_CONTEXT" not in seen["env"]
        assert seen["argv"][0] == str(docker)
        assert smoke_workload.docker_command == [str(docker), "--host", f"unix://{sock}"]


def test_remote_docker_context_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docker = tmp_path / "docker"
    docker.write_text("#!/bin/sh\nexit 0\n")
    docker.chmod(0o700)
    monkeypatch.setattr(smoke_workload.shutil, "which", lambda _: str(docker))
    monkeypatch.setattr(
        smoke_workload.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="tcp://remote.example:2376"),
    )
    with pytest.raises(RuntimeError, match="local Unix Docker socket"):
        smoke_workload._require_tools(True)


def test_missing_pinned_image_never_runs_or_pulls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        smoke_workload, "docker_command", ["/usr/bin/docker", "--host", "unix:///local"]
    )
    monkeypatch.setattr(smoke_workload, "docker_env", {})

    def inspect(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(smoke_workload.subprocess, "run", inspect)
    with pytest.raises(smoke_workload.SmokeIncomplete, match="not preloaded"):
        smoke_workload._require_local_image(IMAGE_REF)
    assert len(calls) == 1 and calls[0][3:5] == ["image", "inspect"]
    assert all("pull" not in command for command in calls)
    with pytest.raises(smoke_workload.SmokeIncomplete, match="exact sha256"):
        smoke_workload._require_local_image("pgvector/pgvector:pg16")
    assert len(calls) == 1


def test_smoke_worker_has_no_docker_pull_command() -> None:
    assert re.search(r"['\"]pull['\"]", inspect.getsource(smoke_workload)) is None


def test_missing_docker_or_image_cannot_publish_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "smoke",
            "--ops-install-root",
            str(root),
            "--app-label",
            "app",
            "--smoke-id",
            "missing-prerequisite",
            "--postgres-image-ref",
            IMAGE_REF,
        ],
    )
    monkeypatch.setattr(
        smoke_workload, "_require_tools", lambda *_: (_ for _ in ()).throw(SystemExit(2))
    )
    with pytest.raises(SystemExit) as exited:
        smoke_workload.main()
    assert exited.value.code == 2
    assert not (root / "shared/smoke/missing-prerequisite").exists()

    monkeypatch.setattr(smoke_workload, "_require_tools", lambda *_: None)
    monkeypatch.setattr(smoke_workload, "_installed_identity", lambda *_: IDENTITY)
    monkeypatch.setattr(
        smoke_workload,
        "_require_local_image",
        lambda *_: (_ for _ in ()).throw(smoke_workload.SmokeIncomplete("image absent")),
    )
    with pytest.raises(SystemExit) as exited:
        smoke_workload.main()
    assert exited.value.code == 2
    assert not (root / "shared/smoke/missing-prerequisite").exists()


def test_preloaded_image_must_match_exact_requested_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        smoke_workload, "docker_command", ["/usr/bin/docker", "--host", "unix:///local"]
    )
    monkeypatch.setattr(smoke_workload, "docker_env", {})
    monkeypatch.setattr(
        smoke_workload.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(["pgvector/pgvector@sha256:" + "e" * 64])
        ),
    )
    with pytest.raises(smoke_workload.SmokeIncomplete, match="does not match"):
        smoke_workload._require_local_image(IMAGE_REF)


def test_disposable_database_uses_loopback_and_random_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(smoke_workload, "PORT", port)
    monkeypatch.setattr(
        smoke_workload, "docker_command", ["/usr/bin/docker", "--host", "unix:///local"]
    )
    monkeypatch.setattr(smoke_workload, "docker_env", {})
    captured: dict[str, object] = {}

    def docker_run(argv: list[str], **kwargs: object) -> None:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]

    monkeypatch.setattr(smoke_workload, "_run", docker_run)
    monkeypatch.setattr(
        smoke_workload.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0)
    )
    smoke_workload._start_disposable_postgres(IMAGE_REF)
    assert "--pull=never" in captured["argv"]
    assert captured["argv"][0] == "/usr/bin/docker"
    assert captured["argv"][-1] == IMAGE_REF
    assert f"127.0.0.1:{port}:5432" in captured["argv"]
    assert "POSTGRES_PASSWORD" in captured["argv"]
    assert not any(smoke_workload.PG_PASSWORD in arg for arg in captured["argv"])
    assert captured["env"]["POSTGRES_PASSWORD"] == smoke_workload.PG_PASSWORD
    assert smoke_workload.PG_DB.startswith("meyar_smoke_")


def test_failed_model_or_missing_capability_cannot_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    monkeypatch.setattr(smoke_workload, "results", [{"step": "local extraction", "ok": False}])
    monkeypatch.setattr(smoke_workload, "checks", set(smoke_workload.REQUIRED_CHECKS))
    with pytest.raises(RuntimeError, match="did not all pass"):
        smoke_workload._publish_evidence(root, "failed-model", "app", IDENTITY, IMAGE_DIGEST)
    monkeypatch.setattr(smoke_workload, "results", [{"step": "smoke", "ok": True}])
    monkeypatch.setattr(
        smoke_workload, "checks", set(smoke_workload.REQUIRED_CHECKS) - {"local_embedding"}
    )
    with pytest.raises(RuntimeError, match="did not all pass"):
        smoke_workload._publish_evidence(root, "missing-capability", "app", IDENTITY, IMAGE_DIGEST)
    assert not (root / "shared/smoke").exists()


def test_caller_cannot_label_another_release_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    monkeypatch.setattr(smoke_workload, "results", [{"step": "complete", "ok": True}])
    monkeypatch.setattr(smoke_workload, "checks", set(smoke_workload.REQUIRED_CHECKS))
    monkeypatch.setattr(
        smoke_workload, "_installed_identity", lambda *_: ("other-release", *IDENTITY[1:])
    )
    with pytest.raises(RuntimeError, match="identity changed"):
        smoke_workload._publish_evidence(root, "wrong-label", "app", IDENTITY, IMAGE_DIGEST)
    assert not (root / "shared/smoke/wrong-label").exists()


def test_successful_installed_worker_receipt_round_trips_to_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _root(tmp_path)
    monkeypatch.setattr(smoke_workload, "results", [{"step": "complete", "ok": True}])
    monkeypatch.setattr(smoke_workload, "checks", set(smoke_workload.REQUIRED_CHECKS))
    monkeypatch.setattr(smoke_workload, "_installed_identity", lambda *_: IDENTITY)
    smoke_workload._publish_evidence(root, "smoke-valid", "app", IDENTITY, IMAGE_DIGEST)
    receipt = json.loads((root / "shared/smoke/smoke-valid/manifest.json").read_text())
    assert receipt["postgres_image_digest"] == IMAGE_DIGEST
    assert lifecycle_acceptance._smoke(root, "smoke-valid", IDENTITY, IMAGE_REF)


def test_developer_commands_remain_uv_based(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(smoke_workload, "installed_python", None)
    assert smoke_workload._app_command("alembic", "upgrade", "head") == [
        "uv",
        "run",
        "alembic",
        "upgrade",
        "head",
    ]
    assert smoke_workload._app_command("meyar.cli", "create-tenant") == [
        "uv",
        "run",
        "meyar",
        "create-tenant",
    ]


def test_lifecycle_accepts_only_exact_installed_release_capabilities(tmp_path: Path) -> None:
    root = _root(tmp_path)
    parent = area(root, "smoke", create=True)
    variants = {
        "valid": {},
        "other-release": {"release_id": "meyar-other+abcdef123456"},
        "other-source": {"source_sha": "b" * 40},
        "other-head": {"alembic_head": "other"},
        "checkout": {"execution_mode": "checkout"},
        "missing": {
            "passed_checks": sorted(smoke_workload.REQUIRED_CHECKS - {"deterministic_ranking"})
        },
        "false-disposable": {"disposable_database": False},
        "missing-image": {"postgres_image_digest": None},
        "changed-image": {"postgres_image_digest": "sha256:" + "e" * 64},
        "mutable-image": {"postgres_image_digest": "pgvector/pgvector:pg16"},
        "legacy": {"format_version": 1},
    }
    for smoke_id, changes in variants.items():
        publish_bundle(parent, smoke_id, {"manifest.json": _receipt(smoke_id, **changes)})
        assert lifecycle_acceptance._smoke(root, smoke_id, IDENTITY, IMAGE_REF) is (
            smoke_id == "valid"
        )
    missing_field = _receipt("missing-image-field")
    missing_field.pop("postgres_image_digest")
    publish_bundle(parent, "missing-image-field", {"manifest.json": missing_field})
    assert not lifecycle_acceptance._smoke(root, "missing-image-field", IDENTITY, IMAGE_REF)
    assert not lifecycle_acceptance._smoke(root, "valid", IDENTITY, "pgvector/pgvector:pg16")
    publish_bundle(
        parent,
        "legacy-shape",
        {
            "manifest.json": {
                "format_version": 1,
                "smoke_id": "legacy-shape",
                "status": "PASS",
                "release_id": IDENTITY[0],
                "source_sha": IDENTITY[1],
                "disposable_database": True,
                "all_steps_passed": True,
                "step_count": 99,
            }
        },
    )
    assert not lifecycle_acceptance._smoke(root, "legacy-shape", IDENTITY, IMAGE_REF)
    tampered = parent / "valid/manifest.json"
    tampered.write_text(tampered.read_text().replace("installed-release", "checkout"))
    with pytest.raises(EvidenceFailure):
        lifecycle_acceptance._smoke(root, "valid", IDENTITY, IMAGE_REF)

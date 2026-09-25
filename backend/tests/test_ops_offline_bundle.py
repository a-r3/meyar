"""Bundle builder tests use a throwaway Git release and no network."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from meyar.ops import offline_bundle
from meyar.ops.build_release import BuildReleaseRequest, build_release
from meyar.ops.model_manifest import ModelApprovalStatus
from meyar.ops.release_manifest import RollbackCompatibility
from meyar.ops.verify_release import verify_release


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _application_release(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    backend = repo / "backend"
    (backend / "src" / "meyar" / "ops").mkdir(parents=True)
    (backend / "src" / "meyar" / "__init__.py").write_text("")
    (backend / "src" / "meyar" / "ops" / "offline_host.py").write_bytes(
        (Path(__file__).parent.parent / "src" / "meyar" / "ops" / "offline_host.py").read_bytes()
    )
    (backend / "pyproject.toml").write_text(
        '[project]\nname = "meyar"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n'
    )
    (backend / "uv.lock").write_bytes((Path(__file__).parent.parent / "uv.lock").read_bytes())
    (backend / "alembic.ini").write_text("[alembic]\nscript_location = %(here)s/alembic\n")
    versions = backend / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "0001_initial.py").write_text("revision = '0001initial'\ndown_revision = None\n")
    (backend / ".env").write_text("SECRET=never-package\n")
    (backend / "tests").mkdir()
    (backend / "tests" / "test_never.py").write_text("assert False\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "synthetic release")
    source_sha = _git(repo, "rev-parse", "HEAD")
    output = tmp_path / "application"
    output.mkdir()
    built = build_release(
        BuildReleaseRequest(
            source_sha=source_sha,
            output_dir=output,
            rollback_compatibility=RollbackCompatibility.APP_ONLY,
            model_manifest_reference="TBD",
            model_approval_status=ModelApprovalStatus.DEVELOPMENT_INTEGRATION,
        ),
        invocation_cwd=repo,
    )
    assert built.ok
    release_id = f"meyar-0.1.0+{source_sha[:12]}"
    return (
        output / f"{release_id}.tar.gz",
        output / f"{release_id}.release-manifest.json",
        output / "SHA256SUMS",
    )


def _mini_wheel() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("fixture/__init__.py", "")
    return output.getvalue()


def test_bundle_build_from_verified_application_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact, manifest, checksums = _application_release(tmp_path)
    wheel_bytes = _mini_wheel()
    wheel_sha = hashlib.sha256(wheel_bytes).hexdigest()

    def select(package: dict[str, object]) -> tuple[str, str]:
        name = str(package["name"]).replace("-", "_")
        return f"https://files.pythonhosted.org/packages/{name}-1-py3-none-any.whl", wheel_sha

    def download(_url: str, _sha: str, destination: Path) -> None:
        destination.write_bytes(wheel_bytes)

    monkeypatch.setattr(offline_bundle, "_selected_wheel", select)
    monkeypatch.setattr(offline_bundle, "_download_locked_wheel", download)
    output = tmp_path / "output"
    output.mkdir()
    request = offline_bundle.BundleBuildRequest(
        artifact_path=artifact,
        release_manifest_path=manifest,
        sha256sums_path=checksums,
        output_dir=output,
        runtime_version="3.12.9",
        runtime_executable_sha256="a" * 64,
    )
    result = offline_bundle.build_deployment_bundle(request)
    assert result.ok, result.model_dump()
    bundle = next(output.iterdir())
    deployment = offline_bundle.DeploymentManifest.model_validate_json(
        (bundle / "deployment_manifest.json").read_bytes()
    )
    assert deployment.source_sha == json.loads(manifest.read_text())["source_sha"]
    assert len(deployment.wheels) == 42
    assert {wheel.name for wheel in deployment.wheels} >= {"pillow", "greenlet"}
    assert verify_release(
        manifest_path=bundle / manifest.name,
        sha256sums_path=bundle / "SHA256SUMS",
        artifact_path=bundle / artifact.name,
    ).ok
    assert not any(path.name in {".env", "test_never.py"} for path in bundle.rglob("*"))
    assert offline_bundle.build_deployment_bundle(request).ok is False  # no overwrite


def test_bundle_build_rejects_tampered_application(tmp_path: Path) -> None:
    artifact, manifest, checksums = _application_release(tmp_path)
    with artifact.open("ab") as stream:
        stream.write(b"tampered")
    output = tmp_path / "output"
    output.mkdir()
    result = offline_bundle.build_deployment_bundle(
        offline_bundle.BundleBuildRequest(
            artifact_path=artifact,
            release_manifest_path=manifest,
            sha256sums_path=checksums,
            output_dir=output,
            runtime_version="3.12.9",
            runtime_executable_sha256="a" * 64,
        )
    )
    assert result.ok is False
    assert list(output.iterdir()) == []

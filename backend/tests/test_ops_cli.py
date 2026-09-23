"""`meyar-ops` CLI boundary (issue #35 PR1 §2/§3/§12): argument parsing,
JSON-on-stdout contract, and the documented exit codes."""

import json
import subprocess
from pathlib import Path

import pytest

import meyar.ops.cli as cli_module
from meyar.ops.result import FindingStatus, OpsExitCode, OpsResultBuilder


def test_preflight_prints_valid_json_and_exits_zero_on_success(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(["preflight"])
    assert exc_info.value.code == OpsExitCode.SUCCESS
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "preflight"
    assert payload["ok"] is True


def test_invalid_command_exits_with_invalid_invocation_code() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(["not-a-real-command"])
    assert exc_info.value.code == OpsExitCode.INVALID_INVOCATION


def test_no_command_exits_with_invalid_invocation_code() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main([])
    assert exc_info.value.code == OpsExitCode.INVALID_INVOCATION


def test_verify_release_missing_required_args_is_invalid_invocation() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(["verify-release"])
    assert exc_info.value.code == OpsExitCode.INVALID_INVOCATION


def test_readiness_check_failure_exits_with_check_failure_code(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def _failing_readiness():
        builder = OpsResultBuilder(action="readiness")
        builder.add(component="database", status=FindingStatus.FAIL, code="X", message="down")
        return builder.build()

    async def _fake_run_readiness():
        return _failing_readiness()

    monkeypatch.setattr(cli_module, "run_readiness", _fake_run_readiness)
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(["readiness"])
    assert exc_info.value.code == OpsExitCode.CHECK_FAILURE
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


def test_uncaught_exception_is_infrastructure_failure_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    def _boom():
        raise RuntimeError("simulated infrastructure failure with secret=hunter2")

    monkeypatch.setattr(cli_module, "run_preflight", _boom)
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(["preflight"])
    assert exc_info.value.code == OpsExitCode.INFRASTRUCTURE_FAILURE
    out = capsys.readouterr().out
    payload = json.loads(out)  # still valid JSON, never a bare traceback
    assert payload["ok"] is False
    assert payload["findings"][0]["code"] == "UNCAUGHT_EXCEPTION"


def test_verify_release_exits_check_failure_on_checksum_mismatch(
    tmp_path: Path, capsys
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "release_version": "0.1.0",
                "release_id": "meyar-0.1.0+aaaaaaaaaaaa",
                "source_sha": "a" * 40,
                "built_at": "2026-09-23T00:00:00Z",
                "required_python_version": ">=3.12",
                "uv_lock_sha256": "b" * 64,
                "alembic_heads": ["6f4c2a9d8e10"],
                "rollback_compatibility": "BACKUP_RESTORE_REQUIRED",
                "model_manifest": {"reference": "x", "status": "DEVELOPMENT_INTEGRATION"},
                "artifact_format": "tar.gz",
            }
        )
    )
    artifact_path = tmp_path / "artifact.tar.gz"
    artifact_path.write_bytes(b"not a real tarball, just checksum-tested bytes")
    sums_path = tmp_path / "SHA256SUMS"
    sums_path.write_text(f"{'0' * 64}  artifact.tar.gz\n")

    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(
            [
                "verify-release",
                "--manifest",
                str(manifest_path),
                "--sha256sums",
                str(sums_path),
                "--artifact",
                str(artifact_path),
            ]
        )
    assert exc_info.value.code == OpsExitCode.CHECK_FAILURE
    payload = json.loads(capsys.readouterr().out)
    codes = {f["component"]: f["code"] for f in payload["findings"]}
    assert codes["artifact_checksum"] == "CHECKSUM_MISMATCH"


def test_build_release_missing_required_args_is_invalid_invocation() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(["build-release"])
    assert exc_info.value.code == OpsExitCode.INVALID_INVOCATION


def test_build_release_invalid_rollback_compatibility_choice_is_invalid_invocation(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(
            [
                "build-release",
                "--source-sha",
                "a" * 40,
                "--output-dir",
                str(tmp_path),
                "--rollback-compatibility",
                "NOT_A_REAL_VALUE",
                "--model-manifest-reference",
                "x",
                "--model-approval-status",
                "DEVELOPMENT_INTEGRATION",
            ]
        )
    assert exc_info.value.code == OpsExitCode.INVALID_INVOCATION


def test_build_release_invalid_model_approval_status_choice_is_invalid_invocation(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(
            [
                "build-release",
                "--source-sha",
                "a" * 40,
                "--output-dir",
                str(tmp_path),
                "--rollback-compatibility",
                "APP_ONLY",
                "--model-manifest-reference",
                "x",
                "--model-approval-status",
                "NOT_A_REAL_VALUE",
            ]
        )
    assert exc_info.value.code == OpsExitCode.INVALID_INVOCATION


def test_build_release_nonexistent_commit_exits_check_failure(tmp_path: Path, capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(
            [
                "build-release",
                "--source-sha",
                "a" * 40,
                "--output-dir",
                str(tmp_path),
                "--rollback-compatibility",
                "APP_ONLY",
                "--model-manifest-reference",
                "docs/DECISIONS.md#D-068",
                "--model-approval-status",
                "DEVELOPMENT_INTEGRATION",
            ]
        )
    assert exc_info.value.code == OpsExitCode.CHECK_FAILURE
    payload = json.loads(capsys.readouterr().out)
    codes = {f["component"]: f["code"] for f in payload["findings"]}
    assert codes["commit_exists"] == "SOURCE_COMMIT_NOT_FOUND"


def test_build_release_succeeds_against_the_real_repository_head(tmp_path: Path, capsys) -> None:
    """End-to-end CLI proof against this actual repository's real HEAD
    commit (read-only — never writes to the real repo)."""
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(
            [
                "build-release",
                "--source-sha",
                head_sha,
                "--output-dir",
                str(tmp_path),
                "--rollback-compatibility",
                "BACKUP_RESTORE_REQUIRED",
                "--model-manifest-reference",
                "docs/DECISIONS.md#D-068",
                "--model-approval-status",
                "DEVELOPMENT_INTEGRATION",
            ]
        )
    assert exc_info.value.code == OpsExitCode.SUCCESS
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert any(f["code"] == "RELEASE_BUILT" for f in payload["findings"])
    assert len(list(tmp_path.glob("*.tar.gz"))) == 1
    assert len(list(tmp_path.glob("*.release-manifest.json"))) == 1
    assert (tmp_path / "SHA256SUMS").is_file()

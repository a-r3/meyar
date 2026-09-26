"""`meyar-ops service-status` (issue #35 PR2). Read-only, injected-runner
tests only — no real `launchctl`/macOS host is required or assumed. Real
`launchctl print`/`bootstrap`/reboot behavior remains UNCONFIRMED until a
macOS/Apple-Silicon rehearsal; see docs/MEYAR_OPS.md."""

from __future__ import annotations

import subprocess

import pytest

from meyar.ops.result import FindingStatus
from meyar.ops.service_status import LAUNCHCTL_PATH, run_service_status


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[LAUNCHCTL_PATH], returncode=returncode, stdout=stdout, stderr=stderr
    )


# --- 16. service-status fixed launchctl argv --------------------------------


def test_runner_receives_fixed_argv_with_absolute_launchctl_path() -> None:
    captured: list[list[str]] = []

    def _runner(argv: list[str]) -> subprocess.CompletedProcess:
        captured.append(argv)
        return _completed(0)

    run_service_status(label="meyar.application", platform_system="Darwin", runner=_runner)
    assert captured == [[LAUNCHCTL_PATH, "print", "system/meyar.application"]]
    assert LAUNCHCTL_PATH == "/bin/launchctl"


def test_invalid_label_never_reaches_the_runner() -> None:
    captured: list[list[str]] = []

    def _runner(argv: list[str]) -> subprocess.CompletedProcess:
        captured.append(argv)
        return _completed(0)

    result = run_service_status(label="bad label", platform_system="Darwin", runner=_runner)
    assert result.ok is False
    assert result.findings[0].code == "INVALID_LABEL"
    assert captured == []


# --- 17. fake launchctl success/non-loaded/failure mappings ----------------


def test_zero_exit_maps_to_ok_service_visible() -> None:
    result = run_service_status(
        label="meyar.application", platform_system="Darwin", runner=lambda argv: _completed(0)
    )
    assert result.ok is True
    assert result.findings[0].code == "SERVICE_VISIBLE"
    assert result.findings[0].status == FindingStatus.OK


def test_not_found_exit_maps_to_fail_service_not_visible() -> None:
    result = run_service_status(
        label="meyar.application", platform_system="Darwin", runner=lambda argv: _completed(113)
    )
    assert result.ok is False
    assert result.findings[0].code == "SERVICE_NOT_VISIBLE"
    assert result.findings[0].status == FindingStatus.FAIL


def test_unexpected_nonzero_exit_is_probe_failure_without_raw_output() -> None:
    result = run_service_status(
        label="meyar.application",
        platform_system="Darwin",
        runner=lambda argv: _completed(1, stdout="secret stdout", stderr="secret stderr"),
    )
    assert result.ok is False
    assert result.findings[0].code == "SERVICE_PROBE_FAILED"
    assert "secret" not in result.model_dump_json()


def test_runner_timeout_maps_to_fail_launchctl_timeout() -> None:
    def _runner(argv: list[str]) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=10.0, output="secret timeout output")

    result = run_service_status(label="meyar.application", platform_system="Darwin", runner=_runner)
    assert result.ok is False
    assert result.findings[0].code == "LAUNCHCTL_TIMEOUT"
    assert "secret" not in result.model_dump_json()


def test_runner_missing_binary_maps_to_fail_launchctl_unavailable() -> None:
    def _runner(argv: list[str]) -> subprocess.CompletedProcess:
        raise FileNotFoundError("no such file or directory: /bin/launchctl")

    result = run_service_status(label="meyar.application", platform_system="Darwin", runner=_runner)
    assert result.ok is False
    assert result.findings[0].code == "LAUNCHCTL_UNAVAILABLE"


# --- 18. PLATFORM_UNSUPPORTED behavior on Linux -----------------------------


def test_non_darwin_platform_is_truthful_platform_unsupported_and_skips_runner() -> None:
    captured: list[list[str]] = []

    def _runner(argv: list[str]) -> subprocess.CompletedProcess:
        captured.append(argv)
        return _completed(0)

    result = run_service_status(label="meyar.application", platform_system="Linux", runner=_runner)
    assert result.ok is False
    assert result.findings[0].code == "PLATFORM_UNSUPPORTED"
    assert captured == [], "launchctl must never be invoked on an unsupported platform"


def test_default_runner_is_used_when_none_injected_and_platform_is_unsupported() -> None:
    # No runner provided, and platform is not Darwin: default_launchctl_runner
    # must never actually be invoked (would fail on a non-macOS CI host).
    result = run_service_status(label="meyar.application", platform_system="Linux")
    assert result.ok is False
    assert result.findings[0].code == "PLATFORM_UNSUPPORTED"


# --- 19. raw launchctl stdout not leaked ------------------------------------


def test_raw_launchctl_stdout_never_appears_in_result() -> None:
    secret_marker = "CANDIDATE-SHAPED-OUTPUT-MARKER-XYZ"
    result = run_service_status(
        label="meyar.application",
        platform_system="Darwin",
        runner=lambda argv: _completed(0, stdout=secret_marker, stderr=secret_marker),
    )
    dumped = result.model_dump_json()
    assert secret_marker not in dumped


# --- 20. credential-bearing error text redacted -----------------------------


def test_credential_bearing_runner_error_is_redacted() -> None:
    def _runner(argv: list[str]) -> subprocess.CompletedProcess:
        raise OSError(
            "launchctl helper failed: postgresql+asyncpg://meyar:hunter2@127.0.0.1:5432/meyar"
        )

    result = run_service_status(label="meyar.application", platform_system="Darwin", runner=_runner)
    assert result.ok is False
    assert "hunter2" not in result.model_dump_json()
    assert "launchctl helper failed" not in result.model_dump_json()


# --- 21. no shell=True -------------------------------------------------------


def test_service_status_source_never_passes_shell_true_as_code() -> None:
    """AST-based (not substring) so this stays true even if a comment or
    docstring elsewhere in the module mentions `shell=True` in prose."""
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parent.parent / "src" / "meyar" / "ops" / "service_status.py"
    ).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "shell":
            assert not (isinstance(node.value, ast.Constant) and node.value.value is True)


def test_default_launchctl_runner_never_sets_shell_true(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_kwargs: dict = {}

    def _fake_run(argv, **kwargs):  # noqa: ANN001, ANN202 - test double signature mirrors subprocess.run
        captured_kwargs.update(kwargs)
        return _completed(0)

    import meyar.ops.service_status as service_status_module

    monkeypatch.setattr(service_status_module.subprocess, "run", _fake_run)
    service_status_module.default_launchctl_runner([LAUNCHCTL_PATH, "print", "system/x"])
    assert captured_kwargs.get("shell", False) is False

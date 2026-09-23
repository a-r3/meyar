"""Read-only `launchctl print system/<label>` probe (issue #35 PR2). This
module never mutates launchd state — no `bootstrap`/`bootout`/`kickstart`,
no `sudo`, no privilege escalation. It only reports whether the configured
label is currently visible in the *system* launchd domain, via a fixed
argv (never `shell=True`, never a bare `launchctl` relying on PATH). It is
macOS-only: on any other platform it returns a truthful
`PLATFORM_UNSUPPORTED` finding rather than pretending the check ran.

The runner is injected (`LaunchctlRunner`) so tests never require a real
`launchctl`/macOS host — see docs/MEYAR_OPS.md for what remains UNCONFIRMED
until a real macOS/Apple-Silicon rehearsal. Raw `launchctl` stdout/stderr
is never included in the returned `OpsResult` — only the fixed argv used,
the process exit status, and a bounded/redacted error string on the rare
infrastructure-level failure path (`launchctl` missing, timeout)."""

from __future__ import annotations

import platform
import subprocess
from typing import Protocol

from meyar.ops.redact import safe_exception_text
from meyar.ops.result import FindingStatus, OpsResult, OpsResultBuilder
from meyar.ops.service_plist import validate_label

LAUNCHCTL_PATH = "/bin/launchctl"
_SUBPROCESS_TIMEOUT_SECONDS = 10.0


class LaunchctlRunner(Protocol):
    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]: ...


def default_launchctl_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, argv[0] is an absolute path
        argv,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
    )


def run_service_status(
    *,
    label: str,
    platform_system: str | None = None,
    runner: LaunchctlRunner | None = None,
) -> OpsResult:
    builder = OpsResultBuilder(action="service-status")
    system = platform_system if platform_system is not None else platform.system()

    if system != "Darwin":
        builder.add(
            component="platform",
            status=FindingStatus.FAIL,
            code="PLATFORM_UNSUPPORTED",
            message=f"service-status requires macOS (Darwin); running on {system}",
        )
        return builder.build()

    try:
        validate_label(label)
    except ValueError as exc:
        builder.add(
            component="label",
            status=FindingStatus.FAIL,
            code="INVALID_LABEL",
            message=safe_exception_text(exc),
        )
        return builder.build()

    argv = [LAUNCHCTL_PATH, "print", f"system/{label}"]
    run = runner if runner is not None else default_launchctl_runner
    try:
        completed = run(argv)
    except subprocess.TimeoutExpired:
        builder.add(
            component="launchctl_probe",
            status=FindingStatus.FAIL,
            code="LAUNCHCTL_TIMEOUT",
            message="launchctl print did not complete within the timeout",
        )
        return builder.build()
    except OSError as exc:
        builder.add(
            component="launchctl_probe",
            status=FindingStatus.FAIL,
            code="LAUNCHCTL_UNAVAILABLE",
            message=safe_exception_text(exc),
        )
        return builder.build()

    if completed.returncode == 0:
        builder.add(
            component="launchctl_probe",
            status=FindingStatus.OK,
            code="SERVICE_VISIBLE",
            message=f"label visible in system launchd domain (exit={completed.returncode})",
        )
    else:
        builder.add(
            component="launchctl_probe",
            status=FindingStatus.FAIL,
            code="SERVICE_NOT_VISIBLE",
            message=f"label not visible in system launchd domain (exit={completed.returncode})",
        )
    return builder.build()

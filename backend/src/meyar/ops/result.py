"""Stable machine-readable result envelope shared by every meyar-ops
command (preflight, status, readiness, verify-release, and future
commands). See docs/MEYAR_OPS.md for the frozen JSON contract.

Security boundary: a Finding's ``message`` must never carry a secret,
credential, full database URL, session value, environment dump, or any
candidate/CV/JD/query content — see ``redact.py`` and
``.claude/rules/security-privacy.md``. This module only defines shape; it
is the caller's responsibility to pass already-safe text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import IntEnum, StrEnum

from pydantic import BaseModel, Field


class FindingStatus(StrEnum):
    """Per-component result. OK/WARN never fail the overall command;
    FAIL does (see OpsResult.ok). SKIPPED means the check could not run
    (e.g. a prerequisite component already failed) — it is reported, not
    hidden."""

    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


class Finding(BaseModel):
    """One bounded, typed component result. Every command preserves every
    component's Finding even when another component fails — nothing is
    ever dropped from the findings list because an earlier check failed."""

    model_config = {"extra": "forbid"}

    component: str = Field(min_length=1, max_length=64)
    status: FindingStatus
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=500)


class OpsResult(BaseModel):
    """The one stable envelope every meyar-ops command returns as JSON on
    stdout. ``ok`` is derived, uniformly across commands: true iff no
    Finding has status FAIL. WARN/SKIPPED findings never affect ``ok``."""

    model_config = {"extra": "forbid"}

    action: str = Field(min_length=1, max_length=64)
    ok: bool
    started_at: datetime
    finished_at: datetime
    findings: list[Finding] = Field(default_factory=list)


class OpsExitCode(IntEnum):
    """Documented process exit codes. Every meyar-ops command uses
    exactly one of these — see docs/MEYAR_OPS.md."""

    SUCCESS = 0
    CHECK_FAILURE = 1
    INVALID_INVOCATION = 2
    INFRASTRUCTURE_FAILURE = 3


class OpsResultBuilder:
    """Accumulates Findings for one command invocation and produces the
    final OpsResult. A single builder instance owns one ``started_at``
    timestamp captured at construction time."""

    def __init__(self, *, action: str) -> None:
        self.action = action
        self.started_at = datetime.now(UTC)
        self.findings: list[Finding] = []

    def add(self, *, component: str, status: FindingStatus, code: str, message: str) -> None:
        self.findings.append(
            Finding(component=component, status=status, code=code, message=message)
        )

    def build(self) -> OpsResult:
        ok = not any(f.status is FindingStatus.FAIL for f in self.findings)
        return OpsResult(
            action=self.action,
            ok=ok,
            started_at=self.started_at,
            finished_at=datetime.now(UTC),
            findings=list(self.findings),
        )


def exit_code_for(result: OpsResult) -> OpsExitCode:
    return OpsExitCode.SUCCESS if result.ok else OpsExitCode.CHECK_FAILURE


def build_single_finding_result(
    *, action: str, component: str, status: FindingStatus, code: str, message: str
) -> OpsResult:
    """Used for a top-level command failure that happened before any
    per-component checks could run (e.g. an uncaught exception at the CLI
    boundary) — still the same one stable envelope, never a bare
    traceback."""
    builder = OpsResultBuilder(action=action)
    builder.add(component=component, status=status, code=code, message=message)
    return builder.build()

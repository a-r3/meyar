"""Stability of the meyar-ops OpsResult/Finding JSON contract."""

import json

import pytest
from pydantic import ValidationError

from meyar.ops.result import (
    Finding,
    FindingStatus,
    OpsExitCode,
    OpsResultBuilder,
    build_single_finding_result,
    exit_code_for,
)


def test_ok_is_true_when_no_fail_findings() -> None:
    builder = OpsResultBuilder(action="preflight")
    builder.add(component="a", status=FindingStatus.OK, code="X", message="fine")
    builder.add(component="b", status=FindingStatus.WARN, code="Y", message="meh")
    builder.add(component="c", status=FindingStatus.SKIPPED, code="Z", message="n/a")
    result = builder.build()
    assert result.ok is True
    assert exit_code_for(result) is OpsExitCode.SUCCESS


def test_ok_is_false_when_any_fail_finding() -> None:
    builder = OpsResultBuilder(action="readiness")
    builder.add(component="a", status=FindingStatus.OK, code="X", message="fine")
    builder.add(component="b", status=FindingStatus.FAIL, code="Y", message="broken")
    result = builder.build()
    assert result.ok is False
    assert exit_code_for(result) is OpsExitCode.CHECK_FAILURE


def test_every_finding_preserved_even_after_a_failure() -> None:
    """Every component's Finding survives even when an earlier one failed
    — nothing is ever dropped from the findings list."""
    builder = OpsResultBuilder(action="readiness")
    builder.add(component="database", status=FindingStatus.FAIL, code="X", message="down")
    builder.add(component="storage_write", status=FindingStatus.OK, code="Y", message="ok")
    result = builder.build()
    components = [f.component for f in result.findings]
    assert components == ["database", "storage_write"]


def test_result_envelope_has_stable_shape() -> None:
    builder = OpsResultBuilder(action="status")
    builder.add(component="a", status=FindingStatus.OK, code="X", message="fine")
    result = builder.build()
    payload = json.loads(result.model_dump_json())
    assert set(payload.keys()) == {"action", "ok", "started_at", "finished_at", "findings"}
    assert set(payload["findings"][0].keys()) == {"component", "status", "code", "message"}


def test_started_at_never_after_finished_at() -> None:
    builder = OpsResultBuilder(action="status")
    result = builder.build()
    assert result.started_at <= result.finished_at


def test_finding_requires_all_fields() -> None:
    with pytest.raises(ValidationError):
        Finding(component="x", status=FindingStatus.OK, code="Y")  # type: ignore[call-arg]


def test_build_single_finding_result_used_for_top_level_failure() -> None:
    result = build_single_finding_result(
        action="preflight",
        component="preflight",
        status=FindingStatus.FAIL,
        code="UNCAUGHT_EXCEPTION",
        message="boom",
    )
    assert result.ok is False
    assert result.findings[0].code == "UNCAUGHT_EXCEPTION"


def test_exit_codes_are_documented_and_distinct() -> None:
    values = {c.value for c in OpsExitCode}
    assert values == {0, 1, 2, 3}

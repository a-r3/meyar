"""`meyar-ops preflight` (issue #35 PR1 §4/§12). Missing optional tooling
must be a truthful WARN finding, never an uncaught exception — and
preflight never writes/deletes anything (no storage probe here)."""

from pathlib import Path

import pytest

import meyar.ops.preflight as preflight_module
from meyar.config import Settings
from meyar.ops.config import OpsSettings
from meyar.ops.result import FindingStatus


def _by_component(result, component: str):
    matches = [f for f in result.findings if f.component == component]
    assert matches, f"no finding for component {component!r}"
    return matches[0]


def test_preflight_returns_findings_for_every_documented_component() -> None:
    result = preflight_module.run_preflight()
    components = {f.component for f in result.findings}
    assert "python_version" in components
    assert "uv" in components
    assert "platform" in components
    assert "disk_space" in components
    assert "storage_root" in components
    assert "ollama_url_loopback" in components
    assert "alembic_config" in components
    assert "llm_model_name" in components
    assert "embedding_model_name" in components
    for tool in ("psql", "pg_dump", "pg_restore"):
        assert f"postgres_client_tool_{tool}" in components


def test_missing_postgres_tool_is_warn_not_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight_module.shutil, "which", lambda _name: None)
    result = preflight_module.run_preflight()
    for tool in ("psql", "pg_dump", "pg_restore"):
        finding = _by_component(result, f"postgres_client_tool_{tool}")
        assert finding.status is FindingStatus.WARN
        assert finding.code == "TOOL_NOT_FOUND"
    # Missing optional tools alone never fail the overall preflight.
    assert result.ok is True


def test_non_loopback_ollama_url_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    bad_settings = Settings(ollama_base_url="http://evil.example.com:11434")
    monkeypatch.setattr(preflight_module, "get_settings", lambda: bad_settings)
    result = preflight_module.run_preflight()
    finding = _by_component(result, "ollama_url_loopback")
    assert finding.status is FindingStatus.FAIL
    assert result.ok is False


def test_disk_space_below_minimum_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        preflight_module, "get_ops_settings", lambda: OpsSettings(min_free_disk_mb=10**9)
    )
    monkeypatch.setattr(
        preflight_module, "get_settings", lambda: Settings(storage_root=str(tmp_path))
    )
    result = preflight_module.run_preflight()
    finding = _by_component(result, "disk_space")
    assert finding.status is FindingStatus.FAIL
    assert finding.code == "DISK_SPACE_INSUFFICIENT"


def test_empty_model_name_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight_module, "get_settings", lambda: Settings(ollama_model=""))
    result = preflight_module.run_preflight()
    finding = _by_component(result, "llm_model_name")
    assert finding.status is FindingStatus.FAIL


def test_missing_required_path_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        preflight_module, "_REQUIRED_PATHS", (tmp_path / "does-not-exist.toml",)
    )
    result = preflight_module.run_preflight()
    finding = _by_component(result, "required_path")
    assert finding.status is FindingStatus.FAIL
    assert finding.code == "PATH_MISSING"


def test_unsupported_python_version_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight_module.sys, "version_info", (3, 11, 0, "final", 0))
    result = preflight_module.run_preflight()
    finding = _by_component(result, "python_version")
    assert finding.status is FindingStatus.FAIL


def test_uv_not_found_is_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    real_which = preflight_module.shutil.which

    def fake_which(name: str) -> str | None:
        return None if name == "uv" else real_which(name)

    monkeypatch.setattr(preflight_module.shutil, "which", fake_which)
    result = preflight_module.run_preflight()
    finding = _by_component(result, "uv")
    assert finding.status is FindingStatus.WARN
    assert finding.code == "UV_NOT_FOUND"


def test_preflight_never_exposes_database_url() -> None:
    result = preflight_module.run_preflight()
    payload = result.model_dump_json()
    assert "meyar_dev_password" not in payload

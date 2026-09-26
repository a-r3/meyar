"""Installed deployment gate: synthetic host, read-only probes, and isolation."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_ops_schema_init import _seal_release, active_root  # noqa: F401 - pytest fixture

import meyar
from meyar.ops import deployment_ready as gate
from meyar.ops import offline_host, schema_init
from meyar.ops.alembic_introspect import DbAlembicRevisionResult
from meyar.ops.host_config import load_host_settings
from meyar.ops.service_lifecycle import ServicePrincipal
from meyar.ops.service_plist import ServiceSpec, render_service_plist

_REAL_PROBE_OLLAMA = gate._probe_ollama


def codes(result: gate.OpsResult) -> dict[str, str]:
    return {finding.component: finding.code for finding in result.findings}


@pytest.fixture
def schema_active_root(request: pytest.FixtureRequest) -> Path:
    return request.getfixturevalue("active_root")  # type: ignore[no-any-return]


@pytest.fixture
def deployment(
    schema_active_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, Path, ServiceSpec, list[list[str]]]]:
    root = schema_active_root
    release = root / "releases/meyar-test+abcdef123456"
    (release / "backend/src/meyar/ops").chmod(0o755)
    (release / "backend/src/meyar/ops/deployment_ready.py").write_text("# synthetic gate\n")
    _seal_release(release)
    monkeypatch.setattr(
        gate, "__file__", str(release / "backend/src/meyar/ops/deployment_ready.py")
    )

    changed: list[tuple[Path, int]] = []
    for ancestor in root.parents:
        if ancestor == Path("/tmp"):
            break
        if ancestor.stat().st_uid == os.geteuid():
            changed.append((ancestor, ancestor.stat().st_mode & 0o7777))
            ancestor.chmod(0o755)
    for name, mode in (
        ("", 0o750),
        ("releases", 0o750),
        ("activations", 0o2750),
        ("activations/g-synthetic", 0o2750),
        ("shared", 0o2750),
        ("shared/config", 0o750),
        ("shared/storage", 0o2770),
        ("shared/logs", 0o2770),
    ):
        (root / name).chmod(mode)
    for name in ("meyar.stdout.log", "meyar.stderr.log"):
        path = root / "shared/logs" / name
        path.write_text("")
        path.chmod(0o660)
    lock = root / ".meyar-ops.lock"
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    directory = tmp_path / "LaunchDaemons"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    spec = ServiceSpec("com.bank.meyar", "_meyar", root, 8765)
    (directory / "com.bank.meyar.plist").write_bytes(render_service_plist(spec))
    (directory / "com.bank.meyar.plist").chmod(0o644)
    monkeypatch.setattr(gate, "_probe_application", lambda port: "APPLICATION_LIVE")

    async def database(url: str, head: str) -> tuple[str, str]:
        assert "synthetic:synthetic" in url
        assert head == "rev1"
        return "DATABASE_REACHABLE", "DB_REVISION_CURRENT"

    async def ollama(settings: object) -> tuple[str, str, str]:
        assert settings.ollama_base_url == "http://127.0.0.1:11434"  # type: ignore[attr-defined]
        return "OLLAMA_REACHABLE", "LLM_MODEL_AVAILABLE", "EMBEDDING_MODEL_AVAILABLE"

    monkeypatch.setattr(gate, "_probe_database", database)
    monkeypatch.setattr(gate, "_probe_ollama", ollama)
    commands: list[list[str]] = []
    try:
        yield root, directory, spec, commands
    finally:
        for path, mode in reversed(changed):
            path.chmod(mode)


def run(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]], **overrides: object
) -> gate.OpsResult:
    root, directory, spec, commands = deployment

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, "secret launchctl output", "")

    options = {
        "platform_system": "Darwin",
        "plist_directory": directory,
        "system_uid": os.geteuid(),
        "principal_resolver": lambda _name, gid: ServicePrincipal(
            os.geteuid() + 1, frozenset({gid})
        ),
        "runner": runner,
    }
    options.update(overrides)
    return gate.run_deployment_ready(root, spec.label, **options)  # type: ignore[arg-type]


def test_full_installed_deployment_ready_without_mutation(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]],
) -> None:
    root, directory, spec, commands = deployment
    watched = [
        root / "shared/config/.env",
        directory / f"{spec.label}.plist",
        root / ".meyar-ops.lock",
    ]
    before = [(path.read_bytes(), path.stat().st_mode) for path in watched]
    result = run(deployment)
    assert result.ok, codes(result)
    assert codes(result) == {
        "active_release": "ACTIVE_RELEASE_VERIFIED",
        "host_config": "HOST_CONFIG_VERIFIED",
        "service_plist": "SERVICE_PLIST_VERIFIED",
        "service_principal": "SERVICE_PRINCIPAL_VERIFIED",
        "runtime_permissions": "RUNTIME_PERMISSIONS_VERIFIED",
        "launchd": "SERVICE_VISIBLE",
        "application_liveness": "APPLICATION_LIVE",
        "database": "DATABASE_REACHABLE",
        "db_schema": "DB_REVISION_CURRENT",
        "ollama": "OLLAMA_REACHABLE",
        "llm_model": "LLM_MODEL_AVAILABLE",
        "embedding_model": "EMBEDDING_MODEL_AVAILABLE",
    }
    assert commands == [["/bin/launchctl", "print", f"system/{spec.label}"]] * 2
    assert before == [(path.read_bytes(), path.stat().st_mode) for path in watched]


@pytest.mark.parametrize("fault", ["missing", "symlink", "mode", "bytes", "extra_key"])
def test_installed_plist_fail_closed(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]], fault: str
) -> None:
    _, directory, spec, commands = deployment
    path = directory / f"{spec.label}.plist"
    if fault == "missing":
        path.unlink()
    elif fault == "symlink":
        path.rename(directory / "other.plist")
        path.symlink_to(directory / "other.plist")
    elif fault == "mode":
        path.chmod(0o666)
    elif fault == "bytes":
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        import plistlib

        payload = plistlib.loads(path.read_bytes())
        payload["EnvironmentVariables"] = {"SECRET": "should-not-leak"}
        path.write_bytes(plistlib.dumps(payload))
    result = run(deployment)
    assert not result.ok
    assert codes(result)["service_plist"] == "SERVICE_PLIST_INVALID"
    assert codes(result)["application_liveness"] == "PREREQUISITE_FAILED"
    assert commands == []
    assert "should-not-leak" not in result.model_dump_json()


def test_wrong_installed_owner_and_nonregular_plist_fail(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]]
) -> None:
    _, directory, spec, _ = deployment
    assert codes(run(deployment, system_uid=os.geteuid() + 1))["service_plist"] == (
        "SERVICE_PLIST_INVALID"
    )
    path = directory / f"{spec.label}.plist"
    path.unlink()
    path.mkdir()
    assert codes(run(deployment))["service_plist"] == "SERVICE_PLIST_INVALID"


def test_multiple_findings_preserved(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def models(_settings: object) -> tuple[str, str, str]:
        return "OLLAMA_REACHABLE", "LLM_MODEL_UNAVAILABLE", "EMBEDDING_MODEL_AVAILABLE"

    monkeypatch.setattr(gate, "_probe_ollama", models)
    result = run(deployment)
    assert not result.ok
    assert codes(result)["application_liveness"] == "APPLICATION_LIVE"
    assert codes(result)["db_schema"] == "DB_REVISION_CURRENT"
    assert codes(result)["llm_model"] == "LLM_MODEL_UNAVAILABLE"
    assert codes(result)["embedding_model"] == "EMBEDDING_MODEL_AVAILABLE"


def test_unsafe_config_skips_config_dependent_probes(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]],
) -> None:
    root, _, _, commands = deployment
    (root / "shared/config/.env").chmod(0o666)
    result = run(deployment)
    assert codes(result)["host_config"] == "HOST_CONFIG_INVALID"
    assert codes(result)["database"] == "PREREQUISITE_FAILED"
    assert codes(result)["ollama"] == "PREREQUISITE_FAILED"
    assert commands == []


def test_invalid_service_principal_and_runtime_are_independent(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]],
) -> None:
    invalid = run(
        deployment, principal_resolver=lambda _name, _gid: ServicePrincipal(0, frozenset())
    )
    assert codes(invalid)["service_principal"] == "SERVICE_PRINCIPAL_INVALID"
    assert codes(invalid)["runtime_permissions"] == "PREREQUISITE_FAILED"
    root, _, _, _ = deployment
    (root / "shared/storage").chmod(0o777)
    unsafe = run(deployment)
    assert codes(unsafe)["runtime_permissions"] == "RUNTIME_PERMISSIONS_UNSAFE"


def test_install_owner_service_user_and_missing_group_fail(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]]
) -> None:
    owner = os.geteuid()
    for principal in (
        ServicePrincipal(owner, frozenset({os.getegid()})),
        ServicePrincipal(owner + 1, frozenset()),
    ):
        result = run(
            deployment,
            principal_resolver=lambda _name, _gid, selected=principal: selected,
        )
        assert codes(result)["service_principal"] == "SERVICE_PRINCIPAL_INVALID"
        assert not result.ok


def test_dev_checkout_and_wrong_python_fail_identity(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gate, "__file__", __file__)
    assert codes(run(deployment))["active_release"] == "ACTIVE_RELEASE_INVALID"


def test_wrong_python_owner_and_tampered_release_fail_identity(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, _ = deployment
    with monkeypatch.context() as patch:
        patch.setattr(gate.os, "geteuid", lambda: os.getuid() + 100)
        assert codes(run(deployment))["active_release"] == "ACTIVE_RELEASE_INVALID"
    import sys

    with monkeypatch.context() as patch:
        patch.setattr(sys, "executable", "/unrelated/python")
        assert codes(run(deployment))["active_release"] == "ACTIVE_RELEASE_INVALID"
    path = root / "releases/meyar-test+abcdef123456/backend/alembic.ini"
    path.chmod(0o644)
    path.write_text(path.read_text() + "# tampered\n")
    assert codes(run(deployment))["active_release"] == "ACTIVE_RELEASE_INVALID"


def test_manifest_migration_identity_mismatch_is_distinct(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]]
) -> None:
    root, _, _, _ = deployment
    release = root / "releases/meyar-test+abcdef123456"
    manifest = release / "release_manifest.json"
    manifest.chmod(0o644)
    payload = json.loads(manifest.read_text())
    payload["alembic_heads"] = ["different"]
    manifest.write_text(json.dumps(payload))
    _seal_release(release)
    result = run(deployment)
    assert codes(result)["db_schema"] == "MIGRATION_IDENTITY_MISMATCH"
    assert not result.ok


@pytest.mark.parametrize(
    ("exit_code", "expected"),
    [(113, "SERVICE_NOT_VISIBLE"), (1, "SERVICE_PROBE_FAILED")],
)
def test_launchctl_failure_does_not_hide_other_findings(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]],
    exit_code: int,
    expected: str,
) -> None:
    commands: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        assert argv == ["/bin/launchctl", "print", "system/com.bank.meyar"]
        return subprocess.CompletedProcess(argv, exit_code, "secret launchctl output", "secret")

    result = run(deployment, runner=runner)
    assert codes(result)["launchd"] == expected
    assert codes(result)["application_liveness"] == "APPLICATION_LIVE"
    assert codes(result)["database"] == "DATABASE_REACHABLE"
    assert codes(result)["ollama"] == "OLLAMA_REACHABLE"
    assert not result.ok
    assert "secret launchctl" not in result.model_dump_json()
    assert commands == [["/bin/launchctl", "print", "system/com.bank.meyar"]]


def test_launchctl_timeout_fails_without_output_leak(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]]
) -> None:
    commands: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        raise subprocess.TimeoutExpired(argv, 1.0, output="secret launchctl output")

    result = run(deployment, runner=runner)
    assert codes(result)["launchd"] == "SERVICE_PROBE_FAILED"
    assert codes(result)["application_liveness"] == "APPLICATION_LIVE"
    assert codes(result)["database"] == "DATABASE_REACHABLE"
    assert codes(result)["ollama"] == "OLLAMA_REACHABLE"
    assert not result.ok
    assert "secret launchctl" not in result.model_dump_json()
    assert commands == [["/bin/launchctl", "print", "system/com.bank.meyar"]]


def test_launchctl_oserror_is_probe_failure_without_output_leak(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]]
) -> None:
    commands: list[list[str]] = []

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        raise OSError("secret launchctl unavailable")

    result = run(deployment, runner=runner)
    assert codes(result)["launchd"] == "SERVICE_PROBE_FAILED"
    assert codes(result)["application_liveness"] == "APPLICATION_LIVE"
    assert codes(result)["database"] == "DATABASE_REACHABLE"
    assert codes(result)["ollama"] == "OLLAMA_REACHABLE"
    assert not result.ok
    assert "secret" not in result.model_dump_json()
    assert commands == [["/bin/launchctl", "print", "system/com.bank.meyar"]]


@pytest.mark.parametrize(
    ("final_exit", "expected"),
    [(113, "SERVICE_NOT_VISIBLE"), (1, "SERVICE_PROBE_FAILED")],
)
def test_service_stopping_or_probe_failing_during_probes_cannot_report_ready(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]],
    final_exit: int,
    expected: str,
) -> None:
    calls = 0

    def runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(argv, 0 if calls == 1 else final_exit, "", "")

    result = run(deployment, runner=runner)
    assert codes(result)["launchd"] == "SERVICE_VISIBLE"
    assert codes(result)["snapshot"] == expected
    assert not result.ok


@pytest.mark.parametrize("second_head", ["rev1", "rev2"])
def test_valid_release_activation_during_probes_cannot_reuse_old_evidence(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]],
    monkeypatch: pytest.MonkeyPatch,
    second_head: str,
) -> None:
    root, directory, spec, commands = deployment
    first_id = "meyar-test+abcdef123456"
    second_id = "meyar-test+123456abcdef"
    second = root / "releases" / second_id
    (root / "shared/backups").chmod(0o750)
    shutil.copytree(root / "releases" / first_id, second)
    for path, _, _ in os.walk(second):
        Path(path).chmod(0o755)
    for relative, content in (
        (".venv/lib/python3.12/site-packages/meyar-source.pth", str(second / "backend/src") + "\n"),
        ("backend/alembic/versions/0001.py", f"revision = '{second_head}'\ndown_revision = None\n"),
        ("release_manifest.json", json.dumps({"alembic_heads": [second_head]})),
    ):
        path = second / relative
        path.chmod(0o644)
        path.write_text(content)
    state = second / "install_state.json"
    state.chmod(0o644)
    payload = json.loads(state.read_text())
    payload["release_id"] = second_id
    state.write_text(json.dumps(payload))
    _seal_release(second)

    source = root / "current/backend/src/meyar"
    monkeypatch.setattr(meyar, "__file__", str(source / "__init__.py"))
    monkeypatch.setattr(schema_init, "__file__", str(source / "ops/schema_init.py"))
    monkeypatch.setattr(gate, "__file__", str(source / "ops/deployment_ready.py"))
    monkeypatch.setattr(sys, "executable", str(root / "current/.venv/bin/python"))
    assert offline_host.verify_active_release(root) == first_id
    assert schema_init._active_config(root, implementation_file=Path(gate.__file__))[1] == "rev1"
    config_before = (root / "shared/config/.env").read_bytes()
    plist_before = (directory / f"{spec.label}.plist").read_bytes()

    async def database(_url: str, head: str) -> tuple[str, str]:
        assert head == "rev1"
        assert offline_host.activate_release(root, second_id) == "RELEASE_ACTIVATED"
        return "DATABASE_REACHABLE", "DB_REVISION_CURRENT"

    monkeypatch.setattr(gate, "_probe_database", database)
    result = run(deployment)
    assert offline_host.verify_active_release(root) == second_id
    assert (
        schema_init._active_config(root, implementation_file=Path(gate.__file__))[1]
        == second_head
    )
    assert (root / "shared/config/.env").read_bytes() == config_before
    assert (directory / f"{spec.label}.plist").read_bytes() == plist_before
    assert codes(result)["launchd"] == "SERVICE_VISIBLE"
    assert codes(result)["application_liveness"] == "APPLICATION_LIVE"
    assert codes(result)["database"] == "DATABASE_REACHABLE"
    assert codes(result)["db_schema"] == "DB_REVISION_CURRENT"
    assert codes(result)["ollama"] == "OLLAMA_REACHABLE"
    assert codes(result)["llm_model"] == "LLM_MODEL_AVAILABLE"
    assert codes(result)["embedding_model"] == "EMBEDDING_MODEL_AVAILABLE"
    assert commands == [["/bin/launchctl", "print", f"system/{spec.label}"]]
    assert codes(result)["snapshot"] == "DEPLOYMENT_CHANGED"
    assert not result.ok


def test_ambient_settings_do_not_override_protected_file(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEYAR_DATABASE_URL", "postgresql+asyncpg://evil:secret@evil.example/evil")
    monkeypatch.setenv("MEYAR_OLLAMA_BASE_URL", "http://evil.example:11434")
    monkeypatch.setenv("MEYAR_OLLAMA_MODEL", "evil-model")
    monkeypatch.setenv("MEYAR_OLLAMA_EMBEDDING_MODEL", "evil-embedding")
    result = run(deployment)
    assert result.ok
    assert "evil" not in result.model_dump_json()


@pytest.mark.parametrize(
    ("revisions", "expected"),
    [
        ([], "NO_DB_REVISION"),
        (["stale"], "SCHEMA_MISMATCH"),
        (["rev1", "other"], "MULTIPLE_DB_REVISIONS"),
        (["rev1"], "DB_REVISION_CURRENT"),
    ],
)
def test_database_select_one_and_read_only_schema(
    revisions: list[str], expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    queries: list[str] = []
    disposed: list[bool] = []

    class Connection:
        async def __aenter__(self) -> Connection:
            return self

        async def __aexit__(self, *_args: object) -> None:
            pass

        async def execute(self, statement: object) -> None:
            queries.append(str(statement))

    class Engine:
        def connect(self) -> Connection:
            return Connection()

        async def dispose(self) -> None:
            disposed.append(True)

    async def revision(_engine: object) -> DbAlembicRevisionResult:
        return DbAlembicRevisionResult(bool(revisions), revisions)

    monkeypatch.setattr(gate, "create_async_engine", lambda *_args, **_kwargs: Engine())
    monkeypatch.setattr(gate, "get_db_alembic_revision", revision)
    result = asyncio.run(
        gate._probe_database("postgresql+asyncpg://x:password@localhost/db", "rev1")
    )
    assert result == ("DATABASE_REACHABLE", expected)
    assert queries == ["SELECT 1"]
    assert disposed == [True]


def test_database_connection_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("postgresql+asyncpg://x:password@localhost/db")

    monkeypatch.setattr(gate, "create_async_engine", broken)
    result = asyncio.run(
        gate._probe_database("postgresql+asyncpg://x:password@localhost/db", "rev1")
    )
    assert result == ("DATABASE_UNREACHABLE", "PREREQUISITE_FAILED")
    assert "password" not in str(result)


def test_ollama_checks_use_explicit_settings_and_isolate_exception(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_host_settings(deployment[0])
    captured: list[object] = []

    class Provider:
        def __init__(self, answer: dict[str, object] | None) -> None:
            self.answer = answer

        async def health(self) -> dict[str, object]:
            if self.answer is None:
                raise RuntimeError("secret provider error")
            return self.answer

    def llm(value: object) -> Provider:
        captured.append(value)
        return Provider(None)

    def embedding(value: object) -> Provider:
        captured.append(value)
        return Provider({"reachable": True, "model_available": True})

    monkeypatch.setattr(gate, "llm_provider_from_settings", llm)
    monkeypatch.setattr(gate, "embedding_provider_from_settings", embedding)
    monkeypatch.setattr(gate, "_probe_ollama", _REAL_PROBE_OLLAMA)
    assert asyncio.run(gate._probe_ollama(settings)) == (
        "OLLAMA_REACHABLE",
        "LLM_MODEL_CHECK_FAILED",
        "EMBEDDING_MODEL_AVAILABLE",
    )
    assert captured == [settings, settings]


def test_traversal_label_rejected_before_plist_or_launchctl(
    deployment: tuple[Path, Path, ServiceSpec, list[list[str]]],
) -> None:
    root, directory, _, commands = deployment
    result = gate.run_deployment_ready(
        root, "../evil", platform_system="Darwin", plist_directory=directory
    )
    assert codes(result)["service_plist"] == "SERVICE_PLIST_INVALID"
    assert commands == []


def test_fixed_loopback_liveness_ignores_proxy_and_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets: list[tuple[str, int, float]] = []

    class FakeResponse:
        status = 302

    class FakeConnection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            targets.append((host, port, timeout))

        def request(self, method: str, path: str) -> None:
            assert (method, path) == ("GET", "/api/v1/health")

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            pass

    monkeypatch.setenv("HTTP_PROXY", "http://evil.example:8080")
    monkeypatch.setenv("ALL_PROXY", "http://evil.example:8080")
    monkeypatch.setattr(gate.http.client, "HTTPConnection", FakeConnection)
    assert gate._probe_application(8765) == "APPLICATION_HEALTH_INVALID"
    assert targets == [("127.0.0.1", 8765, 3.0)]


def test_health_response_exact_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status = 200

        def __init__(self, body: bytes) -> None:
            self.body = body

        def read(self, size: int) -> bytes:
            assert size == 1025
            return self.body[:size]

    class FakeConnection:
        response = FakeResponse(b'{"status":"ok"}')

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def request(self, *_args: object) -> None:
            pass

        def getresponse(self) -> FakeResponse:
            return self.response

        def close(self) -> None:
            pass

    monkeypatch.setattr(gate.http.client, "HTTPConnection", FakeConnection)
    assert gate._probe_application(8765) == "APPLICATION_LIVE"
    for body in (
        b"not json",
        b'{"status":"bad"}',
        b'{"status":"bad","status":"ok"}',
        b"x" * 1025,
    ):
        FakeConnection.response = FakeResponse(body)
        assert gate._probe_application(8765) == "APPLICATION_HEALTH_INVALID"


def test_health_connection_timeout_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    class TimeoutConnection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def request(self, *_args: object) -> None:
            raise TimeoutError("secret response")

        def close(self) -> None:
            pass

    monkeypatch.setattr(gate.http.client, "HTTPConnection", TimeoutConnection)
    assert gate._probe_application(8765) == "APPLICATION_UNREACHABLE"


def test_cli_has_only_root_and_label_arguments() -> None:
    from meyar.ops.cli import _build_parser

    args = _build_parser().parse_args(
        ["deployment-ready", "--install-root", "/tmp/meyar", "--label", "com.bank.meyar"]
    )
    assert vars(args) == {
        "command": "deployment-ready",
        "install_root": Path("/tmp/meyar"),
        "label": "com.bank.meyar",
    }

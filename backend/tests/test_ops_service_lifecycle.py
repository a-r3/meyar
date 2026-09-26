"""Linux simulation of privileged launchd lifecycle; never touches real launchd."""

from __future__ import annotations

import fcntl
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from meyar.ops import offline_host
from meyar.ops.host_config import verify_host_config
from meyar.ops.service_lifecycle import (
    LifecycleFailure,
    ServicePrincipal,
    _publish_plist,
    default_lifecycle_runner,
    resolve_service_principal,
    run_service_lifecycle,
)
from meyar.ops.service_plist import ServiceSpec, render_service_plist


class FakeLaunchctl:
    def __init__(self) -> None:
        self.loaded = False
        self.commands: list[list[str]] = []
        self.fail: str | None = None

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(argv)
        operation = argv[1]
        if operation == self.fail:
            return subprocess.CompletedProcess(argv, 1, "secret output", "secret error")
        if operation == "print":
            return subprocess.CompletedProcess(argv, 0 if self.loaded else 113, "", "")
        if operation == "bootstrap":
            self.loaded = True
        if operation == "bootout":
            self.loaded = False
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def lifecycle_context(
    ops_host_root: Path, tmp_path: Path
) -> Iterator[tuple[ServiceSpec, Path, FakeLaunchctl]]:
    # pytest's private temp parents are 0700; make the synthetic service's
    # ancestor traversal realistic, then restore the original modes.
    changed: list[tuple[Path, int]] = []
    for ancestor in ops_host_root.parents:
        if ancestor == Path("/tmp"):
            break
        mode = ancestor.stat().st_mode & 0o7777
        if ancestor.stat().st_uid == os.geteuid():
            changed.append((ancestor, mode))
            ancestor.chmod(0o755)
    lock = ops_host_root / ".meyar-ops.lock"
    lock.touch(mode=0o600)
    lock.chmod(0o600)
    for relative in (
        "releases",
        "activations",
        "activations/g-synthetic",
        "shared/storage",
        "shared/logs",
    ):
        (ops_host_root / relative).chmod(0o750)
    directory = tmp_path / "LaunchDaemons"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    spec = ServiceSpec("com.bank.meyar", "_meyar", ops_host_root, 8765)
    try:
        yield spec, directory, FakeLaunchctl()
    finally:
        for path, mode in reversed(changed):
            path.chmod(mode)


def run(
    action: str,
    context: tuple[ServiceSpec, Path, FakeLaunchctl],
    **overrides: object,
):
    spec, directory, runner = context
    options = {
        "platform_system": "Darwin",
        "effective_uid": 0,
        "principal_resolver": lambda _user, gid: ServicePrincipal(
            os.geteuid() + 1, frozenset({gid})
        ),
        "runner": runner,
        "plist_directory": directory,
        "system_uid": os.geteuid(),
        "system_gid": os.getegid(),
    }
    options.update(overrides)
    return run_service_lifecycle(action, spec, os.geteuid(), **options)


def code(result) -> str:  # noqa: ANN001, ANN201 - compact OpsResult test helper
    return result.findings[0].code


def test_install_protects_runtime_and_preserves_pr5_config(
    lifecycle_context: tuple[ServiceSpec, Path, FakeLaunchctl],
) -> None:
    spec, directory, runner = lifecycle_context
    config = spec.install_root / "shared/config/.env"
    before = config.read_bytes()
    assert code(run("service-install", lifecycle_context)) == "SERVICE_PLIST_INSTALLED"
    plist = directory / "com.bank.meyar.plist"
    assert plist.read_bytes() == render_service_plist(spec)
    assert plist.stat().st_uid == os.geteuid()
    assert plist.stat().st_mode & 0o777 == 0o644
    assert runner.commands == []
    assert config.read_bytes() == before
    assert config.stat().st_mode & 0o777 == 0o640
    assert verify_host_config(spec.install_root).ok
    for name in ("storage", "logs"):
        metadata = (spec.install_root / "shared" / name).stat()
        assert metadata.st_mode & 0o7777 == 0o2770
        assert metadata.st_gid == (spec.install_root / "shared/config").stat().st_gid
    for name in ("meyar.stdout.log", "meyar.stderr.log"):
        assert (spec.install_root / "shared/logs" / name).stat().st_mode & 0o777 == 0o660
    assert (spec.install_root / "shared/config").stat().st_mode & 0o777 == 0o750
    assert (spec.install_root / "releases/meyar-test+abcdef123456").stat().st_mode & 0o222 == 0


def test_platform_privilege_and_explicit_owner(
    lifecycle_context: tuple[ServiceSpec, Path, FakeLaunchctl],
) -> None:
    assert code(run("service-install", lifecycle_context, platform_system="Linux")) == (
        "PLATFORM_UNSUPPORTED"
    )
    assert code(run("service-install", lifecycle_context, effective_uid=501)) == (
        "PRIVILEGES_REQUIRED"
    )
    spec, directory, runner = lifecycle_context
    result = run_service_lifecycle(
        "service-install",
        spec,
        os.geteuid() + 10,
        platform_system="Darwin",
        effective_uid=0,
        plist_directory=directory,
        runner=runner,
    )
    assert code(result) == "INSTALL_OWNER_MISMATCH"
    assert not (directory / "com.bank.meyar.plist").exists()


@pytest.mark.parametrize(
    ("principal", "expected"),
    [
        (ServicePrincipal(0, frozenset()), "SERVICE_USER_INVALID"),
        (ServicePrincipal(os.geteuid() + 1, frozenset()), "SERVICE_GROUP_MEMBERSHIP_REQUIRED"),
    ],
)
def test_invalid_principal_rejected(
    lifecycle_context: tuple[ServiceSpec, Path, FakeLaunchctl],
    principal: ServicePrincipal,
    expected: str,
) -> None:
    assert code(
        run(
            "service-install",
            lifecycle_context,
            principal_resolver=lambda _user, _gid: principal,
        )
    ) == expected


def test_principal_lookup_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    import meyar.ops.service_lifecycle as lifecycle

    with pytest.raises(LifecycleFailure, match="SERVICE_USER_NOT_FOUND"):
        resolve_service_principal("__no_such_meyar_account__", os.getegid())
    user = type("User", (), {"pw_uid": 501, "pw_gid": 501})()
    monkeypatch.setattr(lifecycle.pwd, "getpwnam", lambda _name: user)
    with pytest.raises(LifecycleFailure, match="SERVICE_GROUP_INVALID"):
        resolve_service_principal("_meyar", 99999999)
    monkeypatch.setattr(lifecycle.grp, "getgrgid", lambda _gid: object())
    monkeypatch.setattr(lifecycle.os, "getgrouplist", lambda _name, _gid: [501])
    with pytest.raises(LifecycleFailure, match="SERVICE_GROUP_MEMBERSHIP_REQUIRED"):
        resolve_service_principal("_meyar", 502)


def test_install_is_idempotent_and_conflicts_fail_closed(
    lifecycle_context: tuple[ServiceSpec, Path, FakeLaunchctl],
) -> None:
    spec, directory, _ = lifecycle_context
    assert run("service-install", lifecycle_context).ok
    assert code(run("service-install", lifecycle_context)) == "SERVICE_PLIST_ALREADY_INSTALLED"
    path = directory / "com.bank.meyar.plist"
    path.write_bytes(b"different")
    assert code(run("service-install", lifecycle_context)) == "SERVICE_PLIST_CONFLICT"
    assert path.read_bytes() == b"different"
    path.unlink()
    path.symlink_to(spec.install_root / "shared/config/.env")
    assert code(run("service-install", lifecycle_context)) == "SERVICE_PLIST_CONFLICT"


def test_nonregular_and_unsafe_existing_plist_rejected(
    lifecycle_context: tuple[ServiceSpec, Path, FakeLaunchctl],
) -> None:
    _, directory, _ = lifecycle_context
    path = directory / "com.bank.meyar.plist"
    path.mkdir()
    assert code(run("service-install", lifecycle_context)) == "SERVICE_PLIST_CONFLICT"
    path.rmdir()
    assert run("service-install", lifecycle_context).ok
    path.chmod(0o666)
    assert code(run("service-start", lifecycle_context)) == "SERVICE_PLIST_CONFLICT"


def test_plist_publication_never_overwrites_a_racing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import meyar.ops.service_lifecycle as lifecycle

    destination = tmp_path / "com.bank.meyar.plist"

    def competing_link(_source: str, target: Path) -> None:
        target.write_bytes(b"unrelated")
        raise FileExistsError

    monkeypatch.setattr(lifecycle.os, "link", competing_link)
    with pytest.raises(LifecycleFailure, match="SERVICE_PLIST_CONFLICT"):
        _publish_plist(destination, b"canonical", os.geteuid(), os.getegid())
    assert destination.read_bytes() == b"unrelated"
    assert not list(tmp_path.glob(".meyar-plist-*"))


def test_plist_partial_publication_failure_leaves_no_final_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import meyar.ops.service_lifecycle as lifecycle

    destination = tmp_path / "com.bank.meyar.plist"

    def failed_link(_source: str, _target: Path) -> None:
        raise OSError("synthetic failure")

    monkeypatch.setattr(lifecycle.os, "link", failed_link)
    with pytest.raises(OSError):
        _publish_plist(destination, b"canonical", os.geteuid(), os.getegid())
    assert not destination.exists()
    assert not list(tmp_path.glob(".meyar-plist-*"))


def test_unsafe_runtime_modes_and_label_traversal_rejected(
    lifecycle_context: tuple[ServiceSpec, Path, FakeLaunchctl],
) -> None:
    spec, directory, runner = lifecycle_context
    bad = ServiceSpec("../other", spec.user_name, spec.install_root, spec.port)
    result = run_service_lifecycle(
        "service-install", bad, os.geteuid(), platform_system="Darwin", effective_uid=0
    )
    assert code(result) == "SERVICE_SPEC_INVALID"
    assert not list(directory.glob("*.plist"))
    release_dir = spec.install_root / "releases"
    release_dir.chmod(0o770)
    assert code(run("service-install", lifecycle_context)) == "RUNTIME_PERMISSIONS_UNSAFE"
    release_dir.chmod(0o750)
    assert run("service-install", lifecycle_context).ok
    storage = spec.install_root / "shared/storage"
    storage.chmod(0o2772)
    assert code(run("service-start", lifecycle_context)) == "RUNTIME_PERMISSIONS_UNSAFE"
    assert runner.commands == []


def test_symlinked_log_and_group_writable_config_rejected(
    lifecycle_context: tuple[ServiceSpec, Path, FakeLaunchctl],
) -> None:
    spec, _, _ = lifecycle_context
    log = spec.install_root / "shared/logs/meyar.stdout.log"
    log.symlink_to(spec.install_root / "shared/config/.env")
    assert code(run("service-install", lifecycle_context)) == "HOST_BINDING_INVALID"
    log.unlink()
    config = spec.install_root / "shared/config/.env"
    config.chmod(0o660)
    assert code(run("service-install", lifecycle_context)) == "HOST_BINDING_INVALID"
    assert not verify_host_config(spec.install_root).ok


def test_probe_failure_does_not_claim_already_stopped(
    lifecycle_context: tuple[ServiceSpec, Path, FakeLaunchctl],
) -> None:
    _, _, runner = lifecycle_context
    assert run("service-install", lifecycle_context).ok
    runner.fail = "print"
    assert code(run("service-stop", lifecycle_context)) == "SERVICE_PROBE_FAILED"


def test_final_visibility_is_required_for_start_and_stop(
    lifecycle_context: tuple[ServiceSpec, Path, FakeLaunchctl],
) -> None:
    assert run("service-install", lifecycle_context).ok

    class AbsentAfterStart(FakeLaunchctl):
        def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
            self.commands.append(argv)
            return subprocess.CompletedProcess(argv, 113 if argv[1] == "print" else 0)

    assert code(run("service-start", lifecycle_context, runner=AbsentAfterStart())) == (
        "SERVICE_NOT_VISIBLE"
    )

    class VisibleAfterStop(FakeLaunchctl):
        def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
            self.commands.append(argv)
            return subprocess.CompletedProcess(argv, 0)

    assert code(run("service-stop", lifecycle_context, runner=VisibleAfterStop())) == (
        "SERVICE_BOOTOUT_FAILED"
    )


def test_service_must_traverse_immutable_release_interior(
    lifecycle_context: tuple[ServiceSpec, Path, FakeLaunchctl],
) -> None:
    spec, _, _ = lifecycle_context
    binary_dir = spec.install_root / "releases/meyar-test+abcdef123456/.venv/bin"
    binary_dir.chmod(0o500)
    assert code(run("service-install", lifecycle_context)) == "RUNTIME_PERMISSIONS_UNSAFE"


def test_service_must_read_all_immutable_runtime_files(
    lifecycle_context: tuple[ServiceSpec, Path, FakeLaunchctl],
) -> None:
    spec, _, _ = lifecycle_context
    pointer = (
        spec.install_root
        / "releases/meyar-test+abcdef123456/.venv/lib/python3.12/site-packages/meyar-source.pth"
    )
    pointer.chmod(0o400)
    assert code(run("service-install", lifecycle_context)) == "RUNTIME_PERMISSIONS_UNSAFE"


def test_lifecycle_exact_argv_and_idempotency(
    lifecycle_context: tuple[ServiceSpec, Path, FakeLaunchctl],
) -> None:
    spec, directory, runner = lifecycle_context
    assert run("service-install", lifecycle_context).ok
    target = "system/com.bank.meyar"
    plist = str(directory / "com.bank.meyar.plist")
    assert code(run("service-start", lifecycle_context)) == "SERVICE_STARTED"
    assert runner.commands == [
        ["/bin/launchctl", "print", target],
        ["/bin/launchctl", "bootstrap", "system", plist],
        ["/bin/launchctl", "kickstart", target],
        ["/bin/launchctl", "print", target],
    ]
    runner.commands.clear()
    assert code(run("service-start", lifecycle_context)) == "SERVICE_ALREADY_LOADED_AND_STARTED"
    assert runner.commands == [
        ["/bin/launchctl", "print", target],
        ["/bin/launchctl", "kickstart", target],
        ["/bin/launchctl", "print", target],
    ]
    runner.commands.clear()
    assert code(run("service-restart", lifecycle_context)) == "SERVICE_RESTARTED"
    assert runner.commands == [
        ["/bin/launchctl", "print", target],
        ["/bin/launchctl", "kickstart", "-k", target],
        ["/bin/launchctl", "print", target],
    ]
    runner.commands.clear()
    assert code(run("service-stop", lifecycle_context)) == "SERVICE_STOPPED"
    assert runner.commands == [
        ["/bin/launchctl", "print", target],
        ["/bin/launchctl", "bootout", target],
        ["/bin/launchctl", "print", target],
    ]
    assert (directory / "com.bank.meyar.plist").exists()
    assert code(run("service-stop", lifecycle_context)) == "SERVICE_ALREADY_STOPPED"
    runner.commands.clear()
    assert code(run("service-restart", lifecycle_context)) == "SERVICE_RESTARTED"
    assert runner.commands == [
        ["/bin/launchctl", "print", target],
        ["/bin/launchctl", "bootstrap", "system", plist],
        ["/bin/launchctl", "kickstart", target],
        ["/bin/launchctl", "print", target],
    ]


@pytest.mark.parametrize("operation", ["bootstrap", "kickstart", "bootout"])
def test_launchctl_failure_is_never_success(
    lifecycle_context: tuple[ServiceSpec, Path, FakeLaunchctl], operation: str
) -> None:
    _, _, runner = lifecycle_context
    assert run("service-install", lifecycle_context).ok
    if operation == "bootout":
        runner.loaded = True
    runner.fail = operation
    action = "service-stop" if operation == "bootout" else "service-start"
    result = run(action, lifecycle_context)
    assert not result.ok
    assert operation.upper() in code(result)
    assert "secret" not in result.model_dump_json()


def test_lock_coordinates_with_pr4_without_changing_inode(
    lifecycle_context: tuple[ServiceSpec, Path, FakeLaunchctl],
) -> None:
    spec, _, _ = lifecycle_context
    lock = spec.install_root / ".meyar-ops.lock"
    original = lock.stat()
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert code(run("service-install", lifecycle_context)) == "OPERATION_BUSY"
    finally:
        os.close(fd)
    after = lock.stat()
    assert (after.st_ino, after.st_uid, after.st_mode & 0o777) == (
        original.st_ino, original.st_uid, original.st_mode & 0o777
    )
    with offline_host._operation_lock(spec.install_root):
        assert code(run("service-install", lifecycle_context)) == "OPERATION_BUSY"


def test_default_runner_discards_output_and_uses_fixed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    import meyar.ops.service_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)
    default_lifecycle_runner(["/bin/launchctl", "print", "system/com.bank.meyar"])
    assert observed["stdout"] == subprocess.DEVNULL
    assert observed["stderr"] == subprocess.DEVNULL
    assert observed["timeout"] == 10.0
    assert observed["env"] == {"PATH": "/usr/bin:/bin", "HOME": "/var/empty"}
    assert observed.get("shell") is None


def test_lifecycle_never_uses_shell_or_account_creation_tools() -> None:
    import ast

    source = (
        Path(__file__).resolve().parent.parent / "src/meyar/ops/service_lifecycle.py"
    ).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "shell":
            assert not (isinstance(node.value, ast.Constant) and node.value.value is True)
    assert not any(name in source for name in ("sudo", "dscl", "sysadminctl"))

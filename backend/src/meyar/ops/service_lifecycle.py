"""Privileged, fail-closed system LaunchDaemon lifecycle for an installed MEYAR release."""

from __future__ import annotations

import grp
import os
import platform
import pwd
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from meyar.ops.host_config import load_host_settings
from meyar.ops.offline_host import (
    InstallFailure,
    _real_directory,
    privileged_operation_lock,
    verify_active_release,
)
from meyar.ops.result import FindingStatus, OpsResult, OpsResultBuilder
from meyar.ops.service_plist import ServiceSpec, render_service_plist
from meyar.ops.service_status import LAUNCHCTL_PATH, LaunchctlRunner

SYSTEM_PLIST_DIRECTORY = Path("/Library/LaunchDaemons")
LOG_NAMES = ("meyar.stdout.log", "meyar.stderr.log")
# launchctl's "Could not find specified service" status on supported macOS.
SERVICE_NOT_FOUND_EXIT = 113


class LifecycleFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ServicePrincipal:
    uid: int
    groups: frozenset[int]


PrincipalResolver = Callable[[str, int], ServicePrincipal]


def resolve_service_principal(user_name: str, service_gid: int) -> ServicePrincipal:
    try:
        account = pwd.getpwnam(user_name)
    except KeyError:
        raise LifecycleFailure("SERVICE_USER_NOT_FOUND") from None
    if account.pw_uid == 0:
        raise LifecycleFailure("SERVICE_USER_INVALID")
    try:
        grp.getgrgid(service_gid)
    except KeyError:
        raise LifecycleFailure("SERVICE_GROUP_INVALID") from None
    try:
        groups = frozenset(os.getgrouplist(user_name, account.pw_gid))
    except OSError:
        raise LifecycleFailure("SERVICE_GROUP_INVALID") from None
    if service_gid not in groups:
        raise LifecycleFailure("SERVICE_GROUP_MEMBERSHIP_REQUIRED")
    return ServicePrincipal(account.pw_uid, groups)


def _check_preparation_target(
    path: Path, owner_uid: int, service_gid: int, *, mutable: bool = False
) -> None:
    _real_directory(path)
    metadata = path.lstat()
    if metadata.st_uid != owner_uid or metadata.st_mode & 0o002:
        raise LifecycleFailure("RUNTIME_PERMISSIONS_UNSAFE")
    if metadata.st_mode & 0o020 and (
        not mutable or metadata.st_gid != service_gid or metadata.st_mode & 0o2000 == 0
    ):
        raise LifecycleFailure("RUNTIME_PERMISSIONS_UNSAFE")


def _set_metadata(path: Path, owner_uid: int, service_gid: int, mode: int) -> None:
    metadata = path.lstat()
    if metadata.st_gid != service_gid:
        os.chown(path, owner_uid, service_gid, follow_symlinks=False)
    os.chmod(path, mode, follow_symlinks=False)


def _active_generation(root: Path) -> Path:
    pointer = root / "current"
    parts = Path(os.readlink(pointer)).parts
    if len(parts) != 3 or parts[0] != "activations" or parts[2] != "current":
        raise LifecycleFailure("HOST_BINDING_INVALID")
    return root / "activations" / parts[1]


def _prepare_runtime(root: Path, owner_uid: int, service_gid: int) -> None:
    generation = _active_generation(root)
    modes = (
        (root, 0o750),
        (root / "releases", 0o750),
        (root / "activations", 0o2750),
        (generation, 0o2750),
        (root / "shared", 0o2750),
        (root / "shared/config", 0o750),
        (root / "shared/storage", 0o2770),
        (root / "shared/logs", 0o2770),
    )
    for path, _mode in modes:
        _check_preparation_target(
            path,
            owner_uid,
            service_gid,
            mutable=path in (root / "shared/storage", root / "shared/logs"),
        )
    for name in LOG_NAMES:
        path = root / "shared/logs" / name
        if os.path.lexists(path):
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != owner_uid
                or metadata.st_mode & 0o007
                or (metadata.st_mode & 0o020 and metadata.st_gid != service_gid)
            ):
                raise LifecycleFailure("RUNTIME_PERMISSIONS_UNSAFE")
    for path, mode in modes:
        _set_metadata(path, owner_uid, service_gid, mode)
    logs = root / "shared/logs"
    for name in LOG_NAMES:
        path = logs / name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise LifecycleFailure("RUNTIME_PERMISSIONS_UNSAFE")
        _set_metadata(path, owner_uid, service_gid, 0o660)


def _service_access(path: Path, principal: ServicePrincipal, bit: int) -> bool:
    metadata = path.stat()
    if metadata.st_uid == principal.uid:
        return bool(metadata.st_mode & (bit << 6))
    if metadata.st_gid in principal.groups:
        return bool(metadata.st_mode & (bit << 3))
    return bool(metadata.st_mode & bit)


def _verify_release_access(root: Path, principal: ServicePrincipal) -> None:
    release = root / "releases" / verify_active_release(root)
    for directory, dirnames, filenames in os.walk(release):
        parent = Path(directory)
        _real_directory(parent)
        if not _service_access(parent, principal, 0o1) or not _service_access(
            parent, principal, 0o4
        ):
            raise LifecycleFailure("RUNTIME_PERMISSIONS_UNSAFE")
        for name in dirnames:
            _real_directory(parent / name)
        for name in filenames:
            path = parent / name
            if not stat.S_ISREG(path.lstat().st_mode) or not _service_access(
                path, principal, 0o4
            ):
                raise LifecycleFailure("RUNTIME_PERMISSIONS_UNSAFE")
    if not _service_access(release / ".venv/bin/python", principal, 0o1):
        raise LifecycleFailure("RUNTIME_PERMISSIONS_UNSAFE")


def _verify_runtime(
    root: Path, owner_uid: int, service_gid: int, principal: ServicePrincipal
) -> None:
    generation = _active_generation(root)
    required = (
        (root, 0o750),
        (root / "releases", 0o750),
        (root / "activations", 0o2750),
        (generation, 0o2750),
        (root / "shared", 0o2750),
        (root / "shared/config", 0o750),
        (root / "shared/storage", 0o2770),
        (root / "shared/logs", 0o2770),
    )
    for parent in root.parents:
        if not _service_access(parent, principal, 0o1):
            raise LifecycleFailure("RUNTIME_PERMISSIONS_UNSAFE")
    for path, mode in required:
        _real_directory(path)
        metadata = path.stat()
        if (
            metadata.st_uid != owner_uid
            or metadata.st_gid != service_gid
            or metadata.st_mode & 0o7777 != mode
            or not _service_access(path, principal, 0o1)
        ):
            raise LifecycleFailure("RUNTIME_PERMISSIONS_UNSAFE")
    env = root / "shared/config/.env"
    if not _service_access(env, principal, 0o4):
        raise LifecycleFailure("RUNTIME_PERMISSIONS_UNSAFE")
    for name in LOG_NAMES:
        path = root / "shared/logs" / name
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != owner_uid
            or metadata.st_gid != service_gid
            or metadata.st_mode & 0o777 != 0o660
            or not _service_access(path, principal, 0o2)
        ):
            raise LifecycleFailure("RUNTIME_PERMISSIONS_UNSAFE")
    _verify_release_access(root, principal)


def _system_directory(directory: Path, system_uid: int) -> None:
    _real_directory(directory)
    metadata = directory.stat()
    if metadata.st_uid != system_uid or metadata.st_mode & 0o022:
        raise LifecycleFailure("SERVICE_PLIST_DESTINATION_UNSAFE")
    if directory == SYSTEM_PLIST_DIRECTORY:
        for parent in (directory.parent, Path("/")):
            parent_stat = parent.stat()
            if parent_stat.st_uid != 0 or parent_stat.st_mode & 0o022:
                raise LifecycleFailure("SERVICE_PLIST_DESTINATION_UNSAFE")


def _installed_bytes(path: Path, system_uid: int) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise LifecycleFailure("SERVICE_PLIST_CONFLICT") from None
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != system_uid
            or metadata.st_mode & 0o022
            or metadata.st_size > 1024 * 1024
        ):
            raise LifecycleFailure("SERVICE_PLIST_CONFLICT")
        return os.read(fd, 1024 * 1024 + 1)
    finally:
        os.close(fd)


def _publish_plist(path: Path, data: bytes, system_uid: int, system_gid: int) -> str:
    existing = _installed_bytes(path, system_uid)
    if existing is not None:
        if existing != data:
            raise LifecycleFailure("SERVICE_PLIST_CONFLICT")
        return "SERVICE_PLIST_ALREADY_INSTALLED"
    fd, temporary = tempfile.mkstemp(prefix=".meyar-plist-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
        metadata = os.fstat(fd)
        if metadata.st_uid != system_uid or metadata.st_gid != system_gid:
            os.fchown(fd, system_uid, system_gid)
        os.fchmod(fd, 0o644)
        os.fsync(fd)
        try:
            os.link(temporary, path)
        except FileExistsError:
            existing = _installed_bytes(path, system_uid)
            if existing == data:
                return "SERVICE_PLIST_ALREADY_INSTALLED"
            raise LifecycleFailure("SERVICE_PLIST_CONFLICT") from None
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return "SERVICE_PLIST_INSTALLED"
    finally:
        os.close(fd)
        os.unlink(temporary)


def default_lifecycle_runner(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - absolute executable and fixed argv
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10.0,
        env={"PATH": "/usr/bin:/bin", "HOME": "/var/empty"},
        check=False,
    )


def _launchctl(runner: LaunchctlRunner, argv: list[str], failure: str) -> int:
    try:
        return runner(argv).returncode
    except (OSError, subprocess.TimeoutExpired):
        raise LifecycleFailure(failure) from None


def _mutate_launchctl(action: str, label: str, path: Path, runner: LaunchctlRunner) -> str:
    target = f"system/{label}"

    def call(args: list[str], failure: str) -> int:
        return _launchctl(runner, [LAUNCHCTL_PATH, *args], failure)

    initial = call(["print", target], "SERVICE_PROBE_FAILED")
    if initial not in (0, SERVICE_NOT_FOUND_EXIT):
        raise LifecycleFailure("SERVICE_PROBE_FAILED")
    loaded = initial == 0
    if action == "service-stop":
        if not loaded:
            return "SERVICE_ALREADY_STOPPED"
        outcome = call(["bootout", target], "SERVICE_BOOTOUT_FAILED")
        final = call(["print", target], "SERVICE_PROBE_FAILED") if outcome == 0 else None
        if outcome != 0 or final != SERVICE_NOT_FOUND_EXIT:
            raise LifecycleFailure("SERVICE_BOOTOUT_FAILED")
        return "SERVICE_STOPPED"
    if not loaded:
        if call(["bootstrap", "system", str(path)], "SERVICE_BOOTSTRAP_FAILED") != 0:
            raise LifecycleFailure("SERVICE_BOOTSTRAP_FAILED")
    kickstart = ["kickstart", "-k", target] if action == "service-restart" and loaded else [
        "kickstart", target
    ]
    if call(kickstart, "SERVICE_KICKSTART_FAILED") != 0:
        raise LifecycleFailure("SERVICE_KICKSTART_FAILED")
    if call(["print", target], "SERVICE_PROBE_FAILED") != 0:
        raise LifecycleFailure("SERVICE_NOT_VISIBLE")
    if action == "service-restart":
        return "SERVICE_RESTARTED"
    return "SERVICE_ALREADY_LOADED_AND_STARTED" if loaded else "SERVICE_STARTED"


def run_service_lifecycle(
    action: str,
    spec: ServiceSpec,
    install_owner_uid: int,
    *,
    platform_system: str | None = None,
    effective_uid: int | None = None,
    principal_resolver: PrincipalResolver = resolve_service_principal,
    runner: LaunchctlRunner = default_lifecycle_runner,
    plist_directory: Path = SYSTEM_PLIST_DIRECTORY,
    system_uid: int = 0,
    system_gid: int = 0,
) -> OpsResult:
    builder = OpsResultBuilder(action=action)
    try:
        if action not in {"service-install", "service-start", "service-stop", "service-restart"}:
            raise LifecycleFailure("INVALID_OPERATION")
        if (platform_system or platform.system()) != "Darwin":
            raise LifecycleFailure("PLATFORM_UNSUPPORTED")
        if (os.geteuid() if effective_uid is None else effective_uid) != 0:
            raise LifecycleFailure("PRIVILEGES_REQUIRED")
        if install_owner_uid <= 0:
            raise LifecycleFailure("INSTALL_OWNER_INVALID")
        try:
            spec.validate()
        except ValueError:
            raise LifecycleFailure("SERVICE_SPEC_INVALID") from None
        root = spec.install_root
        with privileged_operation_lock(root, install_owner_uid):
            load_host_settings(root, expected_owner_uid=install_owner_uid)
            verify_active_release(root)
            service_gid = (root / "shared/config").stat().st_gid
            principal = principal_resolver(spec.user_name, service_gid)
            if principal.uid == 0 or principal.uid == install_owner_uid:
                raise LifecycleFailure("SERVICE_USER_INVALID")
            if service_gid not in principal.groups:
                raise LifecycleFailure("SERVICE_GROUP_MEMBERSHIP_REQUIRED")
            expected = render_service_plist(spec, expected_owner_uid=install_owner_uid)
            _system_directory(plist_directory, system_uid)
            path = plist_directory / f"{spec.label}.plist"
            if action == "service-install":
                existing = _installed_bytes(path, system_uid)
                if existing is not None and existing != expected:
                    raise LifecycleFailure("SERVICE_PLIST_CONFLICT")
                _verify_release_access(root, principal)
                _prepare_runtime(root, install_owner_uid, service_gid)
                _verify_runtime(root, install_owner_uid, service_gid, principal)
                code = _publish_plist(path, expected, system_uid, system_gid)
            else:
                _verify_runtime(root, install_owner_uid, service_gid, principal)
                existing = _installed_bytes(path, system_uid)
                if existing is None:
                    raise LifecycleFailure("SERVICE_NOT_INSTALLED")
                if existing != expected:
                    raise LifecycleFailure("SERVICE_PLIST_CONFLICT")
                code = _mutate_launchctl(action, spec.label, path, runner)
    except LifecycleFailure as exc:
        code = exc.code
    except InstallFailure as exc:
        code = exc.code
    except (ValueError, OSError, KeyError):
        code = "HOST_BINDING_INVALID"
    success = code in {
        "SERVICE_PLIST_INSTALLED",
        "SERVICE_PLIST_ALREADY_INSTALLED",
        "SERVICE_STARTED",
        "SERVICE_ALREADY_LOADED_AND_STARTED",
        "SERVICE_STOPPED",
        "SERVICE_ALREADY_STOPPED",
        "SERVICE_RESTARTED",
    }
    message = "service lifecycle succeeded" if success else "service lifecycle failed"
    builder.add(
        component="service_lifecycle",
        status=FindingStatus.OK if success else FindingStatus.FAIL,
        code=code,
        message=message,
    )
    return builder.build()

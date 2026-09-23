"""macOS LaunchDaemon plist foundation for the MEYAR application process
(issue #35 PR2). This module only renders and verifies a plist — it never
installs to `/Library/LaunchDaemons`, never invokes `launchctl`, and never
requires or performs privilege escalation. See `docs/MEYAR_OPS.md` for the
command contract and `docs/DECISIONS.md` for the accepted service model
(system LaunchDaemon -> dedicated non-root UserName -> MEYAR application
process -> loopback-bound Uvicorn).

The generated plist carries a narrow, deterministic key set
(`REQUIRED_PLIST_KEYS`) — no `EnvironmentVariables` (secrets stay outside
the plist in host-local configuration), no explicit `RunAtLoad` (`KeepAlive`
already supplies the relevant launch semantics), no `ThrottleInterval`
(would only restate launchd's own default). `ProgramArguments` is always a
real argv array invoking the operator-supplied absolute executable via
`-m uvicorn` against the currently accepted application contract
(`meyar.main:app`, host `127.0.0.1`) — never a `Program` shell-string
invocation, and `Executable` never relies on bare `uv`/PATH resolution.

Path fields (`WorkingDirectory`, `Executable`, `StandardOutPath`,
`StandardErrorPath`) are validated lexically only: required absolute,
free of NUL/control characters, free of a lexical `..` traversal
component. Symlink components are deliberately NOT resolved/rejected —
there is no accepted immutable release-path contract yet (see
docs/DECISIONS.md), and inventing containment rules ahead of that
contract is out of scope for this PR."""

from __future__ import annotations

import contextlib
import os
import plistlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.parsers.expat import ExpatError

from meyar.ops.redact import safe_exception_text
from meyar.ops.result import FindingStatus, OpsResult, OpsResultBuilder

# The currently accepted application contract (backend/.env loading and
# `scripts/fresh_deployment_smoke.py` both assume this invocation shape).
# Changing either constant is a product/architecture decision, not a
# meyar-ops implementation detail.
APPLICATION_MODULE = "meyar.main:app"
REQUIRED_HOST = "127.0.0.1"

MIN_PORT = 1
MAX_PORT = 65535

# The exact, narrow key set this PR emits and accepts — see module
# docstring. `service-verify` treats any other top-level key as an
# unsupported/dangerous key, never silently accepted.
REQUIRED_PLIST_KEYS: frozenset[str] = frozenset(
    {
        "Label",
        "UserName",
        "WorkingDirectory",
        "ProgramArguments",
        "StandardOutPath",
        "StandardErrorPath",
        "KeepAlive",
    }
)

_CONTROL_OR_WHITESPACE = re.compile(r"[\x00-\x20\x7f]")
_NUL_OR_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SHELL_EXECUTABLE_BASENAMES = frozenset({"sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh"})

# A MEYAR service plist is a few hundred bytes of flat XML; 1 MiB is
# generous headroom while still bounding the read against an oversized or
# hostile file — see verify_release.py for the same bounded-read pattern
# this mirrors.
_MAX_PLIST_BYTES = 1024 * 1024


def validate_label(label: str) -> None:
    """A safe launchd identifier: non-empty, no path separator, no
    whitespace/control character (including the space and DEL bytes)."""
    if not label:
        raise ValueError("Label must not be empty")
    if "/" in label:
        raise ValueError("Label must not contain '/'")
    if _CONTROL_OR_WHITESPACE.search(label):
        raise ValueError("Label must not contain whitespace or control characters")


def validate_user_name(user_name: str) -> None:
    """meyar-ops only references this account name; it never creates it
    (no service-account creation in this PR)."""
    if not user_name:
        raise ValueError("UserName must not be empty")
    if _CONTROL_OR_WHITESPACE.search(user_name):
        raise ValueError("UserName must not contain whitespace or control characters")
    if user_name == "root":
        raise ValueError("UserName must not be root")


def validate_absolute_path(value: str, *, field_name: str) -> None:
    """Lexical-only absolute-path validation shared by every path field.
    Deliberately does not call `Path.resolve()`/`os.path.realpath()` — no
    blanket rejection of symlink components, since no final immutable
    release-path contract exists yet."""
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    if not value.startswith("/"):
        raise ValueError(f"{field_name} must be an absolute path")
    if _NUL_OR_CONTROL.search(value):
        raise ValueError(f"{field_name} must not contain NUL or control characters")
    if ".." in PurePosixPath(value).parts:
        raise ValueError(f"{field_name} must not contain a lexical '..' path traversal component")


def validate_port(port: int) -> None:
    if not (MIN_PORT <= port <= MAX_PORT):
        raise ValueError(f"port must be between {MIN_PORT} and {MAX_PORT} inclusive")


@dataclass(frozen=True)
class ServiceSpec:
    """Typed, fully operator-supplied input for `service-render`. No field
    has a bank-specific or otherwise hardcoded default."""

    label: str
    user_name: str
    working_directory: str
    executable: str
    port: int
    stdout_path: str
    stderr_path: str

    def validate(self) -> None:
        validate_label(self.label)
        validate_user_name(self.user_name)
        validate_absolute_path(self.working_directory, field_name="WorkingDirectory")
        validate_absolute_path(self.executable, field_name="Executable")
        validate_port(self.port)
        validate_absolute_path(self.stdout_path, field_name="StandardOutPath")
        validate_absolute_path(self.stderr_path, field_name="StandardErrorPath")


def _program_arguments(spec: ServiceSpec) -> list[str]:
    """A real argv array — never a shell command string. `spec.executable`
    is the absolute interpreter path; the module is always invoked via
    `-m uvicorn`, never a bare `uvicorn`/`uv` relying on PATH resolution."""
    return [
        spec.executable,
        "-m",
        "uvicorn",
        APPLICATION_MODULE,
        "--host",
        REQUIRED_HOST,
        "--port",
        str(spec.port),
    ]


def render_service_plist(spec: ServiceSpec) -> bytes:
    """Deterministic plist XML bytes for a validated `ServiceSpec`. Raises
    `ValueError` (never writes anything) when any field fails validation."""
    spec.validate()
    payload: dict[str, object] = {
        "Label": spec.label,
        "UserName": spec.user_name,
        "WorkingDirectory": spec.working_directory,
        "ProgramArguments": _program_arguments(spec),
        "StandardOutPath": spec.stdout_path,
        "StandardErrorPath": spec.stderr_path,
        "KeepAlive": {"SuccessfulExit": False},
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


class ServiceRenderOutputExistsError(Exception):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"output path already exists: {path}")


# `service-render` is render-only — never installation (see module
# docstring). This is the one output-path boundary this PR enforces: the
# privileged LaunchDaemon installation directory itself. It is not a
# general symlink/containment policy for plist *field* values (those stay
# lexical-only, see `validate_absolute_path`).
_PRIVILEGED_LAUNCHDAEMONS_DIR = "/Library/LaunchDaemons"


class ServiceRenderOutputPathPrivilegedError(Exception):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(
            f"output path must not be inside {_PRIVILEGED_LAUNCHDAEMONS_DIR}: {path}"
        )


def _is_privileged_location(candidate: str, *, privileged_dir: str) -> bool:
    return candidate == privileged_dir or candidate.startswith(privileged_dir + os.sep)


def _reject_privileged_output_path(output_path: Path) -> None:
    """Cheap, filesystem-independent pre-filter: rejects an output path
    whose lexically normalized absolute destination is the privileged
    LaunchDaemon installation directory or a descendant of it. Runs
    before any filesystem access, so it still catches the direct/`..`-
    traversal case even on a host where that directory does not exist
    (e.g. Linux CI). This is defense-in-depth only — the actual boundary
    against a *symlinked* parent directory is the resolved-parent check
    in `_open_anchored_output_parent_dir` below; see its docstring for
    why a lexical-only check is not sufficient on its own."""
    normalized = os.path.abspath(str(output_path))
    if _is_privileged_location(normalized, privileged_dir=_PRIVILEGED_LAUNCHDAEMONS_DIR):
        raise ServiceRenderOutputPathPrivilegedError(output_path)


def _open_anchored_output_parent_dir(
    output_path: Path, *, privileged_dir: str | None = None
) -> int:
    """The real output-destination boundary. Resolves `output_path`'s
    parent directory — which must already exist; this never creates it —
    to its canonical filesystem location, following every symlink
    component (`os.path.realpath(..., strict=True)`), and rejects it if
    that canonical location is the privileged LaunchDaemon installation
    directory or a descendant of it. This is what catches an output path
    like `/tmp/staging-link/com.meyar.plist` where `staging-link` is a
    symlink to `/Library/LaunchDaemons`: a purely lexical check on the
    original path string would miss it, since the symlink component
    never lexically spells out the privileged directory.

    Returns an open dir-fd anchored to the *resolved* parent. The caller
    creates the output file's basename relative to this fd
    (`os.open(..., dir_fd=...)`) rather than by re-walking the original
    path string, so a symlink swapped into the original parent path
    after this check returns cannot redirect where the file actually
    gets created.

    `privileged_dir` defaults to the real production constant (read at
    call time, not bind time, so tests can monkeypatch the module
    constant); tests may also pass an explicit value under a tmp
    directory to exercise the symlink-resolution logic without needing
    write access to the real `/Library/LaunchDaemons`."""
    target = _PRIVILEGED_LAUNCHDAEMONS_DIR if privileged_dir is None else privileged_dir
    resolved_parent = os.path.realpath(str(output_path.parent), strict=True)
    if _is_privileged_location(resolved_parent, privileged_dir=target):
        raise ServiceRenderOutputPathPrivilegedError(output_path)
    return os.open(resolved_parent, os.O_RDONLY | os.O_DIRECTORY)


def _unlink_if_same_file(basename: str, dir_fd: int, expected_stat: os.stat_result) -> None:
    """Cleanup after a write/fdopen failure must remove only the exact
    file this invocation created via `O_CREAT | O_EXCL` — never a
    replacement pathname a concurrent actor swapped in afterward.
    Resolved relative to the anchored parent dir-fd (see
    `_open_anchored_output_parent_dir`), not by re-walking the original
    (possibly symlinked) output path. `os.lstat`/`os.unlink` (dir_fd-
    relative, no symlink follow) are compared/scoped by `(st_dev,
    st_ino)` against the identity captured immediately after
    `os.open()`; the pathname is left untouched on any mismatch, and a
    failure to stat it (e.g. it no longer exists) is not itself an
    error."""
    with contextlib.suppress(OSError):
        current_stat = os.lstat(basename, dir_fd=dir_fd)
        if (current_stat.st_dev, current_stat.st_ino) == (
            expected_stat.st_dev,
            expected_stat.st_ino,
        ):
            os.unlink(basename, dir_fd=dir_fd)


def render_service_plist_to_file(spec: ServiceSpec, output_path: Path) -> None:
    """Writes the rendered plist to `output_path`. Validation happens
    first (via `render_service_plist`, then the privileged-output-path
    checks), so invalid input never creates or touches the output file.
    The file is created with `O_CREAT | O_EXCL` relative to an fd already
    anchored to the resolved, non-privileged parent directory (see
    `_open_anchored_output_parent_dir`) — an existing path is never
    silently overwritten, and a symlink swapped into the parent path
    after validation cannot redirect the write. Any failure after the
    file is created — including a failed `os.fdopen()`, not only a
    failed write — removes the partial file it created (identity-
    checked, never a replacement), leaving no partial file behind and no
    leaked descriptor."""
    data = render_service_plist(spec)
    _reject_privileged_output_path(output_path)
    parent_dir_fd = _open_anchored_output_parent_dir(output_path)
    try:
        basename = output_path.name
        try:
            fd = os.open(
                basename,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
                dir_fd=parent_dir_fd,
            )
        except FileExistsError:
            raise ServiceRenderOutputExistsError(output_path) from None
        created_stat = os.fstat(fd)
        try:
            fh = os.fdopen(fd, "wb")
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            _unlink_if_same_file(basename, parent_dir_fd, created_stat)
            raise
        try:
            with fh:
                fh.write(data)
        except BaseException:
            _unlink_if_same_file(basename, parent_dir_fd, created_stat)
            raise
    finally:
        with contextlib.suppress(OSError):
            os.close(parent_dir_fd)


def run_service_render(spec: ServiceSpec, output_path: Path) -> OpsResult:
    builder = OpsResultBuilder(action="service-render")
    try:
        render_service_plist_to_file(spec, output_path)
    except ValueError as exc:
        builder.add(
            component="service_spec",
            status=FindingStatus.FAIL,
            code="SERVICE_SPEC_INVALID",
            message=safe_exception_text(exc),
        )
        return builder.build()
    except ServiceRenderOutputExistsError as exc:
        builder.add(
            component="output_path",
            status=FindingStatus.FAIL,
            code="OUTPUT_PATH_EXISTS",
            message=safe_exception_text(exc),
        )
        return builder.build()
    except ServiceRenderOutputPathPrivilegedError as exc:
        builder.add(
            component="output_path",
            status=FindingStatus.FAIL,
            code="OUTPUT_PATH_PRIVILEGED_LOCATION",
            message=safe_exception_text(exc),
        )
        return builder.build()
    except OSError as exc:
        builder.add(
            component="output_path",
            status=FindingStatus.FAIL,
            code="OUTPUT_WRITE_FAILED",
            message=safe_exception_text(exc),
        )
        return builder.build()

    builder.add(
        component="service_plist",
        status=FindingStatus.OK,
        code="SERVICE_PLIST_RENDERED",
        message=f"wrote LaunchDaemon plist for label '{spec.label}' to {output_path}",
    )
    return builder.build()


class _PlistReadTooLarge(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"plist exceeds bound of {limit} bytes")
        self.limit = limit


def _read_bounded_plist_bytes(path: Path, limit: int) -> bytes:
    """Never issues a read for more than `limit + 1` bytes — the bound is
    enforced by the read call itself, not by a `stat()` taken beforehand
    (the same TOCTOU-safe pattern used by `verify_release.py`)."""
    with path.open("rb") as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise _PlistReadTooLarge(limit)
    return data


def verify_service_plist(plist_path: Path, *, expected_label: str | None = None) -> OpsResult:
    """Bounded read + parse + shape/security verification of an
    operator-supplied plist. Never installs, never invokes `launchctl`.
    Every check below is independently reported; a failure in one check
    never hides another component's finding."""
    builder = OpsResultBuilder(action="service-verify")

    try:
        raw = _read_bounded_plist_bytes(plist_path, _MAX_PLIST_BYTES)
    except _PlistReadTooLarge as exc:
        builder.add(
            component="plist_schema",
            status=FindingStatus.FAIL,
            code="PLIST_TOO_LARGE",
            message=safe_exception_text(exc),
        )
        return builder.build()
    except OSError as exc:
        builder.add(
            component="plist_schema",
            status=FindingStatus.FAIL,
            code="PLIST_UNREADABLE",
            message=safe_exception_text(exc),
        )
        return builder.build()

    try:
        payload = plistlib.loads(raw, fmt=plistlib.FMT_XML)
    except (ExpatError, ValueError) as exc:
        builder.add(
            component="plist_schema",
            status=FindingStatus.FAIL,
            code="PLIST_INVALID",
            message=safe_exception_text(exc),
        )
        return builder.build()

    if not isinstance(payload, dict):
        builder.add(
            component="plist_schema",
            status=FindingStatus.FAIL,
            code="PLIST_NOT_A_DICT",
            message="top-level plist value is not a dictionary",
        )
        return builder.build()

    builder.add(
        component="plist_schema",
        status=FindingStatus.OK,
        code="PLIST_VALID",
        message="plist parsed as a valid XML property list",
    )

    _check_allowed_keys(builder, payload)
    _check_required_keys_present(builder, payload)
    _check_label(builder, payload, expected_label)
    _check_user_name(builder, payload)
    _check_working_directory(builder, payload)
    program_args = _check_program_arguments_shape(builder, payload)
    _check_no_shell_wrapper(builder, payload, program_args)
    _check_executable_and_invocation(builder, program_args)
    _check_std_paths(builder, payload)
    _check_no_environment_variables(builder, payload)
    _check_keep_alive(builder, payload)

    return builder.build()


def _check_allowed_keys(builder: OpsResultBuilder, payload: dict[str, object]) -> None:
    extra = sorted(set(payload) - REQUIRED_PLIST_KEYS)
    if extra:
        builder.add(
            component="allowed_keys",
            status=FindingStatus.FAIL,
            code="UNSUPPORTED_KEY",
            message=f"unsupported plist key(s): {', '.join(extra[:10])}",
        )
        return
    builder.add(
        component="allowed_keys",
        status=FindingStatus.OK,
        code="ALLOWED_KEYS_ONLY",
        message="no unsupported plist keys present",
    )


def _check_required_keys_present(builder: OpsResultBuilder, payload: dict[str, object]) -> None:
    missing = sorted(REQUIRED_PLIST_KEYS - set(payload))
    if missing:
        builder.add(
            component="required_keys",
            status=FindingStatus.FAIL,
            code="MISSING_REQUIRED_KEY",
            message=f"missing required plist key(s): {', '.join(missing)}",
        )
        return
    builder.add(
        component="required_keys",
        status=FindingStatus.OK,
        code="REQUIRED_KEYS_PRESENT",
        message="all required plist keys are present",
    )


def _check_label(
    builder: OpsResultBuilder, payload: dict[str, object], expected_label: str | None
) -> None:
    label = payload.get("Label")
    if not isinstance(label, str):
        builder.add(
            component="label",
            status=FindingStatus.FAIL,
            code="LABEL_MISSING_OR_INVALID_TYPE",
            message="Label is missing or not a string",
        )
        return
    try:
        validate_label(label)
    except ValueError as exc:
        builder.add(
            component="label",
            status=FindingStatus.FAIL,
            code="LABEL_INVALID",
            message=safe_exception_text(exc),
        )
        return
    if expected_label is not None and label != expected_label:
        builder.add(
            component="label",
            status=FindingStatus.FAIL,
            code="LABEL_MISMATCH",
            message="Label does not match --expected-label",
        )
        return
    builder.add(
        component="label",
        status=FindingStatus.OK,
        code="LABEL_VALID",
        message="Label is a well-formed launchd identifier",
    )


def _check_user_name(builder: OpsResultBuilder, payload: dict[str, object]) -> None:
    user_name = payload.get("UserName")
    if not isinstance(user_name, str):
        builder.add(
            component="user_name",
            status=FindingStatus.FAIL,
            code="USER_NAME_MISSING_OR_INVALID_TYPE",
            message="UserName is missing or not a string",
        )
        return
    try:
        validate_user_name(user_name)
    except ValueError as exc:
        builder.add(
            component="user_name",
            status=FindingStatus.FAIL,
            code="USER_NAME_INVALID",
            message=safe_exception_text(exc),
        )
        return
    builder.add(
        component="user_name",
        status=FindingStatus.OK,
        code="USER_NAME_VALID",
        message="UserName is present and is not root",
    )


def _check_working_directory(builder: OpsResultBuilder, payload: dict[str, object]) -> None:
    value = payload.get("WorkingDirectory")
    if not isinstance(value, str):
        builder.add(
            component="working_directory",
            status=FindingStatus.FAIL,
            code="WORKING_DIRECTORY_MISSING_OR_INVALID_TYPE",
            message="WorkingDirectory is missing or not a string",
        )
        return
    try:
        validate_absolute_path(value, field_name="WorkingDirectory")
    except ValueError as exc:
        builder.add(
            component="working_directory",
            status=FindingStatus.FAIL,
            code="WORKING_DIRECTORY_INVALID",
            message=safe_exception_text(exc),
        )
        return
    builder.add(
        component="working_directory",
        status=FindingStatus.OK,
        code="WORKING_DIRECTORY_VALID",
        message="WorkingDirectory is an absolute path",
    )


def _check_program_arguments_shape(
    builder: OpsResultBuilder, payload: dict[str, object]
) -> list[str] | None:
    args = payload.get("ProgramArguments")
    if not isinstance(args, list) or not args or not all(isinstance(a, str) for a in args):
        builder.add(
            component="program_arguments_shape",
            status=FindingStatus.FAIL,
            code="PROGRAM_ARGUMENTS_INVALID",
            message="ProgramArguments must be a non-empty array of strings",
        )
        return None
    builder.add(
        component="program_arguments_shape",
        status=FindingStatus.OK,
        code="PROGRAM_ARGUMENTS_VALID",
        message=f"ProgramArguments is an argv array of {len(args)} string(s)",
    )
    return args


def _check_no_shell_wrapper(
    builder: OpsResultBuilder, payload: dict[str, object], args: list[str] | None
) -> None:
    has_program_key = "Program" in payload
    executable_is_shell = False
    if args:
        executable_is_shell = PurePosixPath(args[0]).name in _SHELL_EXECUTABLE_BASENAMES
    if has_program_key or executable_is_shell:
        builder.add(
            component="no_shell_wrapper",
            status=FindingStatus.FAIL,
            code="SHELL_WRAPPER_DETECTED",
            message=(
                "plist must invoke the application directly via ProgramArguments, "
                "never a shell"
            ),
        )
        return
    builder.add(
        component="no_shell_wrapper",
        status=FindingStatus.OK,
        code="NO_SHELL_WRAPPER",
        message="ProgramArguments invokes the executable directly, no shell wrapper",
    )


def _check_executable_and_invocation(
    builder: OpsResultBuilder, args: list[str] | None
) -> None:
    if args is None:
        for component in ("executable_path", "application_invocation", "host", "port"):
            builder.add(
                component=component,
                status=FindingStatus.SKIPPED,
                code="PROGRAM_ARGUMENTS_INVALID",
                message="skipped: ProgramArguments was invalid",
            )
        return

    executable = args[0]
    try:
        validate_absolute_path(executable, field_name="Executable")
    except ValueError as exc:
        builder.add(
            component="executable_path",
            status=FindingStatus.FAIL,
            code="EXECUTABLE_PATH_INVALID",
            message=safe_exception_text(exc),
        )
    else:
        builder.add(
            component="executable_path",
            status=FindingStatus.OK,
            code="EXECUTABLE_PATH_VALID",
            message="Executable is an absolute path",
        )

    expected_shape = (
        len(args) == 8
        and args[1:4] == ["-m", "uvicorn", APPLICATION_MODULE]
        and args[4] == "--host"
        and args[6] == "--port"
    )
    if not expected_shape:
        builder.add(
            component="application_invocation",
            status=FindingStatus.FAIL,
            code="APPLICATION_INVOCATION_MISMATCH",
            message=(
                "ProgramArguments does not match the expected meyar.main:app "
                "uvicorn invocation"
            ),
        )
        for component in ("host", "port"):
            builder.add(
                component=component,
                status=FindingStatus.SKIPPED,
                code="APPLICATION_INVOCATION_MISMATCH",
                message="skipped: invocation shape mismatch",
            )
        return

    builder.add(
        component="application_invocation",
        status=FindingStatus.OK,
        code="APPLICATION_INVOCATION_VALID",
        message=f"ProgramArguments invokes {APPLICATION_MODULE} via uvicorn",
    )

    host_value = args[5]
    if host_value == REQUIRED_HOST:
        builder.add(
            component="host",
            status=FindingStatus.OK,
            code="HOST_LOOPBACK",
            message="host is exactly 127.0.0.1",
        )
    else:
        builder.add(
            component="host",
            status=FindingStatus.FAIL,
            code="HOST_NOT_LOOPBACK",
            message="host is not exactly 127.0.0.1",
        )

    port_value = args[7]
    try:
        port_int = int(port_value)
        validate_port(port_int)
    except ValueError:
        builder.add(
            component="port",
            status=FindingStatus.FAIL,
            code="PORT_INVALID",
            message="port is not a valid integer in range 1..65535",
        )
    else:
        builder.add(
            component="port",
            status=FindingStatus.OK,
            code="PORT_VALID",
            message=f"port={port_int}",
        )


def _check_std_paths(builder: OpsResultBuilder, payload: dict[str, object]) -> None:
    std_fields = (("StandardOutPath", "stdout_path"), ("StandardErrorPath", "stderr_path"))
    for key, component in std_fields:
        value = payload.get(key)
        code_prefix = component.upper()
        if not isinstance(value, str):
            builder.add(
                component=component,
                status=FindingStatus.FAIL,
                code=f"{code_prefix}_MISSING_OR_INVALID_TYPE",
                message=f"{key} is missing or not a string",
            )
            continue
        try:
            validate_absolute_path(value, field_name=key)
        except ValueError as exc:
            builder.add(
                component=component,
                status=FindingStatus.FAIL,
                code=f"{code_prefix}_INVALID",
                message=safe_exception_text(exc),
            )
            continue
        builder.add(
            component=component,
            status=FindingStatus.OK,
            code=f"{code_prefix}_VALID",
            message=f"{key} is an absolute path",
        )


def _check_no_environment_variables(builder: OpsResultBuilder, payload: dict[str, object]) -> None:
    if "EnvironmentVariables" in payload:
        builder.add(
            component="no_environment_variables",
            status=FindingStatus.FAIL,
            code="ENVIRONMENT_VARIABLES_PRESENT",
            message="EnvironmentVariables must not appear in a MEYAR service plist",
        )
        return
    builder.add(
        component="no_environment_variables",
        status=FindingStatus.OK,
        code="NO_ENVIRONMENT_VARIABLES",
        message="EnvironmentVariables is absent",
    )


def _check_keep_alive(builder: OpsResultBuilder, payload: dict[str, object]) -> None:
    keep_alive = payload.get("KeepAlive")
    if (
        not isinstance(keep_alive, dict)
        or set(keep_alive) != {"SuccessfulExit"}
        or keep_alive.get("SuccessfulExit") is not False
    ):
        builder.add(
            component="keep_alive",
            status=FindingStatus.FAIL,
            code="KEEP_ALIVE_INVALID",
            message="KeepAlive must be exactly {SuccessfulExit: false}",
        )
        return
    builder.add(
        component="keep_alive",
        status=FindingStatus.OK,
        code="KEEP_ALIVE_VALID",
        message="KeepAlive.SuccessfulExit is false",
    )

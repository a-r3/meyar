"""`meyar-ops preflight` — non-destructive host/runtime checks useful
before deployment (issue #35 PR1 §4). Every check is read-only: no
files are created/modified here (the storage write probe lives in
`readiness`, not here), no network call is made beyond parsing a
configured URL, and no database connection is attempted.

Missing *optional* tooling (e.g. `pg_dump`) is a WARN finding, never an
uncaught exception and never a hard FAIL — this command does not choose
a PostgreSQL provisioning topology (Homebrew/Docker/Postgres.app), it
only reports what is actually present on this host.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from meyar.config import get_settings
from meyar.llm.loopback import require_loopback_url
from meyar.ops.alembic_introspect import AlembicIntrospectionError, get_code_alembic_heads
from meyar.ops.config import get_ops_settings, resolve_alembic_ini_path
from meyar.ops.redact import safe_exception_text
from meyar.ops.result import FindingStatus, OpsResult, OpsResultBuilder

REQUIRED_PYTHON_MIN = (3, 12)

# Backend-root marker paths, resolved relative to this installed package's
# own source tree (see meyar.ops.config.resolve_alembic_ini_path for the
# same convention/caveat about a future packaged-artifact layout).
_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_REQUIRED_PATHS = (
    _BACKEND_ROOT / "pyproject.toml",
    _BACKEND_ROOT / "alembic",
)

_POSTGRES_CLIENT_TOOLS = ("psql", "pg_dump", "pg_restore")

_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-/]{0,199}$")


def run_preflight() -> OpsResult:
    builder = OpsResultBuilder(action="preflight")

    _check_python_version(builder)
    _check_uv(builder)
    _check_platform(builder)
    _check_required_paths(builder)
    _check_disk_space(builder)
    _check_storage_root(builder)
    _check_ollama_loopback(builder)
    _check_postgres_client_tools(builder)
    _check_alembic_config(builder)
    _check_model_names(builder)

    return builder.build()


def _check_python_version(builder: OpsResultBuilder) -> None:
    current = sys.version_info[:2]
    required = ".".join(map(str, REQUIRED_PYTHON_MIN))
    if current >= REQUIRED_PYTHON_MIN:
        builder.add(
            component="python_version",
            status=FindingStatus.OK,
            code="PYTHON_VERSION_SUPPORTED",
            message=f"running Python {current[0]}.{current[1]} (>= {required})",
        )
    else:
        builder.add(
            component="python_version",
            status=FindingStatus.FAIL,
            code="PYTHON_VERSION_UNSUPPORTED",
            message=f"running Python {current[0]}.{current[1]}, requires >= {required}",
        )


def _check_uv(builder: OpsResultBuilder) -> None:
    uv_path = shutil.which("uv")
    if uv_path is None:
        builder.add(
            component="uv",
            status=FindingStatus.WARN,
            code="UV_NOT_FOUND",
            message="'uv' not found on PATH",
        )
        return
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
            ["uv", "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        builder.add(
            component="uv",
            status=FindingStatus.WARN,
            code="UV_VERSION_CHECK_FAILED",
            message=f"'uv' found but version check failed: {safe_exception_text(exc)}",
        )
        return
    if proc.returncode == 0:
        builder.add(
            component="uv",
            status=FindingStatus.OK,
            code="UV_AVAILABLE",
            message=proc.stdout.strip() or "uv available",
        )
    else:
        builder.add(
            component="uv",
            status=FindingStatus.WARN,
            code="UV_VERSION_CHECK_FAILED",
            message="'uv --version' exited non-zero",
        )


def _check_platform(builder: OpsResultBuilder) -> None:
    builder.add(
        component="platform",
        status=FindingStatus.OK,
        code="PLATFORM_FACTS",
        message=(
            f"system={platform.system()} machine={platform.machine()} "
            f"python_implementation={platform.python_implementation()}"
        ),
    )


def _check_required_paths(builder: OpsResultBuilder) -> None:
    for path in _REQUIRED_PATHS:
        if path.exists():
            builder.add(
                component="required_path",
                status=FindingStatus.OK,
                code="PATH_PRESENT",
                message=f"{path.name} present",
            )
        else:
            builder.add(
                component="required_path",
                status=FindingStatus.FAIL,
                code="PATH_MISSING",
                message=f"required path missing: {path.name}",
            )


def _check_disk_space(builder: OpsResultBuilder) -> None:
    ops_settings = get_ops_settings()
    settings = get_settings()
    target = Path(settings.storage_root)
    # Free-space check must not require the directory to already exist —
    # walk up to the nearest existing ancestor.
    probe_dir = target
    while not probe_dir.exists() and probe_dir != probe_dir.parent:
        probe_dir = probe_dir.parent
    try:
        free_mb = shutil.disk_usage(probe_dir).free // (1024 * 1024)
    except OSError as exc:
        builder.add(
            component="disk_space",
            status=FindingStatus.FAIL,
            code="DISK_SPACE_UNKNOWN",
            message=f"could not determine free disk space: {safe_exception_text(exc)}",
        )
        return
    if free_mb >= ops_settings.min_free_disk_mb:
        builder.add(
            component="disk_space",
            status=FindingStatus.OK,
            code="DISK_SPACE_SUFFICIENT",
            message=f"{free_mb} MB free (>= {ops_settings.min_free_disk_mb} MB minimum)",
        )
    else:
        builder.add(
            component="disk_space",
            status=FindingStatus.FAIL,
            code="DISK_SPACE_INSUFFICIENT",
            message=f"{free_mb} MB free (< {ops_settings.min_free_disk_mb} MB minimum)",
        )


def _check_storage_root(builder: OpsResultBuilder) -> None:
    settings = get_settings()
    root = Path(settings.storage_root)
    if not root.exists():
        builder.add(
            component="storage_root",
            status=FindingStatus.WARN,
            code="STORAGE_ROOT_NOT_YET_CREATED",
            message="storage root does not exist yet (created on first write)",
        )
        return
    if not root.is_dir():
        builder.add(
            component="storage_root",
            status=FindingStatus.FAIL,
            code="STORAGE_ROOT_NOT_A_DIRECTORY",
            message="storage root path exists but is not a directory",
        )
        return
    ok = os.access(root, os.R_OK | os.W_OK | os.X_OK)
    if ok:
        builder.add(
            component="storage_root",
            status=FindingStatus.OK,
            code="STORAGE_ROOT_ACCESSIBLE",
            message="storage root exists with read/write/traverse permission",
        )
    else:
        builder.add(
            component="storage_root",
            status=FindingStatus.FAIL,
            code="STORAGE_ROOT_PERMISSION_DENIED",
            message="storage root exists but lacks required permissions",
        )


def _check_ollama_loopback(builder: OpsResultBuilder) -> None:
    settings = get_settings()
    try:
        require_loopback_url(settings.ollama_base_url, setting_name="MEYAR_OLLAMA_BASE_URL")
    except ValueError:
        builder.add(
            component="ollama_url_loopback",
            status=FindingStatus.FAIL,
            code="OLLAMA_URL_NOT_LOOPBACK",
            message="configured Ollama base URL is not a loopback address",
        )
        return
    builder.add(
        component="ollama_url_loopback",
        status=FindingStatus.OK,
        code="OLLAMA_URL_LOOPBACK",
        message="configured Ollama base URL is loopback-only",
    )


def _check_postgres_client_tools(builder: OpsResultBuilder) -> None:
    for tool in _POSTGRES_CLIENT_TOOLS:
        path = shutil.which(tool)
        if path is None:
            builder.add(
                component=f"postgres_client_tool_{tool}",
                status=FindingStatus.WARN,
                code="TOOL_NOT_FOUND",
                message=f"'{tool}' not found on PATH (required later for backup/restore)",
            )
        else:
            builder.add(
                component=f"postgres_client_tool_{tool}",
                status=FindingStatus.OK,
                code="TOOL_AVAILABLE",
                message=f"'{tool}' available",
            )


def _check_alembic_config(builder: OpsResultBuilder) -> None:
    ini_path = resolve_alembic_ini_path()
    if ini_path is None:
        builder.add(
            component="alembic_config",
            status=FindingStatus.FAIL,
            code="ALEMBIC_INI_NOT_FOUND",
            message="could not locate alembic.ini",
        )
        return
    try:
        heads = get_code_alembic_heads(ini_path)
    except AlembicIntrospectionError as exc:
        builder.add(
            component="alembic_config",
            status=FindingStatus.FAIL,
            code="ALEMBIC_CONFIG_INVALID",
            message=safe_exception_text(exc),
        )
        return
    builder.add(
        component="alembic_config",
        status=FindingStatus.OK,
        code="ALEMBIC_CONFIG_AVAILABLE",
        message=f"Alembic script directory loaded, {len(heads)} head(s) found",
    )


def _check_model_names(builder: OpsResultBuilder) -> None:
    settings = get_settings()
    for label, value in (
        ("llm_model_name", settings.ollama_model),
        ("embedding_model_name", settings.ollama_embedding_model),
    ):
        if value and _MODEL_NAME_RE.fullmatch(value):
            builder.add(
                component=label,
                status=FindingStatus.OK,
                code="MODEL_NAME_PRESENT",
                message=f"configured: {value}",
            )
        else:
            builder.add(
                component=label,
                status=FindingStatus.FAIL,
                code="MODEL_NAME_INVALID_OR_MISSING",
                message=f"{label} is empty or not syntactically a model name",
            )

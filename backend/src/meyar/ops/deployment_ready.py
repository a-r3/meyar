"""Read-only, installed-host operator acceptance gate for one active release."""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import platform
import plistlib
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from meyar.config import Settings
from meyar.embedding.dependency import embedding_provider_from_settings
from meyar.llm.dependency import llm_provider_from_settings
from meyar.ops.alembic_introspect import get_db_alembic_revision
from meyar.ops.host_config import load_host_settings
from meyar.ops.offline_host import InstallFailure, privileged_operation_lock, verify_active_release
from meyar.ops.result import FindingStatus as Status
from meyar.ops.result import OpsResult, OpsResultBuilder
from meyar.ops.schema_init import SchemaInitFailure, _active_config
from meyar.ops.service_lifecycle import (
    LifecycleFailure,
    PrincipalResolver,
    ServicePrincipal,
    _installed_bytes,
    _system_directory,
    _verify_runtime,
    resolve_service_principal,
)
from meyar.ops.service_plist import (
    REQUIRED_PLIST_KEYS,
    ServiceSpec,
    render_service_plist,
    validate_label,
    validate_user_name,
)
from meyar.ops.service_status import LaunchctlRunner, run_service_status

_HEALTH_BODY_LIMIT = 1024
_HTTP_TIMEOUT = 3.0
_DB_TIMEOUT = 10.0


def _finding(
    builder: OpsResultBuilder, component: str, code: str, *, status: Status = Status.OK
) -> None:
    # Fixed messages and codes only: no configuration, response, or exception text.
    builder.add(
        component=component, status=status, code=code, message=code.lower().replace("_", " ")
    )


def _skip(builder: OpsResultBuilder, component: str) -> None:
    _finding(builder, component, "PREREQUISITE_FAILED", status=Status.SKIPPED)


def _launchd_code(result: OpsResult) -> str:
    """Preserve confirmed absence; normalize all probe uncertainty to failure."""
    code = result.findings[0].code
    return code if code in {"SERVICE_VISIBLE", "SERVICE_NOT_VISIBLE"} else "SERVICE_PROBE_FAILED"


def _installed_spec(
    root: Path, label: str, directory: Path, system_uid: int, owner_uid: int
) -> tuple[ServiceSpec, bytes]:
    """Extract user/port only after safe installed-file and shape checks."""
    validate_label(label)
    _system_directory(directory, system_uid)
    path = directory / f"{label}.plist"
    raw = _installed_bytes(path, system_uid)
    if raw is None or path.stat().st_mode & 0o7777 != 0o644:
        raise ValueError("invalid installed plist")
    payload = plistlib.loads(raw, fmt=plistlib.FMT_XML)
    if not isinstance(payload, dict) or set(payload) != REQUIRED_PLIST_KEYS:
        raise ValueError("invalid installed plist")
    if payload.get("Label") != label or type(payload.get("UserName")) is not str:
        raise ValueError("invalid installed plist")
    user_name = payload["UserName"]
    validate_user_name(user_name)
    args = payload.get("ProgramArguments")
    if not isinstance(args, list) or len(args) != 8 or args[6] != "--port":
        raise ValueError("invalid installed plist")
    port_text = args[7]
    if type(port_text) is not str or not port_text.isascii() or not port_text.isdecimal():
        raise ValueError("invalid installed plist")
    spec = ServiceSpec(label, user_name, root, int(port_text))
    spec.validate()
    if raw != render_service_plist(spec, expected_owner_uid=owner_uid):
        raise ValueError("noncanonical installed plist")
    return spec, raw


def _probe_application(port: int) -> str:
    # http.client opens a direct numeric-loopback socket. It never consults
    # proxy variables, resolves DNS, or follows a redirect.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=_HTTP_TIMEOUT)
    try:
        connection.request("GET", "/api/v1/health")
        response = connection.getresponse()
        if response.status != 200:
            return "APPLICATION_HEALTH_INVALID"
        raw = response.read(_HEALTH_BODY_LIMIT + 1)
        if len(raw) > _HEALTH_BODY_LIMIT:
            return "APPLICATION_HEALTH_INVALID"
        def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        try:
            payload = json.loads(raw, object_pairs_hook=unique_object)
        except (ValueError, UnicodeError):
            return "APPLICATION_HEALTH_INVALID"
        return "APPLICATION_LIVE" if payload == {"status": "ok"} else "APPLICATION_HEALTH_INVALID"
    except (TimeoutError, OSError, http.client.HTTPException):
        return "APPLICATION_UNREACHABLE"
    finally:
        connection.close()


async def _probe_database(database_url: str, head: str) -> tuple[str, str]:
    try:
        engine = create_async_engine(database_url, connect_args={"timeout": 5.0})
    except Exception:  # noqa: BLE001 - driver errors may contain credentials
        return "DATABASE_UNREACHABLE", "PREREQUISITE_FAILED"
    try:
        try:
            async with asyncio.timeout(_DB_TIMEOUT):
                async with engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 - never expose SQLAlchemy/driver text
            return "DATABASE_UNREACHABLE", "PREREQUISITE_FAILED"
        try:
            async with asyncio.timeout(_DB_TIMEOUT):
                result = await get_db_alembic_revision(engine)
        except Exception:  # noqa: BLE001 - catalog errors may contain credentials
            return "DATABASE_REACHABLE", "SCHEMA_QUERY_FAILED"
        if not result.table_exists or not result.revisions:
            return "DATABASE_REACHABLE", "NO_DB_REVISION"
        if len(result.revisions) != 1:
            return "DATABASE_REACHABLE", "MULTIPLE_DB_REVISIONS"
        if result.revisions[0] != head:
            return "DATABASE_REACHABLE", "SCHEMA_MISMATCH"
        return "DATABASE_REACHABLE", "DB_REVISION_CURRENT"
    finally:
        try:
            await asyncio.wait_for(engine.dispose(), timeout=_DB_TIMEOUT)
        except Exception:  # noqa: BLE001 - no driver text escapes
            pass


async def _probe_ollama(settings: Settings) -> tuple[str, str, str]:
    async def health(factory: object) -> dict[str, object] | None:
        try:
            provider = factory(settings)  # type: ignore[operator]
            result = await asyncio.wait_for(provider.health(), timeout=7.0)
            return result if isinstance(result, dict) else None
        except Exception:  # noqa: BLE001 - isolate and sanitize each provider
            return None

    llm = await health(llm_provider_from_settings)
    embedding = await health(embedding_provider_from_settings)
    reachable = any(
        result is not None and result.get("reachable") is True for result in (llm, embedding)
    )
    daemon = "OLLAMA_REACHABLE" if reachable else "OLLAMA_UNREACHABLE"

    def model_code(result: dict[str, object] | None, prefix: str) -> str:
        if result is None:
            return f"{prefix}_MODEL_CHECK_FAILED" if reachable else "PREREQUISITE_FAILED"
        if result.get("reachable") is not True:
            return f"{prefix}_MODEL_CHECK_FAILED" if reachable else "PREREQUISITE_FAILED"
        return (
            f"{prefix}_MODEL_AVAILABLE"
            if result.get("model_available") is True
            else f"{prefix}_MODEL_UNAVAILABLE"
        )

    return daemon, model_code(llm, "LLM"), model_code(embedding, "EMBEDDING")


def run_deployment_ready(
    root: Path,
    label: str,
    *,
    platform_system: str | None = None,
    plist_directory: Path = Path("/Library/LaunchDaemons"),
    system_uid: int = 0,
    principal_resolver: PrincipalResolver = resolve_service_principal,
    runner: LaunchctlRunner | None = None,
) -> OpsResult:
    builder = OpsResultBuilder(action="deployment-ready")
    if (platform_system or platform.system()) != "Darwin":
        _finding(builder, "platform", "PLATFORM_UNSUPPORTED", status=Status.FAIL)
        return builder.build()
    try:
        validate_label(label)
    except ValueError:
        _finding(builder, "service_plist", "SERVICE_PLIST_INVALID", status=Status.FAIL)
        return builder.build()
    owner_uid = os.geteuid()
    if owner_uid <= 0 or not root.is_absolute() or ".." in root.parts:
        _finding(builder, "active_release", "ACTIVE_RELEASE_INVALID", status=Status.FAIL)
        return builder.build()

    settings: Settings | None = None
    release_id: str | None = None
    head: str | None = None
    spec: ServiceSpec | None = None
    installed: bytes | None = None
    principal: ServicePrincipal | None = None
    try:
        with privileged_operation_lock(root, owner_uid):
            try:
                _, head = _active_config(root, implementation_file=Path(__file__))
                release_id = verify_active_release(root)
            except (SchemaInitFailure, InstallFailure) as exc:
                _finding(builder, "active_release", "ACTIVE_RELEASE_INVALID", status=Status.FAIL)
                migration_invalid = isinstance(exc, SchemaInitFailure) and exc.code in {
                    "MIGRATION_IDENTITY_MISMATCH", "MULTIPLE_OR_NO_HEADS"
                }
                _finding(
                    builder,
                    "db_schema",
                    "MIGRATION_IDENTITY_MISMATCH" if migration_invalid else "PREREQUISITE_FAILED",
                    status=Status.FAIL if migration_invalid else Status.SKIPPED,
                )
            else:
                _finding(builder, "active_release", "ACTIVE_RELEASE_VERIFIED")
            try:
                settings = load_host_settings(root)
            except (OSError, ValueError):
                _finding(builder, "host_config", "HOST_CONFIG_INVALID", status=Status.FAIL)
            else:
                _finding(builder, "host_config", "HOST_CONFIG_VERIFIED")
            if head is not None and settings is not None:
                try:
                    spec, installed = _installed_spec(
                        root, label, plist_directory, system_uid, owner_uid
                    )
                except (
                    LifecycleFailure,
                    OSError,
                    ValueError,
                    TypeError,
                    plistlib.InvalidFileException,
                ):
                    _finding(builder, "service_plist", "SERVICE_PLIST_INVALID", status=Status.FAIL)
                else:
                    _finding(builder, "service_plist", "SERVICE_PLIST_VERIFIED")
            else:
                _skip(builder, "service_plist")
            if spec is not None:
                try:
                    service_gid = (root / "shared/config").stat().st_gid
                    principal = principal_resolver(spec.user_name, service_gid)
                    if (
                        principal.uid <= 0
                        or principal.uid == owner_uid
                        or service_gid not in principal.groups
                    ):
                        raise LifecycleFailure("SERVICE_USER_INVALID")
                except (LifecycleFailure, OSError, KeyError):
                    principal = None
                    _finding(
                        builder,
                        "service_principal",
                        "SERVICE_PRINCIPAL_INVALID",
                        status=Status.FAIL,
                    )
                else:
                    _finding(builder, "service_principal", "SERVICE_PRINCIPAL_VERIFIED")
            else:
                _skip(builder, "service_principal")
            if principal is not None:
                try:
                    _verify_runtime(root, owner_uid, service_gid, principal)
                except (LifecycleFailure, InstallFailure, OSError, ValueError):
                    _finding(
                        builder,
                        "runtime_permissions",
                        "RUNTIME_PERMISSIONS_UNSAFE",
                        status=Status.FAIL,
                    )
                else:
                    _finding(builder, "runtime_permissions", "RUNTIME_PERMISSIONS_VERIFIED")
            else:
                _skip(builder, "runtime_permissions")
    except (InstallFailure, OSError) as exc:
        _finding(
            builder,
            "active_release",
            "OPERATION_BUSY"
            if isinstance(exc, InstallFailure) and exc.code == "OPERATION_BUSY"
            else "ACTIVE_RELEASE_INVALID",
            status=Status.FAIL,
        )
        return builder.build()

    if spec is None:
        _skip(builder, "launchd")
        _skip(builder, "application_liveness")
    else:
        status = run_service_status(label=label, platform_system="Darwin", runner=runner)
        launchd_code = _launchd_code(status)
        _finding(
            builder,
            "launchd",
            launchd_code,
            status=Status.OK if launchd_code == "SERVICE_VISIBLE" else Status.FAIL,
        )
        code = _probe_application(spec.port)
        _finding(
            builder,
            "application_liveness",
            code,
            status=Status.OK if code == "APPLICATION_LIVE" else Status.FAIL,
        )
    if settings is None:
        for component in ("database", "db_schema", "ollama", "llm_model", "embedding_model"):
            if component != "db_schema" or head is not None:
                _skip(builder, component)
    else:
        if head is None:
            _skip(builder, "database")
            for component in ("ollama", "llm_model", "embedding_model"):
                _skip(builder, component)
        else:
            database, schema = asyncio.run(_probe_database(settings.database_url, head))
            _finding(
                builder,
                "database",
                database,
                status=Status.OK if database == "DATABASE_REACHABLE" else Status.FAIL,
            )
            if schema == "PREREQUISITE_FAILED":
                _skip(builder, "db_schema")
            else:
                _finding(
                    builder,
                    "db_schema",
                    schema,
                    status=Status.OK if schema == "DB_REVISION_CURRENT" else Status.FAIL,
                )
            daemon, llm, embedding = asyncio.run(_probe_ollama(settings))
            _finding(
                builder,
                "ollama",
                daemon,
                status=Status.OK if daemon == "OLLAMA_REACHABLE" else Status.FAIL,
            )
            for component, code in (("llm_model", llm), ("embedding_model", embedding)):
                _finding(
                    builder,
                    component,
                    code,
                    status=Status.SKIPPED
                    if code == "PREREQUISITE_FAILED"
                    else Status.OK
                    if code.endswith("_AVAILABLE")
                    else Status.FAIL,
                )
    # A mutation that held the shared lock during long probes cannot yield READY.
    # Recheck the installed identity at the end; a later mutation requires a new run.
    if (
        builder.build().ok
        and installed is not None
        and spec is not None
        and release_id is not None
        and head is not None
    ):
        try:
            with privileged_operation_lock(root, owner_uid):
                _, final_head = _active_config(root, implementation_file=Path(__file__))
                if verify_active_release(root) != release_id or final_head != head:
                    raise ValueError("active release changed")
                if load_host_settings(root) != settings:
                    raise ValueError("host config changed")
                if (
                    _installed_spec(root, label, plist_directory, system_uid, owner_uid)[1]
                    != installed
                ):
                    raise ValueError("installed plist changed")
                if principal is not None:
                    service_gid = (root / "shared/config").stat().st_gid
                    _verify_runtime(root, owner_uid, service_gid, principal)
                final_launchd = _launchd_code(
                    run_service_status(label=label, platform_system="Darwin", runner=runner)
                )
                if final_launchd != "SERVICE_VISIBLE":
                    _finding(builder, "snapshot", final_launchd, status=Status.FAIL)
                    return builder.build()
                if _probe_application(spec.port) != "APPLICATION_LIVE":
                    raise ValueError("service changed during readiness probes")
        except (InstallFailure, SchemaInitFailure, OSError, ValueError, LifecycleFailure):
            _finding(builder, "snapshot", "DEPLOYMENT_CHANGED", status=Status.FAIL)
    return builder.build()

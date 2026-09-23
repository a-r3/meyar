"""`meyar-ops status` — read-only operational metadata (issue #35 PR1
§5). Never reads candidate/database *content* — only connectivity,
revision identifiers, and configured model/provider identity. Database
findings never include the connection URL/credentials (see
meyar.ops.redact); reachability is never silently upgraded to "healthy"
when a dependency actually failed."""

from __future__ import annotations

import os
import platform
import sys
from importlib import metadata
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from meyar.config import get_settings
from meyar.db import make_engine
from meyar.embedding.dependency import get_embedding_provider
from meyar.llm.dependency import get_llm_provider
from meyar.ops.alembic_introspect import (
    AlembicIntrospectionError,
    AlembicRevisionQueryError,
    get_code_alembic_heads,
    get_db_alembic_revision,
)
from meyar.ops.config import resolve_alembic_ini_path
from meyar.ops.redact import safe_exception_text
from meyar.ops.release_manifest import ReleaseManifest
from meyar.ops.result import FindingStatus, OpsResult, OpsResultBuilder


async def run_status(*, manifest_path: Path | None = None) -> OpsResult:
    builder = OpsResultBuilder(action="status")

    _check_package_version(builder)
    _check_release_identity(builder, manifest_path)
    _check_python_version(builder)
    _check_platform(builder)
    code_heads = _check_code_alembic_head(builder)
    await _check_db_revision(builder, code_heads)
    _check_storage_root_accessible(builder)
    await _check_ollama_and_models(builder)

    return builder.build()


def _check_package_version(builder: OpsResultBuilder) -> None:
    try:
        version = metadata.version("meyar")
    except metadata.PackageNotFoundError as exc:
        builder.add(
            component="package_version",
            status=FindingStatus.FAIL,
            code="PACKAGE_VERSION_UNKNOWN",
            message=safe_exception_text(exc),
        )
        return
    builder.add(
        component="package_version",
        status=FindingStatus.OK,
        code="PACKAGE_VERSION",
        message=version,
    )


def _check_release_identity(builder: OpsResultBuilder, manifest_path: Path | None) -> None:
    if manifest_path is None:
        builder.add(
            component="release_identity",
            status=FindingStatus.SKIPPED,
            code="NO_RELEASE_MANIFEST",
            message="no release manifest provided (--manifest); running from a source checkout",
        )
        return
    try:
        manifest = ReleaseManifest.model_validate_json(manifest_path.read_text())
    except OSError as exc:
        builder.add(
            component="release_identity",
            status=FindingStatus.FAIL,
            code="MANIFEST_UNREADABLE",
            message=safe_exception_text(exc),
        )
        return
    except ValidationError as exc:
        builder.add(
            component="release_identity",
            status=FindingStatus.FAIL,
            code="MANIFEST_SCHEMA_INVALID",
            message=f"release manifest failed schema validation: {exc.error_count()} error(s)",
        )
        return
    builder.add(
        component="release_identity",
        status=FindingStatus.OK,
        code="RELEASE_IDENTITY",
        message=f"release_id={manifest.release_id} release_version={manifest.release_version}",
    )


def _check_python_version(builder: OpsResultBuilder) -> None:
    builder.add(
        component="python_version",
        status=FindingStatus.OK,
        code="PYTHON_VERSION",
        message=sys.version.split()[0],
    )


def _check_platform(builder: OpsResultBuilder) -> None:
    builder.add(
        component="platform",
        status=FindingStatus.OK,
        code="PLATFORM_FACTS",
        message=f"system={platform.system()} machine={platform.machine()}",
    )


def _check_code_alembic_head(builder: OpsResultBuilder) -> list[str] | None:
    ini_path = resolve_alembic_ini_path()
    if ini_path is None:
        builder.add(
            component="code_alembic_head",
            status=FindingStatus.FAIL,
            code="ALEMBIC_INI_NOT_FOUND",
            message="could not locate alembic.ini",
        )
        return None
    try:
        heads = get_code_alembic_heads(ini_path)
    except AlembicIntrospectionError as exc:
        builder.add(
            component="code_alembic_head",
            status=FindingStatus.FAIL,
            code="ALEMBIC_CONFIG_INVALID",
            message=safe_exception_text(exc),
        )
        return None
    status = FindingStatus.OK if len(heads) == 1 else FindingStatus.WARN
    builder.add(
        component="code_alembic_head",
        status=status,
        code="ALEMBIC_HEADS" if len(heads) == 1 else "MULTIPLE_ALEMBIC_HEADS",
        message=", ".join(sorted(heads)),
    )
    return heads


async def _check_db_revision(builder: OpsResultBuilder, code_heads: list[str] | None) -> None:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    try:
        try:
            db_result = await get_db_alembic_revision(engine)
        except AlembicRevisionQueryError as exc:
            # A genuine query/permission failure is never reported as "no
            # revision" — status stays observational (FAIL, not WARN) but
            # truthfully distinct from both connectivity failure and a
            # real pre-migration database.
            builder.add(
                component="db_current_revision",
                status=FindingStatus.FAIL,
                code="ALEMBIC_REVISION_QUERY_FAILED",
                message=f"alembic revision query failed: {safe_exception_text(exc)}",
            )
            return
        except (SQLAlchemyError, OSError) as exc:
            builder.add(
                component="db_current_revision",
                status=FindingStatus.FAIL,
                code="DATABASE_UNREACHABLE",
                message=f"database unreachable: {safe_exception_text(exc)}",
            )
            return
    finally:
        await engine.dispose()

    if not db_result.table_exists or not db_result.revisions:
        builder.add(
            component="db_current_revision",
            status=FindingStatus.WARN,
            code="NO_DB_REVISION",
            message="database reachable but has no recorded Alembic revision yet",
        )
        return
    if len(db_result.revisions) > 1:
        builder.add(
            component="db_current_revision",
            status=FindingStatus.WARN,
            code="MULTIPLE_DB_REVISIONS",
            message=f"database has {len(db_result.revisions)} Alembic revision rows",
        )
        return
    revision = db_result.revisions[0]
    matches_code = code_heads is not None and revision in code_heads
    builder.add(
        component="db_current_revision",
        status=FindingStatus.OK if matches_code else FindingStatus.WARN,
        code="DB_REVISION_CURRENT" if matches_code else "DB_REVISION_STALE",
        message=revision,
    )


def _check_storage_root_accessible(builder: OpsResultBuilder) -> None:
    settings = get_settings()
    root = Path(settings.storage_root)
    if root.is_dir() and os.access(root, os.R_OK | os.X_OK):
        builder.add(
            component="storage_root_accessible",
            status=FindingStatus.OK,
            code="STORAGE_ROOT_ACCESSIBLE",
            message="storage root exists and is readable",
        )
    else:
        builder.add(
            component="storage_root_accessible",
            status=FindingStatus.FAIL,
            code="STORAGE_ROOT_INACCESSIBLE",
            message="storage root missing or not accessible",
        )


async def _check_ollama_and_models(builder: OpsResultBuilder) -> None:
    """Each provider is isolated in its own try/except so a non-normalized
    exception from provider construction or `.health()` never discards the
    package/version/platform/DB findings already collected above, and one
    broken provider never prevents the other's check from running."""
    settings = get_settings()

    try:
        llm_health = await get_llm_provider().health()
    except Exception as exc:  # noqa: BLE001 - per-provider isolation boundary
        builder.add(
            component="ollama_reachability",
            status=FindingStatus.FAIL,
            code="OLLAMA_HEALTH_CHECK_ERROR",
            message=f"LLM provider health check raised: {safe_exception_text(exc)}",
        )
        builder.add(
            component="configured_llm_identity",
            status=FindingStatus.WARN,
            code="LLM_MODEL_IDENTITY_UNKNOWN",
            message="unknown: LLM provider health check raised an exception",
        )
    else:
        llm_reachable = bool(llm_health.get("reachable"))
        llm_available = bool(llm_health.get("model_available"))
        builder.add(
            component="ollama_reachability",
            status=FindingStatus.OK if llm_reachable else FindingStatus.FAIL,
            code="OLLAMA_REACHABLE" if llm_reachable else "OLLAMA_UNREACHABLE",
            message=f"base_url configured, reachable={llm_reachable}",
        )
        builder.add(
            component="configured_llm_identity",
            status=FindingStatus.OK if llm_available else FindingStatus.WARN,
            code="LLM_MODEL_AVAILABLE" if llm_available else "LLM_MODEL_UNAVAILABLE",
            message=f"model={settings.ollama_model} available={llm_available}",
        )

    try:
        embedding_health = await get_embedding_provider().health()
    except Exception as exc:  # noqa: BLE001 - per-provider isolation boundary
        builder.add(
            component="configured_embedding_identity",
            status=FindingStatus.FAIL,
            code="EMBEDDING_HEALTH_CHECK_ERROR",
            message=f"embedding provider health check raised: {safe_exception_text(exc)}",
        )
    else:
        embedding_available = bool(embedding_health.get("model_available"))
        builder.add(
            component="configured_embedding_identity",
            status=FindingStatus.OK if embedding_available else FindingStatus.WARN,
            code=(
                "EMBEDDING_MODEL_AVAILABLE"
                if embedding_available
                else "EMBEDDING_MODEL_UNAVAILABLE"
            ),
            message=f"model={settings.ollama_embedding_model} available={embedding_available}",
        )

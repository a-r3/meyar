"""`meyar-ops readiness` — component-level local operator readiness
(issue #35 PR1 §6). This is NOT the HTTP `/ready` route (deferred to
issue #46) — it composes the same existing primitives (`meyar.db`,
`meyar.llm`/`meyar.embedding` provider boundaries, the ops storage
probe, Alembic introspection) from the CLI, read-only except for the
bounded, self-cleaning storage write probe. No candidate data is read to
determine readiness."""

from __future__ import annotations

from sqlalchemy import text
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
from meyar.ops.result import FindingStatus, OpsResult, OpsResultBuilder
from meyar.ops.storage_probe import probe_storage_writable


async def run_readiness() -> OpsResult:
    builder = OpsResultBuilder(action="readiness")

    database_ok = await _check_database(builder)
    await _check_db_migration(builder, database_ok=database_ok)
    _check_storage_write(builder)
    await _check_ollama_and_models(builder)

    return builder.build()


async def _check_database(builder: OpsResultBuilder) -> bool:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        builder.add(
            component="database",
            status=FindingStatus.FAIL,
            code="DATABASE_UNREACHABLE",
            message=f"database unreachable: {safe_exception_text(exc)}",
        )
        return False
    finally:
        await engine.dispose()
    builder.add(
        component="database",
        status=FindingStatus.OK,
        code="DATABASE_REACHABLE",
        message="database connection succeeded",
    )
    return True


async def _check_db_migration(builder: OpsResultBuilder, *, database_ok: bool) -> None:
    ini_path = resolve_alembic_ini_path()
    if ini_path is None:
        builder.add(
            component="db_migration",
            status=FindingStatus.FAIL,
            code="ALEMBIC_INI_NOT_FOUND",
            message="could not locate alembic.ini",
        )
        return
    try:
        heads = get_code_alembic_heads(ini_path)
    except AlembicIntrospectionError as exc:
        builder.add(
            component="db_migration",
            status=FindingStatus.FAIL,
            code="ALEMBIC_CONFIG_INVALID",
            message=safe_exception_text(exc),
        )
        return
    if len(heads) != 1:
        builder.add(
            component="db_migration",
            status=FindingStatus.FAIL,
            code="MULTIPLE_OR_NO_HEADS",
            message=f"expected exactly one Alembic head, found {len(heads)}",
        )
        return

    if not database_ok:
        builder.add(
            component="db_migration",
            status=FindingStatus.SKIPPED,
            code="DATABASE_UNREACHABLE",
            message="skipped: database connectivity check already failed",
        )
        return

    settings = get_settings()
    engine = make_engine(settings.database_url)
    try:
        try:
            db_result = await get_db_alembic_revision(engine)
        except AlembicRevisionQueryError as exc:
            builder.add(
                component="db_migration",
                status=FindingStatus.FAIL,
                code="ALEMBIC_REVISION_QUERY_FAILED",
                message=f"alembic revision query failed: {safe_exception_text(exc)}",
            )
            return
        except (SQLAlchemyError, OSError) as exc:
            builder.add(
                component="db_migration",
                status=FindingStatus.FAIL,
                code="DATABASE_UNREACHABLE",
                message=f"database unreachable: {safe_exception_text(exc)}",
            )
            return
    finally:
        await engine.dispose()

    code_head = heads[0]
    if not db_result.table_exists or not db_result.revisions:
        builder.add(
            component="db_migration",
            status=FindingStatus.FAIL,
            code="NO_DB_REVISION",
            message="database has no recorded Alembic revision (never migrated)",
        )
    elif len(db_result.revisions) > 1:
        builder.add(
            component="db_migration",
            status=FindingStatus.FAIL,
            code="MULTIPLE_DB_REVISIONS",
            message=(
                f"database has {len(db_result.revisions)} Alembic revision rows, "
                "expected exactly one"
            ),
        )
    elif db_result.revisions[0] == code_head:
        builder.add(
            component="db_migration",
            status=FindingStatus.OK,
            code="DB_REVISION_CURRENT",
            message=f"database revision matches code head {code_head}",
        )
    else:
        builder.add(
            component="db_migration",
            status=FindingStatus.FAIL,
            code="SCHEMA_MISMATCH",
            message=(
                f"database revision {db_result.revisions[0]} does not match code head {code_head}"
            ),
        )


def _check_storage_write(builder: OpsResultBuilder) -> None:
    settings = get_settings()
    result = probe_storage_writable(settings.storage_root)
    builder.add(
        component="storage_write",
        status=FindingStatus.OK if result.writable else FindingStatus.FAIL,
        code="STORAGE_WRITABLE" if result.writable else "STORAGE_NOT_WRITABLE",
        message=result.message,
    )


async def _check_ollama_and_models(builder: OpsResultBuilder) -> None:
    settings = get_settings()

    llm_health = await get_llm_provider().health()
    reachable = bool(llm_health.get("reachable"))
    builder.add(
        component="ollama",
        status=FindingStatus.OK if reachable else FindingStatus.FAIL,
        code="OLLAMA_REACHABLE" if reachable else "OLLAMA_UNREACHABLE",
        message=f"Ollama daemon reachable={reachable}",
    )

    llm_available = bool(llm_health.get("model_available"))
    builder.add(
        component="llm_model",
        status=FindingStatus.OK if (reachable and llm_available) else FindingStatus.FAIL,
        code="LLM_MODEL_AVAILABLE" if (reachable and llm_available) else "LLM_MODEL_UNAVAILABLE",
        message=f"model={settings.ollama_model} available={llm_available}",
    )

    embedding_health = await get_embedding_provider().health()
    embedding_reachable = bool(embedding_health.get("reachable"))
    embedding_available = bool(embedding_health.get("model_available"))
    builder.add(
        component="embedding_model",
        status=(
            FindingStatus.OK
            if (embedding_reachable and embedding_available)
            else FindingStatus.FAIL
        ),
        code=(
            "EMBEDDING_MODEL_AVAILABLE"
            if (embedding_reachable and embedding_available)
            else "EMBEDDING_MODEL_UNAVAILABLE"
        ),
        message=f"model={settings.ollama_embedding_model} available={embedding_available}",
    )

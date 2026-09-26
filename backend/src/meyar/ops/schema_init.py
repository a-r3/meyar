"""Initialize only an empty host database to the verified active release head."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import meyar
from alembic import command
from meyar.ops.alembic_introspect import get_db_alembic_revision
from meyar.ops.host_config import load_host_settings
from meyar.ops.offline_host import (
    InstallFailure,
    _load_json,
    _operation_lock,
    _regular_file,
    verify_active_release,
)
from meyar.ops.result import FindingStatus, OpsResult, build_single_finding_result


class SchemaInitFailure(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _active_config(root: Path) -> tuple[Config, str]:
    if os.geteuid() == 0 or not root.is_absolute() or ".." in root.parts:
        raise SchemaInitFailure("ACTIVE_RELEASE_INVALID")
    try:
        release_id = verify_active_release(root)
        release = root / "releases" / release_id
        source = release / "backend" / "src" / "meyar"
        python = release / ".venv" / "bin" / "python"
        if (
            Path(meyar.__file__).resolve().parent != source
            or Path(__file__).resolve() != source / "ops" / "schema_init.py"
            or Path(sys.executable).resolve() != python
        ):
            raise SchemaInitFailure("ACTIVE_RELEASE_INVALID")
        ini = release / "backend" / "alembic.ini"
        scripts = release / "backend" / "alembic"
        _regular_file(ini)
        if not stat.S_ISDIR(scripts.lstat().st_mode):
            raise SchemaInitFailure("ACTIVE_RELEASE_INVALID")
        config = Config(str(ini))
        if config.get_main_option("script_location") != str(scripts):
            raise SchemaInitFailure("ACTIVE_RELEASE_INVALID")
        if config.get_main_option("version_locations") is not None:
            raise SchemaInitFailure("ACTIVE_RELEASE_INVALID")
        heads = list(ScriptDirectory.from_config(config).get_heads())
        if len(heads) != 1:
            raise SchemaInitFailure("MULTIPLE_OR_NO_HEADS")
        manifest = _load_json(release / "release_manifest.json")
        if manifest.get("alembic_heads") != heads:
            raise SchemaInitFailure("MIGRATION_IDENTITY_MISMATCH")
        config.attributes["meyar_schema_init"] = True
        return config, heads[0]
    except (InstallFailure, OSError, ValueError, KeyError) as exc:
        raise SchemaInitFailure("ACTIVE_RELEASE_INVALID") from exc
    except SchemaInitFailure:
        raise
    except Exception as exc:  # noqa: BLE001 - migration graph failures may contain paths
        raise SchemaInitFailure("MIGRATION_IDENTITY_MISMATCH") from exc


async def _inspect_database(engine: AsyncEngine) -> tuple[list[str], set[str]]:
    try:
        revisions = await get_db_alembic_revision(engine)
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT tablename FROM pg_catalog.pg_tables "
                    "WHERE schemaname = current_schema()"
                )
            )
            tables = {str(row[0]) for row in rows.fetchall()}
        return revisions.revisions, tables
    except SQLAlchemyError as exc:
        raise SchemaInitFailure("DATABASE_UNREACHABLE") from exc
    except Exception as exc:  # noqa: BLE001 - sanitize catalog and revision query errors
        raise SchemaInitFailure("SCHEMA_INITIALIZATION_FAILED") from exc


async def _run_database(config: Config, head: str, database_url: str) -> str:
    try:
        engine = create_async_engine(database_url, pool_pre_ping=True)
    except Exception as exc:  # noqa: BLE001 - driver errors may include credentials
        raise SchemaInitFailure("DATABASE_UNREACHABLE") from exc
    try:
        revisions, tables = await _inspect_database(engine)
        if len(revisions) > 1:
            raise SchemaInitFailure("MULTIPLE_DB_REVISIONS")
        if revisions == [head]:
            return "SCHEMA_ALREADY_CURRENT"
        if revisions:
            raise SchemaInitFailure("SCHEMA_UPGRADE_REQUIRES_UPDATE_WORKFLOW")
        # A first deployment has no application tables. Treat *any* other
        # user table as an unknown existing schema rather than guessing which
        # historical MEYAR table names still appear in current model metadata.
        if tables - {"alembic_version"}:
            raise SchemaInitFailure("UNVERSIONED_SCHEMA_PRESENT")

        def upgrade(connection: object) -> None:
            config.attributes["connection"] = connection
            try:
                command.upgrade(config, head)
            finally:
                del config.attributes["connection"]

        try:
            async with engine.begin() as connection:
                await connection.run_sync(upgrade)
        except Exception as exc:  # noqa: BLE001 - Alembic/DB errors may contain credentials
            raise SchemaInitFailure("SCHEMA_INITIALIZATION_FAILED") from exc
        try:
            after, _ = await _inspect_database(engine)
        except SchemaInitFailure as exc:
            raise SchemaInitFailure("SCHEMA_POSTCHECK_FAILED") from exc
        if after != [head]:
            raise SchemaInitFailure("SCHEMA_POSTCHECK_FAILED")
        return "SCHEMA_INITIALIZED"
    finally:
        try:
            await engine.dispose()
        except Exception as exc:  # noqa: BLE001 - never expose driver errors
            raise SchemaInitFailure("SCHEMA_INITIALIZATION_FAILED") from exc


def run_schema_init(root: Path) -> OpsResult:
    try:
        if os.geteuid() == 0 or not root.is_absolute() or ".." in root.parts:
            raise SchemaInitFailure("ACTIVE_RELEASE_INVALID")
        with _operation_lock(root):
            config, head = _active_config(root)
            try:
                settings = load_host_settings(root)
            except (OSError, ValueError) as exc:
                raise SchemaInitFailure("HOST_CONFIG_INVALID") from exc
            code = asyncio.run(_run_database(config, head, settings.database_url))
    except InstallFailure as exc:
        code = "OPERATION_BUSY" if exc.code == "OPERATION_BUSY" else "ACTIVE_RELEASE_INVALID"
        status = FindingStatus.FAIL
    except OSError:
        code = "ACTIVE_RELEASE_INVALID"
        status = FindingStatus.FAIL
    except SchemaInitFailure as exc:
        code = exc.code
        status = FindingStatus.FAIL
    else:
        status = FindingStatus.OK
    return build_single_finding_result(
        action="schema-init",
        component="schema",
        status=status,
        code=code,
        message=(
            "schema initialization completed"
            if status is FindingStatus.OK
            else "schema initialization refused or failed"
        ),
    )

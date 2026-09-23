"""Read-only Alembic introspection for meyar-ops. Never runs a migration
(`upgrade`/`downgrade`) — status/readiness only ever compare revisions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine


class AlembicIntrospectionError(Exception):
    """Raised for a code-side problem (missing/unreadable alembic.ini or
    script directory, zero or multiple heads) — never for a database
    connectivity problem, which callers handle separately."""


class AlembicRevisionQueryError(Exception):
    """Raised when the database connection itself succeeded but the
    `alembic_version` table-existence check or revision query failed for
    a genuine reason (permission denied, malformed table, a corrupted
    catalog, ...). Never raised for a connectivity failure — that
    surfaces as an unwrapped `SQLAlchemyError` from `engine.connect()`,
    which callers already handle separately as database-unreachable. This
    distinction exists so a real query/permission failure is never
    reported as "no revision" (see issue #35 PR1 corrective review)."""


@dataclass(frozen=True)
class DbAlembicRevisionResult:
    """`table_exists=False` means `alembic_version` has never been
    created — a truthful, expected pre-migration state, not an error.
    `revisions` holds every `version_num` row found (normally exactly
    one); a count other than 1 while `table_exists` is True is its own
    truthful, distinct outcome for the caller to report — never silently
    collapsed to "current" or "no revision" by reading only `.first()`."""

    table_exists: bool
    revisions: list[str]


def get_code_alembic_heads(alembic_ini_path: Path) -> list[str]:
    config = Config(str(alembic_ini_path))
    try:
        script = ScriptDirectory.from_config(config)
        heads = list(script.get_heads())
    except Exception as exc:  # noqa: BLE001 - normalize every failure mode
        raise AlembicIntrospectionError(f"could not read Alembic script directory: {exc}") from exc
    if not heads:
        raise AlembicIntrospectionError("Alembic script directory has no heads.")
    return heads


async def get_db_alembic_revision(engine: AsyncEngine) -> DbAlembicRevisionResult:
    """PostgreSQL-specific table-existence probe (MEYAR's only supported
    database) followed by reading *every* `version_num` row, never just
    the first. A connectivity failure at `engine.connect()` is never
    caught here — it propagates to the caller unwrapped, exactly as
    before. A failure of either query *after* a successful connection
    (permission denied, a malformed table, ...) is wrapped as
    `AlembicRevisionQueryError` so callers can tell a genuine query
    failure apart from both a connectivity failure and a truthful
    "table does not exist yet"."""
    async with engine.connect() as conn:
        try:
            exists_result = await conn.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_tables "
                    "WHERE schemaname = current_schema() AND tablename = 'alembic_version')"
                )
            )
            table_exists = bool(exists_result.scalar())
            if not table_exists:
                return DbAlembicRevisionResult(table_exists=False, revisions=[])
            rows = await conn.execute(text("SELECT version_num FROM alembic_version"))
            revisions = [str(row[0]) for row in rows.fetchall()]
        except SQLAlchemyError as exc:
            raise AlembicRevisionQueryError(str(exc)) from exc
    return DbAlembicRevisionResult(table_exists=True, revisions=revisions)

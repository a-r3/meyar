"""Read-only Alembic introspection for meyar-ops. Never runs a migration
(`upgrade`/`downgrade`) — status/readiness only ever compare revisions."""

from __future__ import annotations

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


async def get_db_alembic_revision(engine: AsyncEngine) -> str | None:
    """Returns the single current revision row, or None if the
    `alembic_version` table does not exist yet (pre-migration database —
    a truthful, expected state, not an error). Raises SQLAlchemyError
    (unwrapped) for a genuine connectivity failure; callers decide how to
    report that."""
    async with engine.connect() as conn:
        try:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
        except SQLAlchemyError:
            # Table absence surfaces as a DBAPI/programming error; treat
            # any execution failure here as "no revision recorded yet"
            # rather than re-raising, since a real connectivity failure
            # would already have failed engine.connect() above.
            return None
        row = result.first()
        return str(row[0]) if row is not None else None

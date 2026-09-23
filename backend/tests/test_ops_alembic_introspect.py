"""`meyar.ops.alembic_introspect.get_db_alembic_revision` (issue #35 PR1
corrective review, Blocker 4). Distinguishes "table absent" (truthful,
expected pre-migration state) from a genuine query/permission failure
(never silently reported as "no revision"), and reads every revision row
rather than only `.first()`."""

import pytest
from conftest import TEST_DATABASE_URL
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from meyar.ops.alembic_introspect import (
    AlembicRevisionQueryError,
    DbAlembicRevisionResult,
    get_code_alembic_heads,
    get_db_alembic_revision,
)
from meyar.ops.config import resolve_alembic_ini_path


def _real_code_head() -> str:
    ini_path = resolve_alembic_ini_path()
    assert ini_path is not None
    heads = get_code_alembic_heads(ini_path)
    assert len(heads) == 1
    return heads[0]


async def _drop_alembic_version_table() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    await engine.dispose()


async def test_table_absent_reports_not_migrated_truthfully() -> None:
    await _drop_alembic_version_table()
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        result = await get_db_alembic_revision(engine)
    finally:
        await engine.dispose()
    assert result == DbAlembicRevisionResult(table_exists=False, revisions=[])


async def test_single_revision_row_is_reported() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    head = _real_code_head()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
        )
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": head}
        )
    try:
        result = await get_db_alembic_revision(engine)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await engine.dispose()
    assert result == DbAlembicRevisionResult(table_exists=True, revisions=[head])


async def test_multiple_revision_rows_are_all_reported_not_just_first() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
        )
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": "a" * 12}
        )
        await conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": "b" * 12}
        )
    try:
        result = await get_db_alembic_revision(engine)
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await engine.dispose()
    assert result.table_exists is True
    assert set(result.revisions) == {"a" * 12, "b" * 12}
    assert len(result.revisions) == 2


async def test_query_failure_after_successful_connection_raises_distinct_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure of the revision-row query itself (permission denied,
    malformed table, ...) — after the connection and the table-existence
    probe already succeeded — must never be reported the same way as
    "table does not exist yet"."""
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
        )
        await conn.execute(text("DELETE FROM alembic_version"))
        await conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": "c" * 12}
        )

    original_execute = AsyncConnection.execute

    async def _flaky_execute(
        self: AsyncConnection, statement: object, *args: object, **kwargs: object
    ):
        if "SELECT version_num FROM alembic_version" in str(statement):
            raise SQLAlchemyError("simulated permission-denied reading alembic_version")
        return await original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncConnection, "execute", _flaky_execute)

    try:
        with pytest.raises(AlembicRevisionQueryError):
            await get_db_alembic_revision(engine)
    finally:
        monkeypatch.undo()
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        await engine.dispose()

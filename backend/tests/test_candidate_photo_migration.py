"""Fresh PostgreSQL migration acceptance for the additive photo table."""

import asyncio
import uuid
from pathlib import Path

from alembic.config import Config
from conftest import ADMIN_DATABASE_URL
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from meyar.config import get_settings

BASE_URL = ADMIN_DATABASE_URL.rsplit("/", 1)[0]
ADMIN_URL = f"{BASE_URL}/postgres"


async def _execute_admin(statement: str, name: str) -> None:
    engine = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with engine.connect() as connection:
        await connection.execute(text(statement.replace("{name}", name)))
    await engine.dispose()


async def _verify(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT to_regclass('candidate_photo_versions')"))
        assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "8bd12e7c4a60"
        )
        constraints = set(
            (
                await connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint WHERE conrelid = "
                        "'candidate_photo_versions'::regclass"
                    )
                )
            ).scalars()
        )
        assert {
            "fk_photo_exact_document",
            "ck_photo_asset_consistency",
            "ck_photo_status",
            "uq_candidate_photo_version",
            "uq_photo_document_extractor",
        }.issubset(constraints)
    await engine.dispose()


def test_fresh_photo_migration(monkeypatch) -> None:
    name = f"meyar_photo_{uuid.uuid4().hex}"
    url = f"{BASE_URL}/{name}"
    asyncio.run(_execute_admin('CREATE DATABASE "{name}"', name))
    monkeypatch.setenv("MEYAR_DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    try:
        command.upgrade(config, "head")
        asyncio.run(_verify(url))
    finally:
        get_settings.cache_clear()
        asyncio.run(_execute_admin('DROP DATABASE IF EXISTS "{name}"', name))

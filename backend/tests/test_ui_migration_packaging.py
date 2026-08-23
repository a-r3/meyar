import asyncio
import uuid
from importlib import resources
from pathlib import Path

from alembic.config import Config
from conftest import ADMIN_DATABASE_URL
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from meyar.config import get_settings

BASE_URL = ADMIN_DATABASE_URL.rsplit("/", 1)[0]
ADMIN_URL = f"{BASE_URL}/postgres"
PRE_SLICE11_REVISION = "c0a4f2d8e317"
SLICE11_REVISION = "e3b1f7a9c2d4"


async def _create_database(name: str) -> None:
    engine = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with engine.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    await engine.dispose()


async def _drop_database(name: str) -> None:
    engine = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with engine.connect() as connection:
        await connection.execute(
            text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:name"),
            {"name": name},
        )
        await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    await engine.dispose()


async def _assert_upgraded(database_url: str) -> None:
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        columns = set(
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='browser_sessions'"
                    )
                )
            ).scalars()
        )
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        raw_columns = await connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='browser_sessions' AND column_name IN "
                "('api_key','session_token','tenant_id','scopes')"
            )
        )
    await engine.dispose()
    assert {
        "id",
        "api_key_id",
        "session_token_hash",
        "csrf_secret",
        "created_at",
        "expires_at",
        "revoked_at",
    } == columns
    assert raw_columns == 0
    assert revision == SLICE11_REVISION


async def _assert_downgraded(database_url: str) -> None:
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        table = await connection.scalar(text("SELECT to_regclass('browser_sessions')"))
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    await engine.dispose()
    assert table is None
    assert revision == PRE_SLICE11_REVISION


def test_slice11_postgresql_migration_upgrade_downgrade_reupgrade(monkeypatch) -> None:
    database_name = f"meyar_slice11_{uuid.uuid4().hex}"
    database_url = f"{BASE_URL}/{database_name}"
    alembic_config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    asyncio.run(_create_database(database_name))
    monkeypatch.setenv("MEYAR_DATABASE_URL", database_url)
    get_settings.cache_clear()
    try:
        command.upgrade(alembic_config, PRE_SLICE11_REVISION)
        command.upgrade(alembic_config, "head")
        asyncio.run(_assert_upgraded(database_url))
        command.downgrade(alembic_config, PRE_SLICE11_REVISION)
        asyncio.run(_assert_downgraded(database_url))
        command.upgrade(alembic_config, "head")
        asyncio.run(_assert_upgraded(database_url))
    finally:
        get_settings.cache_clear()
        asyncio.run(_drop_database(database_name))


def test_installed_package_resources_are_locatable_and_local_only() -> None:
    ui_root = resources.files("meyar.ui")
    templates = ui_root.joinpath("templates")
    static = ui_root.joinpath("static")
    assert templates.joinpath("base.html").is_file()
    assert templates.joinpath("login.html").is_file()
    assert static.joinpath("styles.css").is_file()

    source = "\n".join(
        child.read_text(encoding="utf-8")
        for directory in (templates, static)
        for child in directory.iterdir()
        if child.is_file()
    )
    prohibited = (
        "|safe",
        "Markup(",
        "localStorage",
        "sessionStorage",
        "IndexedDB",
        "fonts.googleapis.com",
        "cdnjs",
        "jsdelivr",
        "unpkg",
        "google-analytics",
        "telemetry",
        "http://",
        "https://",
    )
    assert all(value not in source for value in prohibited)

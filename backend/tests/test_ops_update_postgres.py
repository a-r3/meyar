"""Real disposable-PostgreSQL proof of the forward-only migration boundary."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from meyar.ops.alembic_introspect import get_db_alembic_revision
from meyar.ops.update_worker import _upgrade

ADMIN = "postgresql+asyncpg://meyar:meyar_dev_password@localhost:55719/meyar"


def _synthetic_config(tmp_path: Path) -> Config:
    scripts = tmp_path / "alembic"
    versions = scripts / "versions"
    versions.mkdir(parents=True)
    (scripts / "env.py").write_text(
        "from alembic import context\n"
        "connection = context.config.attributes['connection']\n"
        "context.configure(connection=connection)\n"
        "with context.begin_transaction():\n"
        "    context.run_migrations()\n"
    )
    (versions / "source.py").write_text(
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        "revision = 'source'\n"
        "down_revision = None\n"
        "def upgrade():\n"
        "    op.create_table('ops_update_probe', sa.Column('id', sa.Integer, primary_key=True))\n"
        "def downgrade():\n"
        "    raise AssertionError('downgrade must never be called')\n"
    )
    (versions / "target.py").write_text(
        "from alembic import op\n"
        "import sqlalchemy as sa\n"
        "revision = 'target'\n"
        "down_revision = 'source'\n"
        "def upgrade():\n"
        "    op.add_column('ops_update_probe', sa.Column('phase', sa.Integer))\n"
        "def downgrade():\n"
        "    raise AssertionError('downgrade must never be called')\n"
    )
    config = Config()
    config.set_main_option("script_location", str(scripts))
    return config


async def _exercise(tmp_path: Path) -> None:
    database = "meyar_update_" + uuid.uuid4().hex[:12]
    admin = create_async_engine(ADMIN, isolation_level="AUTOCOMMIT")
    url = ADMIN.rsplit("/", 1)[0] + "/" + database
    engine = None
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f"CREATE DATABASE {database}"))
        engine = create_async_engine(url)
        config = _synthetic_config(tmp_path)

        def install_source(connection: object) -> None:
            config.attributes["connection"] = connection
            try:
                command.upgrade(config, "source")
            finally:
                del config.attributes["connection"]

        async with engine.begin() as connection:
            await connection.run_sync(install_source)
        assert (await get_db_alembic_revision(engine)).revisions == ["source"]

        # The real Alembic worker performs this exact transactional upgrade.
        assert await _upgrade(config, url, "source", "target")
        assert (await get_db_alembic_revision(engine)).revisions == ["target"]
        async with engine.connect() as connection:
            column = await connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'ops_update_probe' AND column_name = 'phase'"
                )
            )
            assert column.scalar_one() == "phase"
        # Application rollback leaves this target revision untouched.
        assert not await _upgrade(config, url, "source", "target")
        assert (await get_db_alembic_revision(engine)).revisions == ["target"]
    finally:
        if engine is not None:
            await engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS {database}"))
        await admin.dispose()


def test_real_disposable_postgres_forward_upgrade_and_no_downgrade(tmp_path: Path) -> None:
    asyncio.run(_exercise(tmp_path))

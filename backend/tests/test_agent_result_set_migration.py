"""Alembic upgrade/downgrade/re-upgrade proof for agent_result_sets /
agent_result_set_members and the AgentConversation.last_search_candidate_ids
-> context_epoch/active_result_set_id replacement (issue #49)."""

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
PRIOR_HEAD = "8bd12e7c4a60"
NEW_HEAD = "d2a8f6c1b3e9"

CONV_ID = "00000000-0000-0000-0000-0000000000c1"
TENANT_ID = "00000000-0000-0000-0000-0000000000c2"
SESSION_ID = "00000000-0000-0000-0000-0000000000c3"
USER_ID = "00000000-0000-0000-0000-0000000000c4"
MEMBERSHIP_ID = "00000000-0000-0000-0000-0000000000c5"
LEGACY_CANDIDATE_ID = "11111111-1111-1111-1111-111111111111"


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


async def _create_vector_extension(database_url: str) -> None:
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    await engine.dispose()


async def _insert_legacy_conversation(database_url: str) -> None:
    statements = [
        (
            "INSERT INTO tenants (id,name,is_active) VALUES (:tenant_id,'Migration Test',true)",
            {"tenant_id": TENANT_ID},
        ),
        (
            "INSERT INTO users (id,username,password_hash,is_active) VALUES "
            "(:user_id,'migration-user','hash',true)",
            {"user_id": USER_ID},
        ),
        (
            "INSERT INTO tenant_memberships (id,user_id,tenant_id,role,is_active) VALUES "
            "(:membership_id,:user_id,:tenant_id,'HR_USER',true)",
            {"membership_id": MEMBERSHIP_ID, "user_id": USER_ID, "tenant_id": TENANT_ID},
        ),
        (
            "INSERT INTO browser_sessions "
            "(id,user_id,tenant_membership_id,session_token_hash,csrf_secret,expires_at) "
            "VALUES (:session_id,:user_id,:membership_id,repeat('a',64),repeat('b',64),"
            "now() + interval '1 day')",
            {"session_id": SESSION_ID, "user_id": USER_ID, "membership_id": MEMBERSHIP_ID},
        ),
        (
            "INSERT INTO agent_conversations "
            "(id,tenant_id,browser_session_id,turns,last_search_candidate_ids) VALUES "
            "(:conv_id,:tenant_id,:session_id,'[]',:candidate_ids)",
            {
                "conv_id": CONV_ID,
                "tenant_id": TENANT_ID,
                "session_id": SESSION_ID,
                "candidate_ids": f'["{LEGACY_CANDIDATE_ID}"]',
            },
        ),
    ]
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        for statement, params in statements:
            await connection.execute(text(statement), params)
    await engine.dispose()


async def _assert_upgraded(database_url: str) -> None:
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT to_regclass('agent_result_sets')"))
        assert await connection.scalar(text("SELECT to_regclass('agent_result_set_members')"))
        row = (
            await connection.execute(
                text(
                    "SELECT context_epoch, active_result_set_id FROM agent_conversations "
                    "WHERE id=:conv_id"
                ),
                {"conv_id": CONV_ID},
            )
        ).one()
        columns = set(
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='agent_conversations'"
                    )
                )
            ).scalars()
        )
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        member_constraints = set(
            (
                await connection.execute(
                    text(
                        "SELECT conname FROM pg_constraint WHERE conrelid = "
                        "'agent_result_set_members'::regclass"
                    )
                )
            ).scalars()
        )
    await engine.dispose()
    assert row.context_epoch == 1
    assert row.active_result_set_id is None
    assert "last_search_candidate_ids" not in columns
    assert "context_epoch" in columns
    assert "active_result_set_id" in columns
    assert revision == NEW_HEAD
    assert {
        "uq_agent_result_set_member_ordinal",
        "uq_agent_result_set_member_candidate",
        "ck_agent_result_set_member_ordinal_positive",
    }.issubset(member_constraints)


async def _assert_downgraded(database_url: str) -> None:
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        columns = set(
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='agent_conversations'"
                    )
                )
            ).scalars()
        )
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        assert await connection.scalar(text("SELECT to_regclass('agent_result_sets')")) is None
    await engine.dispose()
    assert "last_search_candidate_ids" in columns
    assert "context_epoch" not in columns
    assert "active_result_set_id" not in columns
    assert revision == PRIOR_HEAD


def test_fresh_agent_result_set_migration(monkeypatch) -> None:
    name = f"meyar_agent_result_set_{uuid.uuid4().hex}"
    url = f"{BASE_URL}/{name}"
    asyncio.run(_create_database(name))
    monkeypatch.setenv("MEYAR_DATABASE_URL", url)
    get_settings.cache_clear()
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    try:
        asyncio.run(_create_vector_extension(url))
        command.upgrade(config, "head")
        asyncio.run(_verify_fresh(url))
    finally:
        get_settings.cache_clear()
        asyncio.run(_drop_database(name))


async def _verify_fresh(database_url: str) -> None:
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT to_regclass('agent_result_sets')"))
        assert await connection.scalar(text("SELECT to_regclass('agent_result_set_members')"))
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    await engine.dispose()
    assert revision == NEW_HEAD


def test_agent_result_set_migration_roundtrip_preserves_conversation_row(monkeypatch) -> None:
    database_name = f"meyar_agent_result_set_rt_{uuid.uuid4().hex}"
    database_url = f"{BASE_URL}/{database_name}"
    alembic_config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    asyncio.run(_create_database(database_name))
    monkeypatch.setenv("MEYAR_DATABASE_URL", database_url)
    get_settings.cache_clear()
    try:
        asyncio.run(_create_vector_extension(database_url))
        command.upgrade(alembic_config, PRIOR_HEAD)
        asyncio.run(_insert_legacy_conversation(database_url))
        command.upgrade(alembic_config, NEW_HEAD)
        asyncio.run(_assert_upgraded(database_url))
        command.downgrade(alembic_config, PRIOR_HEAD)
        asyncio.run(_assert_downgraded(database_url))
        command.upgrade(alembic_config, NEW_HEAD)
        asyncio.run(_assert_upgraded(database_url))
    finally:
        get_settings.cache_clear()
        asyncio.run(_drop_database(database_name))

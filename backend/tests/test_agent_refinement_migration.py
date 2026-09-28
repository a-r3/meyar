"""Alembic upgrade/downgrade/re-upgrade proof for the AgentResultSet
refinement-provenance columns (issue #49 PR49-2, revision 543c60f7efc5,
chained on d2a8f6c1b3e9)."""

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
PRIOR_HEAD = "d2a8f6c1b3e9"
NEW_HEAD = "543c60f7efc5"

TENANT_ID = "00000000-0000-0000-0000-0000000000d1"
SESSION_ID = "00000000-0000-0000-0000-0000000000d2"
USER_ID = "00000000-0000-0000-0000-0000000000d3"
MEMBERSHIP_ID = "00000000-0000-0000-0000-0000000000d4"
RESULT_SET_ID = "00000000-0000-0000-0000-0000000000d5"


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


async def _insert_legacy_result_set(database_url: str) -> None:
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
            "INSERT INTO agent_result_sets "
            "(id,tenant_id,browser_session_id,context_epoch,request_sha256,"
            "canonical_search_request,planner_policy_version,planner_prompt_version,"
            "planner_schema_version,planner_model_provider,planner_model_name,"
            "planner_model_revision,search_policy_version,search_mode,result_count,"
            "corpus_fingerprint_sha256,expires_at) VALUES "
            "(:id,:tenant_id,:session_id,1,repeat('a',64),'{}','v1','v1','v1','test','test',"
            "'','v1','STRUCTURED_ONLY',0,repeat('b',64),now() + interval '1 day')",
            {"id": RESULT_SET_ID, "tenant_id": TENANT_ID, "session_id": SESSION_ID},
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
        columns = set(
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='agent_result_sets'"
                    )
                )
            ).scalars()
        )
        row = (
            await connection.execute(
                text(
                    "SELECT result_set_kind, parent_result_set_id, refinement_request_sha256, "
                    "canonical_refinement_request, refinement_policy_version FROM "
                    "agent_result_sets WHERE id=:id"
                ),
                {"id": RESULT_SET_ID},
            )
        ).one()
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    await engine.dispose()
    assert {
        "result_set_kind",
        "parent_result_set_id",
        "refinement_request_sha256",
        "canonical_refinement_request",
        "refinement_policy_version",
    }.issubset(columns)
    # Truthful backfill — never a fabricated REFINEMENT/provenance for a
    # row that predates this migration.
    assert row.result_set_kind == "SEARCH"
    assert row.parent_result_set_id is None
    assert row.refinement_request_sha256 is None
    assert row.canonical_refinement_request is None
    assert row.refinement_policy_version is None
    assert revision == NEW_HEAD


async def _assert_downgraded(database_url: str) -> None:
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        columns = set(
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='agent_result_sets'"
                    )
                )
            ).scalars()
        )
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        row = (
            await connection.execute(
                text("SELECT id FROM agent_result_sets WHERE id=:id"), {"id": RESULT_SET_ID}
            )
        ).one()
    await engine.dispose()
    assert {
        "result_set_kind",
        "parent_result_set_id",
        "refinement_request_sha256",
        "canonical_refinement_request",
        "refinement_policy_version",
    }.isdisjoint(columns)
    assert revision == PRIOR_HEAD
    assert str(row.id) == RESULT_SET_ID  # the pre-existing row itself survives untouched


def test_fresh_agent_refinement_migration(monkeypatch) -> None:
    name = f"meyar_agent_refinement_{uuid.uuid4().hex}"
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
        columns = set(
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='agent_result_sets'"
                    )
                )
            ).scalars()
        )
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    await engine.dispose()
    assert {
        "result_set_kind",
        "parent_result_set_id",
        "refinement_request_sha256",
        "canonical_refinement_request",
        "refinement_policy_version",
    }.issubset(columns)
    assert revision == NEW_HEAD


def test_agent_refinement_migration_roundtrip_preserves_prior_result_set(monkeypatch) -> None:
    database_name = f"meyar_agent_refinement_rt_{uuid.uuid4().hex}"
    database_url = f"{BASE_URL}/{database_name}"
    alembic_config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    asyncio.run(_create_database(database_name))
    monkeypatch.setenv("MEYAR_DATABASE_URL", database_url)
    get_settings.cache_clear()
    try:
        asyncio.run(_create_vector_extension(database_url))
        command.upgrade(alembic_config, PRIOR_HEAD)
        asyncio.run(_insert_legacy_result_set(database_url))
        command.upgrade(alembic_config, NEW_HEAD)
        asyncio.run(_assert_upgraded(database_url))
        command.downgrade(alembic_config, PRIOR_HEAD)
        asyncio.run(_assert_downgraded(database_url))
        command.upgrade(alembic_config, NEW_HEAD)
        asyncio.run(_assert_upgraded(database_url))
    finally:
        get_settings.cache_clear()
        asyncio.run(_drop_database(database_name))

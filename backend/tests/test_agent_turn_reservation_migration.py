"""Alembic proof for issue #85 (revision e5d7a3c91b04 on c84a5e2f9d17):
``agent_conversations.turn_version`` + the server-owned in-flight turn
reservation (``active_turn_id`` / ``active_turn_expires_at``).

- fresh install reaches the single head with the new columns/constraints;
- upgrade backfills existing conversations (version 0, no reservation);
- the reservation pair constraint rejects half-set rows;
- downgrade refuses, before any DDL, while an UNEXPIRED reservation exists;
- a database with no live reservation round-trips down/up with the
  transcript intact."""

import asyncio
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from conftest import ADMIN_DATABASE_URL
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from meyar.config import get_settings

BASE_URL = ADMIN_DATABASE_URL.rsplit("/", 1)[0]
ADMIN_URL = f"{BASE_URL}/postgres"
PRIOR_HEAD = "c84a5e2f9d17"
NEW_HEAD = "e5d7a3c91b04"

TENANT = "00000000-0000-0000-0000-0000000085a1"
USER = "00000000-0000-0000-0000-0000000085b1"
MEMBERSHIP = "00000000-0000-0000-0000-0000000085c1"
CONVERSATION = "00000000-0000-0000-0000-0000000085d1"


def _config() -> Config:
    return Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))


async def _admin(statement: str) -> None:
    engine = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with engine.connect() as connection:
        if statement.startswith("DROP"):
            name = statement.split('"')[1]
            await connection.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:n"),
                {"n": name},
            )
        await connection.execute(text(statement))
    await engine.dispose()


async def _execute(url: str, statements: list[tuple[str, dict]]) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        for statement, params in statements:
            await connection.execute(text(statement), params)
    await engine.dispose()


async def _query(url: str, statement: str) -> list:
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        rows = list((await connection.execute(text(statement))).all())
    await engine.dispose()
    return rows


def _with_database(monkeypatch, prefix: str):  # noqa: ANN001, ANN202
    name = f"{prefix}_{uuid.uuid4().hex}"
    url = f"{BASE_URL}/{name}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    monkeypatch.setenv("MEYAR_DATABASE_URL", url)
    get_settings.cache_clear()
    asyncio.run(_execute(url, [("CREATE EXTENSION IF NOT EXISTS vector", {})]))
    return name, url


def _conversation_rows() -> list[tuple[str, dict]]:
    return [
        ("INSERT INTO tenants (id,name,is_active) VALUES (:t,'Migration 85',true)", {"t": TENANT}),
        (
            "INSERT INTO users (id,username,password_hash,is_active) "
            "VALUES (:u,'synthetic-85','x',true)",
            {"u": USER},
        ),
        (
            "INSERT INTO tenant_memberships (id,user_id,tenant_id,role,is_active) "
            "VALUES (:m,:u,:t,'HR_USER',true)",
            {"m": MEMBERSHIP, "u": USER, "t": TENANT},
        ),
        (
            "INSERT INTO agent_conversations "
            "(id,tenant_id,owner_user_id,owner_membership_id,title_kind,turns) VALUES "
            "(:c,:t,:u,:m,'GENERAL',CAST(:turns AS json))",
            {
                "c": CONVERSATION,
                "t": TENANT,
                "u": USER,
                "m": MEMBERSHIP,
                "turns": '[{"role": "user", "text": "synthetic"}]',
            },
        ),
    ]


def _new_columns(url: str) -> list[str]:
    rows = asyncio.run(
        _query(
            url,
            "SELECT column_name FROM information_schema.columns WHERE "
            "table_name='agent_conversations' AND column_name IN "
            "('turn_version','active_turn_id','active_turn_expires_at') ORDER BY column_name",
        )
    )
    return [row[0] for row in rows]


def _revision(url: str) -> str:
    return asyncio.run(_query(url, "SELECT version_num FROM alembic_version"))[0][0]


def test_upgrade_backfills_existing_conversations_and_enforces_pair(monkeypatch) -> None:
    name, url = _with_database(monkeypatch, "meyar_turn85_up")
    config = _config()
    try:
        command.upgrade(config, PRIOR_HEAD)
        asyncio.run(_execute(url, _conversation_rows()))
        command.upgrade(config, NEW_HEAD)
        assert _revision(url) == NEW_HEAD
        assert _new_columns(url) == ["active_turn_expires_at", "active_turn_id", "turn_version"]
        rows = asyncio.run(
            _query(
                url,
                "SELECT turn_version, active_turn_id, active_turn_expires_at, turns "
                "FROM agent_conversations",
            )
        )
        assert [(row[0], row[1], row[2]) for row in rows] == [(0, None, None)]
        assert rows[0][3] == [{"role": "user", "text": "synthetic"}]
        with pytest.raises(IntegrityError):
            asyncio.run(
                _execute(
                    url,
                    [
                        (
                            "UPDATE agent_conversations SET active_turn_id = :token",
                            {"token": str(uuid.uuid4())},
                        )
                    ],
                )
            )
        with pytest.raises(IntegrityError):
            asyncio.run(
                _execute(url, [("UPDATE agent_conversations SET turn_version = -1", {})])
            )
    finally:
        get_settings.cache_clear()
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))


def test_downgrade_refuses_while_a_turn_reservation_is_live(monkeypatch) -> None:
    name, url = _with_database(monkeypatch, "meyar_turn85_refuse")
    config = _config()
    try:
        command.upgrade(config, NEW_HEAD)
        asyncio.run(
            _execute(
                url,
                [
                    *_conversation_rows(),
                    (
                        "UPDATE agent_conversations SET active_turn_id = :token, "
                        "active_turn_expires_at = now() + interval '10 minutes'",
                        {"token": str(uuid.uuid4())},
                    ),
                ],
            )
        )
        with pytest.raises(RuntimeError, match="still in flight"):
            command.downgrade(config, PRIOR_HEAD)
        assert _revision(url) == NEW_HEAD
        assert len(_new_columns(url)) == 3
    finally:
        get_settings.cache_clear()
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))


def test_expired_reservation_does_not_block_clean_roundtrip(monkeypatch) -> None:
    name, url = _with_database(monkeypatch, "meyar_turn85_rt")
    config = _config()
    try:
        command.upgrade(config, NEW_HEAD)
        asyncio.run(
            _execute(
                url,
                [
                    *_conversation_rows(),
                    (
                        "UPDATE agent_conversations SET turn_version = 3, "
                        "active_turn_id = :token, "
                        "active_turn_expires_at = now() - interval '1 minute'",
                        {"token": str(uuid.uuid4())},
                    ),
                ],
            )
        )
        command.downgrade(config, PRIOR_HEAD)
        assert _revision(url) == PRIOR_HEAD
        assert _new_columns(url) == []
        turns = asyncio.run(_query(url, "SELECT turns FROM agent_conversations"))
        assert turns[0][0] == [{"role": "user", "text": "synthetic"}]
        command.upgrade(config, NEW_HEAD)
        assert _revision(url) == NEW_HEAD
        rows = asyncio.run(
            _query(url, "SELECT turn_version, active_turn_id FROM agent_conversations")
        )
        assert [(row[0], row[1]) for row in rows] == [(0, None)]
    finally:
        get_settings.cache_clear()
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))


def test_single_head_chains_through_the_turn_reservation_revision() -> None:
    script = ScriptDirectory.from_config(_config())
    # issue #86 (D-090) chains f3a9c6d2e815 directly on this revision.
    assert script.get_heads() == ["b88a2c4d6e10"]
    assert script.get_revision("f3a9c6d2e815").down_revision == NEW_HEAD
    assert script.get_revision(NEW_HEAD).down_revision == PRIOR_HEAD

"""Alembic proof for issue #86 (revision f3a9c6d2e815 on e5d7a3c91b04):
ResultSet member-snapshot policy + legacy (nullable) corpus fingerprint.

- upgrade backfills every pre-existing ResultSet to ``member-snapshot-v1``
  and keeps its historical fingerprint value untouched (nothing fabricated);
- new rows may carry a NULL fingerprint;
- downgrade refuses, before any DDL, while a NULL-fingerprint row exists;
- a database with only legacy rows round-trips down/up;
- single Alembic head."""

import asyncio
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from conftest import ADMIN_DATABASE_URL
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from meyar.config import get_settings

BASE_URL = ADMIN_DATABASE_URL.rsplit("/", 1)[0]
ADMIN_URL = f"{BASE_URL}/postgres"
PRIOR_HEAD = "e5d7a3c91b04"
NEW_HEAD = "f3a9c6d2e815"

TENANT = "00000000-0000-0000-0000-0000000086a1"
USER = "00000000-0000-0000-0000-0000000086b1"
MEMBERSHIP = "00000000-0000-0000-0000-0000000086c1"
SESSION = "00000000-0000-0000-0000-0000000086d1"
CONVERSATION = "00000000-0000-0000-0000-0000000086e1"
LEGACY_SET = "00000000-0000-0000-0000-0000000086f1"
NEW_SET = "00000000-0000-0000-0000-0000000086f2"
LEGACY_FINGERPRINT = "b" * 64


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


def _base_rows() -> list[tuple[str, dict]]:
    return [
        ("INSERT INTO tenants (id,name,is_active) VALUES (:t,'Migration 86',true)", {"t": TENANT}),
        (
            "INSERT INTO users (id,username,password_hash,is_active) "
            "VALUES (:u,'synthetic-86','x',true)",
            {"u": USER},
        ),
        (
            "INSERT INTO tenant_memberships (id,user_id,tenant_id,role,is_active) "
            "VALUES (:m,:u,:t,'HR_USER',true)",
            {"m": MEMBERSHIP, "u": USER, "t": TENANT},
        ),
        (
            "INSERT INTO browser_sessions "
            "(id,user_id,tenant_membership_id,session_token_hash,csrf_secret,expires_at) "
            "VALUES (:s,:u,:m,repeat('a',64),repeat('b',64),now() + interval '1 day')",
            {"s": SESSION, "u": USER, "m": MEMBERSHIP},
        ),
        (
            "INSERT INTO agent_conversations "
            "(id,tenant_id,owner_user_id,owner_membership_id,title_kind,turns) "
            "VALUES (:c,:t,:u,:m,'GENERAL','[]')",
            {"c": CONVERSATION, "t": TENANT, "u": USER, "m": MEMBERSHIP},
        ),
    ]


def _result_set(result_set_id: str, fingerprint: str | None, *, with_policy: bool):
    columns = (
        "id,tenant_id,browser_session_id,conversation_id,context_epoch,request_sha256,"
        "canonical_search_request,planner_policy_version,planner_prompt_version,"
        "planner_schema_version,planner_model_provider,planner_model_name,"
        "planner_model_revision,search_policy_version,search_mode,result_count,"
        "corpus_fingerprint_sha256,expires_at"
    )
    values = (
        ":id,:t,:s,:c,1,repeat('a',64),'{}','v1','v1','v1','test','test','','v1',"
        "'STRUCTURED_ONLY',0,:fp,now() + interval '1 day'"
    )
    if with_policy:
        columns += ",snapshot_policy_version"
        values += ",'member-snapshot-v1'"
    return (
        f"INSERT INTO agent_result_sets ({columns}) VALUES ({values})",
        {"id": result_set_id, "t": TENANT, "s": SESSION, "c": CONVERSATION, "fp": fingerprint},
    )


def _rows(url: str) -> list:
    return asyncio.run(
        _query(
            url,
            "SELECT id, corpus_fingerprint_sha256, snapshot_policy_version "
            "FROM agent_result_sets ORDER BY id",
        )
    )


def _nullable(url: str) -> str:
    return asyncio.run(
        _query(
            url,
            "SELECT is_nullable FROM information_schema.columns WHERE "
            "table_name='agent_result_sets' AND column_name='corpus_fingerprint_sha256'",
        )
    )[0][0]


def test_upgrade_backfills_policy_and_keeps_legacy_fingerprint(monkeypatch) -> None:
    name, url = _with_database(monkeypatch, "meyar_rs86_up")
    config = _config()
    try:
        command.upgrade(config, PRIOR_HEAD)
        asyncio.run(
            _execute(url, [*_base_rows(), _result_set(LEGACY_SET, LEGACY_FINGERPRINT,
                                                       with_policy=False)])
        )
        command.upgrade(config, NEW_HEAD)
        assert _revision(url) == NEW_HEAD
        assert [(str(r[0]), r[1], r[2]) for r in _rows(url)] == [
            (LEGACY_SET, LEGACY_FINGERPRINT, "member-snapshot-v1")
        ]
        assert _nullable(url) == "YES"
        # New rows may omit the legacy fingerprint.
        asyncio.run(_execute(url, [_result_set(NEW_SET, None, with_policy=True)]))
    finally:
        get_settings.cache_clear()
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))


def test_downgrade_refuses_while_member_snapshot_rows_exist(monkeypatch) -> None:
    name, url = _with_database(monkeypatch, "meyar_rs86_refuse")
    config = _config()
    try:
        command.upgrade(config, NEW_HEAD)
        asyncio.run(
            _execute(
                url,
                [
                    *_base_rows(),
                    _result_set(LEGACY_SET, LEGACY_FINGERPRINT, with_policy=True),
                    _result_set(NEW_SET, None, with_policy=True),
                ],
            )
        )
        with pytest.raises(RuntimeError, match="refusing"):
            command.downgrade(config, PRIOR_HEAD)
        assert _revision(url) == NEW_HEAD
        assert len(_rows(url)) == 2  # nothing lost
        assert _nullable(url) == "YES"
    finally:
        get_settings.cache_clear()
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))


def test_legacy_only_database_round_trips(monkeypatch) -> None:
    name, url = _with_database(monkeypatch, "meyar_rs86_rt")
    config = _config()
    try:
        command.upgrade(config, PRIOR_HEAD)
        asyncio.run(
            _execute(url, [*_base_rows(), _result_set(LEGACY_SET, LEGACY_FINGERPRINT,
                                                       with_policy=False)])
        )
        command.upgrade(config, NEW_HEAD)
        command.downgrade(config, PRIOR_HEAD)
        assert _revision(url) == PRIOR_HEAD
        assert _nullable(url) == "NO"
        rows = asyncio.run(
            _query(url, "SELECT id, corpus_fingerprint_sha256 FROM agent_result_sets")
        )
        assert [(str(r[0]), r[1]) for r in rows] == [(LEGACY_SET, LEGACY_FINGERPRINT)]
        command.upgrade(config, NEW_HEAD)
        assert [r[2] for r in _rows(url)] == ["member-snapshot-v1"]
    finally:
        get_settings.cache_clear()
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))


def test_member_snapshot_revision_is_ancestor_of_single_head() -> None:
    script = ScriptDirectory.from_config(_config())
    assert script.get_heads() == ["b88a2c4d6e10"]
    assert script.get_revision(NEW_HEAD).down_revision == PRIOR_HEAD


def _revision(url: str) -> str:
    return asyncio.run(_query(url, "SELECT version_num FROM alembic_version"))[0][0]

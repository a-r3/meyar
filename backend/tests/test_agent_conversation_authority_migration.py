"""Alembic proof for issue #80 PR80-1 (revision b7e3c9d41f28 on
543c60f7efc5): durable owner backfill, one session context per migrated
conversation, ResultSet conversation binding, removal of the old live
columns, invalidated pending-draft authority, constraints, fail-closed
integrity abort, lossless downgrade/re-upgrade of 1:1-representable state,
fail-closed downgrade of post-#80 multi-conversation/multi-session state,
fresh install, and a single head."""

import asyncio
import json
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
PRIOR_HEAD = "543c60f7efc5"
NEW_HEAD = "b7e3c9d41f28"

TENANT = "00000000-0000-0000-0000-0000000080a1"
OTHER_TENANT = "00000000-0000-0000-0000-0000000080a2"
USER = "00000000-0000-0000-0000-0000000080b1"
MEMBERSHIP = "00000000-0000-0000-0000-0000000080c1"
SESSION_1 = "00000000-0000-0000-0000-0000000080d1"
SESSION_2 = "00000000-0000-0000-0000-0000000080d2"
CONVERSATION_1 = "00000000-0000-0000-0000-0000000080e1"
CONVERSATION_2 = "00000000-0000-0000-0000-0000000080e2"
ACTIVE_SET = "00000000-0000-0000-0000-0000000080f1"
OLD_SET = "00000000-0000-0000-0000-0000000080f2"
DRAFT_ID = "00000000-0000-0000-0000-000000008099"

TURNS_1 = [
    {"role": "user", "text": "Synthetic vacancy text"},
    {
        "role": "assistant",
        "text": "Qaralama",
        "outcome": "ANSWERED_FROM_TOOL_RESULT",
        "pending_job_draft": {"draft_id": DRAFT_ID, "title": "Synthetic"},
    },
]


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


def _result_set_insert(result_set_id: str, session_id: str, epoch: int) -> tuple[str, dict]:
    return (
        "INSERT INTO agent_result_sets "
        "(id,tenant_id,browser_session_id,context_epoch,request_sha256,"
        "canonical_search_request,planner_policy_version,planner_prompt_version,"
        "planner_schema_version,planner_model_provider,planner_model_name,"
        "planner_model_revision,search_policy_version,search_mode,result_count,"
        "corpus_fingerprint_sha256,expires_at) VALUES "
        "(:id,:tenant,:session,:epoch,repeat('a',64),'{}','v1','v1','v1','test','test',"
        "'','v1','STRUCTURED_ONLY',0,repeat('b',64),now() + interval '1 day')",
        {"id": result_set_id, "tenant": TENANT, "session": session_id, "epoch": epoch},
    )


def _identity_rows(membership_tenant: str = TENANT) -> list[tuple[str, dict]]:
    return [
        (
            "INSERT INTO tenants (id,name,is_active) VALUES (:t,'Migration 80',true),"
            "(:o,'Migration 80 other',true)",
            {"t": TENANT, "o": OTHER_TENANT},
        ),
        (
            "INSERT INTO users (id,username,password_hash,is_active) VALUES "
            "(:u,'migration-80','hash',true)",
            {"u": USER},
        ),
        (
            "INSERT INTO tenant_memberships (id,user_id,tenant_id,role,is_active) VALUES "
            "(:m,:u,:t,'HR_USER',true)",
            {"m": MEMBERSHIP, "u": USER, "t": membership_tenant},
        ),
        *[
            (
                "INSERT INTO browser_sessions "
                "(id,user_id,tenant_membership_id,session_token_hash,csrf_secret,expires_at) "
                "VALUES (:s,:u,:m,repeat(:c,64),repeat('b',64),now() + interval '1 day')",
                {"s": session_id, "u": USER, "m": MEMBERSHIP, "c": char},
            )
            for session_id, char in ((SESSION_1, "a"), (SESSION_2, "c"))
        ],
    ]


def _legacy_rows() -> list[tuple[str, dict]]:
    return [
        *_identity_rows(),
        _result_set_insert(OLD_SET, SESSION_1, 2),
        _result_set_insert(ACTIVE_SET, SESSION_1, 3),
        (
            "INSERT INTO agent_conversations "
            "(id,tenant_id,browser_session_id,turns,context_epoch,active_result_set_id) "
            "VALUES (:c,:t,:s,CAST(:turns AS json),3,:rs)",
            {
                "c": CONVERSATION_1,
                "t": TENANT,
                "s": SESSION_1,
                "turns": json.dumps(TURNS_1),
                "rs": ACTIVE_SET,
            },
        ),
        (
            "INSERT INTO agent_conversations (id,tenant_id,browser_session_id,turns) "
            "VALUES (:c,:t,:s,'[]')",
            {"c": CONVERSATION_2, "t": TENANT, "s": SESSION_2},
        ),
    ]


async def _query(url: str, sql: str, params: dict | None = None) -> list:
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        rows = (await connection.execute(text(sql), params or {})).all()
    await engine.dispose()
    return rows


async def _assert_upgraded(url: str) -> None:
    conversation_columns = {
        row[0]
        for row in await _query(
            url,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='agent_conversations'",
        )
    }
    assert {"owner_user_id", "owner_membership_id", "title_kind", "turns"} <= conversation_columns
    assert {"browser_session_id", "context_epoch", "active_result_set_id"}.isdisjoint(
        conversation_columns
    )
    conversations = {
        str(row.id): row
        for row in await _query(
            url,
            "SELECT id, owner_user_id, owner_membership_id, title_kind, turns "
            "FROM agent_conversations",
        )
    }
    assert set(conversations) == {CONVERSATION_1, CONVERSATION_2}
    for row in conversations.values():
        assert (str(row.owner_user_id), str(row.owner_membership_id)) == (USER, MEMBERSHIP)
    assert conversations[CONVERSATION_1].title_kind == "GENERAL"
    assert conversations[CONVERSATION_2].title_kind == "NEW"
    assert conversations[CONVERSATION_1].turns == TURNS_1  # transcript survives verbatim

    contexts = {
        str(row.conversation_id): row
        for row in await _query(
            url,
            "SELECT conversation_id, tenant_id, browser_session_id, context_epoch, "
            "active_result_set_id, active_pending_draft_id "
            "FROM agent_conversation_session_contexts",
        )
    }
    assert set(contexts) == {CONVERSATION_1, CONVERSATION_2}
    first, second = contexts[CONVERSATION_1], contexts[CONVERSATION_2]
    assert (str(first.browser_session_id), first.context_epoch) == (SESSION_1, 3)
    assert str(first.active_result_set_id) == ACTIVE_SET  # only its ORIGINAL session
    assert (str(second.browser_session_id), second.context_epoch) == (SESSION_2, 1)
    assert second.active_result_set_id is None
    # Pending-draft authority intentionally invalidated by the migration.
    assert first.active_pending_draft_id is None and second.active_pending_draft_id is None
    assert {str(row.tenant_id) for row in contexts.values()} == {TENANT}

    result_sets = {
        str(row.id): str(row.conversation_id)
        for row in await _query(url, "SELECT id, conversation_id FROM agent_result_sets")
    }
    assert result_sets == {ACTIVE_SET: CONVERSATION_1, OLD_SET: CONVERSATION_1}
    nullable = {
        (row.table_name, row.column_name): row.is_nullable
        for row in await _query(
            url,
            "SELECT table_name, column_name, is_nullable FROM information_schema.columns "
            "WHERE (table_name='agent_result_sets' AND column_name='conversation_id') OR "
            "(table_name='agent_conversations' AND column_name IN "
            "('owner_user_id','owner_membership_id','title_kind'))",
        )
    }
    assert set(nullable.values()) == {"NO"} and len(nullable) == 4

    constraints = {
        row[0]
        for row in await _query(
            url,
            "SELECT conname FROM pg_constraint WHERE conrelid IN ("
            "'agent_conversations'::regclass, 'agent_conversation_session_contexts'::regclass,"
            "'agent_result_sets'::regclass)",
        )
    }
    assert {
        "uq_agent_conversation_session_context",
        "ck_agent_conversation_context_epoch",
        "ck_agent_conversation_title_kind",
        "fk_agent_conversations_owner_user",
        "fk_agent_conversations_owner_membership",
        "fk_agent_result_sets_conversation",
    } <= constraints
    delete_rules = {
        row.constraint_name: row.delete_rule
        for row in await _query(
            url,
            "SELECT constraint_name, delete_rule FROM information_schema.referential_constraints "
            "WHERE constraint_name IN ('fk_agent_conversations_owner_user',"
            "'fk_agent_conversations_owner_membership','fk_agent_result_sets_conversation')",
        )
    }
    assert delete_rules == {
        "fk_agent_conversations_owner_user": "NO ACTION",
        "fk_agent_conversations_owner_membership": "NO ACTION",
        "fk_agent_result_sets_conversation": "CASCADE",
    }
    # No FK from the durable conversation to browser_sessions remains.
    session_fks = await _query(
        url,
        "SELECT conname FROM pg_constraint WHERE conrelid='agent_conversations'::regclass "
        "AND confrelid='browser_sessions'::regclass",
    )
    assert session_fks == []
    revision = await _query(url, "SELECT version_num FROM alembic_version")
    assert revision[0][0] == NEW_HEAD


async def _assert_downgraded(url: str) -> None:
    rows = {
        str(row.id): row
        for row in await _query(
            url,
            "SELECT id, browser_session_id, context_epoch, active_result_set_id "
            "FROM agent_conversations",
        )
    }
    assert (str(rows[CONVERSATION_1].browser_session_id), rows[CONVERSATION_1].context_epoch) == (
        SESSION_1,
        3,
    )
    assert str(rows[CONVERSATION_1].active_result_set_id) == ACTIVE_SET
    assert str(rows[CONVERSATION_2].browser_session_id) == SESSION_2
    revision = await _query(url, "SELECT version_num FROM alembic_version")
    assert revision[0][0] == PRIOR_HEAD


def _with_database(monkeypatch, prefix: str):
    name = f"{prefix}_{uuid.uuid4().hex}"
    url = f"{BASE_URL}/{name}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    monkeypatch.setenv("MEYAR_DATABASE_URL", url)
    get_settings.cache_clear()
    asyncio.run(_execute(url, [("CREATE EXTENSION IF NOT EXISTS vector", {})]))
    return name, url


def test_upgrade_backfills_downgrades_and_reupgrades(monkeypatch) -> None:
    name, url = _with_database(monkeypatch, "meyar_conv80_rt")
    config = _config()
    try:
        command.upgrade(config, PRIOR_HEAD)
        asyncio.run(_execute(url, _legacy_rows()))
        command.upgrade(config, NEW_HEAD)
        asyncio.run(_assert_upgraded(url))
        command.downgrade(config, PRIOR_HEAD)
        asyncio.run(_assert_downgraded(url))
        command.upgrade(config, NEW_HEAD)
        asyncio.run(_assert_upgraded(url))
    finally:
        get_settings.cache_clear()
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))


def test_upgrade_refuses_to_fabricate_ownership_across_tenants(monkeypatch) -> None:
    """A conversation whose session's membership belongs to ANOTHER tenant
    cannot be attributed — the migration aborts rather than guessing."""
    name, url = _with_database(monkeypatch, "meyar_conv80_abort")
    config = _config()
    try:
        command.upgrade(config, PRIOR_HEAD)
        asyncio.run(
            _execute(
                url,
                [
                    *_identity_rows(membership_tenant=OTHER_TENANT),
                    (
                        "INSERT INTO agent_conversations (id,tenant_id,browser_session_id,turns) "
                        "VALUES (:c,:t,:s,'[]')",
                        {"c": CONVERSATION_1, "t": TENANT, "s": SESSION_1},
                    ),
                ],
            )
        )
        with pytest.raises(RuntimeError, match="owner could not be derived"):
            command.upgrade(config, NEW_HEAD)
        revision = asyncio.run(_query(url, "SELECT version_num FROM alembic_version"))
        assert revision[0][0] == PRIOR_HEAD
        columns = asyncio.run(
            _query(
                url,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='agent_conversations'",
            )
        )
        assert "owner_user_id" not in {row[0] for row in columns}
    finally:
        get_settings.cache_clear()
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))


RESULT_SET_A = "00000000-0000-0000-0000-0000000080f3"
RESULT_SET_B = "00000000-0000-0000-0000-0000000080f4"
TURNS_B = [{"role": "user", "text": "Synthetic second conversation"}]


def _new_conversation(conversation_id: str, turns: list) -> tuple[str, dict]:
    return (
        "INSERT INTO agent_conversations "
        "(id,tenant_id,owner_user_id,owner_membership_id,turns,title_kind) "
        "VALUES (:c,:t,:u,:m,CAST(:turns AS json),'GENERAL')",
        {"c": conversation_id, "t": TENANT, "u": USER, "m": MEMBERSHIP, "turns": json.dumps(turns)},
    )


def _new_context(conversation_id: str, session_id: str, active: str | None) -> tuple[str, dict]:
    return (
        "INSERT INTO agent_conversation_session_contexts "
        "(id,tenant_id,conversation_id,browser_session_id,context_epoch,active_result_set_id) "
        "VALUES (gen_random_uuid(),:t,:c,:s,1,:rs)",
        {"t": TENANT, "c": conversation_id, "s": session_id, "rs": active},
    )


def _new_result_set(result_set_id: str, session_id: str, conversation_id: str) -> tuple[str, dict]:
    statement, params = _result_set_insert(result_set_id, session_id, 1)
    statement = statement.replace("(id,tenant_id,", "(id,conversation_id,tenant_id,").replace(
        "(:id,:tenant,", "(:id,:conversation,:tenant,"
    )
    return statement, {**params, "conversation": conversation_id}


# Real post-#80 states the previous 1:1 schema cannot represent.
_UNREPRESENTABLE = {
    "two_conversations_one_session": (
        [
            _new_conversation(CONVERSATION_1, TURNS_1),
            _new_conversation(CONVERSATION_2, TURNS_B),
            _new_result_set(RESULT_SET_A, SESSION_1, CONVERSATION_1),
            _new_result_set(RESULT_SET_B, SESSION_1, CONVERSATION_2),
            _new_context(CONVERSATION_1, SESSION_1, RESULT_SET_A),
            _new_context(CONVERSATION_2, SESSION_1, RESULT_SET_B),
        ],
        "BrowserSessions holding multiple conversations",
        {RESULT_SET_A: CONVERSATION_1, RESULT_SET_B: CONVERSATION_2},
        {CONVERSATION_1: TURNS_1, CONVERSATION_2: TURNS_B},
        2,
    ),
    "one_conversation_two_sessions": (
        [
            _new_conversation(CONVERSATION_1, TURNS_1),
            _new_result_set(RESULT_SET_A, SESSION_1, CONVERSATION_1),
            _new_result_set(RESULT_SET_B, SESSION_2, CONVERSATION_1),
            _new_context(CONVERSATION_1, SESSION_1, RESULT_SET_A),
            _new_context(CONVERSATION_1, SESSION_2, RESULT_SET_B),
        ],
        "without exactly one session context",
        {RESULT_SET_A: CONVERSATION_1, RESULT_SET_B: CONVERSATION_1},
        {CONVERSATION_1: TURNS_1},
        2,
    ),
    "conversation_without_context": (
        [_new_conversation(CONVERSATION_1, TURNS_1)],
        "without exactly one session context",
        {},
        {CONVERSATION_1: TURNS_1},
        0,
    ),
}


@pytest.mark.parametrize("scenario", sorted(_UNREPRESENTABLE))
def test_downgrade_fails_closed_on_unrepresentable_state(monkeypatch, scenario: str) -> None:
    """Downgrade never picks a winner, deletes history, or rebinds a
    ResultSet: non-1:1 state aborts before DDL and stays on NEW_HEAD."""
    rows, message, result_sets, transcripts, context_count = _UNREPRESENTABLE[scenario]
    name, url = _with_database(monkeypatch, "meyar_conv80_down")
    config = _config()
    try:
        command.upgrade(config, NEW_HEAD)
        asyncio.run(_execute(url, [*_identity_rows(), *rows]))
        with pytest.raises(RuntimeError, match=message):
            command.downgrade(config, PRIOR_HEAD)

        revision = asyncio.run(_query(url, "SELECT version_num FROM alembic_version"))
        assert revision[0][0] == NEW_HEAD
        conversations = {
            str(row.id): row.turns
            for row in asyncio.run(_query(url, "SELECT id, turns FROM agent_conversations"))
        }
        assert conversations == transcripts  # every transcript survives verbatim
        bindings = {
            str(row.id): str(row.conversation_id)
            for row in asyncio.run(
                _query(url, "SELECT id, conversation_id FROM agent_result_sets")
            )
        }
        assert bindings == result_sets  # no ResultSet deleted or rebound
        contexts = asyncio.run(
            _query(url, "SELECT count(*) FROM agent_conversation_session_contexts")
        )
        assert contexts[0][0] == context_count
        columns = {
            row[0]
            for row in asyncio.run(
                _query(
                    url,
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='agent_conversations'",
                )
            )
        }
        assert "browser_session_id" not in columns and "owner_user_id" in columns
    finally:
        get_settings.cache_clear()
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))


def test_fresh_install_reaches_single_head(monkeypatch) -> None:
    name, url = _with_database(monkeypatch, "meyar_conv80_fresh")
    config = _config()
    try:
        command.upgrade(config, "head")
        revision = asyncio.run(_query(url, "SELECT version_num FROM alembic_version"))
        assert revision[0][0] == NEW_HEAD
        tables = asyncio.run(
            _query(url, "SELECT to_regclass('agent_conversation_session_contexts')")
        )
        assert tables[0][0] is not None
    finally:
        get_settings.cache_clear()
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))
    assert ScriptDirectory.from_config(config).get_heads() == [NEW_HEAD]

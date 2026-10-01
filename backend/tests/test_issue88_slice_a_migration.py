"""Issue #88 slice A — fresh upgrade from a87d4c6e2b19, constraint shape,
representable fail-closed downgrade and re-upgrade (synthetic rows only)."""

import asyncio
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from conftest import ADMIN_DATABASE_URL
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from meyar.config import get_settings

BASE_URL = ADMIN_DATABASE_URL.rsplit("/", 1)[0]
ADMIN_URL = f"{BASE_URL}/postgres"
PRIOR_HEAD = "a87d4c6e2b19"
NEW_HEAD = "b88a2c4d6e10"


async def _admin(statement: str, *, name: str) -> None:
    engine = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    async with engine.connect() as connection:
        if statement.startswith("DROP"):
            await connection.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:name"),
                {"name": name},
            )
        await connection.execute(text(statement))
    await engine.dispose()


async def _run(url: str, statement: str, params: dict | None = None) -> list:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(statement), params or {})
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


def _sql(url: str, statement: str, **params) -> list:  # noqa: ANN003
    return asyncio.run(_run(url, statement, params))


TASK_SQL = (
    "INSERT INTO agent_tasks (id,tenant_id,conversation_id,session_context_id,owner_user_id,"
    "owner_membership_id,task_type,status,phase,state_schema_version,state,pending_draft_id,"
    "policy_version,created_by_submission_id,expires_at,terminal_at) VALUES "
    "(:id,:tenant,:conversation,:context,:user,:member,:task_type,:status,:phase,1,'{}',"
    ":draft,'agent-core-v2-task-v1',:submission,now()+interval '1 hour',:terminal)"
)
CLARIFICATION_SQL = (
    "INSERT INTO agent_clarifications (id,tenant_id,conversation_id,session_context_id,task_id,"
    "context_epoch,clarification_type,answer_schema_version,question_turn_id,"
    "created_from_turn_id,superseded_reason,source_turn_id,source_sha256,source_start,"
    "source_end,semantic_policy_version,routing_policy_version,created_turn_version,status,"
    "attempt,expires_at,created_by_submission_id) VALUES "
    "(:id,:tenant,:conversation,:context,:task,1,:type,'clarification-answers-v1',:question,"
    ":created_from,:reason,:source,:sha,:start,:end,'jd-semantic-policy-v2',"
    "'agent-entry-routing-v4',2,:status,:attempt,now()+interval '30 minutes',:submission)"
)


def test_issue88_slice_a_upgrade_constraints_downgrade_and_single_head(monkeypatch) -> None:
    name = f"meyar_issue88a_{uuid.uuid4().hex}"
    url = f"{BASE_URL}/{name}"
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    assert ScriptDirectory.from_config(config).get_heads() == [NEW_HEAD]
    asyncio.run(_admin(f'CREATE DATABASE "{name}"', name=name))
    monkeypatch.setenv("MEYAR_DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        _sql(url, "CREATE EXTENSION IF NOT EXISTS vector")
        command.upgrade(config, PRIOR_HEAD)
        tenant, user, member, session, conversation, context = (uuid.uuid4() for _ in range(6))
        draft = uuid.uuid4()
        _sql(url, "INSERT INTO tenants (id,name,is_active) VALUES (:id,'Synthetic 88A',true)",
             id=tenant)
        _sql(url, "INSERT INTO users (id,username,password_hash,is_active,security_version) "
             "VALUES (:id,'synthetic-88a','argon2-placeholder',true,gen_random_uuid())", id=user)
        _sql(url, "INSERT INTO tenant_memberships (id,user_id,tenant_id,role,is_active,"
             "security_version) VALUES (:id,:user,:tenant,'HR_USER',true,gen_random_uuid())",
             id=member, user=user, tenant=tenant)
        _sql(url, "INSERT INTO browser_sessions (id,user_id,tenant_membership_id,"
             "session_token_hash,csrf_secret,expires_at) VALUES "
             "(:id,:user,:member,:hash,:csrf,now()+interval '1 hour')",
             id=session, user=user, member=member, hash="c" * 64, csrf="d" * 64)
        legacy_turns = '[{"role":"user","text":"Python mütləqdir."}]'
        _sql(url, "INSERT INTO agent_conversations (id,tenant_id,owner_user_id,"
             "owner_membership_id,turns) VALUES (:id,:tenant,:user,:member,"
             "CAST(:turns AS json))",
             id=conversation, tenant=tenant, user=user, member=member, turns=legacy_turns)
        _sql(url, "INSERT INTO agent_conversation_session_contexts (id,tenant_id,"
             "conversation_id,browser_session_id,context_epoch,active_pending_draft_id) "
             "VALUES (:id,:tenant,:conversation,:session,1,:draft)",
             id=context, tenant=tenant, conversation=conversation, session=session, draft=draft)

        command.upgrade(config, NEW_HEAD)
        # Additive only: nothing fabricated for legacy state.
        assert _sql(url, "SELECT active_clarification_id, active_pending_draft_id FROM "
                    "agent_conversation_session_contexts")[0] == (None, draft)
        assert _sql(url, "SELECT count(*) FROM agent_tasks")[0][0] == 0
        assert _sql(url, "SELECT count(*) FROM agent_clarifications")[0][0] == 0
        assert _sql(url, "SELECT turns FROM agent_conversations")[0][0] == [
            {"role": "user", "text": "Python mütləqdir."}
        ]

        base = {"tenant": tenant, "conversation": conversation, "context": context,
                "user": user, "member": member}
        task = uuid.uuid4()
        _sql(url, TASK_SQL, id=task, task_type="UNDETERMINED", status="WAITING_CLARIFICATION",
             phase="NEEDS_INTENT_CHOICE", draft=None, submission=uuid.uuid4(), terminal=None,
             **base)
        source = uuid.uuid4()
        good = {"task": task, "type": "SEARCH_OR_VACANCY", "question": uuid.uuid4(),
                "created_from": source, "reason": None, "source": source, "sha": "a" * 64,
                "start": 0, "end": 17, "status": "OPEN", "attempt": 1, **base}
        clarification = uuid.uuid4()
        _sql(url, CLARIFICATION_SQL, id=clarification, submission=uuid.uuid4(), **good)
        _sql(url, "UPDATE agent_conversation_session_contexts SET active_clarification_id=:c",
             c=clarification)

        rejected_clarifications = [
            {"status": "SUPERSEDED", "reason": "NOT_ACTIVE"},  # A2: not a transition
            {"status": "SUPERSEDED", "reason": None},
            {"status": "OPEN", "reason": "UNCLEAR"},
            {"attempt": 3},
            {"source": None},  # SEARCH_OR_VACANCY requires its binding
            {"type": "VACANCY_SOURCE_REQUIRED"},  # ... and VSR forbids one
            {"start": 5, "end": 5},
            {"end": 4001},
            {"created_from": uuid.uuid4()},  # attempt 1: created_from = source
            {"status": "OPEN"},  # second OPEN in the same context
        ]
        for change in rejected_clarifications:
            with pytest.raises(DBAPIError):
                _sql(url, CLARIFICATION_SQL, id=uuid.uuid4(), submission=uuid.uuid4(),
                     **{**good, **change})
        rejected_tasks = [
            {"status": "WAITING_CLARIFICATION"},  # one waiting dialogue task per context
            {"status": "COMPLETED", "terminal": None},
            {"status": "WAITING_CONFIRMATION", "draft": None},
            {"status": "UNKNOWN"},
            {"task_type": "UNDETERMINED", "status": "WAITING_CONFIRMATION", "draft": draft},
        ]
        for change in rejected_tasks:
            values = {"task_type": "VACANCY_ANALYSIS", "status": "WAITING_CLARIFICATION",
                      "phase": "NEEDS_SOURCE", "draft": None, "terminal": None, **change}
            with pytest.raises(DBAPIError):
                _sql(url, TASK_SQL, id=uuid.uuid4(), submission=uuid.uuid4(), **base, **values)
        # Lanes coexist: one WAITING_CONFIRMATION task beside the dialogue lane.
        _sql(url, TASK_SQL, id=uuid.uuid4(), task_type="VACANCY_ANALYSIS",
             status="WAITING_CONFIRMATION", phase="DRAFT_REVIEW", draft=draft,
             submission=uuid.uuid4(), terminal=None, **base)

        command.downgrade(config, PRIOR_HEAD)
        assert _sql(url, "SELECT to_regclass('agent_tasks'), "
                    "to_regclass('agent_clarifications')")[0] == (None, None)
        columns = {row[0] for row in _sql(
            url, "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='agent_conversation_session_contexts'")}
        assert "active_clarification_id" not in columns
        # Lane-B authority (and therefore confirmation) survives the downgrade.
        assert _sql(url, "SELECT active_pending_draft_id FROM "
                    "agent_conversation_session_contexts")[0][0] == draft
        command.upgrade(config, NEW_HEAD)
        assert _sql(url, "SELECT version_num FROM alembic_version")[0][0] == NEW_HEAD
    finally:
        get_settings.cache_clear()
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"', name=name))

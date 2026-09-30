"""Fresh #87 upgrade and fail-closed downgrade policy on synthetic rows."""

import asyncio
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from conftest import ADMIN_DATABASE_URL
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from meyar.config import get_settings

BASE_URL = ADMIN_DATABASE_URL.rsplit("/", 1)[0]
ADMIN_URL = f"{BASE_URL}/postgres"
PRIOR_HEAD = "f3a9c6d2e815"
NEW_HEAD = "a87d4c6e2b19"


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
    async with engine.begin() as connection:
        result = await connection.execute(text(statement), params or {})
        rows = list(result.all()) if result.returns_rows else []
    await engine.dispose()
    return rows


def test_issue87_fresh_upgrade_and_downgrade_policy(monkeypatch) -> None:
    name = f"meyar_issue87_{uuid.uuid4().hex}"
    url = f"{BASE_URL}/{name}"
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    asyncio.run(_admin(f'CREATE DATABASE "{name}"', name=name))
    monkeypatch.setenv("MEYAR_DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        asyncio.run(_run(url, "CREATE EXTENSION IF NOT EXISTS vector"))
        command.upgrade(config, PRIOR_HEAD)
        tenant_id, user_id, membership_id, session_id = (uuid.uuid4() for _ in range(4))
        asyncio.run(_run(
            url,
            "INSERT INTO tenants (id,name,is_active) VALUES (:id,'Synthetic 87',true)",
            {"id": tenant_id},
        ))
        asyncio.run(_run(
            url,
            "INSERT INTO users (id,username,password_hash,is_active) "
            "VALUES (:id,'synthetic-87','argon2-placeholder',true)",
            {"id": user_id},
        ))
        asyncio.run(_run(
            url,
            "INSERT INTO tenant_memberships (id,user_id,tenant_id,role,is_active) "
            "VALUES (:id,:user,:tenant,'HR_USER',true)",
            {"id": membership_id, "user": user_id, "tenant": tenant_id},
        ))
        asyncio.run(_run(
            url,
            "INSERT INTO browser_sessions "
            "(id,user_id,tenant_membership_id,session_token_hash,csrf_secret,expires_at) "
            "VALUES (:id,:user,:member,:hash,:csrf,now()+interval '1 hour')",
            {"id": session_id, "user": user_id, "member": membership_id,
             "hash": "a" * 64, "csrf": "b" * 64},
        ))
        command.upgrade(config, NEW_HEAD)
        rows = asyncio.run(_run(
            url,
            "SELECT u.security_version, m.security_version, s.revoked_at "
            "FROM users u JOIN tenant_memberships m ON m.user_id=u.id "
            "JOIN browser_sessions s ON s.user_id=u.id",
        ))
        assert len(rows) == 1 and rows[0][0] and rows[0][1]
        assert rows[0][2] is None  # no fabricated credential change
        assert asyncio.run(_run(url, "SELECT count(*) FROM agent_turn_submissions"))[0][0] == 0
        assert asyncio.run(_run(url, "SELECT count(*) FROM auth_security_events"))[0][0] == 0
        asyncio.run(_run(
            url,
            "INSERT INTO auth_security_events (id,outcome_code) VALUES (:id,'LOGIN_REJECTED')",
            {"id": uuid.uuid4()},
        ))
        with pytest.raises(RuntimeError, match="durable records"):
            command.downgrade(config, PRIOR_HEAD)
        assert asyncio.run(_run(url, "SELECT version_num FROM alembic_version"))[0][0] == NEW_HEAD
        asyncio.run(_run(url, "DELETE FROM auth_security_events"))
        conversation_id = uuid.uuid4()
        asyncio.run(_run(
            url,
            "INSERT INTO agent_conversations "
            "(id,tenant_id,owner_user_id,owner_membership_id,turns) "
            "VALUES (:id,:tenant,:user,:member,'[]')",
            {"id": conversation_id, "tenant": tenant_id, "user": user_id,
             "member": membership_id},
        ))
        asyncio.run(_run(
            url,
            "INSERT INTO agent_turn_submissions "
            "(id,tenant_id,user_id,membership_id,browser_session_id,conversation_id,"
            "context_epoch,status,expires_at) "
            "VALUES (:id,:tenant,:user,:member,:session,:conversation,1,'ISSUED',"
            "now()+interval '1 hour')",
            {"id": uuid.uuid4(), "tenant": tenant_id, "user": user_id,
             "member": membership_id, "session": session_id,
             "conversation": conversation_id},
        ))
        with pytest.raises(RuntimeError, match="agent_turn_submissions contains durable records"):
            command.downgrade(config, PRIOR_HEAD)
        asyncio.run(_run(url, "DELETE FROM agent_turn_submissions"))
        command.downgrade(config, PRIOR_HEAD)
        assert asyncio.run(_run(
            url, "SELECT revoked_at FROM browser_sessions WHERE id=:id", {"id": session_id}
        ))[0][0] is not None
    finally:
        get_settings.cache_clear()
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"', name=name))

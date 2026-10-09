"""#46 L-6 forward upgrade preserves existing confirmation identity (real PG)."""

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
HEAD = "c46d7e8f9012"
PRIOR = "b88a2c4d6e10"


async def execute(url, statement, params=None, *, autocommit=False):
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT" if autocommit else None)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(statement), params or {})
            return result.all() if result.returns_rows else []
    finally:
        await engine.dispose()


def sql(url, statement, params=None, **kwargs):
    return asyncio.run(execute(url, statement, params, **kwargs))


@pytest.mark.parametrize("existing", [False, True])
def test_fresh_and_previous_schema_upgrade_preserves_confirmation(monkeypatch, existing):
    name = f"meyar_46_confirmation_{uuid.uuid4().hex}"
    url = f"{BASE_URL}/{name}"
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    assert ScriptDirectory.from_config(config).get_heads() == [HEAD]
    sql(f"{BASE_URL}/postgres", f'CREATE DATABASE "{name}"', autocommit=True)
    monkeypatch.setenv("MEYAR_DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        if existing:
            command.upgrade(config, PRIOR)
            ids = {key: uuid.uuid4() for key in ("t", "u", "m", "s", "j", "v", "c", "d")}
            ids["provenance"] = '{"synthetic": true}'
            for statement in (
                "INSERT INTO tenants (id,name,is_active) VALUES (:t,'Synthetic migration',true)",
                "INSERT INTO users (id,username,password_hash,is_active,security_version) "
                "VALUES (:u,'synthetic-migration','placeholder',true,gen_random_uuid())",
                "INSERT INTO tenant_memberships "
                "(id,user_id,tenant_id,role,is_active,security_version) "
                "VALUES (:m,:u,:t,'HR_USER',true,gen_random_uuid())",
                "INSERT INTO browser_sessions (id,user_id,tenant_membership_id,session_token_hash,"
                "csrf_secret,expires_at) VALUES (:s,:u,:m,repeat('a',64),repeat('b',64),"
                "now()-interval '90 days')",
                "INSERT INTO jobs (id,tenant_id,title) VALUES (:j,:t,'Synthetic vacancy')",
                "INSERT INTO job_criteria_versions (id,tenant_id,job_id,version_number,criteria,"
                "unsupported_requirements,needs_review_requirements,result_limit,eligible_only,"
                "agent_semantic_provenance) VALUES (:v,:t,:j,1,'[]','[]','[]',20,false,"
                "CAST(:provenance AS json))",
                "INSERT INTO agent_draft_confirmations (id,tenant_id,draft_id,browser_session_id,"
                "job_id,criteria_version_id,status) VALUES (:c,:t,:d,:s,:j,:v,'CONFIRMED')",
            ):
                sql(url, statement, ids)
            before = sql(
                url,
                "SELECT id,tenant_id,draft_id,browser_session_id,job_id,"
                "criteria_version_id,status,confirmed_at FROM agent_draft_confirmations",
            )
        command.upgrade(config, "head")
        assert sql(url, "SELECT version_num FROM alembic_version") == [(HEAD,)]
        command.check(config)
        if existing:
            assert (
                sql(
                    url,
                    "SELECT id,tenant_id,draft_id,browser_session_id,job_id,"
                    "criteria_version_id,status,confirmed_at FROM agent_draft_confirmations",
                )
                == before
            )
            assert sql(
                url, "SELECT historical_browser_session_id FROM agent_draft_confirmations"
            ) == [(ids["s"],)]
            with pytest.raises(IntegrityError):
                sql(
                    url,
                    "UPDATE agent_draft_confirmations SET historical_browser_session_id=:bad",
                    {"bad": uuid.uuid4()},
                )
            # Attached state can downgrade/re-upgrade without losing provenance.
            command.downgrade(config, PRIOR)
            command.upgrade(config, HEAD)
            sql(url, "DELETE FROM browser_sessions WHERE id=:s", ids)
            assert sql(
                url,
                "SELECT browser_session_id,historical_browser_session_id,job_id,"
                "criteria_version_id FROM agent_draft_confirmations",
            ) == [(None, ids["s"], ids["j"], ids["v"])]
            assert sql(url, "SELECT agent_semantic_provenance FROM job_criteria_versions") == [
                ({"synthetic": True},)
            ]
            with pytest.raises(RuntimeError, match="detached confirmation provenance"):
                command.downgrade(config, PRIOR)
            assert sql(url, "SELECT version_num FROM alembic_version") == [(HEAD,)]
            command.check(config)
    finally:
        get_settings.cache_clear()
        sql(f"{BASE_URL}/postgres", f'DROP DATABASE "{name}"', autocommit=True)

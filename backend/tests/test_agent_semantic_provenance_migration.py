"""Alembic proof for issue #84 (revision c84a5e2f9d17 on b7e3c9d41f28):
one nullable ``job_criteria_versions.agent_semantic_provenance`` JSON column.

- upgrade leaves every pre-existing version NULL (provenance is never
  fabricated for manual/API/legacy versions);
- downgrade refuses, before any DDL, while any row carries provenance, and
  leaves both data and schema intact;
- a clean no-provenance database round-trips down/up;
- the migration graph has a single head."""

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
PRIOR_HEAD = "b7e3c9d41f28"
NEW_HEAD = "c84a5e2f9d17"

TENANT = "00000000-0000-0000-0000-0000000084a1"
JOB = "00000000-0000-0000-0000-0000000084b1"
LEGACY_VERSION = "00000000-0000-0000-0000-0000000084c1"
AGENT_VERSION = "00000000-0000-0000-0000-0000000084c2"
CRITERIA = [
    {
        "id": "python",
        "kind": "SKILL",
        "type": "MUST_HAVE",
        "label": "Python",
        "value": "Python",
    }
]
# Synthetic, schema-shaped provenance: exact fragment + digest, no JD text.
PROVENANCE = {
    "schema_version": "jd-semantic-provenance-v2",
    "draft_id": "00000000-0000-0000-0000-0000000084d1",
    "source_sha256": "a" * 64,
    "semantic_policy_version": "jd-semantic-policy-v2",
    "prompt_version": None,
    "model": None,
    "rejected_proposal_count": 0,
    "criteria": [
        {
            "criterion_id": "python",
            "source_spans": [
                {
                    "span_id": "req-0001",
                    "start_offset": 0,
                    "end_offset": 15,
                    "source_text": "Python required",
                    "interpretation_source": "DETERMINISTIC",
                }
            ],
            "origin": {"criterion_type": "MUST_HAVE", "min_years": None, "required_level": None},
            "final": {"criterion_type": "MUST_HAVE", "min_years": None, "required_level": None},
        }
    ],
    "review_decisions": [],
    "conflict_resolutions": [],
    "amendments": [],
}


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


def _with_database(monkeypatch, prefix: str):
    name = f"{prefix}_{uuid.uuid4().hex}"
    url = f"{BASE_URL}/{name}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    monkeypatch.setenv("MEYAR_DATABASE_URL", url)
    get_settings.cache_clear()
    asyncio.run(_execute(url, [("CREATE EXTENSION IF NOT EXISTS vector", {})]))
    return name, url


def _legacy_rows() -> list[tuple[str, dict]]:
    return [
        (
            "INSERT INTO tenants (id,name,is_active) VALUES (:t,'Migration 84',true)",
            {"t": TENANT},
        ),
        (
            "INSERT INTO jobs (id,tenant_id,title,status) VALUES (:j,:t,'Synthetic','ACTIVE')",
            {"j": JOB, "t": TENANT},
        ),
        (
            "INSERT INTO job_criteria_versions "
            "(id,tenant_id,job_id,version_number,criteria,unsupported_requirements,"
            "needs_review_requirements,result_limit,eligible_only) VALUES "
            "(:v,:t,:j,1,CAST(:c AS json),'[]','[]',20,false)",
            {"v": LEGACY_VERSION, "t": TENANT, "j": JOB, "c": json.dumps(CRITERIA)},
        ),
    ]


def _columns(url: str) -> list:
    return asyncio.run(
        _query(
            url,
            "SELECT column_name FROM information_schema.columns WHERE "
            "table_name='job_criteria_versions' AND column_name='agent_semantic_provenance'",
        )
    )


def _revision(url: str) -> str:
    return asyncio.run(_query(url, "SELECT version_num FROM alembic_version"))[0][0]


def test_upgrade_leaves_existing_versions_null(monkeypatch) -> None:
    name, url = _with_database(monkeypatch, "meyar_prov84_up")
    config = _config()
    try:
        command.upgrade(config, PRIOR_HEAD)
        asyncio.run(_execute(url, _legacy_rows()))
        command.upgrade(config, NEW_HEAD)
        assert _revision(url) == NEW_HEAD
        assert len(_columns(url)) == 1
        rows = asyncio.run(
            _query(url, "SELECT id, agent_semantic_provenance FROM job_criteria_versions")
        )
        assert [(str(row[0]), row[1]) for row in rows] == [(LEGACY_VERSION, None)]
    finally:
        get_settings.cache_clear()
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))


def test_downgrade_refuses_while_provenance_exists_and_keeps_data(monkeypatch) -> None:
    name, url = _with_database(monkeypatch, "meyar_prov84_refuse")
    config = _config()
    try:
        command.upgrade(config, NEW_HEAD)
        asyncio.run(
            _execute(
                url,
                [
                    *_legacy_rows(),
                    (
                        "INSERT INTO job_criteria_versions "
                        "(id,tenant_id,job_id,version_number,criteria,unsupported_requirements,"
                        "needs_review_requirements,result_limit,eligible_only,"
                        "agent_semantic_provenance) VALUES "
                        "(:v,:t,:j,2,CAST(:c AS json),'[]','[]',20,true,CAST(:p AS json))",
                        {
                            "v": AGENT_VERSION,
                            "t": TENANT,
                            "j": JOB,
                            "c": json.dumps(CRITERIA),
                            "p": json.dumps(PROVENANCE),
                        },
                    ),
                ],
            )
        )
        with pytest.raises(RuntimeError, match="refusing"):
            command.downgrade(config, PRIOR_HEAD)
        # Nothing changed: still on the new head, column and evidence intact.
        assert _revision(url) == NEW_HEAD
        assert len(_columns(url)) == 1
        stored = asyncio.run(
            _query(
                url,
                "SELECT agent_semantic_provenance FROM job_criteria_versions "
                f"WHERE id = '{AGENT_VERSION}'",
            )
        )
        assert stored[0][0] == PROVENANCE
        # The stored sample is itself a valid strict v2 record.
        from meyar.agent.semantic_provenance import parse_agent_semantic_provenance

        assert parse_agent_semantic_provenance(stored[0][0]) is not None
        legacy = asyncio.run(
            _query(
                url,
                "SELECT agent_semantic_provenance FROM job_criteria_versions "
                f"WHERE id = '{LEGACY_VERSION}'",
            )
        )
        assert legacy[0][0] is None
    finally:
        get_settings.cache_clear()
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))


def test_clean_no_provenance_roundtrip(monkeypatch) -> None:
    name, url = _with_database(monkeypatch, "meyar_prov84_rt")
    config = _config()
    try:
        command.upgrade(config, PRIOR_HEAD)
        asyncio.run(_execute(url, _legacy_rows()))
        command.upgrade(config, NEW_HEAD)
        command.downgrade(config, PRIOR_HEAD)
        assert _revision(url) == PRIOR_HEAD
        assert _columns(url) == []
        count = asyncio.run(_query(url, "SELECT count(*) FROM job_criteria_versions"))
        assert count[0][0] == 1
        command.upgrade(config, NEW_HEAD)
        assert _revision(url) == NEW_HEAD
        assert len(_columns(url)) == 1
    finally:
        get_settings.cache_clear()
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}"'))


def test_single_head_chains_through_the_provenance_revision() -> None:
    script = ScriptDirectory.from_config(_config())
    # issue #85 (D-089) chains e5d7a3c91b04 directly on this revision;
    # issue #86 (D-090) chains f3a9c6d2e815 on that.
    assert script.get_heads() == ["f3a9c6d2e815"]
    assert script.get_revision("e5d7a3c91b04").down_revision == NEW_HEAD
    assert script.get_revision(NEW_HEAD).down_revision == PRIOR_HEAD

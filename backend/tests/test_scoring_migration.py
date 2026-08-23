"""Alembic upgrade/downgrade/re-upgrade proof with one legacy Evaluation."""

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
PRE_SLICE10_REVISION = "7aae8da26969"
SLICE10_REVISION = "c0a4f2d8e317"
LEGACY_EVALUATION_ID = "00000000-0000-0000-0000-000000000008"


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


async def _insert_legacy_evaluation(database_url: str) -> None:
    statements = [
        "INSERT INTO tenants (id,name,is_active) VALUES "
        "('00000000-0000-0000-0000-000000000001','Migration Test',true)",
        "INSERT INTO candidates (id,tenant_id,status) VALUES "
        "('00000000-0000-0000-0000-000000000002',"
        "'00000000-0000-0000-0000-000000000001','ACTIVE')",
        "INSERT INTO candidate_documents "
        "(id,tenant_id,candidate_id,original_filename,mime_type,byte_size,sha256_hash,"
        "storage_key,document_status,parser_status) VALUES "
        "('00000000-0000-0000-0000-000000000003',"
        "'00000000-0000-0000-0000-000000000001',"
        "'00000000-0000-0000-0000-000000000002','synthetic.pdf','application/pdf',1,"
        "repeat('a',64),'migration/test','ACTIVE','PARSED')",
        "INSERT INTO canonical_documents "
        "(id,tenant_id,candidate_document_id,parser_name,parser_version,content) VALUES "
        "('00000000-0000-0000-0000-000000000004',"
        "'00000000-0000-0000-0000-000000000001',"
        "'00000000-0000-0000-0000-000000000003','test','1','{}')",
        "INSERT INTO candidate_profile_versions "
        "(id,tenant_id,candidate_id,candidate_document_id,canonical_document_id,source_sha256,"
        "version_number,schema_version,prompt_version,model_provider,model_name,model_metadata,"
        "status,profile_content) VALUES "
        "('00000000-0000-0000-0000-000000000005',"
        "'00000000-0000-0000-0000-000000000001',"
        "'00000000-0000-0000-0000-000000000002',"
        "'00000000-0000-0000-0000-000000000003',"
        "'00000000-0000-0000-0000-000000000004',repeat('a',64),1,"
        "'candidate-profile-v1','test','test','test','{}','COMPLETED','{}')",
        "INSERT INTO jobs (id,tenant_id,title) VALUES "
        "('00000000-0000-0000-0000-000000000006',"
        "'00000000-0000-0000-0000-000000000001','Legacy Job')",
        "INSERT INTO job_criteria_versions (id,tenant_id,job_id,version_number,criteria) VALUES "
        "('00000000-0000-0000-0000-000000000007',"
        "'00000000-0000-0000-0000-000000000001',"
        "'00000000-0000-0000-0000-000000000006',1,'[]')",
        "INSERT INTO evaluations "
        "(id,tenant_id,candidate_id,candidate_profile_version_id,job_id,"
        "job_criteria_version_id,status,overall_result,policy_engine_version,"
        "criterion_results,completed_at) VALUES "
        f"('{LEGACY_EVALUATION_ID}','00000000-0000-0000-0000-000000000001',"
        "'00000000-0000-0000-0000-000000000002',"
        "'00000000-0000-0000-0000-000000000005',"
        "'00000000-0000-0000-0000-000000000006',"
        "'00000000-0000-0000-0000-000000000007','COMPLETED','STRONG_MATCH',"
        "'meyar-policy-v1','[{\"criterion_id\":\"legacy\",\"status\":\"MATCH\"}]',now())",
    ]
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        for statement in statements:
            await connection.execute(text(statement))
    await engine.dispose()


async def _assert_upgraded_legacy_row(database_url: str) -> None:
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT evaluation_as_of_date, numeric_score, scoring_policy_version, "
                    "score_explanation, criterion_results FROM evaluations WHERE id=:id"
                ),
                {"id": LEGACY_EVALUATION_ID},
            )
        ).one()
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    await engine.dispose()
    assert tuple(row[:4]) == (None, None, None, None)
    assert row.criterion_results == [{"criterion_id": "legacy", "status": "MATCH"}]
    assert revision == SLICE10_REVISION


async def _assert_downgraded_legacy_row(database_url: str) -> None:
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        column_count = await connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns WHERE table_name='evaluations' "
                "AND column_name IN ('evaluation_as_of_date','numeric_score',"
                "'scoring_policy_version','score_explanation')"
            )
        )
        criterion_results = await connection.scalar(
            text("SELECT criterion_results FROM evaluations WHERE id=:id"),
            {"id": LEGACY_EVALUATION_ID},
        )
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    await engine.dispose()
    assert column_count == 0
    assert criterion_results == [{"criterion_id": "legacy", "status": "MATCH"}]
    assert revision == PRE_SLICE10_REVISION


def test_slice10_migration_roundtrip_preserves_legacy_evaluation(monkeypatch) -> None:
    database_name = f"meyar_slice10_{uuid.uuid4().hex}"
    database_url = f"{BASE_URL}/{database_name}"
    alembic_config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    asyncio.run(_create_database(database_name))
    monkeypatch.setenv("MEYAR_DATABASE_URL", database_url)
    get_settings.cache_clear()
    try:
        command.upgrade(alembic_config, PRE_SLICE10_REVISION)
        asyncio.run(_insert_legacy_evaluation(database_url))
        # Keep this historical migration test pinned to the Slice 10 target;
        # later slices own their own head round-trip tests.
        command.upgrade(alembic_config, SLICE10_REVISION)
        asyncio.run(_assert_upgraded_legacy_row(database_url))
        command.downgrade(alembic_config, PRE_SLICE10_REVISION)
        asyncio.run(_assert_downgraded_legacy_row(database_url))
        command.upgrade(alembic_config, SLICE10_REVISION)
        asyncio.run(_assert_upgraded_legacy_row(database_url))
    finally:
        get_settings.cache_clear()
        asyncio.run(_drop_database(database_name))

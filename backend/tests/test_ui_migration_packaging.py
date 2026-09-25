import asyncio
import uuid
from importlib import resources
from pathlib import Path

from alembic.config import Config
from conftest import ADMIN_DATABASE_URL
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from meyar.config import get_settings

BASE_URL = ADMIN_DATABASE_URL.rsplit("/", 1)[0]
ADMIN_URL = f"{BASE_URL}/postgres"
PRE_SLICE11_REVISION = "c0a4f2d8e317"
SLICE11_REVISION = "e3b1f7a9c2d4"
PRE_SLICE1_HUMAN_IDENTITY_REVISION = "db7e4523f491"
# The expected result of `alembic upgrade head` right now — bump this
# alongside alembic/versions whenever a new migration becomes the head
# (most recently: a1c5e9f2b6d3, add agent_conversations — see
# docs/DECISIONS.md, Slice 2 / issue #31).
PRE_DRAFT_CONFIRMATION_REVISION = "a1c5e9f2b6d3"
CURRENT_HEAD_REVISION = "8bd12e7c4a60"


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


async def _assert_upgraded(database_url: str) -> None:
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        columns = set(
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='browser_sessions'"
                    )
                )
            ).scalars()
        )
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        raw_columns = await connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name='browser_sessions' AND column_name IN "
                "('api_key','session_token','tenant_id','scopes')"
            )
        )
        confirmation_columns = set(
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='agent_draft_confirmations'"
                    )
                )
            ).scalars()
        )
    await engine.dispose()
    assert {
        "id",
        "user_id",
        "tenant_membership_id",
        "session_token_hash",
        "csrf_secret",
        "created_at",
        "expires_at",
        "revoked_at",
    } == columns
    assert raw_columns == 0
    assert confirmation_columns == {
        "id",
        "tenant_id",
        "draft_id",
        "browser_session_id",
        "job_id",
        "criteria_version_id",
        "status",
        "confirmed_at",
    }
    assert revision == CURRENT_HEAD_REVISION


async def _assert_downgraded(database_url: str) -> None:
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        table = await connection.scalar(text("SELECT to_regclass('browser_sessions')"))
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    await engine.dispose()
    assert table is None
    assert revision == PRE_SLICE11_REVISION


def test_slice11_postgresql_migration_upgrade_downgrade_reupgrade(monkeypatch) -> None:
    database_name = f"meyar_slice11_{uuid.uuid4().hex}"
    database_url = f"{BASE_URL}/{database_name}"
    alembic_config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    asyncio.run(_create_database(database_name))
    monkeypatch.setenv("MEYAR_DATABASE_URL", database_url)
    get_settings.cache_clear()
    try:
        command.upgrade(alembic_config, PRE_SLICE11_REVISION)
        command.upgrade(alembic_config, "head")
        asyncio.run(_assert_upgraded(database_url))
        command.downgrade(alembic_config, PRE_SLICE11_REVISION)
        asyncio.run(_assert_downgraded(database_url))
        command.upgrade(alembic_config, "head")
        asyncio.run(_assert_upgraded(database_url))
    finally:
        get_settings.cache_clear()
        asyncio.run(_drop_database(database_name))


async def _insert_pre_lifecycle_job(database_url: str) -> uuid.UUID:
    """Inserts a tenant + job row against the schema exactly as it stood
    immediately before the add_job_lifecycle migration (no status/
    archived_at/duplicate_signature columns yet), simulating a real
    pre-existing production row."""
    engine = create_async_engine(database_url)
    tenant_id = uuid.uuid4()
    job_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO tenants (id, name, is_active) VALUES (:id, :name, true)"),
            {"id": tenant_id, "name": "Pre-lifecycle tenant"},
        )
        await connection.execute(
            text("INSERT INTO jobs (id, tenant_id, title) VALUES (:id, :tenant_id, :title)"),
            {"id": job_id, "tenant_id": tenant_id, "title": "Pre-lifecycle JD"},
        )
    await engine.dispose()
    return job_id


async def _assert_job_backfilled_active(database_url: str, job_id: uuid.UUID) -> None:
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT status, archived_at, duplicate_signature FROM jobs WHERE id = :id"
                ),
                {"id": job_id},
            )
        ).one()
    await engine.dispose()
    status_value, archived_at, duplicate_signature = row
    assert status_value == "ACTIVE"
    assert archived_at is None
    assert duplicate_signature is None


def test_job_lifecycle_migration_backfills_existing_jobs_as_active(monkeypatch) -> None:
    """A Job row created before the add_job_lifecycle migration must
    deterministically become status='ACTIVE' (never archived, never
    dropped) on upgrade — no data loss, no manual backfill step."""
    database_name = f"meyar_job_lifecycle_{uuid.uuid4().hex}"
    database_url = f"{BASE_URL}/{database_name}"
    alembic_config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    asyncio.run(_create_database(database_name))
    monkeypatch.setenv("MEYAR_DATABASE_URL", database_url)
    get_settings.cache_clear()
    try:
        command.upgrade(alembic_config, SLICE11_REVISION)
        job_id = asyncio.run(_insert_pre_lifecycle_job(database_url))
        command.upgrade(alembic_config, "head")
        asyncio.run(_assert_job_backfilled_active(database_url, job_id))
    finally:
        get_settings.cache_clear()
        asyncio.run(_drop_database(database_name))


async def _insert_pre_slice1_state(database_url: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Inserts a tenant + api_key row against the schema exactly as it
    stood immediately before the human-identity migration — simulating a
    real pre-existing deployment. Also inserts a pre-migration
    browser_session row (api_key_id-shaped) to prove the migration's
    documented session-clearing behavior."""
    engine = create_async_engine(database_url)
    tenant_id = uuid.uuid4()
    api_key_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO tenants (id, name, is_active) VALUES (:id, :name, true)"),
            {"id": tenant_id, "name": "Pre-Slice1 tenant"},
        )
        await connection.execute(
            text(
                "INSERT INTO api_keys (id, tenant_id, prefix, key_hash, scopes, created_at) "
                "VALUES (:id, :tenant_id, 'meyar_test_pre', :key_hash, '{}', now())"
            ),
            {"id": api_key_id, "tenant_id": tenant_id, "key_hash": "x" * 64},
        )
        await connection.execute(
            text(
                "INSERT INTO browser_sessions "
                "(id, api_key_id, session_token_hash, csrf_secret, expires_at) "
                "VALUES (:id, :api_key_id, :hash, :secret, now() + interval '1 hour')"
            ),
            {
                "id": uuid.uuid4(),
                "api_key_id": api_key_id,
                "hash": "y" * 64,
                "secret": "z" * 64,
            },
        )
    await engine.dispose()
    return tenant_id, api_key_id


async def _assert_slice1_upgrade_preserved_data(
    database_url: str, tenant_id: uuid.UUID, api_key_id: uuid.UUID
) -> None:
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        tenant_row = await connection.execute(
            text("SELECT id FROM tenants WHERE id = :id"), {"id": tenant_id}
        )
        assert tenant_row.scalar_one_or_none() == tenant_id

        api_key_row = await connection.execute(
            text("SELECT id FROM api_keys WHERE id = :id"), {"id": api_key_id}
        )
        assert api_key_row.scalar_one_or_none() == api_key_id

        # Pre-migration session rows are intentionally cleared (they are
        # short-lived, fully revocable session state, not durable
        # identity data — see the migration's upgrade() docstring).
        session_count = await connection.scalar(text("SELECT count(*) FROM browser_sessions"))
        assert session_count == 0

        user_table = await connection.scalar(text("SELECT to_regclass('users')"))
        membership_table = await connection.scalar(
            text("SELECT to_regclass('tenant_memberships')")
        )
        assert user_table is not None
        assert membership_table is not None

        actor_columns = set(
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='audit_events' AND column_name IN "
                        "('actor_type', 'actor_id')"
                    )
                )
            ).scalars()
        )
        assert actor_columns == {"actor_type", "actor_id"}
    await engine.dispose()


def test_slice1_human_identity_migration_preserves_existing_data(monkeypatch) -> None:
    database_name = f"meyar_slice1_{uuid.uuid4().hex}"
    database_url = f"{BASE_URL}/{database_name}"
    alembic_config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    asyncio.run(_create_database(database_name))
    monkeypatch.setenv("MEYAR_DATABASE_URL", database_url)
    get_settings.cache_clear()
    try:
        command.upgrade(alembic_config, PRE_SLICE1_HUMAN_IDENTITY_REVISION)
        tenant_id, api_key_id = asyncio.run(_insert_pre_slice1_state(database_url))
        command.upgrade(alembic_config, "head")
        asyncio.run(_assert_slice1_upgrade_preserved_data(database_url, tenant_id, api_key_id))
        command.downgrade(alembic_config, PRE_SLICE1_HUMAN_IDENTITY_REVISION)
        command.upgrade(alembic_config, "head")
        asyncio.run(_assert_slice1_upgrade_preserved_data(database_url, tenant_id, api_key_id))
    finally:
        get_settings.cache_clear()
        asyncio.run(_drop_database(database_name))


async def _assert_draft_confirmation_schema(database_url: str, *, present: bool) -> None:
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        table = await connection.scalar(
            text("SELECT to_regclass('agent_draft_confirmations')")
        )
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        if present:
            constraints = set(
                (
                    await connection.execute(
                        text(
                            "SELECT conname FROM pg_constraint "
                            "WHERE conrelid = 'agent_draft_confirmations'::regclass"
                        )
                    )
                ).scalars()
            )
            assert {
                "agent_draft_confirmations_pkey",
                "agent_draft_confirmations_tenant_id_fkey",
                "agent_draft_confirmations_browser_session_id_fkey",
                "agent_draft_confirmations_job_id_fkey",
                "agent_draft_confirmations_criteria_version_id_fkey",
                "uq_agent_draft_confirmation",
                "uq_agent_draft_confirmation_job",
                "uq_agent_draft_confirmation_criteria_version",
                "ck_agent_draft_confirmation_status",
            }.issubset(constraints)
    await engine.dispose()
    assert (table is not None) is present
    assert revision == (
        CURRENT_HEAD_REVISION if present else PRE_DRAFT_CONFIRMATION_REVISION
    )


def test_draft_confirmation_migration_upgrade_downgrade_reupgrade(monkeypatch) -> None:
    database_name = f"meyar_draft_confirmation_{uuid.uuid4().hex}"
    database_url = f"{BASE_URL}/{database_name}"
    alembic_config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    asyncio.run(_create_database(database_name))
    monkeypatch.setenv("MEYAR_DATABASE_URL", database_url)
    get_settings.cache_clear()
    try:
        command.upgrade(alembic_config, PRE_DRAFT_CONFIRMATION_REVISION)
        asyncio.run(_assert_draft_confirmation_schema(database_url, present=False))
        command.upgrade(alembic_config, "head")
        asyncio.run(_assert_draft_confirmation_schema(database_url, present=True))
        command.downgrade(alembic_config, PRE_DRAFT_CONFIRMATION_REVISION)
        asyncio.run(_assert_draft_confirmation_schema(database_url, present=False))
        command.upgrade(alembic_config, "head")
        asyncio.run(_assert_draft_confirmation_schema(database_url, present=True))
    finally:
        get_settings.cache_clear()
        asyncio.run(_drop_database(database_name))


def test_installed_package_resources_are_locatable_and_local_only() -> None:
    ui_root = resources.files("meyar.ui")
    templates = ui_root.joinpath("templates")
    static = ui_root.joinpath("static")
    assert templates.joinpath("base.html").is_file()
    assert templates.joinpath("login.html").is_file()
    assert static.joinpath("styles.css").is_file()

    source = "\n".join(
        child.read_text(encoding="utf-8")
        for directory in (templates, static)
        for child in directory.iterdir()
        if child.is_file()
    )
    prohibited = (
        "|safe",
        "Markup(",
        "localStorage",
        "sessionStorage",
        "IndexedDB",
        "fonts.googleapis.com",
        "cdnjs",
        "jsdelivr",
        "unpkg",
        "google-analytics",
        "telemetry",
        "http://",
        "https://",
    )
    assert all(value not in source for value in prohibited)

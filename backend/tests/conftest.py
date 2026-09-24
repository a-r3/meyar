import asyncio
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from meyar.core.roles import ROLE_HR_USER
from meyar.db import Base, get_db
from meyar.main import app
from meyar.services.api_key_repo import create_api_key
from meyar.services.tenant_membership_repo import create_membership
from meyar.services.tenant_repo import create_tenant
from meyar.services.user_repo import create_user
from meyar.storage.dependency import get_document_storage, get_photo_storage
from meyar.storage.local import LocalFilesystemStorage
from meyar.storage.photo import LocalPhotoStorage

DEFAULT_TEST_PASSWORD = "correct-horse-battery-staple-1"

ADMIN_DATABASE_URL = "postgresql+asyncpg://meyar:meyar_dev_password@localhost:55719/meyar"
TEST_DATABASE_URL = "postgresql+asyncpg://meyar:meyar_dev_password@localhost:55719/meyar_test"

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "synthetic_cvs"


async def _prepare_test_database() -> None:
    admin_engine = create_async_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        exists = await conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = 'meyar_test'")
        )
        if exists.scalar_one_or_none() is None:
            await conn.execute(text("CREATE DATABASE meyar_test"))
    await admin_engine.dispose()

    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        # Required before create_all for candidate_embedding_versions'
        # Vector column (Slice 7) — tests build the schema directly via
        # metadata.create_all, bypassing the Alembic migration that
        # normally does this for dev/CI.
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _test_database_ready() -> None:
    """Create the test database and schema once per test session, not once
    per test — establishing a Postgres connection on this dev machine is
    expensive under memory pressure, and per-test DDL made that far worse."""
    asyncio.run(_prepare_test_database())


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine(TEST_DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
        await session.rollback()
        for table in reversed(Base.metadata.sorted_tables):
            await session.execute(table.delete())
        await session.commit()
    await engine.dispose()


@pytest.fixture
async def client(db_session: AsyncSession, tmp_path: Path) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    storage = LocalFilesystemStorage(root=str(tmp_path / "storage"))

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_document_storage] = lambda: storage
    app.dependency_overrides[get_photo_storage] = lambda: LocalPhotoStorage(
        root=str(tmp_path / "storage")
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
async def tenant_and_key(db_session: AsyncSession):
    tenant = await create_tenant(db_session, name=f"Tenant-{uuid.uuid4().hex[:8]}")
    api_key, plaintext = await create_api_key(db_session, tenant_id=tenant.id, env="test")
    await db_session.commit()
    return tenant, api_key, plaintext


@pytest.fixture
async def tenant_and_user(db_session: AsyncSession):
    """A human identity with an active HR_USER membership on a fresh
    tenant — the standard fixture for /ui/login-based tests. Returns
    (tenant, user, plaintext_password, membership)."""
    tenant = await create_tenant(db_session, name=f"Tenant-{uuid.uuid4().hex[:8]}")
    username = f"hr-{uuid.uuid4().hex[:8]}"
    user = await create_user(
        db_session, username=username, plaintext_password=DEFAULT_TEST_PASSWORD
    )
    membership = await create_membership(
        db_session, user_id=user.id, tenant_id=tenant.id, role=ROLE_HR_USER
    )
    await db_session.commit()
    return tenant, user, DEFAULT_TEST_PASSWORD, membership


@pytest.fixture
async def tenant_key_and_user(db_session: AsyncSession):
    """Both dual-access principals on the SAME tenant: an API key for
    seeding data through the machine REST path (e.g. document upload) and
    a human user/membership for exercising the /ui/login-authenticated
    surface against that same data. Returns
    (tenant, api_key, plaintext_key, user, plaintext_password, membership)."""
    tenant = await create_tenant(db_session, name=f"Tenant-{uuid.uuid4().hex[:8]}")
    api_key, plaintext_key = await create_api_key(db_session, tenant_id=tenant.id, env="test")
    username = f"hr-{uuid.uuid4().hex[:8]}"
    user = await create_user(
        db_session, username=username, plaintext_password=DEFAULT_TEST_PASSWORD
    )
    membership = await create_membership(
        db_session, user_id=user.id, tenant_id=tenant.id, role=ROLE_HR_USER
    )
    await db_session.commit()
    return tenant, api_key, plaintext_key, user, DEFAULT_TEST_PASSWORD, membership

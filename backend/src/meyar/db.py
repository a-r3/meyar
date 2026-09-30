from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from meyar.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str | None = None):
    """Single-process engine with an EXPLICIT bounded pool (issue #85,
    docs/DECISIONS.md D-089). Request handlers hold a pooled connection
    only for short DB phases: agent turns release theirs before every local
    inference wait/call (meyar.agent.turn_boundary), so pool size bounds
    concurrent DB work, never concurrent AI work."""
    settings = get_settings()
    return create_async_engine(
        database_url or settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
    )


_engine = None
_session_factory = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        yield session

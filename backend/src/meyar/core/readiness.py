"""Bounded HTTP readiness; no candidate reads, inference or provisioning."""

import asyncio
from weakref import WeakKeyDictionary

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from meyar.config import Settings
from meyar.embedding.dependency import embedding_provider_from_settings
from meyar.llm.dependency import llm_provider_from_settings
from meyar.ops.alembic_introspect import get_code_alembic_heads
from meyar.ops.config import resolve_alembic_ini_path
from meyar.ops.storage_probe import probe_storage_writable

PROBE_SECONDS = 6.0
_storage_work: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Task] = WeakKeyDictionary()


async def readiness_reasons(engine: AsyncEngine, settings: Settings) -> list[str]:
    async def database() -> list[str]:
        try:
            async with asyncio.timeout(PROBE_SECONDS):
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                    ini = resolve_alembic_ini_path()
                    if ini is None:
                        return ["SCHEMA_UNAVAILABLE"]
                    heads = get_code_alembic_heads(ini)
                    exists = await conn.scalar(
                        text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
                    )
                    if not exists:
                        return ["SCHEMA_MISMATCH"]
                    rows = (
                        (
                            await conn.execute(
                                text("SELECT version_num FROM alembic_version LIMIT 2")
                            )
                        )
                        .scalars()
                        .all()
                    )
                    return (
                        [] if len(heads) == 1 and list(rows) == list(heads) else ["SCHEMA_MISMATCH"]
                    )
        except Exception:
            return ["DATABASE_UNREACHABLE"]

    async def model(provider, code: str) -> list[str]:
        try:
            async with asyncio.timeout(PROBE_SECONDS):
                health = await provider.health()
            if not health.get("reachable"):
                return ["OLLAMA_UNREACHABLE"]
            return [] if health.get("model_available") else [code]
        except Exception:
            return [code]

    async def storage() -> list[str]:
        loop = asyncio.get_running_loop()
        previous = _storage_work.get(loop)
        if previous is not None and not previous.done():
            return ["STORAGE_PROBE_BUSY"]
        # A timed-out kernel/filesystem call cannot be cancelled. Keep its slot
        # until it actually finishes, preventing unbounded abandoned threads.
        task = asyncio.create_task(asyncio.to_thread(probe_storage_writable, settings.storage_root))
        _storage_work[loop] = task
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        try:
            async with asyncio.timeout(PROBE_SECONDS):
                result = await asyncio.shield(task)
            return [] if result.writable else ["STORAGE_NOT_WRITABLE"]
        except Exception:
            return ["STORAGE_NOT_WRITABLE"]

    # Providers use the existing loopback-only, proxy/redirect-isolated boundary.
    # Health calls list local models only, never execute candidate inference.
    try:
        llm = llm_provider_from_settings(settings)
        embedding = embedding_provider_from_settings(settings)
    except Exception:
        return ["MODEL_CONFIGURATION_INVALID"]
    results = await asyncio.gather(
        database(),
        storage(),
        model(llm, "LLM_MODEL_UNAVAILABLE"),
        model(embedding, "EMBEDDING_MODEL_UNAVAILABLE"),
    )
    return sorted({reason for result in results for reason in result})

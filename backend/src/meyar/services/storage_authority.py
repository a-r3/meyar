"""Database coordination of storage writers and operator recovery.

Writers take a shared tenant advisory transaction lock before filesystem
mutation; maintenance takes exclusive with try-lock (never waits). These are
cross-process PostgreSQL locks, not an in-process correctness mutex.
"""

import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def storage_lock_key(tenant_id: uuid.UUID) -> int:
    digest = hashlib.sha256(b"meyar-storage-recovery-v1" + tenant_id.bytes).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def storage_writer(db: AsyncSession, tenant_id: uuid.UUID) -> None:
    await db.execute(
        text("SELECT pg_advisory_xact_lock_shared(:key)"), {"key": storage_lock_key(tenant_id)}
    )


async def storage_maintenance(db: AsyncSession, tenant_id: uuid.UUID) -> bool:
    return bool(
        await db.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": storage_lock_key(tenant_id)}
        )
    )

"""Tenant-scoped exact-content authority for folder ingestion (issue #46 S7).

Exact-content dedup (D-021) is a read-then-write decision: look up an already
ingested identical document, otherwise mint a Candidate. Two transactions can
both observe "not present" and both mint one. No row exists yet to lock for a
brand-new content hash and the schema deliberately has no content-unique
identity (dedup-linked paths share one hash), so the authority is a
transaction-scoped PostgreSQL advisory lock keyed by (tenant, sha256):

* holder: ingests the content and keeps the lock until its commit/rollback;
* waiter: blocks, then looks up AFTER the holder ends (READ COMMITTED sees the
  holder's committed rows, or none after a rollback) — never decided on a
  pre-wait observation.

The key is namespaced and tenant-scoped, so other tenants and other content
never wait. A hash collision only adds a spurious wait, never a wrong result.
Lock order (global): Tenant -> FolderSource -> content -> Candidate/document.
One transaction holds many content locks (one per ingested file) taken in
scan order; two scans with the same contents in opposite order can deadlock.
PostgreSQL detects that and aborts one transaction; the caller retries the
whole scan (folder_indexer_service.index_folder_and_commit).
"""

import hashlib
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _content_lock_key(tenant_id: uuid.UUID, sha256_hash: str) -> int:
    digest = hashlib.sha256(f"meyar.folder-content:{tenant_id}:{sha256_hash}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


async def lock_tenant_content(db: AsyncSession, *, tenant_id: uuid.UUID, sha256_hash: str) -> None:
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": _content_lock_key(tenant_id, sha256_hash)},
    )

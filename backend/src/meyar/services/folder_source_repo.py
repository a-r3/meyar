import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.folder_source import FolderSource


async def get_or_create_folder_source(
    db: AsyncSession, *, tenant_id: uuid.UUID, root_path: str
) -> FolderSource:
    """Returns the (tenant, root) FolderSource row, ROW-LOCKED until the
    caller's transaction ends (issue #46 S7 — folder/source authority).

    Reconciliation of one source is single-writer: a concurrent run for the
    same source waits here for the holder's commit/rollback and then reads
    the holder's durable rows. The insert is ON CONFLICT DO NOTHING so two
    runs creating the same brand-new source converge on one row instead of
    one of them failing the unique constraint (a conflicting insert waits for
    the other transaction to finish). FOR NO KEY UPDATE serializes writers
    without blocking FK KEY SHARE checks from tenant-level operations.

    Lock order: Tenant (non-locking active check; SHARE at commit, never
    awaited while holding this lock because suspension takes no source/
    content/candidate lock) -> FolderSource -> content -> Candidate.
    """
    await db.execute(
        pg_insert(FolderSource)
        .values(tenant_id=tenant_id, root_path=root_path)
        .on_conflict_do_nothing(constraint="uq_folder_sources_tenant_root")
    )
    result = await db.execute(
        select(FolderSource)
        .where(FolderSource.tenant_id == tenant_id, FolderSource.root_path == root_path)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()

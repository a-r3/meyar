import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.folder_source import FolderSource


async def get_or_create_folder_source(
    db: AsyncSession, *, tenant_id: uuid.UUID, root_path: str
) -> FolderSource:
    result = await db.execute(
        select(FolderSource).where(
            FolderSource.tenant_id == tenant_id, FolderSource.root_path == root_path
        )
    )
    source = result.scalar_one_or_none()
    if source is not None:
        return source

    source = FolderSource(tenant_id=tenant_id, root_path=root_path)
    db.add(source)
    await db.flush()
    return source

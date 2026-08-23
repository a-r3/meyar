import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.folder_indexed_file import FolderIndexedFile


async def list_folder_indexed_files(
    db: AsyncSession, *, tenant_id: uuid.UUID, folder_source_id: uuid.UUID
) -> list[FolderIndexedFile]:
    result = await db.execute(
        select(FolderIndexedFile).where(
            FolderIndexedFile.tenant_id == tenant_id,
            FolderIndexedFile.folder_source_id == folder_source_id,
        )
    )
    return list(result.scalars().all())


async def get_folder_indexed_file(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    folder_source_id: uuid.UUID,
    relative_path: str,
) -> FolderIndexedFile | None:
    result = await db.execute(
        select(FolderIndexedFile).where(
            FolderIndexedFile.tenant_id == tenant_id,
            FolderIndexedFile.folder_source_id == folder_source_id,
            FolderIndexedFile.relative_path == relative_path,
        )
    )
    return result.scalar_one_or_none()


async def create_folder_indexed_file(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    folder_source_id: uuid.UUID,
    relative_path: str,
    document_type: str,
    byte_size: int,
    sha256_hash: str,
    index_status: str,
    candidate_id: uuid.UUID | None,
    candidate_document_id: uuid.UUID | None,
    failure_code: str | None = None,
    failure_message: str | None = None,
    last_seen_at: datetime | None = None,
) -> FolderIndexedFile:
    row = FolderIndexedFile(
        tenant_id=tenant_id,
        folder_source_id=folder_source_id,
        relative_path=relative_path,
        document_type=document_type,
        byte_size=byte_size,
        sha256_hash=sha256_hash,
        index_status=index_status,
        candidate_id=candidate_id,
        candidate_document_id=candidate_document_id,
        failure_code=failure_code,
        failure_message=failure_message,
        last_seen_at=last_seen_at,
    )
    db.add(row)
    await db.flush()
    return row


async def update_folder_indexed_file(
    db: AsyncSession,
    row: FolderIndexedFile,
    *,
    byte_size: int | None = None,
    sha256_hash: str | None = None,
    index_status: str | None = None,
    candidate_document_id: uuid.UUID | None = ...,  # type: ignore[assignment]
    failure_code: str | None = ...,  # type: ignore[assignment]
    failure_message: str | None = ...,  # type: ignore[assignment]
    last_seen_at: datetime | None = ...,  # type: ignore[assignment]
) -> FolderIndexedFile:
    """Mutates an already-loaded row in place. Uses Ellipsis as the
    not-provided sentinel for the nullable fields so a caller can
    explicitly pass None to clear one (e.g. failure_code=None on a
    successful re-import) without it being indistinguishable from
    "leave unchanged"."""
    if byte_size is not None:
        row.byte_size = byte_size
    if sha256_hash is not None:
        row.sha256_hash = sha256_hash
    if index_status is not None:
        row.index_status = index_status
    if candidate_document_id is not ...:
        row.candidate_document_id = candidate_document_id
    if failure_code is not ...:
        row.failure_code = failure_code
    if failure_message is not ...:
        row.failure_message = failure_message
    if last_seen_at is not ...:
        row.last_seen_at = last_seen_at
    await db.flush()
    return row

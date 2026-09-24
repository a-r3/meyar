import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.candidate_photo_version import PHOTO_AVAILABLE, CandidatePhotoVersion
from meyar.photo.policy import MAX_DERIVED_BYTES
from meyar.services.audit_repo import ACTOR_API_KEY, record_event
from meyar.services.candidate_document_repo import list_candidate_documents
from meyar.services.candidate_repo import delete_candidate_row, get_candidate
from meyar.storage.base import DocumentStorage
from meyar.storage.photo import LocalPhotoStorage


async def delete_candidate_cascade(
    db: AsyncSession,
    storage: DocumentStorage,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    photo_storage: LocalPhotoStorage,
    actor_id: uuid.UUID,
) -> int | None:
    """Delete photo and original assets, candidate rows, and audit in one
    request operation. Restore exact photo bytes if any step fails.
    Returns None only if the candidate does not exist for this tenant."""
    candidate = await get_candidate(db, tenant_id=tenant_id, candidate_id=candidate_id)
    if candidate is None:
        return None

    documents = await list_candidate_documents(db, tenant_id=tenant_id, candidate_id=candidate_id)
    photo_rows = await db.scalars(
        select(CandidatePhotoVersion).where(
            CandidatePhotoVersion.tenant_id == tenant_id,
            CandidatePhotoVersion.candidate_id == candidate_id,
            CandidatePhotoVersion.status == PHOTO_AVAILABLE,
        )
    )
    # Validate and snapshot every derived asset before removing any file.
    # The snapshot is bounded by the extraction policy per photo and permits
    # exact-key compensation if a later filesystem or DB step fails.
    photo_bytes: dict[str, bytes] = {}
    for row in photo_rows:
        key = row.derived_storage_key
        if key is None or row.derived_sha256 is None:
            raise OSError("Invalid AVAILABLE photo provenance")
        content = await photo_storage.read(tenant_id=tenant_id, storage_key=key)
        if (
            len(content) > MAX_DERIVED_BYTES
            or hashlib.sha256(content).hexdigest() != row.derived_sha256
        ):
            raise OSError("Derived photo integrity check failed")
        photo_bytes[key] = content
    try:
        # Delete photo assets first so a photo failure cannot touch an
        # original CV; exact-key compensation covers every later failure.
        for key in photo_bytes:
            await photo_storage.delete(tenant_id=tenant_id, storage_key=key)
        for document in documents:
            await storage.delete(storage_key=document.storage_key)
        await delete_candidate_row(db, tenant_id=tenant_id, candidate_id=candidate_id)
        await record_event(
            db, tenant_id=tenant_id, event_type="CANDIDATE_DELETED",
            metadata={"candidate_id": str(candidate_id), "document_count": len(documents)},
            actor_type=ACTOR_API_KEY, actor_id=actor_id,
        )
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        finally:
            for key, content in photo_bytes.items():
                await photo_storage.restore_exact(
                    tenant_id=tenant_id, storage_key=key, content=content
                )
        raise
    return len(documents)

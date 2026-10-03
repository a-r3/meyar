import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.candidate_photo_version import PHOTO_AVAILABLE, CandidatePhotoVersion
from meyar.services.audit_repo import ACTOR_API_KEY, record_event
from meyar.services.candidate_document_repo import list_candidate_documents
from meyar.services.candidate_repo import delete_candidate_row, get_candidate
from meyar.services.storage_recovery import (
    commit_with_recovery,
    rollback_with_recovery,
    track_staged,
)
from meyar.services.tenant_authority import require_active_tenant
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
    request operation. Asset deletes are staged and only made permanent after
    the DB commit; on any failure every previously existing asset is restored
    byte-for-byte to its exact key (services.storage_recovery).
    Lock order: Tenant SHARE -> Candidate UPDATE, before asset enumeration.
    Upload takes Tenant SHARE -> ApiKey SHARE -> Candidate SHARE. No key/user
    lock is acquired after Candidate. A preceding upload must end before this
    lock succeeds; a later upload rechecks absence after deletion commits.
    Returns None only if the candidate does not exist for this tenant."""
    await require_active_tenant(db, tenant_id, lock=True)
    candidate = await get_candidate(
        db, tenant_id=tenant_id, candidate_id=candidate_id, lock=True
    )
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
    # Validate provenance before touching any asset. An absent or corrupt
    # presentation asset cannot veto core candidate deletion; staging moves
    # bytes without reading them, so each existing object is restored exactly.
    photo_keys: list[str] = []
    for row in photo_rows:
        if row.derived_storage_key is None or row.derived_sha256 is None:
            raise OSError("Invalid AVAILABLE photo provenance")
        photo_keys.append(row.derived_storage_key)

    try:
        # Reversible deletes, photos first so a photo failure cannot touch an
        # original CV. Already-absent objects stage to None and are never
        # fabricated on rollback. Nothing is permanent until the DB commit.
        for key in photo_keys:
            track_staged(
                db, photo_storage,
                await photo_storage.stage_delete(tenant_id=tenant_id, storage_key=key),
            )
        for document in documents:
            track_staged(
                db, storage,
                await storage.stage_delete(
                    tenant_id=tenant_id, storage_key=document.storage_key
                ),
            )
        await delete_candidate_row(db, tenant_id=tenant_id, candidate_id=candidate_id)
        await record_event(
            db, tenant_id=tenant_id, event_type="CANDIDATE_DELETED",
            metadata={"candidate_id": str(candidate_id), "document_count": len(documents)},
            actor_type=ACTOR_API_KEY, actor_id=actor_id,
        )
        await commit_with_recovery(db)  # purges on success; restores on failure
    except Exception as exc:
        # Idempotent: a failed commit was already compensated by commit_with_recovery.
        await rollback_with_recovery(db, exc)
        raise
    return len(documents)

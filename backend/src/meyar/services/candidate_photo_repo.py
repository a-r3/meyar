import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.candidate_photo_version import CandidatePhotoVersion


async def get_photo_for_document(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    document_id: uuid.UUID,
    extractor_version: str,
) -> CandidatePhotoVersion | None:
    return await db.scalar(
        select(CandidatePhotoVersion).where(
            CandidatePhotoVersion.tenant_id == tenant_id,
            CandidatePhotoVersion.candidate_id == candidate_id,
            CandidatePhotoVersion.candidate_document_id == document_id,
            CandidatePhotoVersion.extractor_version == extractor_version,
        )
    )


async def create_photo_version(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    document_id: uuid.UUID,
    extractor_version: str,
    outcome: dict,
    derived_storage_key: str | None,
) -> CandidatePhotoVersion:
    current_max = await db.scalar(
        select(func.max(CandidatePhotoVersion.version_number)).where(
            CandidatePhotoVersion.tenant_id == tenant_id,
            CandidatePhotoVersion.candidate_id == candidate_id,
        )
    )
    available = outcome["status"] == "AVAILABLE"
    row = CandidatePhotoVersion(
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_document_id=document_id,
        version_number=(current_max or 0) + 1,
        extractor_version=extractor_version,
        status=outcome["status"],
        reason_code=outcome.get("reason_code"),
        source_kind=outcome.get("source_kind"),
        source_locator=outcome.get("source_locator"),
        source_page=outcome.get("source_page"),
        source_image_sha256=outcome.get("source_image_sha256"),
        derived_storage_key=derived_storage_key if available else None,
        derived_sha256=outcome.get("derived_sha256") if available else None,
        mime_type="image/jpeg" if available else None,
        width=outcome.get("width") if available else None,
        height=outcome.get("height") if available else None,
    )
    db.add(row)
    await db.flush()
    return row

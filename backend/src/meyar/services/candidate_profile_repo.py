import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.candidate_profile_version import CandidateProfileVersion


async def create_profile_version(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    candidate_document_id: uuid.UUID,
    canonical_document_id: uuid.UUID,
    source_sha256: str,
    schema_version: str,
    prompt_version: str,
    model_provider: str,
    model_name: str,
    model_metadata: dict,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
    profile_content: dict | None = None,
) -> CandidateProfileVersion:
    """Inserts a new immutable profile version for candidate_id. Never
    updates an existing version row — a re-extraction always creates the
    next version_number. On a rare concurrent-write race, the DB's unique
    (candidate_id, version_number) constraint raises IntegrityError."""
    current_max = await db.execute(
        select(func.max(CandidateProfileVersion.version_number)).where(
            CandidateProfileVersion.candidate_id == candidate_id,
            CandidateProfileVersion.tenant_id == tenant_id,
        )
    )
    next_version = (current_max.scalar_one() or 0) + 1

    version = CandidateProfileVersion(
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_document_id=candidate_document_id,
        canonical_document_id=canonical_document_id,
        source_sha256=source_sha256,
        version_number=next_version,
        schema_version=schema_version,
        prompt_version=prompt_version,
        model_provider=model_provider,
        model_name=model_name,
        model_metadata=model_metadata,
        status=status,
        error_code=error_code,
        error_message=error_message,
        profile_content=profile_content,
    )
    db.add(version)
    await db.flush()
    return version


async def get_current_profile_version(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> CandidateProfileVersion | None:
    result = await db.execute(
        select(CandidateProfileVersion)
        .where(
            CandidateProfileVersion.candidate_id == candidate_id,
            CandidateProfileVersion.tenant_id == tenant_id,
        )
        .order_by(CandidateProfileVersion.version_number.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_profile_version_by_id(
    db: AsyncSession, *, tenant_id: uuid.UUID, profile_version_id: uuid.UUID
) -> CandidateProfileVersion | None:
    """Tenant-scoped lookup by the version's own id — the key enforcement
    point for evaluation input resolution (Slice 5): a profile_version_id
    belonging to another tenant simply does not resolve."""
    result = await db.execute(
        select(CandidateProfileVersion).where(
            CandidateProfileVersion.id == profile_version_id,
            CandidateProfileVersion.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def get_profile_version(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID, version_number: int
) -> CandidateProfileVersion | None:
    result = await db.execute(
        select(CandidateProfileVersion).where(
            CandidateProfileVersion.candidate_id == candidate_id,
            CandidateProfileVersion.tenant_id == tenant_id,
            CandidateProfileVersion.version_number == version_number,
        )
    )
    return result.scalar_one_or_none()

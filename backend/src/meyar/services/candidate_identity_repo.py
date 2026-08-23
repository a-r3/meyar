import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.candidate_identity_version import CandidateIdentityVersion


async def create_identity_version(
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
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
    identity_content: dict | None = None,
) -> CandidateIdentityVersion:
    """Inserts a new immutable identity version for candidate_id. Never
    updates an existing version row — a re-extraction always creates the
    next version_number, mirroring create_profile_version. On a rare
    concurrent-write race, the DB's unique (candidate_id, version_number)
    constraint raises IntegrityError."""
    current_max = await db.execute(
        select(func.max(CandidateIdentityVersion.version_number)).where(
            CandidateIdentityVersion.candidate_id == candidate_id,
            CandidateIdentityVersion.tenant_id == tenant_id,
        )
    )
    next_version = (current_max.scalar_one() or 0) + 1

    version = CandidateIdentityVersion(
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
        status=status,
        error_code=error_code,
        error_message=error_message,
        identity_content=identity_content,
    )
    db.add(version)
    await db.flush()
    return version


async def get_current_identity_version(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> CandidateIdentityVersion | None:
    result = await db.execute(
        select(CandidateIdentityVersion)
        .where(
            CandidateIdentityVersion.candidate_id == candidate_id,
            CandidateIdentityVersion.tenant_id == tenant_id,
        )
        .order_by(CandidateIdentityVersion.version_number.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_identity_version_by_id(
    db: AsyncSession, *, tenant_id: uuid.UUID, identity_version_id: uuid.UUID
) -> CandidateIdentityVersion | None:
    result = await db.execute(
        select(CandidateIdentityVersion).where(
            CandidateIdentityVersion.id == identity_version_id,
            CandidateIdentityVersion.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.job_criteria_version import JobCriteriaVersion


async def create_criteria_version(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    job_id: uuid.UUID,
    criteria: list[dict],
    created_by_api_key_id: uuid.UUID | None,
    unsupported_requirements: list[str] | None = None,
    needs_review_requirements: list[str] | None = None,
    result_limit: int = 20,
    eligible_only: bool = False,
) -> JobCriteriaVersion:
    """Insert a new immutable criteria version for job_id. Never updates an
    existing version row. Caller must have already verified job_id belongs
    to tenant_id. On a rare concurrent write race, the DB's unique
    (job_id, version_number) constraint raises IntegrityError — callers
    should surface that as 409 Conflict rather than silently retrying."""
    current_max = await db.execute(
        select(func.max(JobCriteriaVersion.version_number)).where(
            JobCriteriaVersion.job_id == job_id, JobCriteriaVersion.tenant_id == tenant_id
        )
    )
    next_version = (current_max.scalar_one() or 0) + 1

    version = JobCriteriaVersion(
        tenant_id=tenant_id,
        job_id=job_id,
        version_number=next_version,
        criteria=criteria,
        unsupported_requirements=unsupported_requirements or [],
        needs_review_requirements=needs_review_requirements or [],
        result_limit=result_limit,
        eligible_only=eligible_only,
        created_by_api_key_id=created_by_api_key_id,
    )
    db.add(version)
    await db.flush()
    return version


async def get_current_criteria_version(
    db: AsyncSession, *, tenant_id: uuid.UUID, job_id: uuid.UUID
) -> JobCriteriaVersion | None:
    result = await db.execute(
        select(JobCriteriaVersion)
        .where(JobCriteriaVersion.job_id == job_id, JobCriteriaVersion.tenant_id == tenant_id)
        .order_by(JobCriteriaVersion.version_number.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_criteria_version(
    db: AsyncSession, *, tenant_id: uuid.UUID, job_id: uuid.UUID, version_number: int
) -> JobCriteriaVersion | None:
    result = await db.execute(
        select(JobCriteriaVersion).where(
            JobCriteriaVersion.job_id == job_id,
            JobCriteriaVersion.tenant_id == tenant_id,
            JobCriteriaVersion.version_number == version_number,
        )
    )
    return result.scalar_one_or_none()


async def get_criteria_version_by_id(
    db: AsyncSession, *, tenant_id: uuid.UUID, criteria_version_id: uuid.UUID
) -> JobCriteriaVersion | None:
    """Tenant-scoped lookup by the version's own id — the key enforcement
    point for evaluation input resolution (Slice 5): a criteria_version_id
    belonging to another tenant simply does not resolve."""
    result = await db.execute(
        select(JobCriteriaVersion).where(
            JobCriteriaVersion.id == criteria_version_id,
            JobCriteriaVersion.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def list_criteria_versions(
    db: AsyncSession, *, tenant_id: uuid.UUID, job_id: uuid.UUID
) -> list[JobCriteriaVersion]:
    result = await db.execute(
        select(JobCriteriaVersion)
        .where(JobCriteriaVersion.job_id == job_id, JobCriteriaVersion.tenant_id == tenant_id)
        .order_by(JobCriteriaVersion.version_number.asc())
    )
    return list(result.scalars().all())

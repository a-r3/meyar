import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.job import JOB_STATUS_ACTIVE, JOB_STATUS_ARCHIVED, Job


async def create_job(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    title: str,
    duplicate_signature: str | None = None,
) -> Job:
    job = Job(
        tenant_id=tenant_id,
        title=title,
        status=JOB_STATUS_ACTIVE,
        duplicate_signature=duplicate_signature,
    )
    db.add(job)
    await db.flush()
    return job


async def get_job(db: AsyncSession, *, tenant_id: uuid.UUID, job_id: uuid.UUID) -> Job | None:
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()


async def find_active_duplicate_job(
    db: AsyncSession, *, tenant_id: uuid.UUID, duplicate_signature: str
) -> Job | None:
    """Application-level pre-check for the friendly rejection message —
    the actual concurrency-safe guard is the partial unique index on
    (tenant_id, duplicate_signature) WHERE status='ACTIVE', added by the
    add_job_lifecycle migration (see docs/DECISIONS.md D-028)."""
    result = await db.execute(
        select(Job).where(
            Job.tenant_id == tenant_id,
            Job.status == JOB_STATUS_ACTIVE,
            Job.duplicate_signature == duplicate_signature,
        )
    )
    return result.scalar_one_or_none()


async def archive_job(db: AsyncSession, *, tenant_id: uuid.UUID, job_id: uuid.UUID) -> Job | None:
    """Soft lifecycle transition only — never deletes the Job row, its
    criteria versions, or any Evaluation referencing it. Returns None
    (caller renders a safe 404) when job_id doesn't belong to tenant_id."""
    job = await get_job(db, tenant_id=tenant_id, job_id=job_id)
    if job is None:
        return None
    job.status = JOB_STATUS_ARCHIVED
    job.archived_at = datetime.now(UTC)
    await db.flush()
    return job

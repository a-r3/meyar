import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.job import Job


async def create_job(db: AsyncSession, *, tenant_id: uuid.UUID, title: str) -> Job:
    job = Job(tenant_id=tenant_id, title=title)
    db.add(job)
    await db.flush()
    return job


async def get_job(db: AsyncSession, *, tenant_id: uuid.UUID, job_id: uuid.UUID) -> Job | None:
    result = await db.execute(
        select(Job).where(Job.id == job_id, Job.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()

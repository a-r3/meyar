import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.core.auth import TenantContext, require_scope
from meyar.db import get_db
from meyar.models.job_criteria_version import JobCriteriaVersion
from meyar.schemas.job import (
    CriteriaVersionCreateRequest,
    JobCreateRequest,
    JobCriteriaVersionOut,
    JobOut,
)
from meyar.services.audit_repo import ACTOR_API_KEY, record_event
from meyar.services.job_criteria_repo import (
    create_criteria_version,
    get_criteria_version,
    get_current_criteria_version,
    list_criteria_versions,
)
from meyar.services.job_repo import create_job, get_job

router = APIRouter(tags=["jobs"])


def _version_out(version: JobCriteriaVersion) -> JobCriteriaVersionOut:
    return JobCriteriaVersionOut.model_validate(version, from_attributes=True)


async def _get_job_or_404(db: AsyncSession, tenant_id: uuid.UUID, job_id: uuid.UUID):
    job = await get_job(db, tenant_id=tenant_id, job_id=job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return job


@router.post("/jobs", status_code=status.HTTP_201_CREATED, response_model=JobOut)
async def post_job(
    body: JobCreateRequest,
    ctx: TenantContext = Depends(require_scope("jobs:write")),
    db: AsyncSession = Depends(get_db),
) -> JobOut:
    job = await create_job(db, tenant_id=ctx.tenant_id, title=body.title)
    version = await create_criteria_version(
        db,
        tenant_id=ctx.tenant_id,
        job_id=job.id,
        criteria=[c.model_dump(mode="json") for c in body.criteria],
        created_by_api_key_id=ctx.api_key_id,
    )
    await record_event(
        db,
        tenant_id=ctx.tenant_id,
        event_type="job.created",
        metadata={"job_id": str(job.id), "criteria_version": version.version_number},
        actor_type=ACTOR_API_KEY,
        actor_id=ctx.api_key_id,
    )
    await db.commit()
    return JobOut(
        id=job.id,
        tenant_id=job.tenant_id,
        title=job.title,
        created_at=job.created_at,
        current_criteria_version=_version_out(version),
    )


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job_detail(
    job_id: uuid.UUID,
    ctx: TenantContext = Depends(require_scope("jobs:read")),
    db: AsyncSession = Depends(get_db),
) -> JobOut:
    job = await _get_job_or_404(db, ctx.tenant_id, job_id)
    version = await get_current_criteria_version(db, tenant_id=ctx.tenant_id, job_id=job_id)
    if version is None:
        # Should not happen — every job is created with an initial version —
        # but fail loudly rather than returning a job with no criteria.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Job has no criteria version.",
        )
    return JobOut(
        id=job.id,
        tenant_id=job.tenant_id,
        title=job.title,
        created_at=job.created_at,
        current_criteria_version=_version_out(version),
    )


@router.post(
    "/jobs/{job_id}/criteria",
    status_code=status.HTTP_201_CREATED,
    response_model=JobCriteriaVersionOut,
)
async def post_job_criteria(
    job_id: uuid.UUID,
    body: CriteriaVersionCreateRequest,
    ctx: TenantContext = Depends(require_scope("jobs:write")),
    db: AsyncSession = Depends(get_db),
) -> JobCriteriaVersionOut:
    await _get_job_or_404(db, ctx.tenant_id, job_id)
    version = await create_criteria_version(
        db,
        tenant_id=ctx.tenant_id,
        job_id=job_id,
        criteria=[c.model_dump(mode="json") for c in body.criteria],
        created_by_api_key_id=ctx.api_key_id,
    )
    await record_event(
        db,
        tenant_id=ctx.tenant_id,
        event_type="job.criteria_version.created",
        metadata={"job_id": str(job_id), "criteria_version": version.version_number},
    )
    await db.commit()
    return _version_out(version)


@router.get("/jobs/{job_id}/criteria", response_model=list[JobCriteriaVersionOut])
async def list_job_criteria(
    job_id: uuid.UUID,
    ctx: TenantContext = Depends(require_scope("jobs:read")),
    db: AsyncSession = Depends(get_db),
) -> list[JobCriteriaVersionOut]:
    await _get_job_or_404(db, ctx.tenant_id, job_id)
    versions = await list_criteria_versions(db, tenant_id=ctx.tenant_id, job_id=job_id)
    return [_version_out(v) for v in versions]


@router.get("/jobs/{job_id}/criteria/{version_number}", response_model=JobCriteriaVersionOut)
async def get_job_criteria_version(
    job_id: uuid.UUID,
    version_number: int,
    ctx: TenantContext = Depends(require_scope("jobs:read")),
    db: AsyncSession = Depends(get_db),
) -> JobCriteriaVersionOut:
    await _get_job_or_404(db, ctx.tenant_id, job_id)
    version = await get_criteria_version(
        db, tenant_id=ctx.tenant_id, job_id=job_id, version_number=version_number
    )
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Criteria version not found."
        )
    return _version_out(version)

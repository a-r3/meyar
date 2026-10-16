"""Job lifecycle and provenance use cases (issue #45, D-116).

Compatibility contract, deliberately preserved and documented: the human
(UI / agent-confirmation) workflow rejects an identical ACTIVE job through the
duplicate signature; machine ``POST /api/v1/jobs`` creation has no signature
and permits duplicates. Nothing in this module changes either behavior."""

import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.agent.semantic_provenance import parse_agent_semantic_provenance
from meyar.application.errors import ApplicationError, ErrorCode
from meyar.application.principal import Principal
from meyar.models.job import JOB_STATUS_ARCHIVED
from meyar.schemas.api_jobs import (
    ApiJobPage,
    AuthoringChannel,
    CriteriaVersionProvenanceOut,
    DraftConfirmationLink,
    JobArchiveOut,
)
from meyar.services.agent_draft_confirmation_repo import get_confirmation_for_criteria_version
from meyar.services.audit_repo import record_event
from meyar.services.job_criteria_repo import get_criteria_version
from meyar.services.job_repo import archive_job, count_jobs_by_status, get_job
from meyar.ui.service import list_job_views


async def list_jobs(
    db: AsyncSession, principal: Principal, *, status: str, page: int, page_size: int
) -> ApiJobPage:
    """Bounded, newest-first (then id) jobs of one lifecycle status."""
    items = await list_job_views(
        db,
        tenant_id=principal.tenant_id,
        status=status,
        limit=page_size + 1,
        offset=(page - 1) * page_size,
    )
    total = await count_jobs_by_status(db, tenant_id=principal.tenant_id, status=status)
    return ApiJobPage(
        items=items[:page_size],
        page=page,
        page_size=page_size,
        total=total,
        has_next=len(items) > page_size,
    )


async def archive_job_for(
    db: AsyncSession, principal: Principal, *, job_id: uuid.UUID
) -> JobArchiveOut | None:
    """Soft, idempotent ACTIVE → ARCHIVED transition. Never deletes the job,
    its criteria versions or any evaluation. Archiving an already archived job
    changes nothing (the original ``archived_at`` is preserved, no new audit)."""
    job = await get_job(db, tenant_id=principal.tenant_id, job_id=job_id)
    if job is None:
        return None
    if job.status == JOB_STATUS_ARCHIVED:
        await db.rollback()
        return JobArchiveOut(
            job_id=job.id, status=job.status, archived_at=job.archived_at, already_archived=True
        )
    archived = await archive_job(db, tenant_id=principal.tenant_id, job_id=job_id)
    assert archived is not None
    await record_event(
        db,
        tenant_id=principal.tenant_id,
        event_type="job.archived",
        metadata={"job_id": str(archived.id)},
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
    )
    await db.commit()
    return JobArchiveOut(
        job_id=archived.id,
        status=archived.status,
        archived_at=archived.archived_at,
        already_archived=False,
    )


async def get_criteria_provenance(
    db: AsyncSession, principal: Principal, *, job_id: uuid.UUID, version_number: int
) -> CriteriaVersionProvenanceOut | None:
    version = await get_criteria_version(
        db, tenant_id=principal.tenant_id, job_id=job_id, version_number=version_number
    )
    if version is None:
        return None
    try:
        semantic = parse_agent_semantic_provenance(version.agent_semantic_provenance)
    except ValidationError:
        # Persisted provenance is validated on write; a malformed record is
        # corruption and is never papered over with "absent".
        raise ApplicationError(
            ErrorCode.INTERNAL_ERROR, status_code=500, reason="PROVENANCE_INVALID"
        ) from None
    confirmation = await get_confirmation_for_criteria_version(
        db, tenant_id=principal.tenant_id, criteria_version_id=version.id
    )
    channel: Any
    if confirmation is not None or semantic is not None:
        channel = AuthoringChannel.AGENT_CONFIRMED_DRAFT
    elif version.created_by_api_key_id is not None:
        channel = AuthoringChannel.MACHINE_API
    else:
        channel = AuthoringChannel.HUMAN_OR_LEGACY
    recorded = semantic is not None and semantic.non_scorable_requirements is not None
    return CriteriaVersionProvenanceOut(
        job_id=version.job_id,
        criteria_version_id=version.id,
        version_number=version.version_number,
        authoring_channel=channel,
        draft_confirmation=(
            DraftConfirmationLink(
                draft_id=confirmation.draft_id, confirmed_at=confirmation.confirmed_at
            )
            if confirmation is not None
            else None
        ),
        semantic_provenance=semantic,
        non_scorable_recorded=recorded,
        non_scorable_requirements=(
            list(semantic.non_scorable_requirements or []) if semantic is not None else []
        ),
        unsupported_requirements=list(version.unsupported_requirements),
        needs_review_requirements=list(version.needs_review_requirements),
    )

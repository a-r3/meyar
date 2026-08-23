import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.candidate import Candidate


async def create_candidate(db: AsyncSession, *, tenant_id: uuid.UUID) -> Candidate:
    candidate = Candidate(tenant_id=tenant_id)
    db.add(candidate)
    await db.flush()
    return candidate


async def get_candidate(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> Candidate | None:
    result = await db.execute(
        select(Candidate).where(Candidate.id == candidate_id, Candidate.tenant_id == tenant_id)
    )
    return result.scalar_one_or_none()


async def list_candidates_for_tenant(
    db: AsyncSession, *, tenant_id: uuid.UUID
) -> list[Candidate]:
    result = await db.execute(
        select(Candidate)
        .where(Candidate.tenant_id == tenant_id)
        .order_by(Candidate.id.asc())
    )
    return list(result.scalars().all())


async def delete_candidate_row(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> None:
    """DB-level cascade (ondelete=CASCADE FKs) removes CandidateDocument
    and CanonicalDocument rows. Callers must delete the underlying stored
    files via DocumentStorage separately — this only removes DB rows."""
    await db.execute(
        delete(Candidate).where(Candidate.id == candidate_id, Candidate.tenant_id == tenant_id)
    )

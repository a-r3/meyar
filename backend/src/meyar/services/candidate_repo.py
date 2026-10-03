import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.candidate import Candidate


async def count_candidates_for_tenant(db: AsyncSession, *, tenant_id: uuid.UUID) -> int:
    """Tenant-scoped total candidate count — no cross-tenant aggregation."""
    result = await db.execute(
        select(func.count()).select_from(Candidate).where(Candidate.tenant_id == tenant_id)
    )
    return int(result.scalar_one())


async def create_candidate(db: AsyncSession, *, tenant_id: uuid.UUID) -> Candidate:
    candidate = Candidate(tenant_id=tenant_id)
    db.add(candidate)
    await db.flush()
    return candidate


async def get_candidate(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    lock: bool = False,
    share: bool = False,
) -> Candidate | None:
    stmt = (
        select(Candidate).where(Candidate.id == candidate_id, Candidate.tenant_id == tenant_id)
    )
    if lock:
        # Deletion authority: excludes upload SHARE and FK KEY SHARE locks.
        # Callers acquire Tenant authority first and hold through commit/rollback.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    elif share:
        # Persistence authority for derived data: a concurrent delete waits for
        # the caller's commit instead of enumerating assets it cannot yet see.
        stmt = stmt.with_for_update(read=True).execution_options(populate_existing=True)
    result = await db.execute(stmt)
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

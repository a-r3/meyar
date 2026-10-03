import uuid
from collections.abc import Collection

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.candidate import Candidate
from meyar.models.candidate_profile_version import PROFILE_STATUS_COMPLETED, CandidateProfileVersion


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
    """Newest extraction attempt regardless of status; not fact authority."""
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


async def get_latest_profile_version_for_document(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_document_id: uuid.UUID
) -> CandidateProfileVersion | None:
    """Tenant-scoped lookup of the most recent extraction attempt tied to
    one specific CandidateDocument (Slice 14). Used to derive per-document
    processing readiness from existing provenance without a redundant
    status column: a COMPLETED row here means this exact document's
    profile is already extracted and current; any other status, or no
    row at all, means extraction must (re)run for this document."""
    result = await db.execute(
        select(CandidateProfileVersion)
        .where(
            CandidateProfileVersion.tenant_id == tenant_id,
            CandidateProfileVersion.candidate_document_id == candidate_document_id,
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


async def list_current_profile_versions_for_tenant(
    db: AsyncSession, *, tenant_id: uuid.UUID
) -> list[CandidateProfileVersion]:
    """Diagnostic latest-attempt rows, max version_number regardless of status.

    Legacy API retained for operational/history consumers. Professional search
    and ranking MUST use list_effective_profile_versions_for_tenant and apply
    current evidence authority (D-100).
    """
    latest = (
        select(
            CandidateProfileVersion.candidate_id,
            func.max(CandidateProfileVersion.version_number).label("max_version"),
        )
        .where(CandidateProfileVersion.tenant_id == tenant_id)
        .group_by(CandidateProfileVersion.candidate_id)
        .subquery()
    )
    result = await db.execute(
        select(CandidateProfileVersion)
        .join(
            latest,
            (CandidateProfileVersion.candidate_id == latest.c.candidate_id)
            & (CandidateProfileVersion.version_number == latest.c.max_version),
        )
        .where(CandidateProfileVersion.tenant_id == tenant_id)
    )
    return list(result.scalars().all())


async def get_current_profile_versions_for_candidates(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, CandidateProfileVersion]:
    """ONE bounded query (issue #86): the CURRENT CandidateProfileVersion
    (max version_number — the same definition as get_current_profile_version
    and list_current_profile_versions_for_tenant) for exactly the given
    candidates of this tenant, and only for candidates that still exist
    here. ``candidate_ids`` is bounded by the caller (ResultSet members,
    <= MAX_SEARCH_LIMIT) — never the whole tenant. A candidate with no
    profile version, or no longer present, is simply absent."""
    if not candidate_ids:
        return {}
    result = await db.execute(
        select(CandidateProfileVersion)
        .join(
            Candidate,
            (Candidate.id == CandidateProfileVersion.candidate_id)
            & (Candidate.tenant_id == CandidateProfileVersion.tenant_id),
        )
        .where(
            CandidateProfileVersion.tenant_id == tenant_id,
            CandidateProfileVersion.candidate_id.in_(list(candidate_ids)),
        )
        .order_by(
            CandidateProfileVersion.candidate_id,
            CandidateProfileVersion.version_number.desc(),
        )
        .distinct(CandidateProfileVersion.candidate_id)
    )
    return {version.candidate_id: version for version in result.scalars().all()}


# Explicit operational names; legacy "current" helpers retain attempt semantics.
get_latest_profile_attempt = get_current_profile_version
list_latest_profile_attempts_for_tenant = list_current_profile_versions_for_tenant
get_latest_profile_attempts_for_candidates = get_current_profile_versions_for_candidates


def _effective_profile_statement(
    tenant_id: uuid.UUID, candidate_ids: Collection[uuid.UUID] | None = None
) -> Select[tuple[CandidateProfileVersion]]:
    """Select facts only inside each latest attempt's document boundary.

    Both DISTINCT ON stages run in one tenant-scoped SQL statement. The first
    projects only latest-attempt provenance (all statuses); the second selects
    its document's newest COMPLETED, without loading historical content.
    """
    latest_query = select(
        CandidateProfileVersion.tenant_id,
        CandidateProfileVersion.candidate_id,
        CandidateProfileVersion.candidate_document_id,
    ).where(CandidateProfileVersion.tenant_id == tenant_id)
    if candidate_ids is not None:
        latest_query = latest_query.where(CandidateProfileVersion.candidate_id.in_(candidate_ids))
    latest = (
        latest_query.order_by(
            CandidateProfileVersion.candidate_id, CandidateProfileVersion.version_number.desc()
        )
        .distinct(CandidateProfileVersion.candidate_id)
        .subquery()
    )
    return (
        select(CandidateProfileVersion)
        .join(
            latest,
            (CandidateProfileVersion.tenant_id == latest.c.tenant_id)
            & (CandidateProfileVersion.candidate_id == latest.c.candidate_id)
            & (CandidateProfileVersion.candidate_document_id == latest.c.candidate_document_id),
        )
        .join(
            Candidate,
            (Candidate.id == CandidateProfileVersion.candidate_id)
            & (Candidate.tenant_id == CandidateProfileVersion.tenant_id),
        )
        .where(
            CandidateProfileVersion.tenant_id == tenant_id,
            CandidateProfileVersion.status == PROFILE_STATUS_COMPLETED,
        )
        .order_by(
            CandidateProfileVersion.candidate_id, CandidateProfileVersion.version_number.desc()
        )
        .distinct(CandidateProfileVersion.candidate_id)
    )


async def get_effective_profile_version(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> CandidateProfileVersion | None:
    """Newest COMPLETED inside the latest attempt's document (D-100).

    Caller MUST authorize_profile_version. Invalid selected evidence fails
    closed without scanning older completions, including in the same document.
    """
    rows = await db.scalars(_effective_profile_statement(tenant_id, [candidate_id]))
    return rows.one_or_none()


async def get_effective_profile_versions_for_candidates(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, CandidateProfileVersion]:
    """One bounded query, same document rule as the single selector.

    Consumers MUST authorize_profile_versions. No historical content loading
    or cross-document fallback; candidate ownership remains tenant-scoped.
    """
    if not candidate_ids:
        return {}
    rows = await db.scalars(_effective_profile_statement(tenant_id, candidate_ids))
    return {version.candidate_id: version for version in rows}


async def list_effective_profile_versions_for_tenant(
    db: AsyncSession, *, tenant_id: uuid.UUID
) -> list[CandidateProfileVersion]:
    """One set-based query, latest-attempt document only; MUST authorize on read."""
    rows = await db.scalars(_effective_profile_statement(tenant_id))
    return list(rows)

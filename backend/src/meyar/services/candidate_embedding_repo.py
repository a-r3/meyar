import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meyar.models.candidate_embedding_version import CandidateEmbeddingVersion


async def create_embedding_version(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    candidate_profile_version_id: uuid.UUID,
    provider: str,
    model_name: str,
    model_revision: str,
    serializer_version: str,
    source_sha256: str,
    embedding_dimensions: int,
    embedding: list[float],
) -> CandidateEmbeddingVersion:
    """Inserts a new immutable embedding row. Never updates an existing
    row — a re-embed of the same (profile version, provider, model,
    revision) must go through get_embedding_version_by_source first and
    reuse it; this function always creates. On a rare concurrent-write
    race, the DB's unique constraint raises IntegrityError — the final
    idempotency backstop."""
    version = CandidateEmbeddingVersion(
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_profile_version_id=candidate_profile_version_id,
        provider=provider,
        model_name=model_name,
        model_revision=model_revision,
        serializer_version=serializer_version,
        source_sha256=source_sha256,
        embedding_dimensions=embedding_dimensions,
        embedding=embedding,
    )
    db.add(version)
    await db.flush()
    return version


async def get_embedding_version_by_source(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_profile_version_id: uuid.UUID,
    provider: str,
    model_name: str,
    model_revision: str,
) -> CandidateEmbeddingVersion | None:
    """The idempotency lookup: does an embedding already exist for this
    exact (profile version, provider, model, revision)? If so, the
    caller must reuse it rather than calling the embedding provider
    again."""
    result = await db.execute(
        select(CandidateEmbeddingVersion).where(
            CandidateEmbeddingVersion.tenant_id == tenant_id,
            CandidateEmbeddingVersion.candidate_profile_version_id
            == candidate_profile_version_id,
            CandidateEmbeddingVersion.provider == provider,
            CandidateEmbeddingVersion.model_name == model_name,
            CandidateEmbeddingVersion.model_revision == model_revision,
        )
    )
    return result.scalar_one_or_none()


async def get_embedding_version_by_id(
    db: AsyncSession, *, tenant_id: uuid.UUID, embedding_version_id: uuid.UUID
) -> CandidateEmbeddingVersion | None:
    result = await db.execute(
        select(CandidateEmbeddingVersion).where(
            CandidateEmbeddingVersion.id == embedding_version_id,
            CandidateEmbeddingVersion.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def list_embedding_versions_for_candidate(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID
) -> list[CandidateEmbeddingVersion]:
    result = await db.execute(
        select(CandidateEmbeddingVersion)
        .where(
            CandidateEmbeddingVersion.tenant_id == tenant_id,
            CandidateEmbeddingVersion.candidate_id == candidate_id,
        )
        .order_by(CandidateEmbeddingVersion.created_at.asc())
    )
    return list(result.scalars().all())

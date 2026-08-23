import uuid
from collections.abc import Mapping

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

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
    row — a re-embed with the exact same seven-field identity (profile
    version, provider, model, revision, serializer_version,
    source_sha256) must go through get_embedding_version_by_source first
    and reuse it; this function always creates. On a rare concurrent-
    write race, the DB's unique constraint raises IntegrityError — the
    final idempotency backstop."""
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
    serializer_version: str,
    source_sha256: str,
) -> CandidateEmbeddingVersion | None:
    """The idempotency lookup: does an embedding already exist for this
    EXACT seven-field identity — profile version, provider, model,
    revision, serializer_version, AND source_sha256? If so, the caller
    must reuse it rather than calling the embedding provider again.
    Deliberately does not match on a subset of these fields: a
    serializer revision bump, or a source-text change under an
    unchanged serializer_version (e.g. an accidental behavior change),
    must never be masked by an older row's vector — see docs/DECISIONS.md
    D-014."""
    result = await db.execute(
        select(CandidateEmbeddingVersion).where(
            CandidateEmbeddingVersion.tenant_id == tenant_id,
            CandidateEmbeddingVersion.candidate_profile_version_id
            == candidate_profile_version_id,
            CandidateEmbeddingVersion.provider == provider,
            CandidateEmbeddingVersion.model_name == model_name,
            CandidateEmbeddingVersion.model_revision == model_revision,
            CandidateEmbeddingVersion.serializer_version == serializer_version,
            CandidateEmbeddingVersion.source_sha256 == source_sha256,
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


async def search_compatible_embeddings(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    profile_version_source_hashes: Mapping[uuid.UUID, str],
    provider: str,
    model_name: str,
    model_revision: str,
    serializer_version: str,
    embedding_dimensions: int,
    query_vector: list[float],
) -> list[tuple[uuid.UUID, uuid.UUID, uuid.UUID, float]]:
    """Slice 8 semantic retrieval. Returns (candidate_id,
    candidate_profile_version_id, embedding_version_id, cosine_distance).

    `profile_version_source_hashes` maps each eligible candidate's
    CURRENT candidate_profile_version_id to the source_sha256 the
    CURRENT canonical professional serializer produces for that exact
    profile content (see meyar.embedding.serializer,
    build_professional_embedding_text/compute_source_sha256, called by
    meyar.search.service). An embedding row only participates if its
    OWN (candidate_profile_version_id, source_sha256) pair is exactly
    one of these — never merely a profile-version match, and never
    "whichever row happens to come back first/last." Slice 7's DB
    UniqueConstraint on (tenant_id, candidate_profile_version_id,
    provider, model_name, model_revision, serializer_version,
    source_sha256) guarantees at most one row can match each pair for a
    fixed provider/model/revision/serializer — so a candidate can
    contribute at most one row here by construction, independent of SQL
    row-return order (see docs/DECISIONS.md D-015).

    Also exactly matches the six-field active embedding configuration —
    never mixes providers/models/revisions/serializer versions/
    dimensions in one similarity computation. The tenant/profile-
    version+hash/config filter is applied in an inner subquery so
    pgvector's cosine_distance operator (<=>) is only ever evaluated
    against already-compatible rows. Returns an empty list without
    querying if no candidate pairs are given (e.g. no structurally-
    eligible candidates)."""
    if not profile_version_source_hashes:
        return []

    pairs = list(profile_version_source_hashes.items())
    filtered = (
        select(CandidateEmbeddingVersion)
        .where(
            CandidateEmbeddingVersion.tenant_id == tenant_id,
            tuple_(
                CandidateEmbeddingVersion.candidate_profile_version_id,
                CandidateEmbeddingVersion.source_sha256,
            ).in_(pairs),
            CandidateEmbeddingVersion.provider == provider,
            CandidateEmbeddingVersion.model_name == model_name,
            CandidateEmbeddingVersion.model_revision == model_revision,
            CandidateEmbeddingVersion.serializer_version == serializer_version,
            CandidateEmbeddingVersion.embedding_dimensions == embedding_dimensions,
        )
        .subquery()
    )
    row = aliased(CandidateEmbeddingVersion, filtered)

    result = await db.execute(
        select(
            row.candidate_id,
            row.candidate_profile_version_id,
            row.id,
            row.embedding.cosine_distance(query_vector).label("distance"),
        )
    )
    return [(r[0], r[1], r[2], r[3]) for r in result.all()]

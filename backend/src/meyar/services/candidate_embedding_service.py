import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.embedding.provider import EmbeddingProvider, EmbeddingProviderError
from meyar.embedding.serializer import (
    SERIALIZER_VERSION,
    build_professional_embedding_text,
    compute_source_sha256,
)
from meyar.models.candidate_embedding_version import CandidateEmbeddingVersion
from meyar.models.candidate_profile_version import PROFILE_STATUS_COMPLETED
from meyar.services.audit_repo import record_event
from meyar.services.candidate_embedding_repo import (
    create_embedding_version,
    get_embedding_version_by_source,
)
from meyar.services.candidate_profile_repo import get_current_profile_version
from meyar.services.profile_authority import ProfileAuthorityError, authorize_profile_version


class EmbeddingPreconditionError(Exception):
    """Raised when embedding cannot even be attempted (no completed
    profile version to embed yet) — nothing to version, so no
    CandidateEmbeddingVersion row is created for this case."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


async def embed_candidate_profile(
    db: AsyncSession,
    provider: EmbeddingProvider,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    max_input_chars: int,
) -> tuple[CandidateEmbeddingVersion, bool]:
    """Embeds the candidate's CURRENT CandidateProfileVersion's
    professional content — never CandidateIdentity, which this function
    never even queries. "Current" is derived from
    get_current_profile_version (max version_number) at call time, so an
    embedding is only ever created against the candidate's latest
    professional facts.

    Idempotent: if a CandidateEmbeddingVersion already exists for the
    exact seven-field identity (profile version, provider, model,
    revision, serializer_version, source_sha256), that row is returned
    unchanged and the embedding provider is never called again — see
    candidate_embedding_repo.get_embedding_version_by_source and the
    table's unique constraint (the concurrency backstop). A serializer
    revision, or any change to the serialized professional text under
    an unchanged serializer_version, always produces a distinct
    embedding — it is never silently masked by an older row (see
    docs/DECISIONS.md D-014). Returns (version, was_reused).

    Raises EmbeddingPreconditionError if there is no completed profile
    version yet, or if the serialized professional text exceeds
    max_input_chars. Raises EmbeddingProviderError (after an audit
    event) if the provider itself fails — never persists a partial/
    invalid vector; a failed attempt produces no CandidateEmbeddingVersion
    row at all."""
    profile_version = await get_current_profile_version(
        db, tenant_id=tenant_id, candidate_id=candidate_id
    )
    if profile_version is None:
        raise EmbeddingPreconditionError(
            "NO_PROFILE_VERSION",
            "No CandidateProfileVersion exists for this candidate — run profile "
            "extraction first.",
        )
    profile_content = profile_version.profile_content
    if profile_version.status != PROFILE_STATUS_COMPLETED or profile_content is None:
        raise EmbeddingPreconditionError(
            "PROFILE_NOT_COMPLETED",
            "The candidate's current profile version is not COMPLETED "
            f"(status={profile_version.status}).",
        )

    try:
        authorized = await authorize_profile_version(db, version=profile_version)
    except ProfileAuthorityError as exc:
        raise EmbeddingPreconditionError(exc.code, str(exc)) from exc
    profile_content = authorized.model_dump(mode="json")

    # The canonical text and its hash must be computed BEFORE deciding
    # whether an existing embedding is reusable — reuse identity depends
    # on source_sha256/serializer_version, not just the profile version.
    text = build_professional_embedding_text(profile_content)
    if len(text) > max_input_chars:
        raise EmbeddingPreconditionError(
            "INPUT_TOO_LARGE",
            f"Professional embedding text ({len(text)} chars) exceeds the configured "
            f"maximum ({max_input_chars} chars).",
        )
    source_sha256 = compute_source_sha256(text)

    existing = await get_embedding_version_by_source(
        db,
        tenant_id=tenant_id,
        candidate_profile_version_id=profile_version.id,
        provider=provider.provider_name,
        model_name=provider.model_name,
        model_revision=provider.model_revision,
        serializer_version=SERIALIZER_VERSION,
        source_sha256=source_sha256,
    )
    if existing is not None:
        await record_event(
            db,
            tenant_id=tenant_id,
            event_type="CANDIDATE_EMBEDDING_REUSED",
            metadata={
                "candidate_id": str(candidate_id),
                "embedding_version_id": str(existing.id),
                "profile_version_id": str(profile_version.id),
            },
        )
        return existing, True

    try:
        result = await provider.embed(text)
    except EmbeddingProviderError as exc:
        await record_event(
            db,
            tenant_id=tenant_id,
            event_type="CANDIDATE_EMBEDDING_FAILED",
            metadata={
                "candidate_id": str(candidate_id),
                "profile_version_id": str(profile_version.id),
                "error_code": exc.code,
            },
        )
        raise

    version = await create_embedding_version(
        db,
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_profile_version_id=profile_version.id,
        provider=result.provider,
        model_name=result.model_name,
        model_revision=result.model_revision,
        serializer_version=SERIALIZER_VERSION,
        source_sha256=source_sha256,
        embedding_dimensions=result.dimensions,
        embedding=result.vector,
    )
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="CANDIDATE_EMBEDDING_CREATED",
        metadata={
            "candidate_id": str(candidate_id),
            "embedding_version_id": str(version.id),
            "profile_version_id": str(profile_version.id),
            "dimensions": result.dimensions,
            "provider": result.provider,
            "model": result.model_name,
        },
    )
    return version, False

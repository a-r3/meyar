import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from meyar.embedding.provider import (
    EmbeddingBusyError,
    EmbeddingInvalidOutputError,
    EmbeddingProvider,
    EmbeddingProviderError,
    EmbeddingResult,
)
from meyar.embedding.serializer import (
    SERIALIZER_VERSION,
    embedding_source,
)
from meyar.models.candidate_embedding_version import CandidateEmbeddingVersion
from meyar.models.candidate_profile_version import (
    PROFILE_STATUS_COMPLETED,
    CandidateProfileVersion,
)
from meyar.services.audit_repo import record_event
from meyar.services.candidate_embedding_repo import (
    EmbeddingCompatibility,
    create_embedding_version,
    find_compatible_embedding,
    get_embedding_version_by_source,
)
from meyar.services.candidate_profile_repo import (
    get_effective_profile_version,
    get_latest_profile_attempt,
)
from meyar.services.profile_authority import ProfileAuthorityError, authorize_profile_version
from meyar.services.tenant_authority import require_active_tenant


class EmbeddingPreconditionError(Exception):
    """Raised when embedding cannot even be attempted (no completed
    profile version to embed yet) — nothing to version, so no
    CandidateEmbeddingVersion row is created for this case."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def validate_embedding_result(
    result: EmbeddingResult, compatibility: EmbeddingCompatibility
) -> None:
    """One application check for direct and folder embedding output before persistence.

    The provider validates numeric vector quality; the application owns the trusted
    active provenance and configured dimensions (which the provider does not know).
    """
    if (
        (result.provider, result.model_name, result.model_revision)
        != (compatibility.provider, compatibility.model_name, compatibility.model_revision)
        or SERIALIZER_VERSION != compatibility.serializer_version
        or result.dimensions != len(result.vector)
        or (
            compatibility.embedding_dimensions is not None
            and result.dimensions != compatibility.embedding_dimensions
        )
    ):
        raise EmbeddingInvalidOutputError("Embedding result does not match active config.")


@dataclass(frozen=True)
class EmbeddingPlan:
    """DB-derived, immutable input to embedding (issue #46 S9)."""

    profile_version_id: uuid.UUID
    text: str
    source_sha256: str
    existing: CandidateEmbeddingVersion | None


async def prepare_embedding(
    db: AsyncSession,
    provider: EmbeddingProvider,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    max_input_chars: int,
    profile_version: CandidateProfileVersion | None = None,
    compatibility: EmbeddingCompatibility | None = None,
) -> EmbeddingPlan:
    """Short DB phase: authorize the profile to embed, serialize its professional
    text and look up an identical existing embedding. Raises
    EmbeddingPreconditionError when there is nothing embeddable.

    ``profile_version=None`` (API/CLI) embeds the Candidate's D-100 EFFECTIVE profile.
    Folder reconciliation (issue #46 S9) passes the tracked document's OWN
    COMPLETED profile version instead: a Candidate may have several tracked
    documents but only one effective profile, and readiness of each tracked document
    is its own complete, evidence-authorized provenance chain. The version is still
    run through authorize_profile_version, so an unauthorized profile is never
    embedded; search only ever matches embeddings of the effective version."""
    await require_active_tenant(db, tenant_id)
    if compatibility is not None and (
        provider.provider_name != compatibility.provider
        or provider.model_name != compatibility.model_name
        or provider.model_revision != compatibility.model_revision
        or SERIALIZER_VERSION != compatibility.serializer_version
    ):
        raise EmbeddingPreconditionError(
            "EMBEDDING_PROVIDER_CONFIG_MISMATCH",
            "The embedding provider does not match the active embedding configuration.",
        )
    if profile_version is not None and (
        profile_version.tenant_id != tenant_id or profile_version.candidate_id != candidate_id
    ):
        raise EmbeddingPreconditionError(
            "NO_PROFILE_VERSION", "The profile version does not belong to this candidate."
        )
    if profile_version is None:
        profile_version = await get_effective_profile_version(
            db, tenant_id=tenant_id, candidate_id=candidate_id
        )
    if profile_version is None:
        latest = await get_latest_profile_attempt(
            db, tenant_id=tenant_id, candidate_id=candidate_id
        )
        if latest is not None:
            raise EmbeddingPreconditionError(
                "PROFILE_NOT_COMPLETED", "No completed professional profile is available."
            )
        raise EmbeddingPreconditionError(
            "NO_PROFILE_VERSION",
            "No CandidateProfileVersion exists for this candidate — run profile extraction first.",
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
    text, source_sha256 = embedding_source(profile_content)
    if len(text) > max_input_chars:
        raise EmbeddingPreconditionError(
            "INPUT_TOO_LARGE",
            f"Professional embedding text ({len(text)} chars) exceeds the configured "
            f"maximum ({max_input_chars} chars).",
        )
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
    if compatibility is not None:
        compatible = await find_compatible_embedding(
            db,
            tenant_id=tenant_id,
            candidate_profile_version_id=profile_version.id,
            source_sha256=source_sha256,
            compatibility=compatibility,
        )
        if compatible is None and existing is not None:
            # The immutable identity is taken by a row the active configuration cannot
            # use (different dimensions). It is never deleted or replaced (D-014).
            raise EmbeddingPreconditionError(
                "EMBEDDING_IDENTITY_INCOMPATIBLE",
                "An embedding with this immutable identity exists but does not match "
                "the active embedding configuration.",
            )
        existing = compatible
    return EmbeddingPlan(profile_version.id, text, source_sha256, existing)


async def record_embedding_reused(
    db: AsyncSession, *, tenant_id: uuid.UUID, candidate_id: uuid.UUID, plan: EmbeddingPlan
) -> None:
    assert plan.existing is not None
    await record_event(
        db,
        tenant_id=tenant_id,
        event_type="CANDIDATE_EMBEDDING_REUSED",
        metadata={
            "candidate_id": str(candidate_id),
            "embedding_version_id": str(plan.existing.id),
            "profile_version_id": str(plan.profile_version_id),
        },
    )


async def record_embedding_failure(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    plan: EmbeddingPlan,
    exc: EmbeddingProviderError,
) -> None:
    await record_event(
        db,
        tenant_id=tenant_id,
        # Issue #85: a busy shared inference gate is a transient
        # deferral (no embedding attempted), not a provider failure.
        event_type=(
            "CANDIDATE_EMBEDDING_DEFERRED"
            if isinstance(exc, EmbeddingBusyError)
            else "CANDIDATE_EMBEDDING_FAILED"
        ),
        metadata={
            "candidate_id": str(candidate_id),
            "profile_version_id": str(plan.profile_version_id),
            "error_code": exc.code,
        },
    )


async def persist_embedding(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    plan: EmbeddingPlan,
    result: EmbeddingResult,
) -> CandidateEmbeddingVersion:
    """Short DB phase: the immutable embedding row + audit."""
    await require_active_tenant(db, tenant_id)
    version = await create_embedding_version(
        db,
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        candidate_profile_version_id=plan.profile_version_id,
        provider=result.provider,
        model_name=result.model_name,
        model_revision=result.model_revision,
        serializer_version=SERIALIZER_VERSION,
        source_sha256=plan.source_sha256,
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
            "profile_version_id": str(plan.profile_version_id),
            "dimensions": result.dimensions,
            "provider": result.provider,
            "model": result.model_name,
        },
    )
    return version


async def embed_candidate_profile(
    db: AsyncSession,
    provider: EmbeddingProvider,
    *,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    max_input_chars: int,
    compatibility: EmbeddingCompatibility,
) -> tuple[CandidateEmbeddingVersion, bool]:
    """Embeds the candidate's CURRENT CandidateProfileVersion's
    professional content — never CandidateIdentity, which this function
    never even queries. "Current" is derived from
    get_effective_profile_version (latest-attempt document's newest COMPLETED,
    then current evidence verification) at call time. Only same-document
    failed/manual-review attempts may preserve accepted facts; an embedding stays
    bound to the exact selected version.

    The caller supplies explicit trusted active compatibility, including dimensions;
    provider identity and returned result are checked before anything is persisted.
    Idempotent: if a compatible CandidateEmbeddingVersion already exists for the
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
    if compatibility.embedding_dimensions is None:
        raise EmbeddingPreconditionError(
            "EMBEDDING_CONFIG_REQUIRED", "Configured embedding dimensions are required."
        )
    plan = await prepare_embedding(
        db, provider, tenant_id=tenant_id, candidate_id=candidate_id,
        max_input_chars=max_input_chars,
        compatibility=compatibility,
    )
    if plan.existing is not None:
        await record_embedding_reused(
            db, tenant_id=tenant_id, candidate_id=candidate_id, plan=plan
        )
        return plan.existing, True

    try:
        result = await provider.embed(plan.text)
        validate_embedding_result(result, compatibility)
    except EmbeddingProviderError as exc:
        await record_embedding_failure(
            db, tenant_id=tenant_id, candidate_id=candidate_id, plan=plan, exc=exc
        )
        raise

    version = await persist_embedding(
        db, tenant_id=tenant_id, candidate_id=candidate_id, plan=plan, result=result
    )
    return version, False

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base

# No digest/revision reported by the provider for this model.
MODEL_REVISION_UNKNOWN = ""


class CandidateEmbeddingVersion(Base):
    """Immutable, provenance-traceable local embedding for one
    CandidateProfileVersion's professional content — never CandidateIdentity.
    A row here always represents a successfully validated vector; a failed
    attempt is never persisted (see meyar.services.candidate_embedding_service).
    "Current" is NOT simply the newest row: it is the row whose
    candidate_profile_version_id equals the candidate's current
    CandidateProfileVersion (get_current_profile_version) AND whose
    provider/model_name/model_revision/serializer_version/source_sha256
    exactly match the requested embedding configuration — an older
    embedding for a superseded profile version, OR for a superseded
    serializer/source, is STALE even if no newer embedding exists yet.
    The unique constraint is the idempotency backstop: re-embedding the
    exact same seven-field identity (profile version, provider, model,
    revision, serializer_version, source_sha256) reuses this row rather
    than creating a duplicate — a serializer or source-text change
    always produces a distinct row, never a silently-reused stale
    vector. See docs/DECISIONS.md."""

    __tablename__ = "candidate_embedding_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "candidate_profile_version_id",
            "provider",
            "model_name",
            "model_revision",
            "serializer_version",
            "source_sha256",
            name="uq_candidate_embedding_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_profile_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate_profile_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    # "" (MODEL_REVISION_UNKNOWN), never NULL — Postgres unique constraints
    # treat each NULL as distinct, which would silently defeat the
    # idempotency backstop above for providers with no digest.
    model_revision: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    serializer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    # Dimension-agnostic column (no fixed length) — the final production
    # embedding model/dimension is not approved yet (see D- entry); a
    # physical ANN index is deferred until it is.
    embedding: Mapped[list[float]] = mapped_column(Vector(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

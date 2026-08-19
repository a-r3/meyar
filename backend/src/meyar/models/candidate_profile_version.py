import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base

PROFILE_STATUS_COMPLETED = "COMPLETED"
PROFILE_STATUS_FAILED = "FAILED"
PROFILE_STATUS_MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"

SCHEMA_VERSION = "candidate-profile-v1"


class CandidateProfileVersion(Base):
    """Immutable, versioned AI extraction result for one candidate — never
    updated in place. A re-extraction always inserts a new row with the
    next version_number, mirroring JobCriteriaVersion. Carries full
    provenance so an extraction can be explained/reproduced later. See
    docs/MASTER_SPEC.md §13-15 (Slice 4 spec)."""

    __tablename__ = "candidate_profile_versions"
    __table_args__ = (
        UniqueConstraint("candidate_id", "version_number", name="uq_candidate_profile_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate_documents.id", ondelete="CASCADE"), nullable=False
    )
    canonical_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("canonical_documents.id", ondelete="CASCADE"), nullable=False
    )
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    profile_content: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

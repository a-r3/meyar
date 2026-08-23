import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base

IDENTITY_STATUS_COMPLETED = "COMPLETED"
IDENTITY_STATUS_FAILED = "FAILED"
IDENTITY_STATUS_MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"

IDENTITY_SCHEMA_VERSION = "candidate-identity-v1"


class CandidateIdentityVersion(Base):
    """Immutable, versioned identity extraction result for one candidate —
    never updated in place, mirroring CandidateProfileVersion. Deliberately
    a SEPARATE table from CandidateProfile/CandidateProfileVersion: this is
    the only place `full_name`/`email`/`phone` are ever stored. Never read
    by matching, evaluation, search, ranking, or embedding generation — see
    docs/MASTER_SPEC.md §5 and docs/DECISIONS.md. "Current" is derived from
    version_number (max wins), same pattern as CandidateProfileVersion —
    no fragile boolean flag."""

    __tablename__ = "candidate_identity_versions"
    __table_args__ = (
        UniqueConstraint("candidate_id", "version_number", name="uq_candidate_identity_version"),
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
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # PII lives only here — never logged, never in audit metadata, never
    # read outside authorized presentation/CLI-with-explicit-flag paths.
    identity_content: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

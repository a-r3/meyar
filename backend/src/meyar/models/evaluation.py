import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base

EVALUATION_STATUS_COMPLETED = "COMPLETED"
EVALUATION_STATUS_FAILED = "FAILED"


class Evaluation(Base):
    """Immutable evaluation result — never updated in place. A re-run
    (different profile version, criteria version, or policy version)
    always creates a brand-new row; nothing here is ever mutated after
    creation. See docs/MASTER_SPEC.md (Slice 5 spec) §2-3/§15."""

    __tablename__ = "evaluations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False, index=True
    )
    candidate_profile_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("candidate_profile_versions.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_criteria_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("job_criteria_versions.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    overall_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    policy_engine_version: Mapped[str] = mapped_column(String(32), nullable=False)
    criterion_results: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

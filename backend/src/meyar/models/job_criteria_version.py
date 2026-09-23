import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base


class JobCriteriaVersion(Base):
    """An immutable, versioned snapshot of a job's matching criteria. Never
    updated in place — a change always inserts a new row with the next
    version_number. Every evaluation must reference one of these rows by
    id so results remain reproducible. See docs/MASTER_SPEC.md §13."""

    __tablename__ = "job_criteria_versions"
    __table_args__ = (
        UniqueConstraint("job_id", "version_number", name="uq_job_version"),
        CheckConstraint("result_limit BETWEEN 1 AND 100", name="ck_job_criteria_result_limit"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    criteria: Mapped[list] = mapped_column(JSON, nullable=False)
    unsupported_requirements: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    needs_review_requirements: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    result_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=20)
    eligible_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by_api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

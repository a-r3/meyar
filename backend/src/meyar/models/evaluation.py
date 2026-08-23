import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Index, Numeric, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base

EVALUATION_STATUS_COMPLETED = "COMPLETED"
EVALUATION_STATUS_FAILED = "FAILED"


class Evaluation(Base):
    """Immutable evaluation result — never updated in place. Different
    profile/criteria/date/policy provenance creates a new row; an exact
    scored-provenance repeat may reuse the existing row. See D-017."""

    __tablename__ = "evaluations"
    __table_args__ = (
        Index(
            "uq_evaluations_scored_provenance",
            "tenant_id",
            "candidate_profile_version_id",
            "job_criteria_version_id",
            "evaluation_as_of_date",
            "policy_engine_version",
            "scoring_policy_version",
            unique=True,
            postgresql_where=text(
                "evaluation_as_of_date IS NOT NULL AND scoring_policy_version IS NOT NULL"
            ),
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
    evaluation_as_of_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    numeric_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    scoring_policy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    score_explanation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    criterion_results: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

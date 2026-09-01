import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base

JOB_STATUS_ACTIVE = "ACTIVE"
JOB_STATUS_ARCHIVED = "ARCHIVED"


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        # An identical (title + criteria) signature may exist any number
        # of times across ARCHIVED jobs (an archived duplicate never
        # blocks a new active one) and NULL signatures (API/CLI-created
        # jobs, not covered by this UI-only guard) are never constrained
        # — this is the actual concurrency-safe protection against a
        # double-submit race, not just the application-level
        # check-then-insert in meyar.ui.router. See docs/DECISIONS.md
        # D-028. Declared here (not only in the add_job_lifecycle
        # migration) so Base.metadata.create_all — what the test suite
        # actually builds its schema from — creates it too.
        Index(
            "uq_jobs_active_duplicate_signature",
            "tenant_id",
            "duplicate_signature",
            unique=True,
            postgresql_where=text("status = 'ACTIVE' AND duplicate_signature IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=JOB_STATUS_ACTIVE)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # SHA-256 hex digest of the canonical (normalized title + criteria)
    # signature computed at creation time by the /ui/jobs form path only
    # (meyar.ui.service.compute_job_duplicate_signature) — NULL for
    # API/CLI-created jobs and for any job created before this column
    # existed.
    duplicate_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

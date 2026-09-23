import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base

AGENT_DRAFT_CONFIRMATION_STATUS_CONFIRMED = "CONFIRMED"


class AgentDraftConfirmation(Base):
    """Independent, immutable identity link for an agent-draft confirmation."""

    __tablename__ = "agent_draft_confirmations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "draft_id", name="uq_agent_draft_confirmation"),
        UniqueConstraint("job_id", name="uq_agent_draft_confirmation_job"),
        UniqueConstraint(
            "criteria_version_id", name="uq_agent_draft_confirmation_criteria_version"
        ),
        CheckConstraint("status = 'CONFIRMED'", name="ck_agent_draft_confirmation_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    draft_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    browser_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    criteria_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("job_criteria_versions.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AGENT_DRAFT_CONFIRMATION_STATUS_CONFIRMED
    )
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

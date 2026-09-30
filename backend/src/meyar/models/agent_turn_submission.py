"""Server-issued identity for one browser agent turn; never authorization."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base


class AgentTurnSubmission(Base):
    __tablename__ = "agent_turn_submissions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ISSUED', 'PROCESSING', 'COMPLETED', 'ABANDONED')",
            name="ck_agent_turn_submission_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    membership_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant_memberships.id"), nullable=False
    )
    browser_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    context_epoch: Mapped[int] = mapped_column(nullable=False)
    request_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ISSUED")
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    completed_turn_version: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

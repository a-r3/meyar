"""Tenant-independent, privacy-minimal authentication failure trail."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base


class AuthSecurityEvent(Base):
    __tablename__ = "auth_security_events"
    __table_args__ = (
        CheckConstraint(
            "outcome_code IN ('LOGIN_REJECTED', 'NO_ACTIVE_MEMBERSHIP', "
            "'PENDING_TOKEN_INVALID', 'TENANT_SELECTION_INVALID')",
            name="ck_auth_security_event_outcome",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    outcome_code: Mapped[str] = mapped_column(String(48), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

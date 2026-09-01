import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base


class AuditEvent(Base):
    """Tenant-owned audit trail entry. Never carries CV text or PII —
    ids/enums/metadata only. See docs/MASTER_SPEC.md §15.

    `actor_type`/`actor_id` (Slice 1 — issue #30) are the first-class,
    structured accountability fields: who/what caused this event, as an
    id reference only (never a plaintext password/API key/name/email —
    see meyar.services.audit_repo.ACTOR_* constants and
    _assert_metadata_is_privacy_safe). Both are nullable so every event
    recorded before this slice, and every event a caller doesn't yet
    attribute, remains valid — a NULL actor is read as "not attributed",
    never silently coerced into a human or machine claim."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    actor_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

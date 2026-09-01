import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base


class BrowserSession(Base):
    """Revocable server-side UI session for an authenticated human.

    Tenant and role/scopes are intentionally absent: every browser request
    re-derives both, live, from the User + TenantMembership rows (Slice 1 —
    see meyar.ui.auth.get_ui_context) — a disabled user or a
    revoked/deactivated membership takes effect immediately, without
    re-login. Only a SHA-256 session-token digest is persisted; the opaque
    raw token exists solely in the browser cookie. This is the human/UI
    principal only — machine API-key access never creates or consumes a
    BrowserSession (see meyar.core.auth for that unchanged path).
    """

    __tablename__ = "browser_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_membership_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant_memberships.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    csrf_secret: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base


class AgentConversation(Base):
    """Server-held, tenant/session-scoped state for the Slice 2 read-only
    MEYAR AI agent (issue #31, D-035). Exactly one row per BrowserSession
    (1:1, unique FK) — a fresh login always starts a fresh BrowserSession
    and therefore a fresh, empty conversation, so two human sessions never
    share state, and revoking/expiring the session makes this row
    unreachable via meyar.ui.auth without a separate cleanup step.

    ``turns`` is a bounded, privacy-safe transcript: short (role, text)
    pairs only — the HR user's own request text and the agent's own
    framing text, never raw CV content, never CandidateIdentity, never
    chain-of-thought (mirrors the audit-log privacy discipline in
    meyar.services.audit_repo, applied here to conversational state
    instead of the audit trail).

    ``last_search_candidate_ids`` is the SERVER-AUTHORITATIVE ordinal
    resolution table for follow-up references ("birincini aç") — a list of
    candidate_id strings ordered by search rank. The model never supplies
    or trusts its own memory of a candidate_id; every candidate_ref the
    model produces is resolved against this column, tenant-scoped, at
    read time. See meyar.agent.service._resolve_candidate_ref."""

    __tablename__ = "agent_conversations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    browser_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("browser_sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    turns: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    last_search_candidate_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

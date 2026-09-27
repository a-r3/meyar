import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, func
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

    ``active_result_set_id`` + ``context_epoch`` (issue #49, replacing the
    old ``last_search_candidate_ids`` JSON ordinal table) are the
    SERVER-AUTHORITATIVE state a follow-up ``candidate_ref`` ("birincini
    aç") resolves against — see meyar.services.agent_result_set_repo.
    resolve_active_candidate_ref, the ONLY place a candidate_ref becomes a
    real candidate_id. ``context_epoch`` increments on every "Yeni söhbət"
    reset (see meyar.services.agent_conversation_repo.reset_conversation)
    so a stale/tampered ``active_result_set_id`` from a previous
    conversation epoch can never resolve again, even if repointed at a
    still-otherwise-valid AgentResultSet row for this same tenant/session.
    """

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
    context_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    active_result_set_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_result_sets.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

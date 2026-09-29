import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from meyar.db import Base


class AgentConversationTitleKind(StrEnum):
    """Closed, server-owned conversation title category (issue #80).

    Never derived from raw message/JD/candidate text and never produced by a
    model. ``NEW`` transitions at most once, after the first meaningful
    server-validated action (see
    meyar.services.agent_conversation_repo.title_kind_for_turn). The UI
    renders a localized product label plus date/time for each kind."""

    NEW = "NEW"
    CANDIDATE_SEARCH = "CANDIDATE_SEARCH"
    VACANCY_ANALYSIS = "VACANCY_ANALYSIS"
    RESULT_REFINEMENT = "RESULT_REFINEMENT"
    GENERAL = "GENERAL"


_TITLE_KIND_VALUES = ", ".join(f"'{kind.value}'" for kind in AgentConversationTitleKind)


class AgentConversation(Base):
    """Durable, tenant/user/membership-owned MEYAR AI conversation history
    (issue #80, docs/DECISIONS.md D-086).

    BrowserSession is authentication transport, NOT the conversation owner:
    this row has no dependency on any BrowserSession, so logout/login or
    session expiry never deletes or orphans history. Access is always
    re-derived from the live UIContext principal AND checked against
    ``(tenant_id, owner_user_id, owner_membership_id)`` — see
    meyar.services.agent_conversation_repo.get_owned_conversation.

    ``turns`` is a bounded, privacy-safe transcript (never raw CV content,
    CandidateIdentity, or chain-of-thought). It is presentation/history
    only: it is NEVER ResultSet, candidate-ordinal, or pending-mutation
    authority — that lives exclusively in the BrowserSession-bound
    AgentConversationSessionContext below.

    Owner FKs are NO ACTION (not CASCADE): a user or membership row cannot
    be silently deleted while it still owns durable history. Tenant deletion
    still cascades (the membership and the conversation are both removed in
    the same statement, so the deferred NO ACTION check passes)."""

    __tablename__ = "agent_conversations"
    __table_args__ = (
        CheckConstraint(
            f"title_kind IN ({_TITLE_KIND_VALUES})", name="ck_agent_conversation_title_kind"
        ),
        Index(
            "ix_agent_conversations_owner_recent",
            "tenant_id",
            "owner_user_id",
            "owner_membership_id",
            "updated_at",
            "id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    owner_membership_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant_memberships.id"), nullable=False, index=True
    )
    title_kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=AgentConversationTitleKind.NEW.value,
        server_default=AgentConversationTitleKind.NEW.value,
    )
    turns: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AgentConversationSessionContext(Base):
    """BrowserSession-bound LIVE authority for one durable conversation
    (issue #80). Exactly one row per ``(conversation_id,
    browser_session_id)``.

    This is the ONLY live authority for the current ResultSet
    (``active_result_set_id`` + ``context_epoch``, issue #49), candidate
    ordinals, current refinement, and pending JD-draft mutation authority
    (``active_pending_draft_id``). A new BrowserSession (relogin) always
    gets a fresh row with no ResultSet and no pending draft — it never
    inherits another session's live context, even for the same durable
    conversation. Deleted with its BrowserSession (CASCADE); the durable
    conversation itself survives."""

    __tablename__ = "agent_conversation_session_contexts"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "browser_session_id",
            name="uq_agent_conversation_session_context",
        ),
        CheckConstraint("context_epoch >= 1", name="ck_agent_conversation_context_epoch"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    browser_session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    context_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    active_result_set_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_result_sets.id", ondelete="SET NULL"), nullable=True
    )
    # Plain UUID (no FK): a draft is a server-held transcript payload, not a
    # table row. Actionable only when it equals the requested draft id AND
    # the matching payload exists in the owning conversation's transcript.
    active_pending_draft_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

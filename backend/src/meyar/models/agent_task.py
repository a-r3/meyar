"""BrowserSession-context-bound structured task/dialogue state (issue #88
slice A, docs/AGENT_CORE_V2_DESIGN.md §4.3/§18, D-092 + Amendments A1/A2).

Continuity only: a task or clarification is never candidate, ResultSet,
draft, tenant, session or scope authority. Neither table copies any HR, JD
or CV text — a clarification points at its server-issued transcript
entries by ``turn_id`` and binds its source by offsets + SHA-256."""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from meyar.agent.clarification_schemas import (
    ClarificationAnswer,
    ClarificationStatus,
    ClarificationType,
    ResolutionSource,
    SupersededReason,
    TaskPhase,
    TaskStatus,
    TaskType,
)
from meyar.db import Base


def _values(enum: type) -> str:
    return ", ".join(f"'{member.value}'" for member in enum)  # type: ignore[attr-defined]


_TERMINAL_TASK = "'COMPLETED', 'CANCELLED', 'EXPIRED', 'FAILED_SAFE'"


class AgentTask(Base):
    __tablename__ = "agent_tasks"
    __table_args__ = (
        CheckConstraint(f"task_type IN ({_values(TaskType)})", name="ck_agent_task_type"),
        CheckConstraint(f"status IN ({_values(TaskStatus)})", name="ck_agent_task_status"),
        CheckConstraint(f"phase IN ({_values(TaskPhase)})", name="ck_agent_task_phase"),
        CheckConstraint("state_schema_version = 1", name="ck_agent_task_state_schema_version"),
        CheckConstraint(
            f"(status IN ({_TERMINAL_TASK})) = (terminal_at IS NOT NULL)",
            name="ck_agent_task_terminal_at",
        ),
        CheckConstraint(
            "(pending_draft_id IS NOT NULL) = (status = 'WAITING_CONFIRMATION')",
            name="ck_agent_task_pending_draft",
        ),
        CheckConstraint(
            "task_type <> 'UNDETERMINED' OR status IN "
            f"('WAITING_CLARIFICATION', {_TERMINAL_TASK})",
            name="ck_agent_task_undetermined_waiting",
        ),
        Index(
            "uq_agent_tasks_waiting_clarification",
            "session_context_id",
            unique=True,
            postgresql_where=text("status = 'WAITING_CLARIFICATION'"),
        ),
        Index(
            "uq_agent_tasks_waiting_confirmation",
            "session_context_id",
            unique=True,
            postgresql_where=text("status = 'WAITING_CONFIRMATION'"),
        ),
        Index("ix_agent_tasks_retention", "tenant_id", "session_context_id", "terminal_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_context_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_conversation_session_contexts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    owner_membership_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant_memberships.id"), nullable=False
    )
    task_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    state_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # AgentTaskStateV1: closed codes only (unresolved_slots, assumptions).
    state: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # A reference only: actionable solely when equal to the live pointer.
    pending_draft_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by_submission_id: Mapped[uuid.UUID] = mapped_column(nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentClarification(Base):
    __tablename__ = "agent_clarifications"
    __table_args__ = (
        CheckConstraint(
            f"clarification_type IN ({_values(ClarificationType)})",
            name="ck_agent_clarification_type",
        ),
        CheckConstraint(
            f"status IN ({_values(ClarificationStatus)})", name="ck_agent_clarification_status"
        ),
        CheckConstraint("attempt BETWEEN 1 AND 2", name="ck_agent_clarification_attempt"),
        CheckConstraint(
            f"resolved_value IS NULL OR resolved_value IN ({_values(ClarificationAnswer)})",
            name="ck_agent_clarification_resolved_value_enum",
        ),
        CheckConstraint(
            "(resolved_value IS NOT NULL) = (status = 'RESOLVED')",
            name="ck_agent_clarification_resolved_value",
        ),
        CheckConstraint(
            f"resolution_source IS NULL OR resolution_source IN ({_values(ResolutionSource)})",
            name="ck_agent_clarification_resolution_source_enum",
        ),
        CheckConstraint(
            "(resolution_source IS NOT NULL) = (status = 'RESOLVED')",
            name="ck_agent_clarification_resolution_source",
        ),
        CheckConstraint(
            "(resolved_by_submission_id IS NOT NULL) = (status = 'RESOLVED')",
            name="ck_agent_clarification_resolved_by",
        ),
        CheckConstraint(
            f"superseded_reason IS NULL OR superseded_reason IN ({_values(SupersededReason)})",
            name="ck_agent_clarification_superseded_reason_enum",
        ),
        CheckConstraint(
            "(superseded_reason IS NOT NULL) = (status = 'SUPERSEDED')",
            name="ck_agent_clarification_superseded_reason",
        ),
        CheckConstraint(
            "(clarification_type = 'SEARCH_OR_VACANCY' AND source_turn_id IS NOT NULL "
            "AND source_sha256 IS NOT NULL AND source_start IS NOT NULL "
            "AND source_end IS NOT NULL) OR "
            "(clarification_type = 'VACANCY_SOURCE_REQUIRED' AND source_turn_id IS NULL "
            "AND source_sha256 IS NULL AND source_start IS NULL AND source_end IS NULL)",
            name="ck_agent_clarification_source_by_type",
        ),
        CheckConstraint(
            "source_start IS NULL OR (0 <= source_start AND source_start < source_end "
            "AND source_end <= 4000)",
            name="ck_agent_clarification_source_offsets",
        ),
        CheckConstraint(
            "clarification_type <> 'SEARCH_OR_VACANCY' OR attempt <> 1 "
            "OR created_from_turn_id = source_turn_id",
            name="ck_agent_clarification_attempt1_source",
        ),
        Index(
            "uq_agent_clarifications_open",
            "session_context_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_context_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_conversation_session_contexts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    context_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    clarification_type: Mapped[str] = mapped_column(String(32), nullable=False)
    answer_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    question_turn_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_from_turn_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    superseded_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_turn_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    semantic_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    routing_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    # Provenance only since A1 — never a liveness condition.
    created_turn_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_clarifications.id", ondelete="SET NULL"), nullable=True, index=True
    )
    resolved_value: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolution_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_by_submission_id: Mapped[uuid.UUID] = mapped_column(nullable=False, unique=True)
    resolved_by_submission_id: Mapped[uuid.UUID | None] = mapped_column(
        nullable=True, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

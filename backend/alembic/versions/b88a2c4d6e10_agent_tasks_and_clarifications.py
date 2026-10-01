"""Agent tasks, resumable clarifications and the live clarification pointer (#88 slice A).

Revision ID: b88a2c4d6e10
Revises: a87d4c6e2b19

Additive only (docs/AGENT_CORE_V2_DESIGN.md §18, D-092 + A1/A2). Existing
conversations get no task or clarification rows and existing transcript
entries get no fabricated ``turn_id``; every existing session context is
valid with a NULL ``active_clarification_id``.

Downgrade is representable and fail-closed: it drops the pointer column,
then clarifications, then tasks. These are BrowserSession-scoped working
state; the pending-draft pointer (and therefore confirmation) is untouched,
and nothing on the downgraded schema can resume a dropped clarification.
Only dropped row COUNTS are logged.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b88a2c4d6e10"
down_revision: str | None = "a87d4c6e2b19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TERMINAL = "'COMPLETED', 'CANCELLED', 'EXPIRED', 'FAILED_SAFE'"


def upgrade() -> None:
    op.create_table(
        "agent_tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("conversation_id", sa.Uuid(),
                  sa.ForeignKey("agent_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_context_id", sa.Uuid(),
                  sa.ForeignKey("agent_conversation_session_contexts.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("owner_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("owner_membership_id", sa.Uuid(), sa.ForeignKey("tenant_memberships.id"),
                  nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("state_schema_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("pending_draft_id", sa.Uuid(), nullable=True),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("created_by_submission_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("created_by_submission_id",
                            name="agent_tasks_created_by_submission_id_key"),
        sa.CheckConstraint(
            "task_type IN ('UNDETERMINED', 'CANDIDATE_SEARCH', 'RESULT_FOLLOWUP', "
            "'VACANCY_ANALYSIS')", name="ck_agent_task_type"),
        sa.CheckConstraint(
            "status IN ('WAITING_CLARIFICATION', 'WAITING_CONFIRMATION', 'COMPLETED', "
            "'CANCELLED', 'EXPIRED', 'FAILED_SAFE')", name="ck_agent_task_status"),
        sa.CheckConstraint(
            "phase IN ('NEEDS_INTENT_CHOICE', 'NEEDS_SOURCE', 'DRAFT_REVIEW', 'DONE')",
            name="ck_agent_task_phase"),
        sa.CheckConstraint("state_schema_version = 1",
                           name="ck_agent_task_state_schema_version"),
        sa.CheckConstraint(f"(status IN ({_TERMINAL})) = (terminal_at IS NOT NULL)",
                           name="ck_agent_task_terminal_at"),
        sa.CheckConstraint(
            "(pending_draft_id IS NOT NULL) = (status = 'WAITING_CONFIRMATION')",
            name="ck_agent_task_pending_draft"),
        sa.CheckConstraint(
            f"task_type <> 'UNDETERMINED' OR status IN ('WAITING_CLARIFICATION', {_TERMINAL})",
            name="ck_agent_task_undetermined_waiting"),
    )
    op.create_index("ix_agent_tasks_conversation_id", "agent_tasks", ["conversation_id"])
    op.create_index("ix_agent_tasks_session_context_id", "agent_tasks", ["session_context_id"])
    op.create_index("ix_agent_tasks_retention", "agent_tasks",
                    ["tenant_id", "session_context_id", "terminal_at"])
    op.create_index("uq_agent_tasks_waiting_clarification", "agent_tasks",
                    ["session_context_id"], unique=True,
                    postgresql_where=sa.text("status = 'WAITING_CLARIFICATION'"))
    op.create_index("uq_agent_tasks_waiting_confirmation", "agent_tasks",
                    ["session_context_id"], unique=True,
                    postgresql_where=sa.text("status = 'WAITING_CONFIRMATION'"))

    op.create_table(
        "agent_clarifications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("conversation_id", sa.Uuid(),
                  sa.ForeignKey("agent_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_context_id", sa.Uuid(),
                  sa.ForeignKey("agent_conversation_session_contexts.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("agent_tasks.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("context_epoch", sa.Integer(), nullable=False),
        sa.Column("clarification_type", sa.String(length=32), nullable=False),
        sa.Column("answer_schema_version", sa.String(length=64), nullable=False),
        sa.Column("question_turn_id", sa.Uuid(), nullable=False),
        sa.Column("created_from_turn_id", sa.Uuid(), nullable=False),
        sa.Column("superseded_reason", sa.String(length=16), nullable=True),
        sa.Column("source_turn_id", sa.Uuid(), nullable=True),
        sa.Column("source_sha256", sa.String(length=64), nullable=True),
        sa.Column("source_start", sa.Integer(), nullable=True),
        sa.Column("source_end", sa.Integer(), nullable=True),
        sa.Column("semantic_policy_version", sa.String(length=64), nullable=False),
        sa.Column("routing_policy_version", sa.String(length=64), nullable=False),
        sa.Column("created_turn_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_by_id", sa.Uuid(),
                  sa.ForeignKey("agent_clarifications.id", ondelete="SET NULL"), nullable=True),
        sa.Column("resolved_value", sa.String(length=32), nullable=True),
        sa.Column("resolution_source", sa.String(length=16), nullable=True),
        sa.Column("created_by_submission_id", sa.Uuid(), nullable=False),
        sa.Column("resolved_by_submission_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("created_by_submission_id",
                            name="agent_clarifications_created_by_submission_id_key"),
        sa.UniqueConstraint("resolved_by_submission_id",
                            name="agent_clarifications_resolved_by_submission_id_key"),
        sa.CheckConstraint(
            "clarification_type IN ('SEARCH_OR_VACANCY', 'VACANCY_SOURCE_REQUIRED')",
            name="ck_agent_clarification_type"),
        sa.CheckConstraint("status IN ('OPEN', 'RESOLVED', 'SUPERSEDED', 'EXPIRED')",
                           name="ck_agent_clarification_status"),
        sa.CheckConstraint("attempt BETWEEN 1 AND 2", name="ck_agent_clarification_attempt"),
        sa.CheckConstraint(
            "resolved_value IS NULL OR resolved_value IN ('CANDIDATE_SEARCH', "
            "'VACANCY_ANALYSIS')", name="ck_agent_clarification_resolved_value_enum"),
        sa.CheckConstraint("(resolved_value IS NOT NULL) = (status = 'RESOLVED')",
                           name="ck_agent_clarification_resolved_value"),
        sa.CheckConstraint(
            "resolution_source IS NULL OR resolution_source IN ('BUTTON', 'LABEL', 'MODEL', "
            "'SOURCE_MESSAGE')", name="ck_agent_clarification_resolution_source_enum"),
        sa.CheckConstraint("(resolution_source IS NOT NULL) = (status = 'RESOLVED')",
                           name="ck_agent_clarification_resolution_source"),
        sa.CheckConstraint("(resolved_by_submission_id IS NOT NULL) = (status = 'RESOLVED')",
                           name="ck_agent_clarification_resolved_by"),
        sa.CheckConstraint(
            "superseded_reason IS NULL OR superseded_reason IN ('UNCLEAR', 'NEW_TASK')",
            name="ck_agent_clarification_superseded_reason_enum"),
        sa.CheckConstraint("(superseded_reason IS NOT NULL) = (status = 'SUPERSEDED')",
                           name="ck_agent_clarification_superseded_reason"),
        sa.CheckConstraint(
            "(clarification_type = 'SEARCH_OR_VACANCY' AND source_turn_id IS NOT NULL "
            "AND source_sha256 IS NOT NULL AND source_start IS NOT NULL "
            "AND source_end IS NOT NULL) OR "
            "(clarification_type = 'VACANCY_SOURCE_REQUIRED' AND source_turn_id IS NULL "
            "AND source_sha256 IS NULL AND source_start IS NULL AND source_end IS NULL)",
            name="ck_agent_clarification_source_by_type"),
        sa.CheckConstraint(
            "source_start IS NULL OR (0 <= source_start AND source_start < source_end "
            "AND source_end <= 4000)", name="ck_agent_clarification_source_offsets"),
        sa.CheckConstraint(
            "clarification_type <> 'SEARCH_OR_VACANCY' OR attempt <> 1 "
            "OR created_from_turn_id = source_turn_id",
            name="ck_agent_clarification_attempt1_source"),
    )
    op.create_index("ix_agent_clarifications_conversation_id", "agent_clarifications",
                    ["conversation_id"])
    op.create_index("ix_agent_clarifications_session_context_id", "agent_clarifications",
                    ["session_context_id"])
    op.create_index("ix_agent_clarifications_task_id", "agent_clarifications", ["task_id"])
    op.create_index("ix_agent_clarifications_superseded_by_id", "agent_clarifications",
                    ["superseded_by_id"])
    op.create_index("uq_agent_clarifications_open", "agent_clarifications",
                    ["session_context_id"], unique=True,
                    postgresql_where=sa.text("status = 'OPEN'"))

    op.add_column("agent_conversation_session_contexts",
                  sa.Column("active_clarification_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_agent_session_context_active_clarification",
        "agent_conversation_session_contexts", "agent_clarifications",
        ["active_clarification_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    connection = op.get_bind()
    counts = {
        table: connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
        for table in ("agent_clarifications", "agent_tasks")
    }
    logging.getLogger("alembic.runtime.migration").info(
        "Dropping #88 slice A working state: %s clarification rows, %s task rows",
        counts["agent_clarifications"], counts["agent_tasks"],
    )
    op.drop_constraint("fk_agent_session_context_active_clarification",
                       "agent_conversation_session_contexts", type_="foreignkey")
    op.drop_column("agent_conversation_session_contexts", "active_clarification_id")
    op.drop_index("uq_agent_clarifications_open", "agent_clarifications")
    op.drop_index("ix_agent_clarifications_superseded_by_id", "agent_clarifications")
    op.drop_index("ix_agent_clarifications_task_id", "agent_clarifications")
    op.drop_index("ix_agent_clarifications_session_context_id", "agent_clarifications")
    op.drop_index("ix_agent_clarifications_conversation_id", "agent_clarifications")
    op.drop_table("agent_clarifications")
    op.drop_index("uq_agent_tasks_waiting_confirmation", "agent_tasks")
    op.drop_index("uq_agent_tasks_waiting_clarification", "agent_tasks")
    op.drop_index("ix_agent_tasks_retention", "agent_tasks")
    op.drop_index("ix_agent_tasks_session_context_id", "agent_tasks")
    op.drop_index("ix_agent_tasks_conversation_id", "agent_tasks")
    op.drop_table("agent_tasks")

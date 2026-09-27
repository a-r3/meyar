"""add agent_result_sets/agent_result_set_members; replace
AgentConversation.last_search_candidate_ids with context_epoch +
active_result_set_id (issue #49)

Revision ID: d2a8f6c1b3e9
Revises: 8bd12e7c4a60
Create Date: 2026-09-28

The DROP of ``agent_conversations.last_search_candidate_ids`` intentionally
discards any live ephemeral ordinal state — there is no provenance
(planner/search request, corpus fingerprint, expiry) to safely reconstruct
an AgentResultSet from that old bare id list, so this migration never
attempts to backfill one. A deployment upgrade may require an HR user to
run a fresh search before using an ordinal follow-up ("birincini aç") again
— existing conversation transcripts and pending job drafts are untouched.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d2a8f6c1b3e9"
down_revision: str | Sequence[str] | None = "8bd12e7c4a60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_result_sets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("browser_session_id", sa.Uuid(), nullable=False),
        sa.Column("context_epoch", sa.Integer(), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=False),
        sa.Column("canonical_search_request", sa.JSON(), nullable=False),
        sa.Column("planner_policy_version", sa.String(length=64), nullable=False),
        sa.Column("planner_prompt_version", sa.String(length=64), nullable=False),
        sa.Column("planner_schema_version", sa.String(length=64), nullable=False),
        sa.Column("planner_model_provider", sa.String(length=64), nullable=False),
        sa.Column("planner_model_name", sa.String(length=128), nullable=False),
        sa.Column("planner_model_revision", sa.String(length=64), nullable=False),
        sa.Column("search_policy_version", sa.String(length=64), nullable=False),
        sa.Column("search_mode", sa.String(length=32), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("corpus_fingerprint_sha256", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["browser_session_id"], ["browser_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_result_sets_tenant_id", "agent_result_sets", ["tenant_id"])
    op.create_index(
        "ix_agent_result_sets_browser_session_id", "agent_result_sets", ["browser_session_id"]
    )

    op.create_table(
        "agent_result_set_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("result_set_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        # Immutable snapshot references — deliberately NOT ForeignKey
        # columns against candidates/candidate_profile_versions/
        # candidate_embedding_versions. See
        # meyar.models.agent_result_set.AgentResultSetMember docstring: a
        # later hard candidate delete must never be blocked by, and must
        # never cascade-delete or silently renumber, a historical
        # membership row here.
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_profile_version_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_embedding_version_id", sa.Uuid(), nullable=True),
        sa.Column("relevance_score", sa.Float(), nullable=False),
        sa.Column("structured_score", sa.Float(), nullable=True),
        sa.Column("semantic_score", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["result_set_id"], ["agent_result_sets.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "result_set_id", "ordinal", name="uq_agent_result_set_member_ordinal"
        ),
        sa.UniqueConstraint(
            "result_set_id", "candidate_id", name="uq_agent_result_set_member_candidate"
        ),
        sa.CheckConstraint("ordinal >= 1", name="ck_agent_result_set_member_ordinal_positive"),
    )
    op.create_index(
        "ix_agent_result_set_members_result_set_id",
        "agent_result_set_members",
        ["result_set_id"],
    )

    op.add_column(
        "agent_conversations",
        sa.Column(
            "context_epoch", sa.Integer(), nullable=False, server_default="1"
        ),
    )
    op.add_column(
        "agent_conversations",
        sa.Column("active_result_set_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_agent_conversations_active_result_set",
        "agent_conversations",
        "agent_result_sets",
        ["active_result_set_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_column("agent_conversations", "last_search_candidate_ids")


def downgrade() -> None:
    op.add_column(
        "agent_conversations",
        sa.Column("last_search_candidate_ids", sa.JSON(), nullable=True),
    )
    op.execute("UPDATE agent_conversations SET last_search_candidate_ids = '[]'::json")
    op.alter_column("agent_conversations", "last_search_candidate_ids", nullable=False)
    op.drop_constraint(
        "fk_agent_conversations_active_result_set", "agent_conversations", type_="foreignkey"
    )
    op.drop_column("agent_conversations", "active_result_set_id")
    op.drop_column("agent_conversations", "context_epoch")

    op.drop_index(
        "ix_agent_result_set_members_result_set_id", table_name="agent_result_set_members"
    )
    op.drop_table("agent_result_set_members")

    op.drop_index("ix_agent_result_sets_browser_session_id", table_name="agent_result_sets")
    op.drop_index("ix_agent_result_sets_tenant_id", table_name="agent_result_sets")
    op.drop_table("agent_result_sets")

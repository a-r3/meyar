"""add agent_conversations (Slice 2 — Read-Only Local AI Agent Foundation)

Revision ID: a1c5e9f2b6d3
Revises: f4a91c2e6b7d
Create Date: 2026-09-01

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1c5e9f2b6d3"
down_revision: str | Sequence[str] | None = "f4a91c2e6b7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("browser_session_id", sa.Uuid(), nullable=False),
        sa.Column("turns", sa.JSON(), nullable=False),
        sa.Column("last_search_candidate_ids", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["browser_session_id"], ["browser_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_conversations_tenant_id", "agent_conversations", ["tenant_id"]
    )
    op.create_index(
        "ix_agent_conversations_browser_session_id",
        "agent_conversations",
        ["browser_session_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_conversations_browser_session_id", table_name="agent_conversations"
    )
    op.drop_index("ix_agent_conversations_tenant_id", table_name="agent_conversations")
    op.drop_table("agent_conversations")

"""add independent durable agent draft confirmations

Revision ID: c7e91a4d2f60
Revises: a1c5e9f2b6d3
Create Date: 2026-09-15

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7e91a4d2f60"
down_revision: str | Sequence[str] | None = "a1c5e9f2b6d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_draft_confirmations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("browser_session_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("criteria_version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "confirmed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("status = 'CONFIRMED'", name="ck_agent_draft_confirmation_status"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["browser_session_id"], ["browser_sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["criteria_version_id"], ["job_criteria_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "draft_id", name="uq_agent_draft_confirmation"),
        sa.UniqueConstraint("job_id", name="uq_agent_draft_confirmation_job"),
        sa.UniqueConstraint(
            "criteria_version_id", name="uq_agent_draft_confirmation_criteria_version"
        ),
    )
    op.create_index(
        "ix_agent_draft_confirmations_tenant_id",
        "agent_draft_confirmations",
        ["tenant_id"],
    )
    op.create_index(
        "ix_agent_draft_confirmations_browser_session_id",
        "agent_draft_confirmations",
        ["browser_session_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_draft_confirmations_browser_session_id",
        table_name="agent_draft_confirmations",
    )
    op.drop_index(
        "ix_agent_draft_confirmations_tenant_id", table_name="agent_draft_confirmations"
    )
    op.drop_table("agent_draft_confirmations")

"""add server-side browser sessions

Revision ID: e3b1f7a9c2d4
Revises: c0a4f2d8e317
Create Date: 2026-08-23

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e3b1f7a9c2d4"
down_revision: str | Sequence[str] | None = "c0a4f2d8e317"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("api_key_id", sa.Uuid(), nullable=False),
        sa.Column("session_token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_secret", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_browser_sessions_api_key_id", "browser_sessions", ["api_key_id"])
    op.create_index("ix_browser_sessions_expires_at", "browser_sessions", ["expires_at"])
    op.create_index(
        "ix_browser_sessions_session_token_hash",
        "browser_sessions",
        ["session_token_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_browser_sessions_session_token_hash", table_name="browser_sessions")
    op.drop_index("ix_browser_sessions_expires_at", table_name="browser_sessions")
    op.drop_index("ix_browser_sessions_api_key_id", table_name="browser_sessions")
    op.drop_table("browser_sessions")

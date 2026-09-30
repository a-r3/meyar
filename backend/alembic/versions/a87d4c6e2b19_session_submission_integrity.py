"""Session security stamps, auth failures, and agent submissions (#87).

Revision ID: a87d4c6e2b19
Revises: f3a9c6d2e815

Existing BrowserSessions remain valid: no credential change is fabricated by
this migration. Existing conversations receive no fake submission history.
Downgrade refuses to erase durable audit/submission rows. Once empty, it
revokes all BrowserSessions before removing the pending-login stamps, so no
old authenticated cookie can survive a security-policy rollback.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a87d4c6e2b19"
down_revision: str | None = "f3a9c6d2e815"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("users", "tenant_memberships"):
        op.add_column(table, sa.Column("security_version", sa.Uuid(), nullable=True))
        op.execute(sa.text(f"UPDATE {table} SET security_version = gen_random_uuid()"))
        op.alter_column(table, "security_version", nullable=False)

    op.create_table(
        "auth_security_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("outcome_code", sa.String(length=48), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "outcome_code IN ('LOGIN_REJECTED', 'NO_ACTIVE_MEMBERSHIP', "
            "'PENDING_TOKEN_INVALID', 'TENANT_SELECTION_INVALID')",
            name="ck_auth_security_event_outcome",
        ),
    )
    op.create_table(
        "agent_turn_submissions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("membership_id", sa.Uuid(), sa.ForeignKey("tenant_memberships.id"),
                  nullable=False),
        sa.Column("browser_session_id", sa.Uuid(),
                  sa.ForeignKey("browser_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", sa.Uuid(),
                  sa.ForeignKey("agent_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("context_epoch", sa.Integer(), nullable=False),
        sa.Column("request_sha256", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reservation_id", sa.Uuid(), nullable=True),
        sa.Column("completed_turn_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('ISSUED', 'PROCESSING', 'COMPLETED', 'ABANDONED')",
                           name="ck_agent_turn_submission_status"),
    )
    op.create_index("ix_agent_turn_submissions_browser_session_id",
                    "agent_turn_submissions", ["browser_session_id"])
    op.create_index("ix_agent_turn_submissions_conversation_id",
                    "agent_turn_submissions", ["conversation_id"])


def downgrade() -> None:
    connection = op.get_bind()
    for table in ("auth_security_events", "agent_turn_submissions"):
        if connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first():
            raise RuntimeError(f"Refusing #87 downgrade: {table} contains durable records")
    connection.execute(sa.text(
        "UPDATE browser_sessions SET revoked_at = now() WHERE revoked_at IS NULL"
    ))
    op.drop_index("ix_agent_turn_submissions_conversation_id", "agent_turn_submissions")
    op.drop_index("ix_agent_turn_submissions_browser_session_id", "agent_turn_submissions")
    op.drop_table("agent_turn_submissions")
    op.drop_table("auth_security_events")
    for table in ("tenant_memberships", "users"):
        op.drop_column(table, "security_version")

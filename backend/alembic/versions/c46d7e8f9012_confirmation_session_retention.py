"""Preserve confirmed provenance independently of retained BrowserSessions.

D-114/#46 L-6. Historical UUID is provenance, never live authority.
Downgrade fails closed when historical records no longer have live parents.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c46d7e8f9012"
down_revision: str | None = "b88a2c4d6e10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "agent_draft_confirmations"
FK = "agent_draft_confirmations_browser_session_id_fkey"
CHECK = "ck_agent_draft_confirmation_session_identity"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("historical_browser_session_id", sa.Uuid(), nullable=True))
    op.execute(
        sa.text(
            "UPDATE agent_draft_confirmations SET historical_browser_session_id = browser_session_id"
        )
    )
    op.alter_column(TABLE, "historical_browser_session_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_constraint(FK, TABLE, type_="foreignkey")
    op.alter_column(TABLE, "browser_session_id", existing_type=sa.Uuid(), nullable=True)
    op.create_foreign_key(
        FK, TABLE, "browser_sessions", ["browser_session_id"], ["id"], ondelete="SET NULL"
    )
    op.create_check_constraint(
        CHECK,
        TABLE,
        "browser_session_id IS NULL OR browser_session_id = historical_browser_session_id",
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM agent_draft_confirmations WHERE browser_session_id IS NULL)"
            )
        )
        .scalar_one()
    ):
        raise RuntimeError("Cannot downgrade detached confirmation provenance.")
    op.drop_constraint(CHECK, TABLE, type_="check")
    op.drop_constraint(FK, TABLE, type_="foreignkey")
    op.alter_column(TABLE, "browser_session_id", existing_type=sa.Uuid(), nullable=False)
    op.create_foreign_key(
        FK, TABLE, "browser_sessions", ["browser_session_id"], ["id"], ondelete="CASCADE"
    )
    op.drop_column(TABLE, "historical_browser_session_id")

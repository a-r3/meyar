"""agent turn version + server-owned in-flight turn reservation (issue #85)

Revision ID: e5d7a3c91b04
Revises: c84a5e2f9d17
Create Date: 2026-09-30

Adds to ``agent_conversations``:

- ``turn_version`` (INTEGER NOT NULL DEFAULT 0, >= 0): monotonic transcript
  version bumped by every transcript write;
- ``active_turn_id`` (UUID NULL) + ``active_turn_expires_at`` (TIMESTAMPTZ
  NULL), set together or not at all: the server-owned reservation of the one
  in-flight agent turn that released its DB connection for local inference.

Backfill: existing rows get ``turn_version = 0`` and no reservation — no
turn can be in flight across a migration (the service is stopped).

Downgrade policy (fail closed): refused while any UNEXPIRED reservation
exists (a turn may still be in flight and would otherwise commit into a
schema that can no longer detect staleness). Otherwise the columns hold
only ephemeral concurrency state, and dropping them loses no business,
audit, or transcript data (docs/DECISIONS.md D-089).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e5d7a3c91b04"
down_revision: str | None = "c84a5e2f9d17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_conversations",
        sa.Column("turn_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "agent_conversations",
        sa.Column("active_turn_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "agent_conversations",
        sa.Column("active_turn_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_agent_conversation_turn_version", "agent_conversations", "turn_version >= 0"
    )
    op.create_check_constraint(
        "ck_agent_conversation_active_turn_pair",
        "agent_conversations",
        "(active_turn_id IS NULL) = (active_turn_expires_at IS NULL)",
    )


def downgrade() -> None:
    connection = op.get_bind()
    in_flight = connection.execute(
        sa.text(
            "SELECT count(*) FROM agent_conversations "
            "WHERE active_turn_id IS NOT NULL AND active_turn_expires_at > now()"
        )
    ).scalar_one()
    if in_flight:
        raise RuntimeError(
            f"{in_flight} agent conversation turn reservation(s) are still in flight; "
            "e5d7a3c91b04 downgrade refused (issue #85, D-089). Stop the service and "
            "let reservations expire before downgrading."
        )
    op.drop_constraint("ck_agent_conversation_active_turn_pair", "agent_conversations")
    op.drop_constraint("ck_agent_conversation_turn_version", "agent_conversations")
    op.drop_column("agent_conversations", "active_turn_expires_at")
    op.drop_column("agent_conversations", "active_turn_id")
    op.drop_column("agent_conversations", "turn_version")

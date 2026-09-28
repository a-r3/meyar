"""add AgentResultSet refinement provenance (issue #49 PR49-2)

Revision ID: 543c60f7efc5
Revises: d2a8f6c1b3e9
Create Date: 2026-09-28

Adds ``result_set_kind``/``parent_result_set_id``/
``refinement_request_sha256``/``canonical_refinement_request``/
``refinement_policy_version`` to ``agent_result_sets`` so a
REFINE_CANDIDATE_RESULTS turn can persist a derived, immutable result set
whose provenance chain back to its parent (and ultimately its root search)
is reconstructable without ever mutating a prior row.

Every row that already exists is backfilled truthfully as a root SEARCH
result set (``result_set_kind='SEARCH'``, ``parent_result_set_id=NULL``,
refinement columns left NULL) — no fabricated refinement provenance is
ever invented for pre-existing rows.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "543c60f7efc5"
down_revision: str | Sequence[str] | None = "d2a8f6c1b3e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_result_sets",
        sa.Column(
            "result_set_kind",
            sa.String(length=16),
            nullable=False,
            server_default="SEARCH",
        ),
    )
    op.add_column(
        "agent_result_sets",
        sa.Column("parent_result_set_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "agent_result_sets",
        sa.Column("refinement_request_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "agent_result_sets",
        sa.Column("canonical_refinement_request", sa.JSON(), nullable=True),
    )
    op.add_column(
        "agent_result_sets",
        sa.Column("refinement_policy_version", sa.String(length=64), nullable=True),
    )
    # Every pre-existing row was always a root search result — backfilled
    # explicitly rather than relying only on the column default, so the
    # data is truthful even if a future migration changes the default.
    op.execute("UPDATE agent_result_sets SET result_set_kind = 'SEARCH'")


def downgrade() -> None:
    op.drop_column("agent_result_sets", "refinement_policy_version")
    op.drop_column("agent_result_sets", "canonical_refinement_request")
    op.drop_column("agent_result_sets", "refinement_request_sha256")
    op.drop_column("agent_result_sets", "parent_result_set_id")
    op.drop_column("agent_result_sets", "result_set_kind")

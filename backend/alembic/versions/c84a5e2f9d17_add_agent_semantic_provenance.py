"""immutable agent semantic provenance on job_criteria_versions (issue #84)

Revision ID: c84a5e2f9d17
Revises: b7e3c9d41f28
Create Date: 2026-09-30

Adds ONE nullable JSON column, ``job_criteria_versions.agent_semantic_
provenance``, holding the strict ``jd-semantic-provenance-v2`` record
(meyar.agent.semantic_provenance) for a JD draft confirmed through the
agent: per criterion every supporting source span (id/offsets/exact
fragment/interpretation source), the source-derived origin and confirmed
final parameters, the semantic policy/prompt version, the accepted
local-model identity, and explicit human review decisions, semantic-conflict
choices and the ordered follow-up amendment chain. The JSON contract is
versioned inside the record; the column itself is schema-agnostic.

Backfill: existing rows get NULL. Pre-#84, manual and API versions have no
agent semantic provenance and none is fabricated.

Downgrade policy (fail closed): allowed only while NO row carries non-null
semantic provenance. Once provenance has been persisted, dropping the column
would silently destroy audit evidence for immutable criteria versions, so
the downgrade refuses before any DDL and the database is left untouched.
Production rollback follows the release/schema compatibility policy (D-086).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c84a5e2f9d17"
down_revision: str | None = "b7e3c9d41f28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job_criteria_versions",
        sa.Column("agent_semantic_provenance", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    connection = op.get_bind()
    count = connection.execute(
        sa.text(
            "SELECT count(*) FROM job_criteria_versions "
            "WHERE agent_semantic_provenance IS NOT NULL"
        )
    ).scalar_one()
    if count:
        raise RuntimeError(
            "job_criteria_versions carry persisted agent semantic provenance "
            f"({count} row(s)); c84a5e2f9d17 downgrade would destroy immutable audit "
            "evidence (issue #84, D-088) — refusing. Production rollback must follow "
            "the release/schema compatibility policy (D-086)."
        )
    op.drop_column("job_criteria_versions", "agent_semantic_provenance")

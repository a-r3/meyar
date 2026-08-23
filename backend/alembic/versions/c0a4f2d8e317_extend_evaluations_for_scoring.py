"""extend evaluations for deterministic scoring

Revision ID: c0a4f2d8e317
Revises: 7aae8da26969
Create Date: 2026-08-23

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c0a4f2d8e317"
down_revision: str | Sequence[str] | None = "7aae8da26969"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("evaluations", sa.Column("evaluation_as_of_date", sa.Date(), nullable=True))
    op.add_column("evaluations", sa.Column("numeric_score", sa.Numeric(5, 2), nullable=True))
    op.add_column(
        "evaluations", sa.Column("scoring_policy_version", sa.String(length=32), nullable=True)
    )
    op.add_column("evaluations", sa.Column("score_explanation", sa.JSON(), nullable=True))
    op.create_index(
        "uq_evaluations_scored_provenance",
        "evaluations",
        [
            "tenant_id",
            "candidate_profile_version_id",
            "job_criteria_version_id",
            "evaluation_as_of_date",
            "policy_engine_version",
            "scoring_policy_version",
        ],
        unique=True,
        postgresql_where=sa.text(
            "evaluation_as_of_date IS NOT NULL AND scoring_policy_version IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_evaluations_scored_provenance", table_name="evaluations")
    op.drop_column("evaluations", "score_explanation")
    op.drop_column("evaluations", "scoring_policy_version")
    op.drop_column("evaluations", "numeric_score")
    op.drop_column("evaluations", "evaluation_as_of_date")

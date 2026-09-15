"""add durable criteria review disclosures and result limit

Revision ID: 6f4c2a9d8e10
Revises: c7e91a4d2f60
Create Date: 2026-09-16

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "6f4c2a9d8e10"
down_revision: str | Sequence[str] | None = "c7e91a4d2f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job_criteria_versions",
        sa.Column(
            "unsupported_requirements",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
    )
    op.add_column(
        "job_criteria_versions",
        sa.Column(
            "needs_review_requirements",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
    )
    op.add_column(
        "job_criteria_versions",
        sa.Column("result_limit", sa.Integer(), server_default="20", nullable=False),
    )
    op.add_column(
        "job_criteria_versions",
        sa.Column("eligible_only", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_check_constraint(
        "ck_job_criteria_result_limit",
        "job_criteria_versions",
        "result_limit BETWEEN 1 AND 100",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_job_criteria_result_limit", "job_criteria_versions", type_="check"
    )
    op.drop_column("job_criteria_versions", "eligible_only")
    op.drop_column("job_criteria_versions", "result_limit")
    op.drop_column("job_criteria_versions", "needs_review_requirements")
    op.drop_column("job_criteria_versions", "unsupported_requirements")

"""add job lifecycle status and duplicate signature

Revision ID: db7e4523f491
Revises: e3b1f7a9c2d4
Create Date: 2026-08-31

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "db7e4523f491"
down_revision: str | Sequence[str] | None = "e3b1f7a9c2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NOT NULL with a server_default backfills every existing row to
    # ACTIVE deterministically in the same statement — no separate UPDATE,
    # no data loss.
    op.add_column(
        "jobs",
        sa.Column("status", sa.String(length=32), nullable=False, server_default="ACTIVE"),
    )
    op.add_column("jobs", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "jobs", sa.Column("duplicate_signature", sa.String(length=64), nullable=True)
    )
    op.create_index("ix_jobs_tenant_id_status", "jobs", ["tenant_id", "status"])
    # Partial unique index: an identical (title + criteria) signature may
    # exist any number of times across ARCHIVED jobs (an archived
    # duplicate never blocks a new active one) and NULL signatures
    # (API/CLI-created jobs, not covered by this UI-only guard) are never
    # constrained — this is the actual concurrency-safe protection
    # against a double-submit race, not just an application-level
    # check-then-insert. See docs/DECISIONS.md D-028.
    op.create_index(
        "uq_jobs_active_duplicate_signature",
        "jobs",
        ["tenant_id", "duplicate_signature"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE' AND duplicate_signature IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_jobs_active_duplicate_signature", table_name="jobs")
    op.drop_index("ix_jobs_tenant_id_status", table_name="jobs")
    op.drop_column("jobs", "duplicate_signature")
    op.drop_column("jobs", "archived_at")
    op.drop_column("jobs", "status")
